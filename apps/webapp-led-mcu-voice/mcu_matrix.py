#!/usr/bin/env python3
#
# Copyright (c) 2025 Foundries.io
#
# SPDX-License-Identifier: BSD-3-Clause
#
"""Thread-safe MCUmgr SMP client for the UNO Q 8x13 LED matrix.

The UNO Q's STM32U5 MCU runs a Zephyr firmware (SWD-loaded into SRAM at boot by
the host's zephyr-loader.service) that exposes an LED-matrix service over MCUmgr
SMP (custom group 64) on /dev/mcu-console (LPUART1 -> /dev/ttyHS1, 115200 8N1).

This replaces the old arduino-router / Arduino_RouterBridge "set_matrix" path,
which spoke msgpack-RPC and is incompatible with this firmware.

Protocol ported from tools/lighting-poc/mcu_link.py of the Foundries demo repo
workspace-oss-2026-demo. Standard library only.

Frame input is a flat list of 104 ints (0/1) in row-major order (row*13 + col),
which is exactly what webapp-led-mcu-voice.py already produces.
"""
import base64
import os
import struct
import termios
import threading
import time

GROUP = 64
CMD_HELLO, CMD_CLOCK, CMD_RENDER, CMD_STATUS, CMD_CANCEL, CMD_LOG = range(6)
CMD_SET_LEDS = 6
OP_READ, OP_WRITE = 0, 2
ROWS, COLS = 8, 13
MATRIX_BYTES = (ROWS * COLS + 7) // 8  # 13


# --- minimal CBOR (maps, text/byte strings, integers, simple values) ---------

def _cbor_head(major, value):
    if value < 24:
        return bytes([major << 5 | value])
    for size, info, fmt in ((1, 24, ">B"), (2, 25, ">H"), (4, 26, ">I"), (8, 27, ">Q")):
        if value < 1 << (8 * size):
            return bytes([major << 5 | info]) + struct.pack(fmt, value)
    raise ValueError("integer too large")


def cbor_encode(obj):
    if isinstance(obj, bool):
        return b"\xf5" if obj else b"\xf4"
    if isinstance(obj, int):
        return _cbor_head(0, obj) if obj >= 0 else _cbor_head(1, -1 - obj)
    if isinstance(obj, (bytes, bytearray)):
        return _cbor_head(2, len(obj)) + bytes(obj)
    if isinstance(obj, str):
        data = obj.encode()
        return _cbor_head(3, len(data)) + data
    if isinstance(obj, dict):
        return _cbor_head(5, len(obj)) + b"".join(
            cbor_encode(k) + cbor_encode(v) for k, v in obj.items())
    raise TypeError(f"cannot encode {type(obj)}")


def cbor_decode(data, pos=0):
    initial = data[pos]
    major, info = initial >> 5, initial & 31
    pos += 1
    if info < 24:
        value = info
    elif info in (24, 25, 26, 27):
        size = 1 << (info - 24)
        value = int.from_bytes(data[pos:pos + size], "big")
        pos += size
    elif info == 31 and major in (4, 5):
        value = None  # indefinite length
    else:
        raise ValueError(f"unsupported CBOR item {initial:#x}")
    if major == 0:
        return value, pos
    if major == 1:
        return -1 - value, pos
    if major in (2, 3):
        raw = data[pos:pos + value]
        return (bytes(raw) if major == 2 else raw.decode()), pos + value
    if major in (4, 5):
        items, count = [], 0
        while (value is None and data[pos] != 0xFF) or (value is not None and count < value * (2 if major == 5 else 1)):
            item, pos = cbor_decode(data, pos)
            items.append(item)
            count += 1
        if value is None:
            pos += 1
        return (dict(zip(items[::2], items[1::2])) if major == 5 else items), pos
    if major == 7:
        return {20: False, 21: True, 22: None}.get(value), pos
    raise ValueError(f"unsupported CBOR major type {major}")


# --- SMP over the serial transport -------------------------------------------

def crc16_xmodem(data):
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = (crc << 1 ^ 0x1021) if crc & 0x8000 else crc << 1
            crc &= 0xFFFF
    return crc


