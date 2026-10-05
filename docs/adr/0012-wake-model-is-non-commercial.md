# ADR 0012 — the wake model is non-commercial, and it stays out of the tree

**Status:** accepted, stage 1 (the desk's wake word)
**Relates to:** [ADR 0001](0001-clean-room-not-fork.md), which this does not weaken.

## The facts

openWakeWord's **code** is Apache-2.0. Its **pretrained models** — including `hey_jarvis_v0.1`, and the
shared `melspectrogram` and `embedding_model` files — are licensed **CC BY-NC-SA 4.0**, "due to the
inclusion of datasets with unknown or restrictive licensing as part of the training data" (the
project's own README, read from the 0.6.0 wheel's metadata, not remembered).

`jarvis/audio/wake.py` does not use the `openwakeword` package at all. It runs those model files on
onnxruntime with its own streaming code, written from a reading of the Apache-2.0 pipeline, so no
openWakeWord code is in this tree either.

## The decision

1. **The models are never committed.** `python -m jarvis wake download` fetches them into
   `$XDG_DATA_HOME/jarvis/wake` on the user's machine, pinned by SHA-256. This repository stays MIT:
   ADR 0001's concern is NC material *entering the tree*, and none does.
2. **The licence is said where the model arrives.** `wake download` prints it and `doctor` repeats
   it. A non-commercial term found out about later is the failure ADR 0001 exists to prevent, one
   step removed.
3. **The code is model-agnostic.** `PHRASES` and `MODELS` in `wake.py` are the whole binding to these
   particular files. A permissively licensed model replaces them without touching the turn gate,
   the watch thread, the desk wiring or the tests that use a scripted model.

## What it costs

Personal use, the case this project is built for, is unaffected. **A commercial deployment of Jarvis
cannot use these models**: no hosted version, no paid install for someone else, with `hey_jarvis_v0.1`
in it. The way out, when it matters, is one of:

- train a custom openWakeWord model on synthetic speech (its training notebook does this), checking
  the licence of every dataset that goes in, since that is what made these NC;
- sherpa-onnx keyword spotting, whose models are Apache-2.0 — English only, which docs/architecture.md
  already accepts for the offline phrases;
- `voice.wake_word = ""`, which needs no model at all.

## What was considered and refused

- **Vendoring the `.onnx` files** so a fresh clone works offline. Refused outright: that is NC
  material in the tree, the precise thing ADR 0001 forbids.
- **Silently falling back to always-listening** when the model is missing. Refused for a different
  reason: the user asked for a desk that sends nothing until it hears its name.
