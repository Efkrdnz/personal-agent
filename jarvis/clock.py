"""Time for humans.

The database stores UTC (see :func:`jarvis.ids.now`). This module is the only
place a local timezone appears, and it appears at exactly one moment: when
Jarvis says a time out loud.

Europe/Istanbul is UTC+03 with no DST since 2016, which makes it easy to be
casual about — right up until the daemon runs on a cloud box in another zone, or
a briefing is scheduled at 10:00 "local" and fires at 07:00. So the conversion
is explicit and has a test.
"""

from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jarvis.ids import parse_ts

__all__ = ["day_month", "local_tz", "to_local", "spoken_time", "spoken_date"]

DEFAULT_TZ = "Europe/Istanbul"


def local_tz() -> ZoneInfo:
    """The zone Jarvis speaks in. Override with JARVIS_TZ for travel or tests."""
    name = os.environ.get("JARVIS_TZ", DEFAULT_TZ)
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        # Same type, better sentence: on Windows this is a missing tzdata
        # package, not a misspelt zone, and the traceback should say so.
        raise ZoneInfoNotFoundError(
            f"no timezone data for {name!r}. On Windows: uv pip install tzdata"
        ) from exc


def to_local(ts: str | datetime) -> datetime:
    """A stored UTC timestamp -> an aware datetime in the speaking zone."""
    dt = parse_ts(ts) if isinstance(ts, str) else ts
    return dt.astimezone(local_tz())


def spoken_time(ts: str | datetime) -> str:
    """'14:05' — 24-hour, because that is how the time is said in Turkish."""
    return to_local(ts).strftime("%H:%M")


def spoken_date(ts: str | datetime) -> str:
    """'Tuesday 16 September' — no year; if it needs a year, say so explicitly."""
    return day_month(to_local(ts))


def day_month(dt: datetime) -> str:
    """'Tuesday 16 September', built by hand.

    Not ``strftime("%A %-d %B")``: the ``-`` flag is a glibc extension, and
    Windows' C runtime raises ValueError on it, so every spoken date crashed
    there.
    """
    return f"{dt.strftime('%A')} {dt.day} {dt.strftime('%B')}"
