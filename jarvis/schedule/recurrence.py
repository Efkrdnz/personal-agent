"""When does 10:00 happen next? Pure functions over strings, no connection.

The database stores UTC; a human says "ten in the morning". Everything in this
module is the conversion between those two, and it is a separate file from the
table so that the awkward half — a daemon in another zone, a day that is 23 or
25 hours long, a machine that was off when the moment passed — can be tested
without a database at all.

TWO DIRECTIONS, AND THEY ARE NOT THE SAME QUESTION. :func:`next_after` arms the
schedule; :func:`last_at_or_before` answers "the fire I am running now, which
morning is it FOR?". A machine that comes back from three days off must not
deliver three briefings, and it must not deliver the one from three days ago
either: the occurrence it is late for is today's.

NO SECOND TIMESTAMP FORMATTER. Everything here returns a timestamp by shifting
an existing one through :func:`jarvis.jobs.shift_ts`, which is the only place in
the tree that writes the format :func:`jarvis.ids.now` defines. A local
``strftime`` would be a second copy of that decision, and the two would drift on
the day somebody widened one of them to microseconds.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jarvis.ids import parse_ts
from jarvis.jobs import shift_ts

__all__ = [
    "MAX_OCCURRENCE_SCAN",
    "at_local_parts",
    "check_at_local",
    "instant_of",
    "last_at_or_before",
    "missed_occurrences",
    "next_after",
    "zone",
]

#: A hard stop on every occurrence walk. A pointer left years in the past is a
#: bug somewhere else; a loop that hangs the daemon while it counts the days is a
#: bug here, and this is the cheaper of the two to prevent.
MAX_OCCURRENCE_SCAN = 4000


def zone(tz: str) -> ZoneInfo:
    """The IANA zone, or a ValueError naming the string that was not one."""
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown timezone {tz!r}") from exc


def at_local_parts(at_local: str) -> tuple[int, int]:
    """``'10:00'`` -> ``(10, 0)``. Strict: this string is a promise to a human."""
    parts = at_local.split(":")
    if len(parts) != 2 or not all(len(p) == 2 and p.isdigit() for p in parts):
        raise ValueError(f"at_local must be 'HH:MM' wall clock, got {at_local!r}")
    hh, mm = int(parts[0]), int(parts[1])
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError(f"at_local must be a real time of day, got {at_local!r}")
    return hh, mm


def check_at_local(at_local: str, tz: str) -> None:
    """Raise unless this pair could ever fire. Called at the door, not at 10:00."""
    at_local_parts(at_local)
    zone(tz)


def instant_of(day: date, at_local: str, tz: str, *, ref_ts: str) -> str:
    """The UTC instant at which ``at_local`` happens on this LOCAL day.

    A wall-clock time that does not exist (spring forward) or happens twice
    (autumn back) is resolved by zoneinfo's ``fold=0`` rather than raising.
    Istanbul has had no DST since 2016, but a daemon told to fire in a zone that
    does must still fire, and taking the FIRST of two identical local times is
    the same "repeating beats dropping" choice the briefing cursor makes.
    """
    hh, mm = at_local_parts(at_local)
    local = datetime(day.year, day.month, day.day, hh, mm, tzinfo=zone(tz))
    return _ts_at(ref_ts, local.astimezone(UTC))


def next_after(ts: str, at_local: str, tz: str) -> str:
    """The first occurrence STRICTLY after ``ts``. This is what arms a schedule."""
    base = parse_ts(ts)
    day = base.astimezone(zone(tz)).date() - timedelta(days=1)
    # Yesterday first, not today: with a large UTC offset the local day either
    # side of the instant can hold the nearer occurrence, and a DST shift can put
    # them out of clock order. Four days is two more than any zone needs.
    for _ in range(4):
        cand = instant_of(day, at_local, tz, ref_ts=ts)
        if parse_ts(cand) > base:
            return cand
        day += timedelta(days=1)
    raise ValueError(f"no occurrence of {at_local} in {tz} after {ts}")


def last_at_or_before(ts: str, at_local: str, tz: str) -> str:
    """The most recent occurrence at or before ``ts``. This is what a fire is FOR.

    The reboot case is the whole reason it exists: the machine went down at 09:50
    with the pointer on today's 10:00 and came back at 10:05. The fire that runs
    now is for today's 10:00, five minutes late — and if it had been down for
    three days it would STILL be for today's 10:00, which is the difference
    between one late briefing and four.
    """
    base = parse_ts(ts)
    day = base.astimezone(zone(tz)).date() + timedelta(days=1)
    for _ in range(4):
        cand = instant_of(day, at_local, tz, ref_ts=ts)
        if parse_ts(cand) <= base:
            return cand
        day -= timedelta(days=1)
    raise ValueError(f"no occurrence of {at_local} in {tz} at or before {ts}")


def missed_occurrences(from_ts: str, to_ts: str, at_local: str, tz: str) -> int:
    """How many occurrences fall in ``[from_ts, to_ts)``. Zero on the normal path.

    Called with the schedule's own pointer and the occurrence actually being
    delivered, so it answers "how many mornings did this machine sleep through",
    which is a number the event log should carry rather than a silence.
    """
    if from_ts >= to_ts:
        return 0
    count = 0
    cursor = from_ts
    while cursor < to_ts and count < MAX_OCCURRENCE_SCAN:
        count += 1
        cursor = next_after(cursor, at_local, tz)
    return count


def _ts_at(ref_ts: str, target: datetime) -> str:
    """``target``, written in the shape :func:`jarvis.ids.now` defines.

    Expressed as a shift from a timestamp that already has that shape — see the
    module docstring. The shift is rounded to milliseconds first because
    ``total_seconds()`` is a float: an error of one microsecond the wrong way
    would truncate ``...000Z`` to ``...999Z``, i.e. move an occurrence back a
    millisecond, once in a while and never reproducibly.
    """
    delta = round((target - parse_ts(ref_ts)).total_seconds(), 3)
    return shift_ts(ref_ts, delta)
