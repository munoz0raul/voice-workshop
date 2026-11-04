#!/usr/bin/env python3
#
# Copyright (c) 2025 Foundries.io
#
# SPDX-License-Identifier: BSD-3-Clause
#

import sys
import os
import threading
import json
import time
import itertools
import socket
from contextlib import contextmanager
from queue import Queue
from weakref import WeakSet
from flask import Flask, Response, request, send_file, send_from_directory
import logging
from edge_impulse_linux.audio import AudioImpulseRunner

APP_TAG = "[APP]"

def log(msg: str):
    print(f"{APP_TAG} {msg}")

def log_debug(msg: str):
    if DEBUG:
        print(f"{APP_TAG} {msg}")

# Try to import Device Bridge, fallback to mock for testing
try:
    from arduino.app_utils import *
    USE_REAL_BRIDGE = True
    log("Using real Arduino Bridge")
except ImportError:
    USE_REAL_BRIDGE = False
    log("Using mock Bridge for testing")

    class MockBridge:
        @staticmethod
        def call(function_name, *args):
            log_debug(f"[MOCK] Bridge.call('{function_name}', {', '.join(map(str, args))})")
            return True

    Bridge = MockBridge()

# Wrapper for async Bridge calls
def bridge_call_async(function_name, *args):
    """Call Bridge asynchronously to avoid blocking the UI thread"""
    def _call():
        try:
            Bridge.call(function_name, *args)
        except Exception as e:
            log(f"Bridge call failed: {e}")

    thread = threading.Thread(target=_call, daemon=True)
    thread.start()

# LED matrix is driven over MCUmgr SMP (Zephyr firmware on the STM32U5 MCU via
# /dev/mcu-console), not the old arduino-router "set_matrix" msgpack path.
from mcu_matrix import MatrixMCU
matrix_mcu = MatrixMCU(os.getenv("MCU_PORT", "/dev/mcu-console"))

# LED states tracking (RGB LEDs)
led_states = {
    'led3_r': False, 'led3_g': False, 'led3_b': False,
    'led4_r': False, 'led4_g': False, 'led4_b': False
}

# System LED mappings (sysfs)
LED_NAMES = ("blue", "green", "red")
LED_SET_1 = {
    "blue": "/sys/class/leds/unoq:user-blue1/brightness",
    "green": "/sys/class/leds/unoq:user-green1/brightness",
    "red": "/sys/class/leds/unoq:user-red1/brightness",
}
LED_SET_2 = {
    "blue": "/sys/class/leds/unoq:bt-blue2/brightness",
    "green": "/sys/class/leds/unoq:wlan-green2/brightness",
    "red": "/sys/class/leds/unoq:panic-red2/brightness",
}

def _write_led(name: str, on: bool):
    paths = [LED_SET_1.get(name), LED_SET_2.get(name)]
    for path in paths:
        if not path:
            continue
        try:
            with open(path, 'w') as f:
                f.write('1' if on else '0')
        except Exception as e:
            if DEBUG:
                log_debug(f"[LED] could not set {path} -> {on}: {e}")

def set_system_leds(color: str):
    """Set system LEDs for given color (blue, green, red, yellow, purple, off)."""
    mapping = {
        'blue': {'blue'},
        'green': {'green'},
        'red': {'red'},
        'yellow': {'green', 'red'},
        'purple': {'blue', 'red'},
    }
    wanted = mapping.get((color or '').lower(), set())
    for n in LED_NAMES:
        _write_led(n, n in wanted)

def set_led_color(color: str):
    """Set status LEDs for the given color (blue, green, red, yellow, purple, off).

    Two paths run independently:
    - UNO Q: sysfs LEDs under /sys/class/leds/ (unoq:user-* nodes).
    - VENTUNO Q: 4 RGB LEDs on the STM32H5 MCU via MCUmgr CMD_SET_LEDS.
      On UNO Q this call returns False silently (command not in handler table).
    """
    try:
        set_system_leds(color)
    except Exception as e:
        log(f"Set LED color {color} failed: {e}")
    matrix_mcu.set_color(color)

# Flask app
app = Flask(__name__)

# Status management for Server-Sent Events
status_connections = WeakSet()
current_status = "Ready"
current_color = ""

