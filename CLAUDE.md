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
python tools/check_layers.py       # dependencies point inwards, or the seams are a lie
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
- A `can_use_tool` callback survives a **9-minute** block (measured: held 540s, not reaped, answer landed).
  That is past the 5-minute `askUserQuestionTimeout` setting. The 90-minute case is still unproven — re-run
  `tools/probe_long_block.py --seconds 5400` on the target machine.
- The current Gemini Live model is `gemini-3.8-live`, **not** the `gemini-3.1-flash-live-preview` the build
  sheet named. Pin `google-genai>=2.23,<3`. `speech_config.language_code` does **not** control output
  language on native-audio models.

## Traps found the hard way

Things that are true, non-obvious, and will cost you an afternoon if you assume otherwise.

- **`UNIQUE(job_id, dedupe_key, attempt)` does not constrain rows where `job_id` IS NULL.** SQLite treats
  NULLs in a unique index as distinct, and a briefing gate has no job. Verified: two identical NULL-job rows
  insert happily. Idempotency there rests on the `SELECT` inside `create_request`'s `BEGIN IMMEDIATE`, not on
  the index a reader would point at.
- **"Five minutes re-queues by writing `deliver_after`" is not literally possible.** `answer_request` is a
  compare-and-swap that settles every open delivery in the same transaction, so the original row cannot stay
  pending once the user taps. The honest shape is: answer it, then re-ask the *same occurrence* at
  `attempt + 1` with the new delivery's `due_at`. The roadmap's wording predates the spine.
- **`create_request` has no `now_ts` injection.** It reads the spine's own `now()`, so a test advancing an
  injected clock cannot make its own requests expire on that fake clock. It is the one place "inject the
  clock" does not reach.
- **A task-completion notice has no request kind of its own** and borrows `free_text`. A notice is a request
  whose answer is *optional*, and `REQ_KINDS` has no word for that — so a reader of the table cannot tell
  news from a question by kind alone.
- **Two migrations must never share a number.** `db.migrate()` applies by integer prefix and bumps
  `user_version`, so a duplicate silently applies one and skips the other. Two parallel agents hit this;
  there is now a test for it.

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
4. **The spine is standard-library only.** `jarvis/{db,ids,clock,bus,requests,jobs,reconcile,effects,presence,kill,ledger,answers}.py`
   must import under `python -S`. `jarvis/cc/` and the voice layer add their own deps behind extras.
5. **Comments say why, never what.**
6. **`jarvis/migrations/*.sql` is frozen.** Add a new numbered migration; never edit an applied one.
7. **There is exactly one composition root**, `jarvis/__main__.py`, and it holds wiring and no decisions.
   `tools/check_layers.py` exempts that one path by name and a test asserts the set has one element.

## Layout

```
jarvis/           the spine — one SQLite file, several processes, stdlib only
  requests.py     THE unified gate: every human decision is one row, one lifecycle
  answers.py      the AskUserQuestion payload and answer shapes, for every channel
  bus.py          the event bus, which is also the hash-chained activity log
  jobs.py         jobs outlive the process that started them
  effects.py      undo as three honest classes, decided before execution
  presence.py     can I be heard if I speak into this room?
  ledger.py       three providers whose units do not reconcile
  secrets.py      keyring first, environment second, a file never
  config.py       settings that are not secrets, and a refusal if one appears
jarvis/tools/     what a spoken sentence is allowed to make happen
jarvis/cc/        the Claude Code driver — its own OS process, never speaks
jarvis/__main__.py  THE composition root. The one file allowed to know every layer
spikes/           experiments with recorded results; they stay runnable
tools/            CI guards and probes
docs/             findings, architecture, roadmap, ADRs
```

## Running it

```bash
python -m jarvis doctor            # what is missing, and the command that fixes it
python -m jarvis secrets set gemini_api_key
python -m jarvis config init
python -m jarvis desk              # listen, talk, drive Claude Code
```

