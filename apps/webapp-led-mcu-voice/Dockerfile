#
# Copyright (c) 2025 Foundries.io
#
# SPDX-License-Identifier: BSD-3-Clause
#
# Voice -> LED/matrix web app for the UNO Q. The 8x13 matrix is driven over
# MCUmgr SMP to the STM32U5's Zephyr firmware (see mcu_matrix.py); there is no
# Arduino sketch to compile or flash, so this is a single-stage runtime image.

FROM debian:trixie-slim
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
    apt-get install -y \
        ca-certificates \
        alsa-utils \
        libgl1 \
        libglib2.0-0 \
        portaudio19-dev \
        sox \
        libasound2 \
        libasound2-dev \
        python3-pyaudio \
        python3 python3-venv python3-dev python3-pip \
        bash && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

ENV VENV=/opt/venv
RUN python3 -m venv $VENV
ENV PATH="$VENV/bin:$PATH"

# The runtime Python deps are the set the app was proven working with on the
# device; kept as-is to avoid changing inference behaviour.
RUN pip install --upgrade pip setuptools wheel && \
    pip install --no-cache-dir \
        cbor \
        edge_impulse_linux \
        "opencv-python>=4.5.1.48,<5" \
        pyaudio \
        six \
        pyalsaaudio \
        sounddevice \
        numpy \
        flask && \
    rm -rf ~/.cache/pip

RUN mkdir -p /app

# Application code and web UI
COPY webapp-led-mcu-voice.py /app/
COPY mcu_matrix.py /app/
COPY index.html /app/index.html
COPY assets/ /app/assets/

# The Edge Impulse model, baked into the image. This is all the plain-Debian
# demo needs. On the fleet, the assets app can override it at
# /var/local/assets/deployment.eim (see apps/webapp-led-mcu-voice-assets).
COPY deployment.eim /app/
RUN chmod +x /app/deployment.eim

COPY start.sh /app/
RUN chmod +x /app/start.sh
WORKDIR /app

CMD ["/app/start.sh"]