# Voice recognition configuration
VOICE_MODEL_PATH = os.getenv("VOICE_MODEL_PATH", "/app/deployment.eim")
if len(sys.argv) > 1 and sys.argv[1]:
    VOICE_MODEL_PATH = sys.argv[1]
PA_ALSA_DEVICE = os.getenv("PA_ALSA_DEVICE")
VOICE_ENABLED = os.getenv("VOICE_ENABLED", "1") != "0"

LABELS = {
    "blue", "green", "purple", "red", "yellow", "select"
}
COLOR = {
    "blue", "green", "purple", "red", "yellow"
}

def _env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None:
        return default
    try:
        return float(v)
    except ValueError:
        log(f"ENV {name}='{v}' invalid; using default {default}")
        return default

def _env_int(name: str) -> int | None:
    v = os.getenv(name)
    if v is None or v == "":
        return None
    try:
        return int(v)
    except ValueError:
        log(f"ENV {name}='{v}' invalid; ignoring")
        return None

def _print_scores(scores: dict, total_ms: int):
    print(f"Result ({total_ms} ms.) ", end="")
    for label in sorted(COLOR):
        score = scores.get(label, 0.0)
        print(f"{label}: {score:.2f}\t", end="")
    print("", flush=True)

THRESH = _env_float("THRESH", 0.80)
DEBUG = _env_float("DEBUG", 0)
DEBOUNCE_SECONDS = _env_float("DEBOUNCE_SECONDS", 2.0)
SELECT_SUPPRESS_SECONDS = _env_float("SELECT_SUPPRESS_SECONDS", 10.0)
SELECT_COOLDOWN_SECONDS = _env_float("SELECT_COOLDOWN_SECONDS", 5.0)

voice_shutdown_event = threading.Event()
voice_thread = None
voice_started = False
last_audio_ts = 0.0

# Runtime mic device override — set via POST /device; None means use PA_ALSA_DEVICE env.
_device_id_override = None
_device_lock = threading.Lock()

# Persistent device config: survives container restarts and OTA updates via the
# /var/local/assets volume shared with the assets app.
CONFIG_PATH = os.getenv("DEVICE_CONFIG_PATH", "/var/local/assets/device_config.json")

def _load_device_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f).get("device_id")
    except (OSError, json.JSONDecodeError):
        return None

def _save_device_config(device_id):
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w") as f:
            json.dump({"device_id": device_id}, f)
    except Exception as e:
        log(f"Config save failed: {e}")

# Watchdog configuration
WATCHDOG_POLL_SECONDS = _env_float("WATCHDOG_POLL_SECONDS", 2.0)
AUDIO_WATCHDOG_SECONDS = _env_float("AUDIO_WATCHDOG_SECONDS", 15.0)
AUDIO_RESTART_MIN_SECONDS = _env_float("AUDIO_RESTART_MIN_SECONDS", 20.0)

watchdog_stop_event = threading.Event()
watchdog_thread = None
watchdog_lock = threading.Lock()
last_audio_restart_ts = 0.0

# Matrix state tracking (13x8 = 104 LEDs)
MATRIX_COLS = 13
MATRIX_ROWS = 8
MATRIX_SIZE = 104

# Matrix microphone frame + select animation frames
FRAME_MICROPHONE = [
    [0,0,0,0,0,1,1,0,0,0,0,0,0],
    [0,0,0,0,1,1,1,1,0,0,0,0,0],
    [0,0,0,0,1,1,1,1,0,0,0,0,0],
    [0,0,0,0,1,1,1,1,0,0,0,0,0],
    [0,0,0,0,1,1,1,1,0,0,0,0,0],
    [0,0,1,0,0,1,1,0,0,1,0,0,0],
    [0,0,0,1,1,1,1,1,1,0,0,0,0],
    [0,0,0,0,0,1,1,0,0,0,0,0,0]
]