`doctor` is the front door. A voice assistant fails at startup with no screen and no log anybody will
find, so the whole of "why won't it start" is one command. [`docs/setup.md`](docs/setup.md) is the same
thing in prose, for the parts that happen in a browser.

## Two falsifiable tests

The architecture claims seven seams make the phone layer a re-wiring rather than a rewrite. That is checkable:

- **Stage 3 (Telegram)** must touch **zero** files in `jarvis/audio/`, `jarvis/voice/`, `jarvis/live/` and
  zero lines of `jarvis/cc/driver.py`.
- **Stage 6 (phone)** must touch **zero** files in `jarvis/cc/` and zero lines of `jarvis/requests.py`.

If either fails, the seam did not hold — fix the seam rather than working around it.

## Where things stand

Every stage's PARTS are built and tested. What is missing is almost entirely COMPOSITION: processes that
read the rows other processes write. An audit of all five entry points found four such gaps, none of which
any test could have caught, because every one of them is a caller that does not exist.

**NOTHING CONNECTS TWO PROCESSES YET.** This is the single sentence to read before believing anything else
here. Each gap below is one missing caller:

- ~~**No channel is ever handed a Claude Code question.**~~ **CLOSED.** `jarvis/schedule/loop.py` has a
  fifth sweep, `route_undelivered`, which hands every open driver question to `routing.deliver`. It routes
  UNCONDITIONALLY — `schedule_delivery` is idempotent per `(request, channel, attempt)`, and a guard would
  make a half-written ladder permanent, since `deliver` is not atomic across rungs. `ROUTABLE_KINDS` names
  what this sweep owns; a kind that is not there is somebody else's to deliver, which is what stops it
  stealing a snoozed briefing gate. **The scheduler must be running for Telegram to get questions.**
- ~~**No command creates a `claude_code` job.**~~ **CLOSED.** `python -m jarvis run "..."`, plus `pending`
  and `answer` to see and settle questions from a terminal. Verified live end to end against the real CLI.
- ~~**Nothing consumes `repo_setup`.**~~ **CLOSED.** `jarvis/project/runner.py` is the state machine:
  tidy the transcript, read the list back as a `readback` request any channel can present, apply spoken
  edits ("drop three"), then create the repository, clone it, and hand stage 2 a `claude_code` job with a
  cwd. `python -m jarvis build` drives it one step at a time. With no GitHub token it builds LOCALLY and
  says so, because R2's order is "the repository before the code", not "refuse to work without one".
- **Nothing consumes `briefing.started`.** `jarvis/schedule/gate.py` publishes it, `jarvis/briefing/` has no
  importer outside its own directory, and `navigator.begin()` has no caller. Worse, that event kind is
  published from TWO places with incompatible payloads.
- **Nothing reads the event log at all.** `bus.read_since` and `bus.commit_cursor` — the bus's whole consumer
  API — have no production caller. The hash-chained log is written and never read.
And two smaller ones: the desk has no wake word, and `DESK.tools` names four tools that do not exist
(`doctor` prints which). `voice.wake_word` and `voice.output_device` are config keys nothing reads.

See [`docs/roadmap.md`](docs/roadmap.md) for what is next and what was deliberately cut.

## The bug class this repo is prone to

Found the hard way, twice in one day, and worth stating as a rule: **this tree's tests exercise layers and
its bugs live between them.** Both were a missing call, not a wrong one, so nothing raised and nothing
failed:

- `DeskLeg.open()` opened the PortAudio stream and never called `.start()`. Every audio test drives
  `graph.step` directly, so 1,982 tests were green while the only thing that calls it in production — the
  device callback — was never armed. `python -m jarvis desk` printed "listening." and was deaf and mute.
- The desk's terminal listener branched on two event kinds `LiveSession` has never emitted.

When you add a seam, add a test that asserts the CALLER exists, not just that the callee works.
