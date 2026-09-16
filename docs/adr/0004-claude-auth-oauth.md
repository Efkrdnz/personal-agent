# ADR 0004 — Claude Code runs on the Max subscription OAuth token

**Status:** accepted, 16 Sep 2026
**Decided by:** Big Efk

## Context

This single fact reshapes three subsystems, and the research found a hard contradiction in the obvious
hardening advice: `--bare` is the recommended mode for scripted Claude Code calls, but it **explicitly does
not read `CLAUDE_CODE_OAUTH_TOKEN`**. Following that advice would silently move the project onto metered API
billing without anyone noticing.

## Decision

Use `claude setup-token` against the Max subscription. Two consequences, both accepted:

1. **Hardening is not `--bare`.** It is sandboxing, `permissions.blockReadsOutsideWorkingDirectories`, and a
   default-DENY tool table per channel. The research is unambiguous that deny rules are leaky — `Bash(rm *)`
   does not match `/bin/rm` — so the durable controls are scoped credentials and sandboxing, not deny lists.

2. **The spend ledger's primary unit is `rate_window_pct`, not dollars.** `ResultMessage.total_cost_usd` is
   not meaningful under OAuth, so `usd_equiv` is NULL for Claude and `SpendStatus.unpriced` is **spoken
   aloud** — a $0 total must never be mistaken for free. The build sheet asked for a "warning threshold";
   under this decision it means "you are near your 5-hour limit" rather than a currency amount.

The ledger is still built unit-agnostic, recording both a dollar estimate and a rate-limit-window position,
so switching to an API key later is a config change rather than a rewrite.

## Verification

Spike S9(a) confirms in twenty minutes whether `ResultMessage` reports `total_cost_usd` under the OAuth
token. Run it before stage 2 begins.

## The silent failure to guard against

`claude setup-token` produces a credential with roughly a one-year life. **Set a calendar reminder at 11
months.** Token expiry is the single most likely silent failure of the whole system: everything works, then
one morning nothing does, with no obvious cause.
