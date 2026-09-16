"""Spend, in three units that do not reconcile — and saying so out loud.

There is no exchange rate between the things this system spends, and inventing
one would be the most comfortable lie available:

``claude_code``
    Runs on a Max subscription OAuth token (ADR 0004). The meaningful unit is
    ``rate_window_pct`` — where you are in the current rate-limit window — and
    ``usd_equiv`` is NULL, because ``ResultMessage.total_cost_usd`` under OAuth
    is an API-equivalent estimate for usage that is not billed per call. Under
    an API key the same runner produces ``usd_est`` that IS money. Which one it
    is is a :class:`LedgerConfig` field, never a literal in this module.

``gemini``
    Seconds and tokens measured locally, converted — if at all — through a price
    table nobody has verified against an invoice. Unpriced by default:
    ``gemini_usd_per_sec`` is ``None`` until someone checks a bill.

``carrier`` / ``livekit``
    THREE meters, not one: LiveKit agent minutes, LiveKit SIP minutes, and the
    carrier's own minutes. They bill on three cycles at three rates and adding
    them up produces a number that is true of nothing.

So the totals are deliberately partial, and the partialness is part of the
answer: :class:`SpendStatus` carries ``priced_usd`` *and* ``unpriced``, a list of
human-readable meter descriptions, and :func:`spoken_status` names every one of
them in the sentence Jarvis says. A zero in ``priced_usd`` means "no meter here
converts to dollars", which is not the same as free, and the spoken line is
written so it can never be read that way.

THE THRESHOLD IS TWO LADDERS, not one. ADR 0004: under a subscription "warning
threshold" means "you are near your five-hour limit", not a currency amount. So
:func:`check_threshold` watches priced dollars against ``threshold_usd`` AND
every rate-limit gauge against ``rate_window_warn_pct``, and fires on a rising
edge of either — ONCE per crossing. The latch is a row in ``cursors``, claimed
inside :func:`jarvis.db.tx`, because the process that crosses a threshold is
frequently not the process that speaks the warning, and a warning that repeats
on every row is a warning nobody listens to.

Nothing here publishes to the bus. ``record()`` has no actor and the bus row
would restate a fact the ``spend`` table already holds durably; the caller that
knows its actor publishes ``spend.recorded`` itself.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from jarvis.clock import local_tz
from jarvis.db import tx
from jarvis.ids import canon, nid, now, parse_ts

__all__ = [
    "CALL_METERS",
    "CARRIER_MIN",
    "DEFAULT_CONFIG",
    "GAUGE_UNITS",
    "KNOWN_PROVIDERS",
    "KNOWN_UNITS",
    "LIVEKIT_AGENT_MIN",
    "LIVEKIT_SIP_MIN",
    "ClaudeAuth",
    "LedgerConfig",
    "Level",
    "MeterTotal",
    "ProviderTotals",
    "Signal",
    "SpendStatus",
    "ThresholdCrossing",
    "Window",
    "check_threshold",
    "record",
    "record_claude_code",
    "spoken_status",
    "status",
    "threshold_signals",
    "window_bounds",
]

# ───────────────────────────── the vocabulary ─────────────────────────────

ClaudeAuth = Literal["subscription", "api_key"]
Level = Literal["ok", "warn", "over"]
Window = str | timedelta

#: From the schema comment. This is DOCUMENTATION, not a gate: :func:`record`
#: accepts any non-empty unit, for the same reason :func:`jarvis.bus.publish`
#: accepts any kind — a ledger that refuses an unplanned meter at 3am loses the
#: measurement entirely. An unknown unit is never silently priced: it has no
#: ``usd_equiv`` rule, so it lands in ``unpriced`` and gets spoken aloud.
KNOWN_UNITS: frozenset[str] = frozenset(
    {"usd_est", "rate_window_pct", "gemini_sec", "call_min_try", "tokens", "agent_min", "sip_min"}
)
KNOWN_PROVIDERS: frozenset[str] = frozenset({"claude_code", "gemini", "carrier", "livekit"})

#: Units that are a POSITION, not a quantity. Summing them is nonsense: two
#: readings of "47% of the window" do not make 94%. These aggregate to the
#: latest reading instead, which is what "where am I right now" means.
GAUGE_UNITS: frozenset[str] = frozenset({"rate_window_pct"})

# The three call meters. The schema's unit comment lists only ``call_min_try``,
# which cannot tell LiveKit's agent minutes from LiveKit's SIP minutes — and
# there is no meter column to add one to. ``unit`` is free TEXT with no CHECK,
# so the meter identity is the (provider, unit) PAIR and two extra unit strings
# carry the distinction. No schema change; see the report.
LIVEKIT_AGENT_MIN: tuple[str, str] = ("livekit", "agent_min")
LIVEKIT_SIP_MIN: tuple[str, str] = ("livekit", "sip_min")
CARRIER_MIN: tuple[str, str] = ("carrier", "call_min_try")
CALL_METERS: tuple[tuple[str, str], ...] = (LIVEKIT_AGENT_MIN, LIVEKIT_SIP_MIN, CARRIER_MIN)

#: The three call meters, said the way the design doc names them. Kept apart
#: from _UNIT_LABELS because "call_min_try" means the carrier's minutes only in
#: the carrier's row; LiveKit's two meters are neither of them.
_CALL_METER_NAMES: dict[tuple[str, str], str] = {
    LIVEKIT_AGENT_MIN: "agent minutes",
    LIVEKIT_SIP_MIN: "SIP minutes",
    CARRIER_MIN: "carrier minutes",
}

_RANK: dict[str, int] = {"ok": 0, "warn": 1, "over": 2}

#: Prefix for the threshold latch row in ``cursors``. That table is a generic
#: (name, value, updated_at) KV — its schema comment lists the cursors known at
#: design time, not an allowed set — and the latch is exactly a cursor: a small
#: durable position that several processes read and one of them advances.
LATCH_PREFIX = "spend_threshold:"

#: How a unit is said out loud. Missing units fall back to the raw unit string,
#: which is ugly on purpose: an unnamed meter should sound wrong.
_UNIT_LABELS: dict[str, str] = {
    "usd_est": "dollars",
    "rate_window_pct": "percent of the rate-limit window",
    "gemini_sec": "seconds",
    "call_min_try": "minutes",
    "agent_min": "agent minutes",
    "sip_min": "SIP minutes",
    "tokens": "tokens",
}


# ───────────────────────────── config ─────────────────────────────


@dataclass(frozen=True, slots=True)
class LedgerConfig:
    """Everything that could reasonably differ between two installs.

    Frozen, and passed in: a module-level mutable default would be a singleton
    shared across processes that do not actually share memory, and the first
    time one of them "changed the config" the others would disagree silently.
    """

    threshold_usd: float = 20.0
    warn_ratio: float = 0.8  # of threshold_usd
    #: ADR 0004. ``subscription`` => claude dollars are an API-equivalent
    #: estimate and are NOT counted as money; ``api_key`` => they are the bill.
    claude_auth: ClaudeAuth = "subscription"
    rate_window_warn_pct: float = 80.0
    rate_window_over_pct: float = 95.0
    #: None => gemini stays unpriced and gets named aloud. Set it only against a
    #: real invoice; the whole point of ``unpriced`` is that an unverified table
    #: is worse than an admitted gap.
    gemini_usd_per_sec: float | None = None

    def __post_init__(self) -> None:
        # config.toml is hand-edited, so a number can arrive as a string or as
        # `true`. Both must be refused the same way every other bad value is: a
        # bare comparison raises TypeError on the string, and `true` is an int in
        # Python, which would silently install a one-dollar ceiling.
        numeric = ("threshold_usd", "warn_ratio", "rate_window_warn_pct", "rate_window_over_pct")
        for field in numeric:
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{field} must be a number, got {value!r}")
        if not (self.threshold_usd > 0 and math.isfinite(self.threshold_usd)):
            raise ValueError(f"threshold_usd must be positive and finite, got {self.threshold_usd}")
        if not 0 < self.warn_ratio <= 1:
            raise ValueError(f"warn_ratio must be in (0, 1], got {self.warn_ratio}")
        if self.claude_auth not in ("subscription", "api_key"):
            raise ValueError(f"unknown claude_auth {self.claude_auth!r}")
        if not 0 < self.rate_window_warn_pct <= self.rate_window_over_pct <= 100:
            raise ValueError("rate window thresholds must satisfy 0 < warn <= over <= 100")
        rate = self.gemini_usd_per_sec
        if rate is not None and (
            isinstance(rate, bool)
            or not isinstance(rate, (int, float))
            or not (math.isfinite(rate) and rate >= 0)
        ):
            raise ValueError("gemini_usd_per_sec must be a finite, non-negative rate or None")

    @classmethod
    def from_mapping(cls, m: dict[str, Any]) -> LedgerConfig:
        """Build from the ``[spend]`` table of config.toml, ignoring strangers.

        Unknown keys are dropped rather than raising: config.toml is hand-edited
        and a stale key should not stop the daemon booting.
        """
        fields = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in m.items() if k in fields})


DEFAULT_CONFIG = LedgerConfig()


# ───────────────────────────── the shapes ─────────────────────────────


@dataclass(frozen=True, slots=True)
class MeterTotal:
    """One (provider, unit) meter over the window. Never merged with another."""

    provider: str
    unit: str
    amount: float  # latest reading for a gauge, sum for everything else
    usd_equiv: float | None  # None when NO row in this meter carried dollars
    rows: int
    unpriced_rows: int
    estimated: bool  # True if ANY row was an estimate

    @property
    def meter(self) -> tuple[str, str]:
        return (self.provider, self.unit)


@dataclass(frozen=True, slots=True)
class ProviderTotals:
    provider: str
    priced_usd: float
    meters: tuple[MeterTotal, ...]


@dataclass(frozen=True, slots=True)
class SpendStatus:
    """The doc's SpendStatus, plus the window it was computed over.

    ``priced_usd`` is deliberately NOT the total spend. It is the part of the
    spend that converts to dollars at all; ``unpriced`` is the rest, in words.
    """

    priced_usd: float
    threshold_usd: float
    pct: float
    by_provider: dict[str, ProviderTotals]
    unpriced: list[str]
    window: str
    since: str | None

    @property
    def priced_estimated(self) -> bool:
        """Is the dollar figure itself an estimate?

        Only meters that actually contributed dollars count: a measured invoice
        line does not become an estimate because an unpriced meter next to it is
        one, and saying so would make the honest word meaningless.
        """
        return any(
            m.estimated and m.usd_equiv is not None
            for p in self.by_provider.values()
            for m in p.meters
        )


@dataclass(frozen=True, slots=True)
class Signal:
    """One threshold ladder's current reading."""

    name: str  # 'priced_usd' | '<provider>:rate_window_pct'
    level: Level
    value: float
    limit: float
    spoken: str