class _SmpSerial:
    def __init__(self, port):
        self.fd = os.open(port, os.O_RDWR | os.O_NOCTTY)
        attrs = termios.tcgetattr(self.fd)
        attrs[0] = attrs[1] = attrs[3] = 0          # raw iflag/oflag/lflag
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        attrs[4] = attrs[5] = termios.B115200
        attrs[6][termios.VMIN], attrs[6][termios.VTIME] = 0, 1
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        termios.tcflush(self.fd, termios.TCIOFLUSH)
        self.seq = 0
        self.buf = b""

    def request(self, op, cmd, payload, timeout=2.0):
        body = cbor_encode(payload)
        self.seq = (self.seq + 1) & 0xFF
        header = struct.pack(">BBHHBB", op, 0, len(body), GROUP, self.seq, cmd)
        packet = header + body
        packet += struct.pack(">H", crc16_xmodem(packet))
        encoded = base64.b64encode(struct.pack(">H", len(packet)) + packet)
        first = True
        while encoded:
            room = 127 - 3  # marker (2) and newline (1)
            chunk, encoded = encoded[:room], encoded[room:]
            os.write(self.fd, (b"\x06\x09" if first else b"\x04\x14") + chunk + b"\n")
            first = False
        return self._read_response(timeout)

    def _read_line(self, deadline):
        while b"\n" not in self.buf:
            if time.monotonic() > deadline:
                raise TimeoutError("no SMP response")
            self.buf += os.read(self.fd, 512)
        line, self.buf = self.buf.split(b"\n", 1)
        return line

    def _read_response(self, timeout):
        deadline = time.monotonic() + timeout
        data = b""
        while True:
            line = self._read_line(deadline)
            start = line.find(b"\x06\x09")
            if start >= 0:
                data = base64.b64decode(line[start + 2:])
            elif line.startswith(b"\x04\x14") and data:
                data += base64.b64decode(line[2:])
            else:
                continue  # not an SMP frame
            if len(data) >= 2 and len(data) - 2 >= struct.unpack(">H", data[:2])[0]:
                break
        length = struct.unpack(">H", data[:2])[0]
        packet = data[2:2 + length]
        if crc16_xmodem(packet[:-2]) != struct.unpack(">H", packet[-2:])[0]:
            raise ValueError("SMP response CRC mismatch")
        _op, _flags, body_len, group, seq, cmd = struct.unpack(">BBHHBB", packet[:8])
        if group != GROUP or seq != self.seq:
            raise ValueError(f"unexpected response group {group} seq {seq}")
        reply, _ = cbor_decode(packet[8:8 + body_len])
        if reply.get("rc", 0):
            raise RuntimeError(f"command {cmd} failed: rc {reply['rc']}")
        return reply

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass


