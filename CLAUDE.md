# Jarvis — working notes

A voice-first assistant that drives Claude Code by voice, holds plan mode as a spoken conversation, and is
reachable by phone. Python 3.11, one SQLite file, several small processes.

**Read [`docs/findings.md`](docs/findings.md) before disagreeing with the architecture.** Several obvious
designs are refuted there by evidence, including some that earlier research asserted confidently and review
overturned.

## Setup

```bash
uv venv && . .venv/bin/activate
uv pip install -e '.[cc,dev]'

pytest -q                          # the whole suite
ruff check . && ruff format .
python tools/check_gitignore.py    # secret patterns must actually fire
```

## Measured facts — do not re-derive these

These were established by running the real Claude Code CLI (v2.1.273, `claude-agent-sdk` 0.2.153), not by
reading docs. Full detail in [`spikes/s0_crossproc/RESULTS.md`](spikes/s0_crossproc/RESULTS.md).

- `AskUserQuestion` reaches `can_use_tool`. Answer with
  `PermissionResultAllow(updated_input={**input_data, "answers": answers})`.
- `answers` is keyed by the **exact question string**. Single-select → one label **string**; `multiSelect` →
  a **list** of label strings; "none of these" → the user's own words, never `"Other"`, never a label.
- **Never** set `response` alongside `answers` — Claude then receives *"The user responded: …"* and the
  per-question answers are silently discarded.
- `tool_use_id` **is stable** across defer and resume. It is a valid idempotency key.
- A `PreToolUse` hook **can** return `permissionDecision: "defer"`. The CLI exits with
  `stop_reason="tool_deferred"` and a populated `deferred_tool_use`. Defer fires **before** `can_use_tool`,
  so the question must be recovered from `deferred_tool_use.input["questions"]`.
- **Never** put `AskUserQuestion` in `allowed_tools` — a whole-tool allow entry auto-approves before the
  callback is consulted, and settings files can shadow it invisibly.
- `permission_mode` must never be `"dontAsk"` — it *denies* `AskUserQuestion`. The schema has a CHECK
  constraint refusing it.
- A `can_use_tool` callback survives at least a 3-minute block (measured). The 90-minute case is probed by
  `tools/probe_long_block.py`.
- The current Gemini Live model is `gemini-3.8-live`, **not** the `gemini-3.1-flash-live-preview` the build
  sheet named. Pin `google-genai>=2.23,<3`. `speech_config.language_code` does **not** control output
  language on native-audio models.

## The rules that are load-bearing

Full versions in [`CONTRIBUTING.md`](CONTRIBUTING.md). The short form:

1. **Nothing from Mark-LIII enters this tree.** MIT, clean-room. CC BY-NC would bind every derivative
   permanently. CI smoke-alarms for it. ([ADR 0001](docs/adr/0001-clean-room-not-fork.md))
2. **No secret in the tree.** Credentials live in the OS keyring. `.gitignore` comments go on their **own
   line** — a trailing same-line comment makes the pattern a literal that matches nothing, which is exactly
   how the reference build publishes its API key.
3. **Assume the reader is another process, started after you died.** Functions take an open
   `sqlite3.Connection` first and never open one. No module-level mutable state. Races get a test with two
   real connections.
4. **The spine is standard-library only.** `jarvis/{db,ids,clock,bus,requests,jobs,reconcile,effects,presence,kill,ledger}.py`
   must import under `python -S`. `jarvis/cc/` and the voice layer add their own deps behind extras.
5. **Comments say why, never what.**
6. **`jarvis/migrations/*.sql` is frozen.** Add a new numbered migration; never edit an applied one.

## Layout

```
jarvis/           the spine — one SQLite file, several processes, stdlib only
  requests.py     THE unified gate: every human decision is one row, one lifecycle
  bus.py          the event bus, which is also the hash-chained activity log
  jobs.py         jobs outlive the process that started them
  effects.py      undo as three honest classes, decided before execution
  presence.py     can I be heard if I speak into this room?
  ledger.py       three providers whose units do not reconcile
jarvis/cc/        the Claude Code driver — its own OS process, never speaks
spikes/           experiments with recorded results; they stay runnable
tools/            CI guards and probes
docs/             findings, architecture, roadmap, ADRs
```

## Two falsifiable tests

The architecture claims seven seams make the phone layer a re-wiring rather than a rewrite. That is checkable:

- **Stage 3 (Telegram)** must touch **zero** files in `jarvis/audio/`, `jarvis/voice/`, `jarvis/live/` and
  zero lines of `jarvis/cc/driver.py`.
- **Stage 6 (phone)** must touch **zero** files in `jarvis/cc/` and zero lines of `jarvis/requests.py`.

If either fails, the seam did not hold — fix the seam rather than working around it.

## Where things stand

Stage 0 (spikes) and the stage-1 spine are done. See [`docs/roadmap.md`](docs/roadmap.md) for what is next
and what was deliberately cut.
