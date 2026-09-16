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
from zoneinfo import ZoneInfo

from jarvis.ids import parse_ts

__all__ = ["local_tz", "to_local", "spoken_time", "spoken_date"]

DEFAULT_TZ = "Europe/Istanbul"


def local_tz() -> ZoneInfo:
    """The zone Jarvis speaks in. Override with JARVIS_TZ for travel or tests."""
    return ZoneInfo(os.environ.get("JARVIS_TZ", DEFAULT_TZ))


def to_local(ts: str | datetime) -> datetime:
    """A stored UTC timestamp -> an aware datetime in the speaking zone."""
    dt = parse_ts(ts) if isinstance(ts, str) else ts
    return dt.astimezone(local_tz())


def spoken_time(ts: str | datetime) -> str:
    """'14:05' — 24-hour, because that is how the time is said in Turkish."""
    return to_local(ts).strftime("%H:%M")


def spoken_date(ts: str | datetime) -> str:
    """'Tuesday 16 September' — no year; if it needs a year, say so explicitly."""
    return to_local(ts).strftime("%A %-d %B")
