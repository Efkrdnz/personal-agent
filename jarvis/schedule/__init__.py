"""The thing that wakes up, and the morning gate it raises when it does.

WHY THERE IS NO APSCHEDULER HERE. The roadmap named APScheduler 3.11.3 with a
SQLAlchemyJobStore on this same file. Three measured facts decided against it and
they are recorded in :file:`docs/adr/0011-scheduler-is-a-table.md`:

  * its job store keeps each job as a PICKLE of a callable, so the state of the
    thing that was supposed to wake you up is unreadable with the sqlite3 CLI at
    2am and breaks when a function moves module;
  * two schedulers against one job store is explicitly unsupported upstream,
    and "two processes must not both brief you" is the exact failure this
    component exists to prevent — solved here by one compare-and-swap;
  * it brings SQLAlchemy, i.e. a second connection pool with its own pragmas,
    into the one process whose entire job is to be running and reliable.

The requirement is "fire at 10:00 Europe/Istanbul daily, survive a reboot and
support call-me-back-in-five", not general cron. That is a table, a recurrence
function and a tick loop.

WHAT IS HERE:

``recurrence``  when 10:00 next happens, and which morning a late fire is FOR
``store``       the schedules table: arm, claim, fire once, advance
``gate``        "Good moment for your briefing?" as ONE request with three options
``routing``     presence -> delivery rows; the only file that names a channel
``completion``  a finished job, routed the same way
``loop``        one pass of the daemon; ``python -m jarvis.schedule`` is the loop

WHAT IS NOT HERE, on purpose: anything that knows what a briefing CONTAINS. The
gate publishes ``briefing.started`` and stops. A section is a request like any
other, so the thing that reads the sections out needs nothing from this package
except the event that says the user said yes.
"""

from __future__ import annotations

from jarvis.schedule.completion import Notice, raise_completion
from jarvis.schedule.gate import (
    FIVE_LABEL,
    GATE_QUESTION,
    MAX_SNOOZES,
    NOW_LABEL,
    SKIP_LABEL,
    SNOOZE_S,
    GateOutcome,
    handle_answer,
    raise_gate,
)
from jarvis.schedule.loop import TickReport, tick
from jarvis.schedule.recurrence import last_at_or_before, next_after
from jarvis.schedule.routing import deliver, ladder
from jarvis.schedule.store import (
    Claim,
    Schedule,
    claim,
    complete,
    due,
    ensure_schedule,
    get_schedule,
    list_schedules,
    set_enabled,
)

__all__ = [
    "FIVE_LABEL",
    "GATE_QUESTION",
    "MAX_SNOOZES",
    "NOW_LABEL",
    "SKIP_LABEL",
    "SNOOZE_S",
    "Claim",
    "GateOutcome",
    "Notice",
    "Schedule",
    "TickReport",
    "claim",
    "complete",
    "deliver",
    "due",
    "ensure_schedule",
    "get_schedule",
    "handle_answer",
    "ladder",
    "last_at_or_before",
    "list_schedules",
    "next_after",
    "raise_completion",
    "raise_gate",
    "set_enabled",
    "tick",
]
