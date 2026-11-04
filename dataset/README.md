# Dataset

The training audio for the keyword model: people saying the colours plus the
`select` trigger, and `noise` / `unknown` samples so the model can tell speech it
cares about from everything else.

## Get the audio

The dataset is `edge-impulse-voice-detection-export.zip` (~100 MB), too large to
commit to the repo, so it is attached to a GitHub **Release**:

> **Download:** [Releases page](https://github.com/munoz0raul/voice-workshop/releases)
> → the `edge-impulse-voice-detection-export.zip` asset.

It is in Edge Impulse's export format (per-label folders of `.wav` files, with
the label in each filename), so you can upload the extracted folder straight into
**Data acquisition**. Follow [`../docs/edge-impulse.md`](../docs/edge-impulse.md)
from step 3.

## Clone the public project instead

If you would rather start from the finished project — dataset, impulse, and
trained model already in place — clone the public Edge Impulse project:

> **Public project:** `TODO` — add the Edge Impulse public project URL here.

In Studio, **Dashboard → Clone this project** copies it into your account so you
can retrain or redeploy without re-uploading anything.

## Labels

Ten classes: `blue`, `green`, `noise`, `orange`, `purple`, `red`, `select`,
`unknown`, `update`, `yellow`. The app acts on six of them (`select` + the five
colours `blue green purple red yellow`); the rest absorb non-keyword sound. See
[Why these labels](../docs/edge-impulse.md#why-these-labels).
