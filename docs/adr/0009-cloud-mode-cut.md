# ADR 0009 — Cloud mode is cut from v1

**Status:** accepted, 16 Sep 2026
**Decided by:** Big Efk

## Context

The build sheet asks Jarvis to infer local-vs-cloud from phrasing and spin the project up in the cloud "so
nothing touches local disk". Research established that this cannot be built on Claude Code cloud sessions.

**Claude Code cloud sessions are a product surface, not an API.** The only programmatic doors are
create-and-forget: `claude --cloud "task"` and the experimental routines `/fire` endpoint, both of which queue
a message and exit without waiting. There is no *live streaming* read path — `--teleport` is a post-hoc read,
useful for reviewing a finished run and useless for narrating a question mid-run.

Plan mode is the core feature, and it needs a bidirectional readable stream. The CLI confirms the
incompatibility independently: a `PreToolUse` defer is converted to a hard DENY for calls served to a cloud
session, with the message *"deferral is not supported for calls served to a cloud session"*. So the defer path
— the mechanism behind "Jarvis calls you when you're away" — does not exist in the cloud.

Managed Agents does offer full create/send/SSE-read, but it is a different harness with different behaviour,
which would mean writing the plan-mode conversation twice.

## Decision

Cut it. The local/cloud classifier still ships: it asks when the phrasing is genuinely ambiguous, and
otherwise answers "local" — out loud, honestly, rather than silently doing something else.

## Revisit when

There are six months of recorded phrasing data showing cloud mode is actually asked for. Then build it via
Anthropic's own documented pattern: plan **locally** where the permission host exists, commit the plan, then
`claude --cloud "execute the plan in docs/plan.md"` — which sidesteps the read-path problem instead of
fighting it.
