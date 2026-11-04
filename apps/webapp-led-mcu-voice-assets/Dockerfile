#
# Copyright (c) 2025 Foundries.io
#
# SPDX-License-Identifier: BSD-3-Clause
#
# Delivers the Edge Impulse model to the voice app. The model is baked into
# this image at build time; on start it is copied into the shared volume the
# voice app reads. Retraining the model means replacing deployment.eim here,
# rebuilding this image, and rolling out a new update (see docs/WORKSHOP.md).

FROM alpine:3.19.1

RUN apk add --no-cache tini coreutils procps

# The model, baked in. Replace this file to ship a retrained model.
COPY deployment.eim /assets/deployment.eim

COPY start.sh /usr/local/bin/start.sh
RUN chmod +x /usr/local/bin/start.sh
