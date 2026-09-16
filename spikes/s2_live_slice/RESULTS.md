# Stage 2, live slice — results

**Run 16 Sep 2026. Both scenarios PASS.** The headline feature works end to end through the real spine,
with the plan-mode answer supplied by a different OS process.

```bash
python spikes/s2_live_slice/run_slice.py --scenario single
python spikes/s2_live_slice/run_slice.py --scenario multi
```

Spike S1 proved the *CLI mechanics*. This proves those mechanics survive being routed through `jobs`,
`requests`, `bus`, `ledger` and the permission host — a different claim, and the one stage 2 rests on.

## Single-select

What the user would have heard, generated locally from the frozen payload — the numbering is ours, the
labels are carried verbatim:

```
How should todos be stored?
1. SQLite
2. JSON file
3. Plain text
Or say your own answer.
```

Answered by **index** `[1]` from a process that never imports the SDK. The host resolved the index to a
label by local lookup, validated it against the frozen array, and returned `updated_input`.

```
sent    {"answers": {"How should todos be stored?": "SQLite"}, "sources": {...: "option"}}
Claude  "SQLite it is."
job     done · rc 0 · hash chain verifies
```

## multiSelect

```
Which features should the tiny todo CLI include?
You can pick more than one.
1. Due dates   2. Tags   3. Priorities   4. Recurring
Or say your own answer.
```

Answered `[1, 3]`. Note the value is a **list**, which is the shape `multiSelect` requires and the one that
fails silently if you get it backwards:

```
sent    {"answers": {"Which features...": ["Due dates", "Priorities"]}}
Claude  "You chose to include Due dates and Priorities."
job     done · rc 0 · hash chain verifies
```

## The spend line, working as intended

R5 asked for a running total with a warning threshold. Under a Max subscription the meaningful unit is not
dollars, and the ledger says so out loud rather than reporting a reassuring `$0.00`:

> "Nothing priced today, against a 20 dollar ceiling. That number does not include claude code, 55431
> tokens, not dollars; and claude code, Max subscription: an API-equivalent estimate, not a bill.
> **Zero priced is not the same as free.**"

## Observation: the activity log is ambiguous

The recorded event sequence for the single-select run:

```
job.created · job.started · job.started · tool.used · request.created · job.blocked ·
job.started · request.consumed · job.stopped · spend.recorded · job.progress · job.finished
```

Nothing here is *wrong* — `starting` and `running` both map to `job.started`, and the third one is the
legitimate `blocked -> running` when the answer arrived. But the activity log exists to answer "what did you
do today" honestly, and three identical `job.started` entries plus a `job.progress` that means both
"queued" and "finishing" make that answer harder to give than it should be.

`set_state` already accepts an `event=` override, so the fix belongs in the caller rather than in the
spine's default map: the `blocked -> running` transition should publish `job.resumed`, which already exists
as an event kind. Not urgent, not a correctness bug, and recorded here rather than silently accepted.

## Still unproven at this layer

- The **defer** path through the spine. S1 proved it at the CLI level; the driver's
  `stop_reason == "tool_deferred"` branch has unit coverage but has not been exercised live end to end.
- `ExitPlanMode`. The prompts here deliberately say *"Do not call ExitPlanMode"* to keep the slice to one
  mechanism. The plan read-back and approve/deny path is written and unit-tested but not yet live.
- Everything upstream of the request row: there is no microphone, no Gemini, and no tidier in this loop.
  `jarvis/spec.py` enforces the tidier's guarantees mechanically and is tested, but a real spoken spec has
  never been through it.
