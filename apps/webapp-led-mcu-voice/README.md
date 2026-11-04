# webapp-led-mcu-voice

The application. It runs the Edge Impulse keyword model on the microphone and, on
a recognised colour, lights the board's RGB LED and draws on the 8x13 LED matrix.
A web UI on `:8000` mirrors the state. On a host without the LED hardware it runs
just fine — the matrix and LEDs are skipped and the colour shows in the web UI.

## Run it

From this directory:

```sh
docker compose up --build
# open http://localhost:8000
```

Or use the one-command compose at the repo root, which builds this same app.

## How it works

- **Voice** — `webapp-led-mcu-voice.py` runs the Edge Impulse audio runner on the
  model at `VOICE_MODEL_PATH`. Labels are `select` plus the colours
  `blue green purple red yellow`; say `select`, then a colour.
- **Matrix** — when present, driven over MCUmgr SMP (custom group 64) to the
  Zephyr firmware on the MCU, via `mcu_matrix.py` on `MCU_PORT`. The first time
  the port cannot be opened (no UNO Q / VENTUNO Q on this host) the matrix
  disables itself after one log line.
- **RGB LEDs** — the on-board LED. On the UNO Q this is host sysfs under
  `/sys/class/leds`; on the VENTUNO Q it is an MCU command. Both are skipped
  silently when absent.
- **Model** — `start.sh` prefers a model dropped at
  `/var/local/assets/deployment.eim` (the OTA path, see
  [`../webapp-led-mcu-voice-assets`](../webapp-led-mcu-voice-assets)) and falls
  back to the `deployment.eim` baked into this image. The local Docker demo uses
  the baked-in model.

## Audio capture device

PortAudio uses the system default input device (`PA_ALSA_DEVICE` is left unset in
the compose file). If capture fails with `Invalid sample rate`, the raw `hw:X,Y`
device is locked to its hardware rate (often 44100 Hz) and rejects the model's
16 kHz open. Open the gear (⚙) in the web UI and pick a device marked
**recommended** — an ALSA `default`/`plughw` device that resamples to 16 kHz. The
choice is saved to `device_config.json` in the `assets` volume and survives
restarts.

## Configuration

Set in `docker-compose.yml`:

| Env | Default | Meaning |
|-----|---------|---------|
| `THRESH` | `0.80` | minimum classification score to accept a word |
| `DEBOUNCE_SECONDS` | `2.0` | gap before the next word is accepted |
| `SELECT_SUPPRESS_SECONDS` | `10.0` | how long after `select` a colour is accepted |
| `SELECT_COOLDOWN_SECONDS` | `5.0` | ignore repeated `select` for this long |
| `PA_ALSA_DEVICE` | unset | PortAudio input device index; unset = default |
| `MCU_PORT` | `/dev/mcu-console` | LED-matrix serial port (ignored if absent) |
| `VOICE_ENABLED` | `1` | set `0` to run the UI without audio |

## Status

Verified live on the UNO Q (voice → matrix + RGB LED, web UI on `:8000`). The
portability changes in this repo — graceful skip of the matrix and sysfs LEDs
when absent, default-device audio, no privileged/host networking — are so the
same image runs on plain Debian; that path has **not** been rebuilt and run here
(no Docker on the machine these edits were made on).
