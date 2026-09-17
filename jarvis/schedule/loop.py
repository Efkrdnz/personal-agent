"""One pass of the daemon: fire, expire, act, notify, route. Everything else is sleeping.

FIVE SWEEPS, each of which is safe to run twice and safe to interrupt anywhere:

``fire``      due schedules, claimed one at a time, one late fire at most.
``expire``    the spine's typed ``on_timeout`` outcomes, which need SOMEBODY to
              run them; this is the process that is awake, so it is this one.
``answers``   an answered gate, acted on — the server-side pointer means the
              answer may have arrived on a channel this process has never heard
              of, hours after the question was asked.
``notify``    jobs that finished since the cursor, routed by presence.
``route``     questions somebody ELSE raised and nobody has been asked about.

THE FIFTH SWEEP IS WHY THE PHONE STAGE IS A RE-WIRING. ``jarvis/cc`` raises a
row when Claude Code asks something and then blocks; it may not import this
package, and it must not know which channel will answer — that is the whole
seam. So somebody has to stand between "a question exists" and "a channel was
told", and it is this process, because deciding WHEN you get asked is already
its job. Without it the question is raised, ``/status`` reports it, and no
channel can ever present it: which is exactly how this shipped for one release.

NO CLOCK OF ITS OWN. Every function takes ``now_ts``; the process passes
:func:`jarvis.ids.now` once per tick so that the four sweeps agree about what
time it is, and a test passes whatever instant it wants to be at.

THE HANDLER TABLE IS A CONSTANT, not a registry that modules add themselves to at
import time. A schedule row names its handler in ``fires``; an unknown name is
reported, not guessed at, and the schedule is left due rather than advanced —
"the daemon is one version behind" must not silently eat a morning.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from jarvis import jobs
from jarvis import requests as rq
from jarvis.bus import publish
from jarvis.ids import now
from jarvis.schedule import completion, gate, store
from jarvis.schedule.routing import deliver

__all__ = [
    "COMPLETION_CURSOR",
    "DEFAULT_HANDLERS",
    "FireReport",
    "Handler",
    "TickReport",
    "completion_cursor",
    "fire_due",
    "handle_gate_answers",
    "notify_finished",
    "ROUTABLE_KINDS",
    "RouteReport",
    "route_undelivered",
    "set_completion_cursor",
    "sweep_expiries",
    "tick",
]

#: Request kinds this sweep is responsible for: the ones a DRIVER raises and no
#: other sweep owns. Deliberately a list rather than "everything pending".
#:
#: A briefing gate is raised AND delivered by ``fire_due`` in the same breath,
#: and ``handle_gate_answers`` re-asks a snoozed one with ``deliver_after`` set
#: five minutes out. A blind sweep would route that row again with no snooze and
#: steal it — the user would be asked immediately, having just said "in five
#: minutes". So this sweep names what it owns, and a kind that is not here is
#: somebody else's to deliver.
#:
#: GROWS ONE KIND AT A TIME, as each raiser gets a runner. When the project
#: builder lands, its read-back kind joins this tuple and the test below is what
#: notices if it does not.
ROUTABLE_KINDS: frozenset[str] = frozenset({"plan_question", "exit_plan", "tool_permission"})

#: Where "which finished jobs have already been told to the user" lives. The
#: cursors table is the shared key-value store migration 001 names; this is one
#: more name in it rather than one more table.
COMPLETION_CURSOR = "completion_last_run"

#: Fallback window the first time a machine ever ticks: one hour, not a day. A
#: fresh install must not announce every job it finds in the history it inherited.
COMPLETION_LOOKBACK_S = 3600.0

Handler = Callable[[sqlite3.Connection, store.Claim, str, str], str | None]


@dataclass(frozen=True, slots=True)
class FireReport:
    """What one schedule did this tick, in the words the log uses."""

    schedule: str
    outcome: store.FireOutcome
    due_at: str
    late_s: float
    missed: int
    request_id: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class TickReport:
    """Everything one pass did. Empty is the normal answer."""

    fired: tuple[FireReport, ...] = ()
    expired: tuple[rq.Expiry, ...] = ()
    answers: tuple[gate.GateOutcome, ...] = ()
    notices: tuple[completion.Notice, ...] = ()
    routed: tuple[RouteReport, ...] = ()
    unknown_handlers: tuple[str, ...] = field(default=())


@dataclass(frozen=True, slots=True)
class RouteReport:
    """One question this sweep handed to the ladder, and where it went.

    ``channels`` empty means presence reached nobody — the question is raised and
    undeliverable, which is a real state and not an error. ``skipped`` is the
    sentence saying why it was not routed at all.
    """

    request_id: str
    kind: str
    short_label: str
    channels: tuple[str, ...] = ()
    skipped: str | None = None


def _fire_briefing_gate(
    con: sqlite3.Connection, held: store.Claim, actor: str, now_ts: str
) -> str | None:
    """Raise the morning gate for the occurrence this claim is for, and route it."""
    req = gate.raise_gate(
        con,
        schedule=held.schedule.name,
        occurrence=held.due_at,
        actor=actor,
        now_ts=now_ts,
    )
    deliver(con, req, now_ts=now_ts)
    store.note_request(con, held.schedule.id, req.id, now_ts=now_ts)
    return req.id


#: ``fires`` -> what to do. Immutable: a module-level dict that anything could
#: append to would be module-level mutable state, and which schedules exist would
#: then depend on what happened to be imported.
DEFAULT_HANDLERS: Mapping[str, Handler] = MappingProxyType({"briefing_gate": _fire_briefing_gate})


def fire_due(
    con: sqlite3.Connection,
    *,
    actor: str,
    claimed_by: str | None = None,
    now_ts: str | None = None,
    handlers: Mapping[str, Handler] | None = None,
    limit: int = 50,
) -> tuple[list[FireReport], list[str]]:
    """Claim and fire every schedule that is due. Returns (reports, unknown handlers).

    Claim first, fire second, complete third. A fire that raises releases the
    claim and leaves the pointer alone, so the next tick retries it — which is
    only safe because raising the same occurrence twice returns the same request.
    """
    ts = now_ts or now()
    who = claimed_by or actor
    table = handlers if handlers is not None else DEFAULT_HANDLERS
    reports: list[FireReport] = []
    unknown: list[str] = []

    for sched in store.due(con, ts, limit=limit):
        try:
            held = store.claim(con, sched.id, who, now_ts=ts)
        except ValueError as exc:
            # A row whose at_local/tz pair no occurrence can be computed from —
            # a hand edit with the sqlite3 CLI, which this table is explicitly
            # designed to invite. Without this it propagates out of tick(), so
            # ONE typo costs the expiry sweep, the gate answers and the
            # completion notices as well, every tick, until somebody notices the
            # daemon exiting 5 in a loop. One bad schedule must not stop the
            # rest, and it must be visible on the bus rather than in a traceback.
            publish(
                con,
                "schedule.failed",
                actor,
                {
                    "schedule": sched.name,
                    "at_local": sched.at_local,
                    "tz": sched.tz,
                    "error": f"{type(exc).__name__}: {exc}",
                },
                idem_key=f"sched:{sched.id}:unusable:{sched.at_local}:{sched.tz}",
            )
            reports.append(
                FireReport(
                    sched.name,
                    "error",
                    sched.next_run_at,
                    0.0,
                    0,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            continue
        if held is None:
            continue  # another process got there first; it is firing, not us

        if not held.within_grace:
            store.complete(con, held, outcome="missed", now_ts=ts)
            publish(
                con,
                "schedule.missed",
                actor,
                {
                    "schedule": sched.name,
                    "due_at": held.due_at,
                    "late_s": held.late_s,
                    "grace_s": sched.grace_s,
                    "slept_through": held.missed,
                },
                idem_key=f"sched:{sched.id}:{held.due_at}:missed",
            )
            reports.append(FireReport(sched.name, "missed", held.due_at, held.late_s, held.missed))
            continue

        handler = table.get(sched.fires)
        if handler is None:
            # Left due on purpose: an unknown handler is a deployment that is
            # behind, and advancing the pointer would turn "restart the daemon"
            # into "you lost Tuesday".
            store.release(con, held, now_ts=ts)
            unknown.append(sched.fires)
            continue

        try:
            request_id = handler(con, held, actor, ts)
        except Exception as exc:  # noqa: BLE001 - one bad schedule must not stop the rest
            store.release(con, held, now_ts=ts)
            publish(
                con,
                "schedule.failed",
                actor,
                {
                    "schedule": sched.name,
                    "due_at": held.due_at,
                    "error": f"{type(exc).__name__}: {exc}",
                },
                idem_key=f"sched:{sched.id}:{held.due_at}:failed:{ts}",
            )
            reports.append(
                FireReport(
                    sched.name,
                    "error",
                    held.due_at,
                    held.late_s,
                    held.missed,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            continue

        store.complete(con, held, outcome="fired", now_ts=ts)
        publish(
            con,
            "schedule.fired",
            actor,
            {
                "schedule": sched.name,
                "due_at": held.due_at,
                "fired_at": ts,
                # Named rather than implied: a fire that was five minutes late is
                # in the log as five minutes late, so nothing later has to infer
                # it from two timestamps and get it wrong.
                "late_s": held.late_s,
                "late": held.late,
                "slept_through": held.missed,
                "request_id": request_id,
            },
            request_id=request_id,
            idem_key=f"sched:{sched.id}:{held.due_at}:fired",
        )
        reports.append(
            FireReport(sched.name, "fired", held.due_at, held.late_s, held.missed, request_id)
        )
    return reports, unknown


def sweep_expiries(
    con: sqlite3.Connection, *, actor: str, now_ts: str | None = None
) -> list[rq.Expiry]:
    """Apply the spine's typed timeouts and say on the bus what each one decided."""
    ts = now_ts or now()
    out = rq.expire_due(con, ts)
    for exp in out:
        publish(
            con,
            "request.expired",
            actor,
            {"outcome": exp.outcome, "on_timeout": exp.on_timeout, "reason": exp.reason},
            job_id=exp.job_id,
            request_id=exp.request_id,
            idem_key=f"req:{exp.request_id}:expired:{exp.outcome}",
        )
    return out


