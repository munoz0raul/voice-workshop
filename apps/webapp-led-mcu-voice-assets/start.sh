#!/bin/sh
#
# Copyright (c) 2025 Foundries.io
#
# SPDX-License-Identifier: BSD-3-Clause
#
# Copy the baked-in model into the shared volume the voice app reads. If the
# model on disk differs from the one in this image (a new version just rolled
# out), restart the voice app so it picks it up, then idle.
#
# This replaces the old GitHub-polling delivery: the model now ships inside
# this image, so a new model is a new image and a new OTA update, not a push to
# a repo the device polls. No token, no network.
set -eu

trap 'echo "assets: terminating"; exit 0' INT TERM HUP

: "${SRC:=/assets/deployment.eim}"
: "${OUT_DIR:=/saved/assets}"
: "${FILE:=deployment.eim}"
DST="$OUT_DIR/$FILE"

echo "assets: SRC=$SRC OUT_DIR=$OUT_DIR"
echo "assets: LATENCY-PROBE build=3 (dorbell removed)"

mkdir -p "$OUT_DIR"

if [ ! -f "$SRC" ]; then
    echo "assets: [ERROR] baked model $SRC not found"
    exit 1
fi

changed=false
if [ ! -f "$DST" ]; then
    echo "assets: no model in output -> copying"
    changed=true
elif ! cmp -s "$SRC" "$DST"; then
    echo "assets: model differs -> copying"
    changed=true
else
    echo "assets: model already current"
fi

if [ "$changed" = true ]; then
    cp -f "$SRC" "$DST"
    chmod 755 "$DST"
    echo "assets: wrote $DST"

    # Restart the voice app so start.sh reselects the model. Needs pid: host.
    PIDS="$(pgrep -f 'webapp-led-mcu-voice.py' 2>/dev/null || true)"
    [ -n "$PIDS" ] || PIDS="$(pgrep -f '/app/start.sh' 2>/dev/null || true)"
    if [ -n "$PIDS" ]; then
        echo "assets: restarting voice app (PIDs: $PIDS)"
        # shellcheck disable=SC2086
        kill $PIDS 2>/dev/null || true
    else
        echo "assets: voice app not running yet; it will pick up the model on start"
    fi
fi

echo "assets: done; idling"
exec sleep infinity
