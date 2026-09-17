# ADR 0011 — the scheduler is a table, not APScheduler

**Status:** accepted, stage 5
**Supersedes:** the `jarvis/sched.py (APScheduler + SQLAlchemyJobStore on the SAME sqlite file)` line
in `docs/architecture.md` and in the stage 5 entry of `docs/roadmap.md`.

## The decision

`jarvis/schedule/` is a `schedules` table in the existing SQLite file plus a tick loop that computes
`next_run_at`. Roughly 400 lines of standard library across six files, no new dependency.

## Why

**The requirement is not cron.** It is: fire at 10:00 Europe/Istanbul daily, survive a reboot, and
support "call me back in five". There is one schedule. A second one is plausible; a hundred is not.
APScheduler is a general trigger engine — cron expressions, interval jobs, an executor pool, an
event system — and none of that is the problem being solved.

**Two schedulers against one job store is unsupported upstream, and that is the exact failure mode
that matters here.** The thing this component must never do is brief the user twice, and APScheduler
3.x offers no leader election or row-level claim: the documented answer is "run only one". A daemon
that is only correct if you never accidentally start it twice is not the property to build a
briefing on. One `UPDATE … WHERE the claim is free RETURNING` gives it by construction, and
`tests/test_schedule_store.py` proves it with two real connections and two threads.

**The job store is a pickle.** `SQLAlchemyJobStore` stores each job as a pickled callable reference
plus pickled args. When the briefing does not arrive, the question at 2am is "what did it think it
was supposed to do, and when did it last run" — and the answer has to be readable with the `sqlite3`
CLI, not by unpickling a blob in a Python process that has to import the right modules first. The
same pickle makes moving a function to another module a silent breakage of persisted state.

**It brings SQLAlchemy into the one process whose whole job is to be running.** A second connection
pool against the same file, with its own `busy_timeout`, its own `journal_mode` assumptions and its
own opinion about transactions, in the process that must still be alive tomorrow morning. Rule 3 in
`CONTRIBUTING.md` exists because every process here opens the same file; adding a second library
that opens it differently is the specific thing that rule is about.

**Misfire handling is where a library would have earned its place, and it did not.** APScheduler's
`misfire_grace_time` plus `coalesce=True` is genuinely the right semantics, and it is also six lines
of local code: the occurrence a late fire is *for* is `last_at_or_before(now)`, lateness is the
difference, past the grace window the fire is dropped and recorded. Having written it locally, it is
testable by advancing an injected clock, which the library version is not without freezing time
globally.

## What it costs

* Nobody else has run this scheduler. The library has. Against that: the whole surface is one table
  and three functions, every one of them tested, and `sqlite3 jarvis.db 'select * from schedules'`
  is the debugger.
* No cron expressions. "Weekdays only" or "the first of the month" would need real work here, where
  APScheduler has them for free. Revisit this ADR the first time a schedule needs a rule that
  `HH:MM` daily cannot express — that is the honest trigger, and it has not happened yet.
* `docs/architecture.md` line 719 still says the job store points at this file. It is wrong now.

## Revisit when

A second schedule kind needs a recurrence that is not "daily at a wall-clock time", or the table
grows past a handful of rows, or somebody wants distributed execution across machines.