def handle_gate_answers(
    con: sqlite3.Connection, *, actor: str, now_ts: str | None = None
) -> list[gate.GateOutcome]:
    """Act on every schedule whose pointer names an answered gate.

    The pointer is read from the schedules row rather than from a query over
    requests, which is what makes this work across processes and restarts: the
    daemon that asks the question and the daemon that reads the answer need not
    be the same one, or even be running at the same time.
    """
    ts = now_ts or now()
    out: list[gate.GateOutcome] = []
    for sched in store.list_schedules(con):
        if sched.last_request_id is None:
            continue
        req = rq.get_request(con, sched.last_request_id)
        if req is None or req.state != "answered":
            continue
        # Both halves checked, not one. A schedule that fires something else has
        # a pointer at a request that is not a gate, and reading a non-gate
        # answer through the gate's three labels would resolve to "unreadable"
        # and consume somebody else's answer on the way past.
        if sched.fires != "briefing_gate" or req.kind != "briefing_gate":
            continue
        outcome = gate.handle_answer(con, req, actor=actor, now_ts=ts)
        if outcome.next_request is not None:
            # The snooze, delivered: one delivery row with a later due_at, and
            # the pointer moved to the re-ask. The schedules row's TIME columns
            # are untouched — postponing is not rescheduling.
            deliver(
                con,
                outcome.next_request,
                now_ts=ts,
                deliver_after=outcome.deliver_after,
            )
            store.note_request(con, sched.id, outcome.next_request.id, now_ts=ts)
        out.append(outcome)
    return out


