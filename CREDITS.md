# Credits

**[FatihMakes / Mark-LIII](https://github.com/FatihMakes/Mark-LIII)** — an excellent open-source voice
assistant that made the shape of this problem legible. Reading it saved weeks: its action-discovery
design, its empirical audio-device probing (whose comments document three failed approaches and the
measurements that killed them), and its session-resumption handling all taught us something specific.

**No source is copied.** This tree is an independent, MIT-licensed implementation. That is a deliberate
decision recorded in [ADR 0001](docs/adr/0001-clean-room-not-fork.md), taken because CC BY-NC 4.0's
NonCommercial term binds every derivative permanently — not because the reference is anything other than
good work. Where this project departs from it architecturally, that is a difference of requirements
(a phone leg, multiple processes, verbatim fidelity), not a criticism.

Also relied on, with thanks:

- **openWakeWord** — the `hey_jarvis` pretrained model.
- **[Silero VAD](https://github.com/snakers4/silero-vad)** v6.2.3 — the voice activity model that tells
  a word from a breath. The model file `silero_vad.onnx` is fetched at its pinned SHA-256 and ships inside
  Jarvis.exe, under the MIT License, Copyright (c) 2020-present Silero Team; the full notice travels with
  it (`packaging/licenses/silero-vad.txt`). It is never committed to this repository.
- **Google Gemini Live API** — the conversational voice.
- **Anthropic Claude Code** and the Claude Agent SDK — the thing this assistant exists to drive.
