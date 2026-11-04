#!/bin/bash
#
# Copyright (c) 2025 Foundries.io
#
# SPDX-License-Identifier: BSD-3-Clause
#
# Entry point for the voice -> LED web app.
#
# The app degrades gracefully when the UNO Q / VENTUNO Q hardware is absent
# (plain Debian, a laptop, CI): the LED matrix and sysfs LEDs are skipped, and
# only a microphone and a browser are needed. So this script does no hardware
# setup that would fail on a generic host -- it locates the MCU console only if
# one happens to be present, picks the model, and runs the app.
set -e

# Locate the LED-matrix MCU console if this is an UNO Q / VENTUNO Q:
#   VENTUNO Q: USB-CDC (ttyACM*)      UNO Q: Qualcomm LPUART (ttyHS1)
# Host symlinks are not mirrored into the container's /dev, so recreate
# /dev/mcu-console here. On a host with neither port (plain Debian) MCU_PORT
# keeps the default below, which won't exist -- the app then disables the
# matrix after one log line (see mcu_matrix.py).
if [ -z "${MCU_PORT:-}" ]; then
    for _acm in /dev/ttyACM[0-9]; do
        if [ -e "$_acm" ]; then
            ln -sf "${_acm##*/}" /dev/mcu-console 2>/dev/null || true
            MCU_PORT=/dev/mcu-console
            break
        fi
    done
    if [ -z "${MCU_PORT:-}" ] && [ -e /dev/ttyHS1 ]; then
        ln -sf ttyHS1 /dev/mcu-console 2>/dev/null || true
        MCU_PORT=/dev/mcu-console
    fi
    : "${MCU_PORT:=/dev/mcu-console}"
fi
export MCU_PORT
echo ">>> MCU_PORT=$MCU_PORT"

echo ">>> Activating virtualenv"
source /opt/venv/bin/activate

# Prefer a model dropped in the shared assets volume (the assets app / OTA
# delivers it there); fall back to the model baked into this image. For the
# plain-Debian demo there is no assets volume, so the baked-in model is used.
ASSETS_DIR="${ASSETS_DIR:-/var/local/assets}"
MODEL_NAME="${MODEL_NAME:-deployment.eim}"
ASSET_MODEL="${ASSETS_DIR}/${MODEL_NAME}"
DEFAULT_MODEL="/app/${MODEL_NAME}"

if [ -f "$ASSET_MODEL" ] && [ -r "$ASSET_MODEL" ]; then
    echo ">>> Using model from assets: $ASSET_MODEL"
    export VOICE_MODEL_PATH="$ASSET_MODEL"
else
    echo ">>> Using baked-in model: $DEFAULT_MODEL"
    export VOICE_MODEL_PATH="$DEFAULT_MODEL"
fi

echo ">>> Running voice app..."
exec python /app/webapp-led-mcu-voice.py "$VOICE_MODEL_PATH"