def completion_cursor(con: sqlite3.Connection) -> str | None:
    row = con.execute("SELECT value FROM cursors WHERE name=?", (COMPLETION_CURSOR,)).fetchone()
    return None if row is None else str(row["value"])


def set_completion_cursor(con: sqlite3.Connection, ts: str) -> str:
    """Advance the cursor. INCLUSIVE, like the briefing's, and for the same reason.

    :func:`jarvis.reconcile.set_briefing_cursor` explains it: ``jobs.since``
    matches ``updated_at >= cursor`` and timestamps are millisecond-granular, so a
    job that finishes in the same millisecond gets seen twice. Repeating beats
    dropping, and here the repeat costs nothing at all because
    :func:`jarvis.schedule.completion.raise_completion` is idempotent per
    (job, state).
    """
    con.execute(
        "INSERT INTO cursors (name, value, updated_at) VALUES (?,?,?)"
        " ON CONFLICT(name) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (COMPLETION_CURSOR, ts, now()),
    )
    return ts


def notify_finished(
    con: sqlite3.Connection, *, actor: str, now_ts: str | None = None
) -> list[completion.Notice]:
    """Route the news about every job that finished since the cursor."""
    ts = now_ts or now()
    start = completion_cursor(con) or jobs.shift_ts(ts, -COMPLETION_LOOKBACK_S)
    out: list[completion.Notice] = []
    for job in jobs.since(con, start):
        if job.state not in completion.TERMINAL_STATES:
            continue
        notice = completion.raise_completion(con, job.id, actor=actor, now_ts=ts)
        if notice is None:
            continue
        # Only route a notice that is still open: a re-seen job returns its
        # existing row, and re-delivering an answered one would ask again.
        if notice.request.state == "pending":
            deliver(con, notice.request, now_ts=ts)
        out.append(notice)
    set_completion_cursor(con, ts)
    return out


