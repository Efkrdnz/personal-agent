# Jarvis

A voice-first personal assistant that drives Claude Code by voice, holds Claude Code's plan-mode
question-and-answer as a spoken conversation, and picks up the phone when you're not at the desk.

**Status: planned, not built.** This repository currently contains the plan. No runtime code yet.

---

## What it will do

Say *"Hey Jarvis, let's build an app"* and it creates the GitHub repo, clones it, tidies your rambling spec
into a clean prompt, **reads that prompt back to you word for word** so you can catch drift, and hands it to
Claude Code with the model and reasoning effort you named out loud. When Claude Code enters plan mode and
starts asking questions, Jarvis reads each question and its options aloud — exactly, in order — and takes
your answer by number ("one and three"), by description, or as free text when none of the options fit.

When you're away from the desk it reaches you instead: Telegram first, a phone call if that goes unanswered.
It rings you in the morning with a briefing you can navigate by voice. It calls restaurants on your behalf, in
Turkish, and does not improvise. It keeps a timestamped log of everything it did and a running total of what
it spent.

---

## Start here

| Document | What it's for |
|---|---|
| [`docs/findings.md`](docs/findings.md) | What was actually established, and what was refuted. **Read this before disagreeing with the architecture.** |
| [`docs/architecture.md`](docs/architecture.md) | The design: components, data model, the bus, the Claude Code driver, voice fidelity, the audio graph, presence |
| [`docs/roadmap.md`](docs/roadmap.md) | Nine spikes, then eight stages, ~13 weeks. Open decisions and honest risks |
| [`docs/telephony.md`](docs/telephony.md) | Getting a +90 number. One phone call decides it — the questions are written out in Turkish |
| [`docs/adr/`](docs/adr/) | Decisions taken, with the reasoning and the revisit conditions |

---

## The four things worth knowing before you read further

**1. The core feature is directly supported, and that was verified by running it.**
`AskUserQuestion` is not a UI popup to be scraped — it is a real tool call routed to the Agent SDK's
`can_use_tool` callback, answered with
`PermissionResultAllow(updated_input={**input_data, "answers": {question: label_or_list}})`. A research agent
confirmed this end to end against the real CLI. The riskiest-looking requirement in the build sheet turns out
to be the best-supported one.

It also caught the most expensive wrong turn available: a bare `claude -p` run has **no permission host** and
can never receive an `AskUserQuestion`. Build on the SDK, not on stream-json over a subprocess.

**2. Gemini Live has no verbatim speech path — and fidelity was the whole point.**
Everything the reference build "says" is injected as a fake *user* turn and paraphrased by the model. So a
read-back of a tidied prompt would itself be a paraphrase, and plan-mode option labels — which you answer by
number — could be silently reordered, translated or merged.

The answer is a rule, not a workaround: **load-bearing text never passes through a generative model.** Gemini
is the conversationalist; a local deterministic reader speaks the exact bytes. Jarvis has two voices,
deliberately and permanently, with an earcon marking the handoff. *When the other voice speaks, those are
somebody else's exact words.* And the model may emit an option **index**, never a label — the numbering is
generated locally, so reordering is structurally impossible rather than prompt-hoped.

**3. The phone layer is not a feature bolted onto stage 2 — it is a re-plumbing of it, unless you plan for it.**
A desk-shaped assistant (one session, one queue, tools awaited inline, the UI object handed to every action)
works beautifully right up until the phone arrives, at which point every assumption breaks at once. Seven
cheap seams on commit one prevent that; the most important is that **the Claude Code driver is its own OS
process from the first line of code.** Retrofitting it is a rewrite; having it on day one is an entrypoint.

Two `git diff --stat` tests make that claim falsifiable — see the end of the roadmap.

**4. The build sheet's pins are stale.**
There is no current "Gemini 3.1 Flash Live". Use `gemini-3.8-live` and `google-genai>=2.23,<3`. And
`speech_config.language_code` does **not** control output language on native-audio models — the Turkish
requirement has no confirmed mechanism yet, which is why it has a spike and a defined fallback.

---

## Decisions already taken

- **Clean-room, MIT** — not a fork. CC BY-NC 4.0 would bind every derivative permanently, and the file most
  worth forking is the file being deleted. ([ADR 0001](docs/adr/0001-clean-room-not-fork.md))
- **Max subscription OAuth** — so hardening is sandboxing rather than `--bare`, and the spend ledger's unit is
  rate-limit proximity, not dollars. ([ADR 0004](docs/adr/0004-claude-auth-oauth.md))
- **Chase a +90 Turkish number** — outbound Turkish caller ID only works if the call originates domestically.
  ([ADR 0010](docs/adr/0010-turkish-number.md), [`docs/telephony.md`](docs/telephony.md))
- **Cloud mode cut from v1** — Claude Code cloud sessions have no live read path, and defer is refused
  outright there. ([ADR 0009](docs/adr/0009-cloud-mode-cut.md))

Still open, with recommendations, at the end of [`docs/roadmap.md`](docs/roadmap.md).

---

## Credits

Design and behaviour informed by [FatihMakes / Mark-LIII](https://github.com/FatihMakes/Mark-LIII), an
excellent voice assistant that made the shape of this problem legible. **No source is copied** — see
[ADR 0001](docs/adr/0001-clean-room-not-fork.md) for why, and `CONTRIBUTING.md` for the rule that keeps it
that way.