ANIMATION_COLOR_FRAMES = [
    [[0,0,0,0,0,0,0,0,1,0,0,0,0],
     [0,0,0,0,1,0,0,0,1,0,0,0,0],
     [0,0,0,1,1,1,0,0,1,0,0,1,0],
     [0,1,0,1,1,1,0,1,1,1,0,1,0],
     [1,1,1,1,1,1,1,1,1,1,1,1,1],
     [0,1,0,1,1,1,0,1,1,1,0,1,0],
     [0,0,0,1,1,1,0,0,1,0,0,0,0],
     [0,0,0,0,1,0,0,0,1,0,0,0,0]],
    [[0,0,0,0,0,0,0,0,0,1,0,0,0],
     [0,0,0,0,0,1,0,0,0,1,0,0,0],
     [0,0,0,0,1,1,1,0,0,1,0,0,1],
     [0,0,1,0,1,1,1,0,1,1,1,0,1],
     [1,1,1,1,1,1,1,1,1,1,1,1,1],
     [0,0,1,0,1,1,1,0,1,1,1,0,1],
     [0,0,0,0,1,1,1,0,0,1,0,0,0],
     [0,0,0,0,0,1,0,0,0,1,0,0,0]],
    [[0,0,0,0,0,0,0,0,0,0,1,0,0],
     [0,0,0,0,0,0,1,0,0,0,1,0,0],
     [1,0,0,0,0,1,1,1,0,0,1,0,0],
     [1,0,0,1,0,1,1,1,0,1,1,1,0],
     [1,1,1,1,1,1,1,1,1,1,1,1,1],
     [1,0,0,1,0,1,1,1,0,1,1,1,0],
     [0,0,0,0,0,1,1,1,0,0,1,0,0],
     [0,0,0,0,0,0,1,0,0,0,1,0,0]],
    [[0,0,0,0,0,0,0,0,0,0,0,1,0],
     [0,0,0,0,0,0,0,1,0,0,0,1,0],
     [0,1,0,0,0,0,1,1,1,0,0,1,0],
     [0,1,0,0,1,0,1,1,1,0,1,1,1],
     [1,1,1,1,1,1,1,1,1,1,1,1,1],
     [0,1,0,0,1,0,1,1,1,0,1,1,1],
     [0,0,0,0,0,0,1,1,1,0,0,1,0],
     [0,0,0,0,0,0,0,1,0,0,0,1,0]],
    [[0,0,0,0,0,0,0,0,0,0,0,0,1],
     [0,0,0,0,0,0,0,0,1,0,0,0,1],
     [0,0,1,0,0,0,0,1,1,1,0,0,1],
     [1,0,1,0,0,1,0,1,1,1,0,1,1],
     [1,1,1,1,1,1,1,1,1,1,1,1,1],
     [1,0,1,0,0,1,0,1,1,1,0,1,1],
     [0,0,0,0,0,0,0,1,1,1,0,0,1],
     [0,0,0,0,0,0,0,0,1,0,0,0,1]],
    [[1,0,0,0,0,0,0,0,0,0,0,0,0],
     [1,0,0,0,0,0,0,0,0,1,0,0,0],
     [1,0,0,1,0,0,0,0,1,1,1,0,0],
     [1,1,0,1,0,0,1,0,1,1,1,0,1],
     [1,1,1,1,1,1,1,1,1,1,1,1,1],
     [1,1,0,1,0,0,1,0,1,1,1,0,1],
     [1,0,0,0,0,0,0,0,1,1,1,0,0],
     [1,0,0,0,0,0,0,0,0,1,0,0,0]],
    [[0,1,0,0,0,0,0,0,0,0,0,0,0],
     [0,1,0,0,0,0,0,0,0,0,1,0,0],
     [0,1,0,0,1,0,0,0,0,1,1,1,0],
     [1,1,1,0,1,0,0,1,0,1,1,1,0],
     [1,1,1,1,1,1,1,1,1,1,1,1,1],
     [1,1,1,0,1,0,0,1,0,1,1,1,0],
     [0,1,0,0,0,0,0,0,0,1,1,1,0],
     [0,1,0,0,0,0,0,0,0,0,1,0,0]],
    [[0,0,1,0,0,0,0,0,0,0,0,0,0],
     [0,0,1,0,0,0,0,0,0,0,0,1,0],
     [0,0,1,0,0,1,0,0,0,0,1,1,1],
     [0,1,1,1,0,1,0,0,1,0,1,1,1],
     [1,1,1,1,1,1,1,1,1,1,1,1,1],
     [0,1,1,1,0,1,0,0,1,0,1,1,1],
     [0,0,1,0,0,0,0,0,0,0,1,1,1],
     [0,0,1,0,0,0,0,0,0,0,0,1,0]],
    [[0,0,0,1,0,0,0,0,0,0,0,0,0],
     [0,0,0,1,0,0,0,0,0,0,0,0,1],
     [1,0,0,1,0,0,1,0,0,0,0,1,1],
     [1,0,1,1,1,0,1,0,0,1,0,1,1],
     [1,1,1,1,1,1,1,1,1,1,1,1,1],
     [1,0,1,1,1,0,1,0,0,1,0,1,1],
     [1,0,0,1,0,0,0,0,0,0,0,1,1],
     [0,0,0,1,0,0,0,0,0,0,0,0,1]],
    [[0,0,0,0,1,0,0,0,0,0,0,0,0],
     [1,0,0,0,1,0,0,0,0,0,0,0,0],
     [1,1,0,0,1,0,0,1,0,0,0,0,1],
     [1,1,0,1,1,1,0,1,0,0,1,0,1],
     [1,1,1,1,1,1,1,1,1,1,1,1,1],
     [1,1,0,1,1,1,0,1,0,0,1,0,1],
     [1,1,0,0,1,0,0,0,0,0,0,0,1],
     [1,0,0,0,1,0,0,0,0,0,0,0,0]],
    [[0,0,0,0,0,1,0,0,0,0,0,0,0],
     [0,1,0,0,0,1,0,0,0,0,0,0,0],
     [1,1,1,0,0,1,0,0,1,0,0,0,0],
     [1,1,1,0,1,1,1,0,1,0,0,1,0],
     [1,1,1,1,1,1,1,1,1,1,1,1,1],
     [1,1,1,0,1,1,1,0,1,0,0,1,0],
     [1,1,1,0,0,1,0,0,0,0,0,0,0],
     [0,1,0,0,0,1,0,0,0,0,0,0,0]],
    [[0,0,0,0,0,0,1,0,0,0,0,0,0],
     [0,0,1,0,0,0,1,0,0,0,0,0,0],
     [0,1,1,1,0,0,1,0,0,1,0,0,0],
     [0,1,1,1,0,1,1,1,0,1,0,0,1],
     [1,1,1,1,1,1,1,1,1,1,1,1,1],
     [0,1,1,1,0,1,1,1,0,1,0,0,1],
     [0,1,1,1,0,0,1,0,0,0,0,0,0],
     [0,0,1,0,0,0,1,0,0,0,0,0,0]],
    [[0,0,0,0,0,0,0,1,0,0,0,0,0],
     [0,0,0,1,0,0,0,1,0,0,0,0,0],
     [0,0,1,1,1,0,0,1,0,0,1,0,0],
     [1,0,1,1,1,0,1,1,1,0,1,0,0],
     [1,1,1,1,1,1,1,1,1,1,1,1,1],
     [1,0,1,1,1,0,1,1,1,0,1,0,0],
     [0,0,0,1,1,0,0,1,0,0,0,0,0],
     [0,0,0,1,0,0,0,1,0,0,0,0,0]]
]

