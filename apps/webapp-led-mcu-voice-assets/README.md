# webapp-led-mcu-voice-assets

> **Optional / advanced.** You do **not** need this app for the demo. The model
> is already baked into [`webapp-led-mcu-voice`](../webapp-led-mcu-voice), and the
> top-level `docker compose up` runs only that app. This directory shows how a
> retrained model reaches devices **over the air on a fleet** — the scale version
> of "drop in a new `.eim`."

Delivers the Edge Impulse model to [`webapp-led-mcu-voice`](../webapp-led-mcu-voice).

The model (`deployment.eim`) is **baked into this image**. On start, `start.sh`
copies it into `/var/local/assets/` — the volume the voice app reads — and, if
the model changed, restarts the voice app so it reloads. Then it idles.

This is the update path at fleet scale: a retrained model is a new
`deployment.eim` in this directory, a rebuilt image, and a new OTA update. No
GitHub token, no device-side polling.

## Why it needs extra privileges

Unlike the voice app, this one is **not** portable by design — it is meant to run
as a container on the device alongside the voice app. Its `docker-compose.yml`
uses `pid: host` (to restart the voice app's process when a new model lands) and
`privileged` + a `/var/local` bind mount (to write the shared model volume). That
is why it is kept separate and out of the one-command demo.

## Retrain and ship a new model

1. Retrain in Edge Impulse and download the Linux (AARCH64) `.eim`
   ([`../../docs/edge-impulse.md`](../../docs/edge-impulse.md)).
2. Replace `deployment.eim` in this directory.
3. Rebuild this image and roll it out through your OTA/fleet tooling.

## Status

The bake-and-copy flow is written but has **not** been rebuilt and run through
OTA on a device from this repo. The restart-by-PID mechanism needs `pid: host`
(set in the compose file).