@dataclass(frozen=True, slots=True)
class ThresholdCrossing:
    """Returned exactly once per rising edge, to whichever process got there."""

    level: Level
    signals: tuple[Signal, ...]
    status: SpendStatus
    spoken: str


# ───────────────────────────── writing ─────────────────────────────


def _check_amount(name: str, value: float) -> float:
    v = float(value)
    if not math.isfinite(v):
        raise ValueError(f"{name} must be finite, got {value!r}")
    # A negative row is a sign error far more often than a refund, and netting
    # one out silently under-reports — the one direction this ledger must never
    # fail in. Record the credit as a note and fix the source instead.
    if v < 0:
        raise ValueError(f"{name} must not be negative, got {value!r}")
    return v


def record(
    con: sqlite3.Connection,
    provider: str,
    unit: str,
    amount: float,
    *,
    usd_equiv: float | None = None,
    estimated: bool = True,
    job_id: str | None = None,
    note: str | None = None,
    ts: str | None = None,
) -> str:
    """Append one spend row and return its id. Autocommit: one INSERT is atomic.

    ``usd_equiv=None`` is the honest default. Passing dollars for a gauge unit
    is a category error and raises — a rate-limit window position priced at
    $3.00 would put a fiction directly into ``priced_usd``, which is the exact
    failure this module exists to prevent.

    ``ts`` exists for backfills and for tests that need a row at a known instant;
    it is validated through :func:`jarvis.ids.parse_ts` because window bounds are
    compared lexicographically and an off-format timestamp would sort wrong.
    """
    if not provider or not provider.strip():
        raise ValueError("provider must be a non-empty string")
    if not unit or not unit.strip():
        raise ValueError("unit must be a non-empty string")
    amt = _check_amount("amount", amount)
    if unit in GAUGE_UNITS and not 0 <= amt <= 100:
        raise ValueError(f"{unit} is a percentage of a window, got {amount!r}")
    if usd_equiv is not None:
        if unit in GAUGE_UNITS:
            raise ValueError(
                f"{unit} is a position in a rate-limit window, not money; "
                "usd_equiv must be NULL (ADR 0004)"
            )
        usd_equiv = _check_amount("usd_equiv", usd_equiv)
    stamp = now() if ts is None else ts
    if ts is not None:
        parse_ts(ts)

    sid = nid("spd")
    con.execute(
        """INSERT INTO spend (id, ts, job_id, provider, unit, amount, usd_equiv, estimated, note)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (sid, stamp, job_id, provider, unit, amt, usd_equiv, int(bool(estimated)), note),
    )
    return sid


def record_claude_code(
    con: sqlite3.Connection,
    *,
    config: LedgerConfig = DEFAULT_CONFIG,
    rate_window_pct: float | None = None,
    usd_est: float | None = None,
    tokens: float | None = None,
    job_id: str | None = None,
    note: str | None = None,
    ts: str | None = None,
) -> list[str]:
    """Record a Claude Code turn under whichever auth mode config says.

    This function is the ONLY place the auth decision lives. Under a
    subscription the reported ``total_cost_usd`` is still stored — it is a real
    measurement of API-equivalent usage and will be wanted the day someone
    compares the subscription to metered billing — but with ``usd_equiv`` NULL,
    so it is named aloud as unpriced instead of quietly becoming a bill.
    """
    if rate_window_pct is None and usd_est is None and tokens is None:
        raise ValueError("record_claude_code needs at least one of rate_window_pct/usd_est/tokens")
    ids: list[str] = []
    if rate_window_pct is not None:
        ids.append(
            record(
                con,
                "claude_code",
                "rate_window_pct",
                rate_window_pct,
                job_id=job_id,
                note=note,
                ts=ts,
            )
        )
    if usd_est is not None:
        priced = config.claude_auth == "api_key"
        ids.append(
            record(
                con,
                "claude_code",
                "usd_est",
                usd_est,
                usd_equiv=usd_est if priced else None,
                job_id=job_id,
                note=note,
                ts=ts,
            )
        )
    if tokens is not None:
        # Tokens are never the dollars: under an API key the usd_est row above
        # is the money, and double-pricing them would count the turn twice.
        ids.append(record(con, "claude_code", "tokens", tokens, job_id=job_id, note=note, ts=ts))
    return ids


# ───────────────────────────── windows ─────────────────────────────


def _stamp(dt: datetime) -> str:
    """A datetime in :func:`jarvis.ids.now`'s exact wire format.

    Mirrored deliberately: window bounds are compared to ``ts`` with ``>=`` in
    SQL, and a lexicographic comparison is only honest if both sides are the
    same fixed width. ``ids.now()`` formats *the current instant* and has no
    arbitrary-datetime form; the tests assert ``_stamp(parse_ts(now())) == now()``
    so the two cannot drift apart.
    """
    u = dt.astimezone(UTC)
    return f"{u.strftime('%Y-%m-%dT%H:%M:%S')}.{u.microsecond // 1000:03d}Z"


def window_bounds(window: Window, *, ref: str | None = None) -> tuple[str | None, str]:
    """``(since_ts_or_None, label)`` for ``'5h'``, ``'30d'``, ``'today'``, ``'month'``,
    ``'all'`` or a :class:`datetime.timedelta`.

    An unparseable window RAISES rather than defaulting to all-time: a typo that
    silently widened the window would under-report the percentage against the
    threshold, and under-reporting is the direction that hurts.
    """
    if isinstance(window, timedelta):
        if window <= timedelta(0):
            raise ValueError(f"window must be positive, got {window!r}")
        base = parse_ts(ref) if ref else parse_ts(now())
        return _stamp(base - window), _humanised(window)

    label = window.strip().lower()
    if label == "all":
        return None, "all time"
    base = parse_ts(ref) if ref else parse_ts(now())
    if label in ("today", "month"):
        # The only local-time decision in this module. "Today" means the day the
        # human is living in (jarvis.clock's zone), not the day UTC is having.
        loc = base.astimezone(local_tz())
        start = loc.replace(hour=0, minute=0, second=0, microsecond=0)
        if label == "month":
            start = start.replace(day=1)
        return _stamp(start), ("today" if label == "today" else "this month")
    if len(label) > 1 and label[-1] in "smhd":
        try:
            n = float(label[:-1])
        except ValueError as exc:
            raise ValueError(f"unparseable window {window!r}") from exc
        if n <= 0:
            raise ValueError(f"window must be positive, got {window!r}")
        secs = n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[label[-1]]
        delta = timedelta(seconds=secs)
        return _stamp(base - delta), _humanised(delta)
    raise ValueError(f"unparseable window {window!r} (try '5h', '30d', 'today', 'month', 'all')")


def _humanised(delta: timedelta) -> str:
    secs = int(delta.total_seconds())
    steps = ((86400, "day", "days"), (3600, "hour", "hours"), (60, "minute", "minutes"))
    for unit_secs, one, many in steps:
        if secs >= unit_secs and secs % unit_secs == 0:
            n = secs // unit_secs
            return f"the last {n} {one if n == 1 else many}"
    return f"the last {secs} seconds"


# ───────────────────────────── reading ─────────────────────────────

_TOTALS_SQL = """
SELECT s.provider AS provider,
       s.unit     AS unit,
       COUNT(*)   AS n_rows,
       SUM(s.amount) AS total,
       SUM(COALESCE(s.usd_equiv, 0.0)) AS usd,
       SUM(CASE WHEN s.usd_equiv IS NULL THEN 1 ELSE 0 END) AS n_unpriced,
       MAX(s.estimated) AS any_estimated,
       (SELECT s2.amount FROM spend s2
         WHERE s2.provider = s.provider AND s2.unit = s.unit AND s2.ts >= :since
         ORDER BY s2.ts DESC, s2.rowid DESC LIMIT 1) AS latest
  FROM spend s
 WHERE s.ts >= :since
 GROUP BY s.provider, s.unit
 ORDER BY s.provider, s.unit
"""


def status(
    con: sqlite3.Connection,
    window: Window,
    *,
    config: LedgerConfig = DEFAULT_CONFIG,
) -> SpendStatus:
    """The partial dollar total, per-meter detail, and the meters it leaves out."""
    since, label = window_bounds(window)
    rows = con.execute(_TOTALS_SQL, {"since": since or ""}).fetchall()

    by_provider: dict[str, ProviderTotals] = {}
    unpriced: list[str] = []
    priced_usd = 0.0
    grouped: dict[str, list[MeterTotal]] = {}

    for r in rows:
        unit = str(r["unit"])
        gauge = unit in GAUGE_UNITS
        amount = float(r["latest"]) if gauge else float(r["total"])
        n_unpriced = int(r["n_unpriced"])
        n_rows = int(r["n_rows"])
        # A meter is priced only insofar as its rows carried dollars; a meter
        # with SOME priced rows contributes what it has AND still gets named,
        # because a partial conversion is a gap too.
        usd = float(r["usd"]) if n_unpriced < n_rows else None
        meter = MeterTotal(
            provider=str(r["provider"]),
            unit=unit,
            amount=amount,
            usd_equiv=usd,
            rows=n_rows,
            unpriced_rows=n_unpriced,
            estimated=bool(r["any_estimated"]),
        )
        grouped.setdefault(meter.provider, []).append(meter)
        if usd is not None:
            priced_usd += usd
        if n_unpriced:
            unpriced.append(f"{meter.provider} ({_unpriced_reason(meter, config)})")

    for provider, meters in grouped.items():
        by_provider[provider] = ProviderTotals(
            provider=provider,
            priced_usd=round(sum(m.usd_equiv or 0.0 for m in meters), 6),
            meters=tuple(meters),
        )

    priced_usd = round(priced_usd, 6)
    return SpendStatus(
        priced_usd=priced_usd,
        threshold_usd=config.threshold_usd,
        pct=round(100.0 * priced_usd / config.threshold_usd, 3),
        by_provider=by_provider,
        unpriced=unpriced,
        window=label,
        since=since,
    )


def _unpriced_reason(meter: MeterTotal, config: LedgerConfig) -> str:
    """Why this meter is not in ``priced_usd``, in words a person can hear.

    The claude_code/rate_window_pct wording under a subscription is VERBATIM from
    the architecture doc's SpendStatus comment. It is quoted in the tests; it is
    the canonical example of the whole idea and should not be improved.
    """
    provider, unit = meter.provider, meter.unit
    if unit == "rate_window_pct":
        if provider == "claude_code" and config.claude_auth == "subscription":
            return "Max subscription: rate-limit windows, not dollars"
        return f"rate-limit windows, not dollars; at {meter.amount:.0f} percent"
    if provider == "claude_code" and unit == "usd_est":
        return "Max subscription: an API-equivalent estimate, not a bill"
    if provider == "gemini":
        return (
            f"{meter.amount:.0f} {_UNIT_LABELS.get(unit, unit)} estimated locally "
            "against an unverified price table"
        )
    if (provider, unit) in _CALL_METER_NAMES:
        who = "LiveKit" if provider == "livekit" else "the carrier"
        name = _CALL_METER_NAMES[(provider, unit)]
        return f"{meter.amount:.1f} {name}, billed by {who} on its own cycle"
    if unit == "tokens":
        return f"{meter.amount:.0f} tokens, not dollars"
    return f"{meter.amount:g} {_UNIT_LABELS.get(unit, unit)}, not converted to dollars"


# ───────────────────────────── speaking ─────────────────────────────


def _spoken_money(usd: float) -> str:
    cents = int(round(usd * 100))
    whole, rest = divmod(abs(cents), 100)
    # Under a dollar is said in cents. "0 dollars 87" is heard as a glitch, and a
    # line whose whole job is to be believed cannot afford to sound broken.
    if whole == 0 and rest:
        return "1 cent" if rest == 1 else f"{rest} cents"
    head = "1 dollar" if whole == 1 else f"{whole} dollars"
    return head if rest == 0 else f"{head} {rest:02d}"


def _spoken_limit(usd: float) -> str:
    """The same money, used as an adjective: 'a 20 dollar ceiling'."""
    said = _spoken_money(usd)
    return said[:-1] if said.endswith(("dollars", "cents")) else said


def _and_join(parts: list[str]) -> str:
    if len(parts) == 1:
        return parts[0]
    return "; ".join(parts[:-1]) + "; and " + parts[-1]


def _spoken_unpriced(status: SpendStatus) -> list[str]:
    """The ``unpriced`` strings, said rather than printed: ``claude_code (x)`` ->
    ``claude code, x``. Derived from the same list so the two cannot diverge."""
    said: list[str] = []
    for entry in status.unpriced:
        name, _, reason = entry.partition(" (")
        said.append(f"{name.replace('_', ' ')}, {reason.rstrip(')')}")
    return said


def spoken_status(status: SpendStatus) -> str:
    """One sentence for the desk, which NAMES EVERY UNPRICED METER.

    The naming is not decoration. ``priced_usd`` under a Max subscription is
    frequently 0.00 while the machine has been working for hours, and a line
    that stopped at the dollars would be heard as "today was free". So the
    unpriced clause is unconditional when there is anything unpriced, and the
    zero case says out loud that zero is not free.
    """
    if not status.by_provider:
        return f"Nothing recorded {_where(status)}."

    ceiling = _spoken_limit(status.threshold_usd)
    if status.priced_usd <= 0:
        head = f"Nothing priced {_where(status)}, against a {ceiling} ceiling."
    else:
        money = _spoken_money(status.priced_usd)
        # Every figure in this ledger is an estimate unless someone read an
        # invoice, and the sentence says which it is rather than implying a bill.
        money = f"an estimated {money}" if status.priced_estimated else money
        head = (
            f"Priced spend {_where(status)} is {money}, "
            f"{status.pct:.0f} percent of a {ceiling} ceiling."
        )

    if not status.unpriced:
        return f"{head} Every meter this window converts to dollars."

    tail = f"That number does not include {_and_join(_spoken_unpriced(status))}."
    if status.priced_usd <= 0:
        tail += " Zero priced is not the same as free."
    return f"{head} {tail}"


def _where(status: SpendStatus) -> str:
    label = status.window
    if label.startswith("the last"):
        return f"in {label}"
    return label if label.startswith(("today", "this")) else f"over {label}"


# ───────────────────────────── thresholds ─────────────────────────────


def threshold_signals(
    status: SpendStatus, config: LedgerConfig = DEFAULT_CONFIG
) -> tuple[Signal, ...]:
    """Every ladder's current level. Pure; no I/O, no latch.

    Two ladders, because ADR 0004 says a "warning threshold" means two different
    things depending on how Claude is authenticated, and both can be true at
    once: dollars for what is billed, rate-window percentage for the thing that
    actually stops work under a Max subscription.
    """
    warn_at = config.threshold_usd * config.warn_ratio
    if status.priced_usd >= config.threshold_usd:
        usd_level: Level = "over"
        usd_spoken = (
            f"Priced spend is {_spoken_money(status.priced_usd)}, "
            f"over the {_spoken_limit(config.threshold_usd)} ceiling."
        )
    elif status.priced_usd >= warn_at:
        usd_level = "warn"
        usd_spoken = (
            f"Priced spend is {_spoken_money(status.priced_usd)}, "
            f"past the {_spoken_limit(warn_at)} warning line."
        )
    else:
        usd_level = "ok"
        usd_spoken = f"Priced spend is {_spoken_money(status.priced_usd)}."
    signals = [Signal("priced_usd", usd_level, status.priced_usd, config.threshold_usd, usd_spoken)]

    for provider, totals in status.by_provider.items():
        for meter in totals.meters:
            if meter.unit != "rate_window_pct":
                continue
            pct = meter.amount
            spoken_provider = provider.replace("_", " ")
            if pct >= config.rate_window_over_pct:
                level: Level = "over"
            elif pct >= config.rate_window_warn_pct:
                level = "warn"
            else:
                level = "ok"
            said = f"{spoken_provider} is at {pct:.0f} percent of its rate-limit window."
            signals.append(
                Signal(f"{provider}:rate_window_pct", level, pct, config.rate_window_over_pct, said)
            )
    return tuple(signals)


@contextmanager
def _atomic(con: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE unless the caller already owns a transaction.

    The latch claim has to compose into a caller's atom — a tool that records
    the turn's spend and checks the threshold in one transaction must not have
    the check rolled back independently of the row that caused it. Nesting BEGIN
    is an error in SQLite, so join rather than open; either way the claim is
    serialised by one write lock.
    """
    if con.in_transaction:
        yield con
    else:
        with tx(con) as t:
            yield t


def check_threshold(
    con: sqlite3.Connection,
    window: Window,
    *,
    config: LedgerConfig = DEFAULT_CONFIG,
    latch: str | None = None,
) -> ThresholdCrossing | None:
    """Fire ONCE per rising edge, in whichever process gets there first.

    The latch is a ``cursors`` row holding ``{signal_name: level}``. Read and
    written inside one ``BEGIN IMMEDIATE``, so two daemons calling this in the
    same second produce exactly one crossing: the loser reads the level the
    winner just wrote and sees no rise. Falling back below a line rewrites the
    latch silently and RE-ARMS it, which is what "once per crossing" means — the
    second crossing of the same line is a second warning, not a repeat of the
    first.
    """
    ts = now()
    with _atomic(con):
        # The snapshot is taken INSIDE the write lock, not before it. Reading
        # `spend` first and only then queuing for the lock is the bug this
        # ordering exists to prevent: the loser of a race would wait out the
        # winner and then write a latch computed from a view of the table that
        # the winner has already moved past — downgrading 'over' back to 'warn'
        # and re-announcing the same crossing on the next call. BEGIN IMMEDIATE
        # holds the write lock, so everything read under it is the latest
        # committed state and the latch matches the totals it was derived from.
        st = status(con, window, config=config)
        signals = threshold_signals(st, config)
        name = latch or f"{LATCH_PREFIX}{st.window}"
        row = con.execute("SELECT value FROM cursors WHERE name=?", (name,)).fetchone()
        previous = _decode_latch(row["value"] if row is not None else None)
        risen = tuple(s for s in signals if _RANK[s.level] > _RANK[previous.get(s.name, "ok")])
        # 'ok' is the absence of a key, so a signal whose meter stopped
        # reporting re-arms rather than staying latched at 'warn' forever.
        current = {s.name: s.level for s in signals if s.level != "ok"}
        if current != previous:
            con.execute(
                """INSERT INTO cursors (name, value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET value=excluded.value,
                                                   updated_at=excluded.updated_at""",
                (name, canon(current), ts),
            )
    if not risen:
        return None

    level: Level = "over" if any(s.level == "over" for s in risen) else "warn"
    spoken = " ".join(s.spoken for s in risen)
    if st.unpriced:
        spoken += f" And not in that number: {_and_join(_spoken_unpriced(st))}."
    return ThresholdCrossing(level=level, signals=risen, status=st, spoken=spoken)


def _decode_latch(value: str | None) -> dict[str, Level]:
    """A corrupt latch re-arms rather than raising: worst case one extra warning,
    against a ledger that refuses to answer at all."""
    if not value:
        return {}
    try:
        loaded = json.loads(value)
    except (ValueError, TypeError):
        return {}
    if not isinstance(loaded, dict):
        return {}
    return {str(k): v for k, v in loaded.items() if v in _RANK}