# Initialize matrix state (all LEDs off)
matrix_state = [[0 for _ in range(MATRIX_COLS)] for _ in range(MATRIX_ROWS)]
current_matrix_animation = None
matrix_animation_thread = None
stop_matrix_animation_flag = threading.Event()

class WebStatus:
    _lock = threading.Lock()

    @classmethod
    def _broadcast(cls):
        payload = {
            "status": current_status,
            "matrix": [cell for row in matrix_state for cell in row],
            "color": current_color
        }
        for q in status_connections:
            try:
                q.put(payload)
            except Exception:
                pass

    @classmethod
    def update_status(cls, status: str):
        global current_status
        with cls._lock:
            current_status = status
            cls._broadcast()

    @classmethod
    def update_color(cls, color: str):
        global current_color
        with cls._lock:
            current_color = color
            cls._broadcast()

@contextmanager
def _suppress_stderr():
    """Temporarily silences stderr (e.g., ALSA warnings during initialization)."""
    try:
        fd = sys.stderr.fileno()
        old = os.dup(fd)
        with open(os.devnull, 'w') as devnull:
            os.dup2(devnull.fileno(), fd)
        yield
    finally:
        try:
            os.dup2(old, fd)
            os.close(old)
        except Exception:
            pass

def _restart_voice_recognition(reason: str = "", force: bool = False):
    global voice_thread, voice_started, last_audio_ts, last_audio_restart_ts
    if not VOICE_ENABLED:
        return
    now = time.time()
    if not force and (now - last_audio_restart_ts) < AUDIO_RESTART_MIN_SECONDS:
        return
    last_audio_restart_ts = now
    if DEBUG:
        print(f"[VOICE] Restarting voice runner ({reason})")
    try:
        voice_shutdown_event.set()
        if voice_thread and voice_thread.is_alive():
            voice_thread.join(timeout=2.0)
    except Exception:
        pass
    voice_thread = None
    voice_started = False
    voice_shutdown_event.clear()
    voice_thread = threading.Thread(target=_voice_recognition_loop, daemon=True)
    voice_thread.start()
    voice_started = True
    last_audio_ts = time.time()

