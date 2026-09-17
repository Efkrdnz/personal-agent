"""``python -m jarvis.schedule`` — the daemon that wakes up, as its own OS process.

Like ``python -m jarvis.cc`` and ``python -m jarvis.telegram``, this is a process
and not a library: it opens the one SQLite file, ticks, and exits. It has no
sound card, no token and no network, and it never speaks — it writes rows and
lets whichever channel can reach a human do the talking.

IT DOES NOT ATTACH TO THE ``channels`` TABLE, deliberately. A channel is
something a request can be delivered TO; the scheduler only ever raises them
(``human: False`` in the architecture's capability table). A row claiming the
scheduler is a channel would make it eligible for a ladder rung, and a question
would be "delivered" to a process that cannot ask anybody anything.

EXIT CODES, because a supervisor reads them:

    0   the loop ended cleanly (or ``--once`` finished, or a query was printed)
    2   refused to start: the time or the zone it was given is not a real one
    5   the loop died in a way nothing here anticipated

TWO PROCESSES ARE SAFE. Running a second copy by accident costs nothing: the
claim is a compare-and-swap, so exactly one of them fires each morning.
"""

from __future__ import annotations

import argparse
import os
import signal
import sqlite3
import sys
import time
from collections.abc import Callable, Sequence
from types import FrameType

from jarvis.clock import DEFAULT_TZ, spoken_time
from jarvis.db import open_db
from jarvis.ids import now
from jarvis.schedule import gate, store
from jarvis.schedule.loop import TickReport, tick
from jarvis.schedule.recurrence import check_at_local

__all__ = ["DEFAULT_INTERVAL_S", "build_parser", "install", "main", "run"]

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_CRASHED = 5

#: Fifteen seconds. The only deadlines this loop serves are a schedule (accurate
#: to the minute is ample) and a five-minute snooze, and a daemon that wakes four
#: times a minute to run three indexed queries costs nothing measurable.
DEFAULT_INTERVAL_S = 15.0

BRIEFING_SCHEDULE = "morning_briefing"
BRIEFING_AT_LOCAL = "10:00"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m jarvis.schedule", description=__doc__)
    p.add_argument("--db", default=None, help="database path; defaults to $JARVIS_DB")
    p.add_argument("--once", action="store_true", help="one tick, then exit")
    p.add_argument("--list", action="store_true", help="print the schedules and exit")
    p.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL_S,
        help="seconds between ticks",
    )
    p.add_argument(
        "--at",
        default=BRIEFING_AT_LOCAL,
        help="local wall-clock time of the morning briefing, HH:MM",
    )
    p.add_argument("--tz", default=DEFAULT_TZ, help="IANA zone the --at time is in")
    p.add_argument(
        "--retime",
        action="store_true",
        help="move the morning briefing to --at/--tz (otherwise an existing one is left alone)",
    )
    p.add_argument("--enable", action="store_true", help="enable the morning briefing and exit")
    p.add_argument("--disable", action="store_true", help="disable the morning briefing and exit")
    return p


def install(
    con: sqlite3.Connection,
    *,
    at_local: str = BRIEFING_AT_LOCAL,
    tz: str = DEFAULT_TZ,
    retime: bool = False,
    now_ts: str | None = None,
) -> store.Schedule:
    """Arm the morning briefing. Idempotent, and it does not re-arm on restart."""
    sched = store.ensure_schedule(
        con,
        name=BRIEFING_SCHEDULE,
        fires="briefing_gate",
        at_local=at_local,
        tz=tz,
        payload={"question": gate.GATE_QUESTION},
        now_ts=now_ts,
    )
    if retime and (sched.at_local != at_local or sched.tz != tz):
        sched = store.retime(con, BRIEFING_SCHEDULE, at_local=at_local, tz=tz, now_ts=now_ts)
    return sched


def _say(line: str) -> None:
    """One line to stdout, and never a reason to fail.

    The same swallow as ``jarvis.cc.__main__``: a detached daemon writing to a
    closed pipe must not turn a working scheduler into a traceback.
    """
    try:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
    except OSError:
        pass


def _describe(sched: store.Schedule) -> str:
    state = "on " if sched.enabled else "off"
    late = "" if sched.last_late_s is None else f", last {sched.last_late_s:.0f}s late"
    return (
        f"{state} {sched.name}: {sched.fires} at {sched.at_local} {sched.tz}"
        f" -> next {spoken_time(sched.next_run_at)} ({sched.next_run_at})"
        f"{late}, {sched.fire_count} fired, {sched.missed_count} missed"
    )


def run(
    con: sqlite3.Connection,
    *,
    actor: str,
    claimed_by: str,
    interval_s: float = DEFAULT_INTERVAL_S,
    once: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    stop: Callable[[], bool] = lambda: False,
) -> list[TickReport]:
    """Tick until asked to stop. ``sleep`` is injected so no test waits for real.

    Every tick is a fresh ``now()``. The loop holds nothing between passes on
    purpose: whatever this process knows is in the database already, so being
    killed here is the same as being killed anywhere else.
    """
    reports: list[TickReport] = []
    while True:
        report = tick(con, actor=actor, claimed_by=claimed_by, now_ts=now())
        reports.append(report)
        for fired in report.fired:
            _say(f"{fired.schedule}: {fired.outcome} for {fired.due_at} ({fired.late_s:.0f}s late)")
        for outcome in report.answers:
            _say(f"briefing {outcome.occurrence}: {outcome.action}")
        for notice in report.notices:
            _say(f"notice: {notice.line}")
        if once or stop():
            return reports
        sleep(interval_s)


def main(argv: Sequence[str] | None = None, *, sleep: Callable[[float], None] = time.sleep) -> int:
    args = build_parser().parse_args(argv)
    try:
        check_at_local(args.at, args.tz)
    except ValueError as exc:
        _say(f"refused: {exc}")
        return EXIT_REFUSED

    con = open_db(args.db)
    try:
        if args.enable or args.disable:
            install(con, at_local=args.at, tz=args.tz)
            sched = store.set_enabled(con, BRIEFING_SCHEDULE, args.enable)
            _say(_describe(sched))
            return EXIT_OK

        install(con, at_local=args.at, tz=args.tz, retime=args.retime)
        if args.list:
            for sched in store.list_schedules(con):
                _say(_describe(sched))
            return EXIT_OK

        # The claim identifies the PROCESS, not the role: two schedulers on one
        # box must be distinguishable in the row that says who is firing.
        claimed_by = f"scheduler:{os.getpid()}"
        stopping = False

        def _stop(signum: int, frame: FrameType | None) -> None:
            nonlocal stopping
            stopping = True

        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, _stop)

        run(
            con,
            actor="scheduler",
            claimed_by=claimed_by,
            interval_s=args.interval,
            once=args.once,
            sleep=sleep,
            stop=lambda: stopping,
        )
        return EXIT_OK
    except KeyboardInterrupt:
        return EXIT_OK
    except Exception as exc:  # noqa: BLE001 - the exit code IS the report
        _say(f"crashed: {type(exc).__name__}: {exc}")
        return EXIT_CRASHED
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
