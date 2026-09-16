# Spike S1 — results

**Run 16 Sep 2026. Claude Code CLI v2.1.273, `claude-agent-sdk` 0.2.153, Python 3.11.15.
All four scenarios PASS. Total cost of the whole spike: $0.12.**

This is the spike the roadmap said to run before anything else, because its failure would mean
requirement 3's core flow needs a different design. It passed, so the design stands.

Reproduce with:

```bash
uv venv .venv && . .venv/bin/activate && uv pip install -e '.[cc,dev]'
python spikes/s0_crossproc/run_spike.py --gap 180
```

---

## What was proven

| # | Scenario | Result |
|---|---|---|
| 1 | Single-select answered by a **different OS process** | PASS — Claude replied *"Todos will be stored in SQLite."* |
| 2 | `multiSelect` answered with a **list** of labels | PASS — `["Due dates", "Priorities"]`; Claude confirmed both |
| 3 | "None of these" as **free text** | PASS — Claude quoted `"Postgres, actually"` back verbatim |
| 4 | **defer → SIGKILL → 180s gap → resume** | PASS — see below |

### Scenario 4, step by step

```
p1  DEFER=1 → PreToolUse returns permissionDecision "defer"
    → driver EXITS ON ITS OWN, rc=0, stop_reason="tool_deferred"
    → ResultMessage.deferred_tool_use carries the FULL questions payload
p2  SIGKILL the process group. Nothing survives.
p3  A different process writes the answer to SQLite. No driver is listening.
    answers = {"How should todos be stored?": "JSON file"}
p4  180 seconds pass. A FRESH process resumes the session.
    → the question RE-FIRES into can_use_tool
    → the answer is REPLAYED from the DB without asking anyone
    → Claude: "We'll store todos in a JSON file."   stop_reason="end_turn"
```

Activity log, verbatim:

```
tool.pre · tool.defer_requested · job.finished ·
tool.pre · question.raised · question.answer_replayed · job.finished
```

---

## Findings that change the plan

**1. `tool_use_id` IS stable across defer and resume.** The roadmap flagged this as an explicit open
question — "is `tool_use_id` identical when the question re-fires after a resume?" — and noted that if
it were not, `dedupe_key` would become load-bearing and the unique index on the requests table would
have to change.

Measured: `toolu_01G6XWrHkijLpMNjMjnBeabu` appeared in the PreToolUse hook before the defer, and the
*same* id re-appeared when the question re-fired in a brand-new process 180 seconds later.

**So `tool_use_id` is a valid idempotency key for the requests table, and `dedupe_key` can stay a
convenience rather than a correctness mechanism.**

**2. A `PreToolUse` hook CAN return `defer`.** The research phase carried a refutation saying a
PreToolUse hook "can only allow or deny". That is wrong for this build: returning

```python
{
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "defer",
        "permissionDecisionReason": "...",
    }
}
```

made the CLI exit with `stop_reason="tool_deferred"` and a populated `deferred_tool_use`. The
away-from-desk path is real, and it is the documented shape.

Note *where* defer fires: **before `can_use_tool`**, so no request row exists yet. The question text
must be recovered from `ResultMessage.deferred_tool_use.input["questions"]`, not from the permission
host. The architecture's away-path must read it from there.

**3. Do not put `AskUserQuestion` in `allowed_tools`.** The SDK emits:

> `CanUseToolShadowedWarning: can_use_tool will not be invoked for: AskUserQuestion. An allowed_tools
> entry that allows a whole tool auto-approves it before the callback is consulted.`

In this run the callback still fired despite the warning — but the warning also says *"Allow rules from
settings files can also shadow the callback but are not visible here"*, which is a silent failure mode
that would remove the permission host without any error. Listing the very tool the host exists to serve
is not worth the risk. Removed; everything still works, and the warning is gone.

**4. `total_cost_usd` was populated** in this environment ($0.0348–$0.0430 per scenario). That is half
of probe S9(a). It does **not** settle the question for the target machine, which will run under a Max
subscription OAuth token — re-run `probe_auth_mode.py` there before deciding the spend ledger's unit.

---

## Payload shapes, confirmed against the real CLI

The answer is keyed by the **exact question string**, and the value's type depends on `multiSelect`:

```python
# single-select → ONE label string
{"How should todos be stored?": "SQLite"}

# multiSelect → a LIST of label strings
{"Which features should be included in the tiny todo CLI?": ["Due dates", "Priorities"]}

# "none of these" → the user's OWN WORDS, not a label and not "Other"
{"Which database should the tiny todo CLI use?": "Postgres, actually"}
```

Returned as:

```python
PermissionResultAllow(updated_input={**input_data, "answers": answers})
```

`response` is never set alongside `answers` — when `response` is present Claude receives *"The user
responded: …"* instead of the per-question answer list, silently discarding the answers.

`input["questions"]` is an **array**; each entry has `question`, `header`, `options[].label`,
`options[].description` and `multiSelect`.

---

## Bug found in this spike, worth remembering

Phase 3 first appeared to fail — the resumed process blocked forever, looking exactly like "resume lost
the answer". The cause was `sqlite3.connect(db)` without `isolation_level=None`: Python's default opens
a *deferred transaction*, and `con.close()` rolled the offline answer back silently.

Every process in this system writes answers that another process reads. **Autocommit, or an explicit
commit, is not optional** — and a silent rollback is indistinguishable from a protocol failure. The real
`jarvis/db.py` sets `isolation_level=None` for exactly this reason.

---

## Still unproven

- Whether `can_use_tool` can block for **90 minutes** without the CLI reaping it (probe S9(c)). The
  longest block measured here was seconds. This matters: if the CLI reaps long-pending callbacks, the
  at-desk flow silently becomes defer-only.
- Behaviour when **sibling tool calls** are in the same batch as the deferred one — the research warns
  defer is silently ignored there.
- Everything about **cloud sessions**, where defer is documented as refused outright.