def _voice_recognition_loop():
    global last_audio_ts
    if not VOICE_ENABLED:
        log("Voice recognition disabled (VOICE_ENABLED=0)")
        clear_matrix_display()
        return

    if not os.path.exists(VOICE_MODEL_PATH):
        log(f"Voice model not found: {VOICE_MODEL_PATH}")
        WebStatus.update_status("Voice model not found")
        return

    log(f"Voice model: {VOICE_MODEL_PATH}")
    log(f"THRESH={THRESH:.2f} DEBOUNCE={DEBOUNCE_SECONDS:.2f}")

    last_audio_ts = time.time()
    while not voice_shutdown_event.is_set():
        # Device ID: runtime override (from /device POST) takes priority over env.
        with _device_lock:
            selected_device_id = _device_id_override if _device_id_override is not None else _env_int("PA_ALSA_DEVICE")
        if selected_device_id is not None:
            log(f"Audio device: {selected_device_id}")

        try:
            with AudioImpulseRunner(VOICE_MODEL_PATH) as runner:
                model_info = runner.init()
                log('Runner: ' + model_info['project']['owner'] + ' / ' + model_info['project']['name'])

                last_send_ts = 0.0
                next_ready_ts = 0.0
                ready_announced = True
                select_window_until = 0.0
                select_block_until = 0.0
                select_pending = False

                _iter = runner.classifier(device_id=selected_device_id)
                with _suppress_stderr():
                    try:
                        first_item = next(_iter)
                    except StopIteration:
                        return

                for res, audio in itertools.chain([first_item], _iter):
                    if voice_shutdown_event.is_set():
                        break

                    last_audio_ts = time.time()

                    now = time.time()
                    if not ready_announced and now >= next_ready_ts:
                        log(f"Listening (debounce {DEBOUNCE_SECONDS}s)")
                        ready_announced = True

                    total_ms = res['timing']['dsp'] + res['timing']['classification']
                    scores = res['result']['classification']

                    _print_scores(scores, total_ms)

                    if DEBUG:
                        log_debug(f"Scores ({total_ms} ms): {scores}")

                    candidates = [l for l in LABELS if l in scores]
                    best_label = max(candidates, key=lambda l: scores.get(l, -1.0)) if candidates else None
                    best_score = scores.get(best_label, 0.0) if best_label else 0.0

                    if (now - last_send_ts) >= DEBOUNCE_SECONDS and best_label and best_score >= THRESH:
                        if best_label == "select":
                            if now < select_block_until:
                                last_send_ts = now
                                next_ready_ts = now + DEBOUNCE_SECONDS
                                ready_announced = False
                                continue

                            WebStatus.update_status("Select the Color")
                            start_color_animation()
                            select_pending = True
                            select_window_until = now + SELECT_SUPPRESS_SECONDS
                            select_block_until = now + SELECT_COOLDOWN_SECONDS
                            continue

                        if best_label in COLOR:
                            if select_pending and now <= select_window_until:
                                log(f"Result: {best_label} ({best_score:.2f})")
                                WebStatus.update_status("Say 'Select' to start")
                                WebStatus.update_color(best_label)
                                show_microphone_icon()
                                try:
                                    set_led_color(best_label)
                                except Exception:
                                    if DEBUG:
                                        log_debug(f"[LED] set_led_color failed for {best_label}")

                                select_pending = False
                                last_send_ts = now
                                next_ready_ts = now + DEBOUNCE_SECONDS
                                ready_announced = False
                                continue

                    if select_pending and now > select_window_until:
                        WebStatus.update_status("Say 'Select' to start")
                        WebStatus.update_color("")
                        show_microphone_icon()
                        try:
                            set_led_color("off")
                        except Exception:
                            if DEBUG:
                                log_debug("[LED] set_led_color failed on window expiry")
                        select_pending = False

        except Exception as e:
            log(f"Voice runner error: {e}")
            WebStatus.update_status("Audio device error — open ⚙ to change mic")
            time.sleep(2.0)