class MatrixMCU:
    """Thread-safe front-end that keeps one SMP session open and renders frames.

    Lazily opens /dev/mcu-console and sends HELLO on first use. On any link
    error it transparently drops the session and re-HELLOs on the next render,
    so an MCU reset (which loses the session) self-heals.
    """

    def __init__(self, port="/dev/mcu-console"):
        self.port = port
        self._lock = threading.Lock()
        self._link = None
        self._session = None
        self._rev = 0
        # Set once when the port cannot be opened at all (no UNO Q / VENTUNO Q
        # on this host, e.g. plain Debian or a laptop). After that every matrix
        # call short-circuits quietly so animation frames don't spam the log.
        self._disabled = False

    def _disable(self, reason):
        if not self._disabled:
            self._disabled = True
            print(f"[MCU] matrix disabled: {reason} (no LED matrix on this host)")

    def _ensure_locked(self):
        if self._link is not None:
            return
        link = _SmpSerial(self.port)
        session = int.from_bytes(os.urandom(8), "big") | 1
        hello = link.request(OP_WRITE, CMD_HELLO, {"s": session})
        if hello.get("v") != 1 or hello.get("rows") != ROWS or hello.get("cols") != COLS:
            link.close()
            raise RuntimeError(f"unexpected HELLO reply: {hello}")
        self._link = link
        self._session = session
        self._rev = 0

    def _render_locked(self, bitmap):
        self._ensure_locked()
        self._rev = (self._rev + 1) & 0xFFFFFFFF
        self._link.request(OP_WRITE, CMD_RENDER,
                           {"s": self._session, "f": self._rev, "t": 0, "p": bitmap})

    def _drop(self):
        if self._link is not None:
            self._link.close()
        self._link = None
        self._session = None

    @staticmethod
    def pack(bits):
        """Pack a flat iterable of 104 truthy/falsy values into 13 bytes (LSB-first)."""
        packed = bytearray(MATRIX_BYTES)
        for i, v in enumerate(bits):
            if i >= ROWS * COLS:
                break
            if int(v):
                packed[i // 8] |= 1 << (i % 8)
        return bytes(packed)

    def render_bits(self, bits):
        """Render a flat list of 104 ints (0/1) in row-major order. Never raises.

        Returns False when no MCU is attached. The first time the port cannot be
        opened (FileNotFoundError) the matrix is disabled for good, so a host
        without the hardware degrades to one log line instead of a failure per
        frame. A transient link error on an existing port still retries and
        self-heals (an MCU reset re-HELLOs on the next render).
        """
        if self._disabled:
            return False
        bitmap = self.pack(bits)
        with self._lock:
            for attempt in range(2):
                try:
                    self._render_locked(bitmap)
                    return True
                except FileNotFoundError:
                    self._drop()
                    self._disable(f"no MCU console at {self.port}")
                    return False
                except Exception as e:
                    self._drop()
                    last = e
            print(f"[MCU] render failed: {last}")
            return False

    def clear(self):
        return self.render_bits([0] * (ROWS * COLS))

    _COLOR_RGB = {
        "blue":   (0, 0, 1),
        "green":  (0, 1, 0),
        "red":    (1, 0, 0),
        "yellow": (1, 1, 0),
        "purple": (1, 0, 1),
        "off":    (0, 0, 0),
    }

    def set_color(self, color: str) -> bool:
        """Set all RGB LEDs on VENTUNO Q to a named color.

        Returns True on success, False if the board does not support the command
        (UNO Q) or the link is broken.  Never raises; never drops the link on a
        mere MGMT_ERR_ENOTSUPPORT (RuntimeError from _SmpSerial.request).
        """
        if self._disabled:
            return False
        rgb = self._COLOR_RGB.get(color.lower(), (0, 0, 0))
        with self._lock:
            try:
                self._ensure_locked()
                self._link.request(OP_WRITE, CMD_SET_LEDS,
                                   {"r": rgb[0], "g": rgb[1], "b": rgb[2]})
                return True
            except RuntimeError:
                # MCUmgr returned a non-zero rc (command not in handler table on
                # UNO Q).  The link is still healthy — do NOT drop it.
                return False
            except FileNotFoundError:
                # No MCU console on this host; disable the matrix for good.
                self._drop()
                self._disable(f"no MCU console at {self.port}")
                return False
            except Exception:
                self._drop()
                return False


# --- standalone test: render the microphone icon, then clear -----------------

if __name__ == "__main__":
    import sys
    port = sys.argv[1] if len(sys.argv) > 1 else "/dev/mcu-console"
    FRAME_MICROPHONE = [
        [0,0,0,0,0,1,1,0,0,0,0,0,0],
        [0,0,0,0,1,1,1,1,0,0,0,0,0],
        [0,0,0,0,1,1,1,1,0,0,0,0,0],
        [0,0,0,0,1,1,1,1,0,0,0,0,0],
        [0,0,0,0,1,1,1,1,0,0,0,0,0],
        [0,0,1,0,0,1,1,0,0,1,0,0,0],
        [0,0,0,1,1,1,1,1,1,0,0,0,0],
        [0,0,0,0,0,1,1,0,0,0,0,0,0],
    ]
    flat = [c for row in FRAME_MICROPHONE for c in row]
    m = MatrixMCU(port)
    print("render microphone:", m.render_bits(flat))
    time.sleep(3)
    print("all on:", m.render_bits([1] * (ROWS * COLS)))
    time.sleep(2)
    print("clear:", m.clear())