def route_undelivered(
    con: sqlite3.Connection, *, actor: str, now_ts: str | None = None
) -> list[RouteReport]:
    """THE MISSING CALLER. Hand every unrouted driver question to the ladder.

    Routes UNCONDITIONALLY rather than first asking "does this row already have
    deliveries". That looks wasteful and is the whole correctness argument:

    * :func:`jarvis.requests.schedule_delivery` is idempotent per
      ``(request, channel, attempt)``, so re-routing a question costs one
      no-op INSERT and cannot ask anybody twice.
    * :func:`jarvis.schedule.routing.deliver` is NOT atomic across rungs — it
      loops, and each rung opens its own transaction. A process killed between
      the desk rung and the Telegram rung leaves a half-ladder, and a
      "skip rows that already have deliveries" guard would make that half-ladder
      PERMANENT: the user would be asked only on a channel nobody is listening
      to, forever, with no error anywhere.
    * ``deliver`` re-asks presence every time. A question routed while the user
      was asleep gets a better ladder once they are back, which is precisely the
      behaviour ``deliver``'s own docstring promises and a guard would defeat.

    So the sweep is self-healing by construction, and the cost of that is one
    SELECT per tick per open question.
    """
    ts = now_ts or now()
    out: list[RouteReport] = []
    for req in rq.open_requests(con):
        if req.kind not in ROUTABLE_KINDS:
            continue
        try:
            if req.job_id is not None:
                job = jobs.get(con, req.job_id)
                if job is None or job.state in jobs.TERMINAL_STATES:
                    # Its driver is gone. Asking would be asking on behalf of
                    # nobody: whatever the answer, there is no process left to
                    # consume it. Reported rather than cancelled — deciding a
                    # question is moot belongs to reconcile, not to the router.
                    out.append(
                        RouteReport(req.id, req.kind, req.short_label, skipped="the job is over")
                    )
                    continue
            made = deliver(con, req, now_ts=ts)
            out.append(
                RouteReport(req.id, req.kind, req.short_label, tuple(d.channel_kind for d in made))
            )
        except Exception as exc:  # noqa: BLE001 - see fire_due: one bad row must not cost the rest
            # A busy timeout under a second scheduler is a supported
            # configuration, and without this it would take out the other four
            # sweeps with it, every tick, until somebody noticed exit 5 in a loop.
            publish(
                con,
                "request.route_failed",
                actor,
                {"kind": req.kind, "error": f"{type(exc).__name__}: {exc}"},
                request_id=req.id,
            )
            out.append(
                RouteReport(req.id, req.kind, req.short_label, skipped=f"{type(exc).__name__}")
            )
    return out


def tick(
    con: sqlite3.Connection,
    *,
    actor: str = "scheduler",
    claimed_by: str | None = None,
    now_ts: str | None = None,
    handlers: Mapping[str, Handler] | None = None,
) -> TickReport:
    """One whole pass, in the order that makes each step see the last one's work.

    Expiries run BEFORE answers so that a gate which timed out into "skip today"
    is acted on in the same tick it resolved, instead of sitting answered for
    fifteen seconds; and fires run first of all so that a gate raised this second
    is delivered before anything looks at it.
    """
    ts = now_ts or now()
    fired, unknown = fire_due(con, actor=actor, claimed_by=claimed_by, now_ts=ts, handlers=handlers)
    expired = sweep_expiries(con, actor=actor, now_ts=ts)
    answered = handle_gate_answers(con, actor=actor, now_ts=ts)
    notices = notify_finished(con, actor=actor, now_ts=ts)
    # LAST, and the order is load-bearing. The four sweeps above each raise a
    # request and deliver it in the same breath — including the snoozed gate,
    # which `handle_gate_answers` delivers with `deliver_after` five minutes out.
    # Routing before them would reach that row first, deliver it with no snooze,
    # and ask the user immediately after they said "in five minutes".
    routed = route_undelivered(con, actor=actor, now_ts=ts)
    return TickReport(
        fired=tuple(fired),
        expired=tuple(expired),
        answers=tuple(answered),
        notices=tuple(notices),
        routed=tuple(routed),
        unknown_handlers=tuple(unknown),
    )