def start_voice_recognition():
    global voice_thread, voice_started, last_audio_ts
    if voice_thread and voice_thread.is_alive():
        return voice_thread
    voice_shutdown_event.clear()
    voice_thread = threading.Thread(target=_voice_recognition_loop, daemon=True)
    voice_thread.start()
    voice_started = True
    last_audio_ts = time.time()
    show_microphone_icon()
    return voice_thread

def display_frame(frame):
    matrix_flat = []
    for y in range(MATRIX_ROWS):
        for x in range(MATRIX_COLS):
            matrix_state[y][x] = frame[y][x]
            matrix_flat.append(frame[y][x])
    # Row-major flat list (row*13 + col) == the MCU matrix bitmap index order.
    matrix_mcu.render_bits(matrix_flat)
    WebStatus._broadcast()

def clear_matrix_display():
    for y in range(MATRIX_ROWS):
        for x in range(MATRIX_COLS):
            matrix_state[y][x] = 0
    matrix_mcu.clear()
    WebStatus._broadcast()

def show_microphone_icon():
    stop_matrix_animation()
    try:
        display_frame(FRAME_MICROPHONE)
    except Exception as e:
        if DEBUG:
            log_debug(f"[MATRIX] Show microphone failed: {e}")

def matrix_animation_loop(frames, delay=0.08):
    while not stop_matrix_animation_flag.is_set():
        for frame in frames:
            if stop_matrix_animation_flag.is_set():
                break
            try:
                display_frame(frame)
            except Exception as e:
                if DEBUG:
                    log_debug(f"[MATRIX] Animation frame error: {e}")
            time.sleep(delay)

def start_color_animation():
    global current_matrix_animation, matrix_animation_thread
    stop_matrix_animation()
    current_matrix_animation = "color"
    stop_matrix_animation_flag.clear()
    matrix_animation_thread = threading.Thread(
        target=matrix_animation_loop,
        args=(ANIMATION_COLOR_FRAMES, 0.08),
        daemon=True
    )
    matrix_animation_thread.start()

def stop_matrix_animation():
    global current_matrix_animation, matrix_animation_thread
    if matrix_animation_thread and matrix_animation_thread.is_alive():
        stop_matrix_animation_flag.set()
        matrix_animation_thread.join(timeout=1.0)
    current_matrix_animation = None
    stop_matrix_animation_flag.clear()

def watchdog_loop():
    while not watchdog_stop_event.is_set():
        now = time.time()

        try:
            if VOICE_ENABLED and voice_started and last_audio_ts > 0:
                if (now - last_audio_ts) > AUDIO_WATCHDOG_SECONDS:
                    with watchdog_lock:
                        _restart_voice_recognition("stale audio")
        except Exception as e:
            if DEBUG:
                log_debug(f"[WATCHDOG] error: {e}")

        time.sleep(max(0.2, WATCHDOG_POLL_SECONDS))

def start_watchdog():
    global watchdog_thread
    if watchdog_thread and watchdog_thread.is_alive():
        return
    watchdog_stop_event.clear()
    watchdog_thread = threading.Thread(target=watchdog_loop, daemon=True)
    watchdog_thread.start()

def _get_local_ips() -> list[str]:
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            addr = info[4][0]
            if addr and ":" not in addr:
                ips.add(addr)
    except Exception:
        pass

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass

    ips.discard("127.0.0.1")
    ips.discard("0.0.0.0")
    return sorted(ips)

def _list_audio_devices() -> list:
    """Return PortAudio input devices as [{id, name, recommended}].

    Devices named 'default' or 'sysdefault' route through ALSA's software
    resampler and will accept the model's 16 kHz capture.  Raw hw:X,Y devices
    are locked to the hardware sample rate (often 44100 Hz) and will fail with
    'Invalid sample rate'.  We flag the resampling-capable ones as recommended.
    """
    try:
        import pyaudio
        p = pyaudio.PyAudio()
        devices = []
        for i in range(p.get_device_count()):
            info = p.get_device_info_by_index(i)
            if info.get('maxInputChannels', 0) > 0:
                name = info['name']
                # ALSA virtual devices (default, sysdefault, plug*) handle
                # resampling; raw hw:X,Y devices do not.
                recommended = bool(name in ('default', 'sysdefault') or
                                   name.startswith('plug'))
                devices.append({'id': i, 'name': name, 'recommended': recommended})
        p.terminate()
        return devices
    except Exception as e:
        log(f"Device listing failed: {e}")
        return []


# Routes
@app.route('/')
def index():
    """Serve the main HTML page"""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    index_path = os.path.join(base_dir, 'index.html')
    return send_file(index_path)

@app.route('/status')
def status_stream():
    """Server-Sent Events endpoint for real-time status updates"""
    def event_stream():
        q = Queue()
        status_connections.add(q)
        try:
            WebStatus._broadcast()
            while True:
                data = q.get()
                yield f"data: {json.dumps(data)}\n\n"
        except GeneratorExit:
            status_connections.discard(q)

    return Response(event_stream(), mimetype='text/event-stream')

@app.route('/devices')
def list_devices():
    """List available PortAudio input devices."""
    with _device_lock:
        current = _device_id_override if _device_id_override is not None else _env_int("PA_ALSA_DEVICE")
    return json.dumps({'devices': _list_audio_devices(), 'current': current}), 200, {'Content-Type': 'application/json'}

@app.route('/device', methods=['POST'])
def set_device():
    """Set the active capture device. Body: {id: <int>} or {id: null} to revert to env."""
    global _device_id_override
    data = request.get_json(silent=True) or {}
    device_id = data.get('id')
    if device_id is not None and not isinstance(device_id, int):
        return ('Bad Request', 400)
    with _device_lock:
        _device_id_override = device_id
    _save_device_config(device_id)
    log(f"Device set to {device_id} (saved to config); restarting voice runner")
    threading.Thread(target=_restart_voice_recognition, kwargs={'reason': 'device change', 'force': True}, daemon=True).start()
    return ('', 204)

@app.route('/assets/<path:filename>')
def serve_assets(filename):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    assets_dir = os.path.join(base_dir, "assets")
    return send_from_directory(assets_dir, filename)

@app.route('/<path:filename>')
def serve_static(filename):
    allowed = {'arduino.png', 'edgeimpulse.png', 'foundries.png', 'qualcomm.png', 'favicon.ico'}
    if filename in allowed:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        return send_from_directory(os.path.join(base_dir, "assets"), filename)
    return ("Not Found", 404)

def main():
    global _device_id_override
    saved_device = _load_device_config()
    if saved_device is not None:
        with _device_lock:
            _device_id_override = saved_device
        log(f"Loaded saved mic device: {saved_device}")
    log("Class Voice LED")
    log("Web UI endpoints:")
    log("- http://0.0.0.0:8000")
    log("- http://127.0.0.1:8000")
    for ip in _get_local_ips():
        log(f"- http://{ip}:8000")
    log(f"Matrix: {MATRIX_COLS}x{MATRIX_ROWS} = {MATRIX_SIZE} LEDs")

    try:
        logging.getLogger('werkzeug').setLevel(logging.WARNING)
        set_led_color('blue')
        start_voice_recognition()
        start_watchdog()
        app.run(host='0.0.0.0', port=8000, debug=False, threaded=True)
    except KeyboardInterrupt:
        print("\n\nShutting down...")
        sys.exit(0)

if __name__ == "__main__":
    main()

