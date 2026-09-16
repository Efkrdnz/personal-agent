"""Presence: can I be HEARD, and separately, what can REACH you at all.

TWO AXES, and separating them is most of the design. PRESENCE answers "if I
speak into this room, will a human hear it?". REACHABILITY answers "which
channels can reach the user right now?". They are not the same question and
conflating them produces both of this system's characteristic failures: calling
a man who is sitting right there, and speaking into an empty room while Claude
Code waits forty minutes.

THE ASYMMETRY IS THE WHOLE POINT. Becoming ``present`` takes ONE piece of
evidence and happens instantly — a wake word, a transcribed utterance, a
keypress. Becoming ``away`` takes sustained absence (:data:`IDLE_AWAY_S`, five
times the threshold for ``maybe``) or one decisive negative. That is deliberate
and it is not symmetric prudence: a false ``away`` costs one redundant Telegram
message, a false ``present`` costs forty minutes of a stalled build. Everything
in :func:`decide` that looks over-eager to leave ``present`` is that trade being
paid on purpose.

THE CHEAPEST SENSOR IS FREE. A spoken question nobody answers within
:data:`UNANSWERED_PROBE_S` is itself the evidence that the chair is empty — and
it is precisely the evidence the OS cannot give you, because the screen is not
idle, the room is. :func:`probe_outcome` and :func:`note_probe` are the hook the
requests layer calls; they need no new hardware and no new permission.

PURITY, because this subsystem is otherwise untestable. :func:`decide` is a pure
function of plain data: a mapping of live signals plus an override, in, a
verdict out. :func:`evaluate_presence` only loads the rows and calls it, and
WRITES NOTHING. Persisting the verdict is :func:`update_presence`, a separate
call, because "what do I believe right now" and "record that belief and tell
everyone" fail in different ways and one of them does I/O.

THE PLATFORM TRAP, named because nothing else would catch it: XScreenSaver
returns a CONSTANT ZERO FOREVER under Wayland. Presence would pin to ``present``,
escalation would never fire, and every away-from-desk feature would die quietly
while appearing to work. Hence the probe order in :func:`idle_seconds` (Wayland
first, X11 last) and :func:`idle_probe_self_test`, which asserts idle time
actually RISES across a pause and is the only thing that can tell a working
probe from a lying one. When every probe fails, ``idle_seconds()`` returns None,
presence is ``unknown``, and — the important part — ``unknown`` routes like
``maybe`` but KEEPS ``phone`` reachable. Being blind must degrade toward one
redundant message, never toward silence.
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import sqlite3
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from jarvis.bus import publish
from jarvis.clock import to_local
from jarvis.db import tx
from jarvis.ids import canon, now, parse_ts

# shift_ts lives in jobs because that is where the first "how long has this been
# like that" question was asked; it is a timestamp utility, not a job concept,
# and re-deriving the format here would be the house rule's exact prohibition.
from jarvis.jobs import shift_ts

__all__ = [
    "DEFAULT_TTL_S",
    "IDLE_PROBES",
    "IDLE_ASLEEP_S",
    "IDLE_AWAY_S",
    "IDLE_MAYBE_S",
    "OVERRIDE_MODES",
    "OVERRIDE_TTL_S",
    "POSITIVE_SOURCES",
    "QUIET_END_MIN",
    "QUIET_START_MIN",
    "REACHABLE_BY_STATE",
    "SOURCES",
    "UNANSWERED_PROBE_S",
    "Channel",
    "Override",
    "OverrideMode",
    "Presence",
    "PresenceState",
    "Signal",
    "UnknownSignal",
    "active_signals",
    "clear_signal",
    "decide",
    "evaluate_presence",
    "idle_probe_self_test",
    "idle_seconds",
    "in_quiet_hours",
    "note_heard",
    "note_probe",
    "poll_idle",
    "probe_outcome",
    "purge_expired",
    "read_override",
    "read_presence",
    "record_signal",
    "screen_locked",
    "set_override",
    "set_signal",
    "should_defer",
    "spoken_ago",
    "spoken_duration",
    "update_presence",
]

# ───────────────────────────── the vocabulary ─────────────────────────────

PresenceState = Literal["present", "maybe", "away", "asleep", "unknown"]
Channel = Literal["desk", "telegram", "phone"]
OverrideMode = Literal["present", "away", "dnd", "desk_only"]

#: Every signal the schema's ``presence_signals.source`` comment enumerates.
#: ``override`` is here because the schema lists it, but it is NOT stored the way
#: the others are — see :func:`record_signal`.
SOURCES: tuple[str, ...] = (
    "idle",
    "lock",
    "wakeword",
    "utterance",
    "probe",
    "override",
    "telegram",
    "call",
    "geofence",
)

#: Sources that are DECISIVE POSITIVES: one of these, unexpired, means a human
#: was in the room making noise, whatever the OS idle counter thinks. A person
#: talking to Jarvis touches no keyboard, so idle time and speech disagree by
#: design and speech wins.
POSITIVE_SOURCES: frozenset[str] = frozenset(("wakeword", "utterance"))

#: TTLs from the design doc's signal table. A signal older than its TTL IS NOT
#: EVIDENCE — not weak evidence, none. That is what stops a dead poller from
#: pinning presence to whatever it last saw, which is the same failure as the
#: Wayland trap arriving by a different road.
DEFAULT_TTL_S: dict[str, int] = {
    "idle": 15,
    "lock": 15,
    "wakeword": 300,
    "utterance": 300,
    "probe": 600,
    "override": 14_400,
    "telegram": 600,
    # "live" in the doc's table. 60s is a heartbeat window: jarvis-phone rewrites
    # it while the call is up, so a crashed phone worker stops claiming a call is
    # in progress within a minute instead of forever.
    "call": 60,
    # Not in the doc's table at all (geofence is listed as a source with no TTL).
    # 15 minutes: long enough that a phone in a pocket with no signal does not
    # flap, short enough that a stale fix cannot keep the desk marked unreachable
    # for an evening.
    "geofence": 900,
}

IDLE_MAYBE_S = 120.0
IDLE_AWAY_S = 600.0
IDLE_ASLEEP_S = 1800.0

#: A spoken question with no audio answer inside this window is a decisive
#: negative. Thirty seconds is from the design doc and is not arbitrary: it is
#: long enough for a human to finish a sentence and short enough that the
#: Telegram rung still fires inside ``escalate_after_s`` (90).
UNANSWERED_PROBE_S = 30.0

#: Quiet hours, 23:30-08:30 in the speaking zone. Minutes-since-midnight so the
#: wrap-around past midnight is one comparison instead of three.
QUIET_START_MIN = 23 * 60 + 30
QUIET_END_MIN = 8 * 60 + 30

OVERRIDE_MODES: tuple[str, ...] = ("present", "away", "dnd", "desk_only")

#: How long each spoken override lasts if the speaker did not say. "I'm going
#: out" is four hours because that is a plausible errand; "don't call me" is
#: eight because it is usually a working day. Both EXPIRE: an override that
#: outlives its reason is how a system goes quiet for a week.
OVERRIDE_TTL_S: dict[str, int] = {
    "present": 14_400,
    "away": 14_400,
    "dnd": 28_800,
    "desk_only": 28_800,
}

#: Which channels can reach a human in each state, straight from the doc.
#:
#: ``unknown`` keeps ``phone``, and this is the single most important line in the
#: table. ``unknown`` means every idle probe failed, which is exactly when the
#: system is least entitled to remove a way of reaching somebody. It routes like
#: ``maybe`` — the router prefers the desk — but it never suppresses the ladder.
REACHABLE_BY_STATE: dict[str, tuple[Channel, ...]] = {
    "present": ("desk", "telegram"),
    "maybe": ("desk", "telegram"),
    "away": ("telegram", "phone"),
    "asleep": ("telegram",),
    "unknown": ("desk", "telegram", "phone"),
}

#: Canonical order, so two verdicts that reach the same channels compare equal
#: and the event log does not fill with reorderings.
_CHANNEL_ORDER: tuple[Channel, ...] = ("desk", "telegram", "phone")


class UnknownSignal(ValueError):
    """A source the schema does not enumerate. Never silently accepted."""


# ───────────────────────────── plain data ─────────────────────────────


@dataclass(frozen=True, slots=True)
class Signal:
    """One row of ``presence_signals``, with its expiry attached."""

    source: str
    value: dict[str, Any]
    ts: str
    ttl_s: int

    def age_s(self, now_ts: str) -> float:
        return (parse_ts(now_ts) - parse_ts(self.ts)).total_seconds()

    def alive(self, now_ts: str) -> bool:
        """Expiry is EXCLUSIVE: at exactly ttl_s the signal is already gone.

        A 15-second idle poll that misses one tick must stop being evidence at
        the boundary rather than linger for a tick longer; the cost of being
        briefly ``unknown`` is one extra Telegram message, and the cost of
        lingering is the Wayland failure with a different cause.
        """
        return self.age_s(now_ts) < self.ttl_s


@dataclass(frozen=True, slots=True)
class Override:
    """The manual escape hatch. Beats every sensor until it expires."""

    mode: OverrideMode | None
    until: str | None
    set_by: str | None
    set_at: str | None

    def alive(self, now_ts: str) -> bool:
        if self.mode is None:
            return False
        return self.until is None or self.until > now_ts

    def remaining_s(self, now_ts: str) -> float | None:
        if self.until is None:
            return None
        return (parse_ts(self.until) - parse_ts(now_ts)).total_seconds()


@dataclass(frozen=True, slots=True)
class Presence:
    """The verdict. ``reason`` is read back verbatim on "where am I?".

    ``reachable`` is a tuple where the design sketch says list, on purpose: this
    object is handed to the router and to tools, and a frozen dataclass holding a
    mutable list is a verdict any caller can quietly edit.
    """

    state: PresenceState
    since: str
    confidence: float
    reachable: tuple[Channel, ...]
    reason: str

    def can_hear_me(self) -> bool:
        return "desk" in self.reachable and self.state in ("present", "maybe", "unknown")


# ───────────────────────────── spoken helpers ─────────────────────────────


def spoken_duration(seconds: float) -> str:
    """ "40 seconds", "12 minutes", "2 hours" — never "0:12:03"."""
    s = int(max(0.0, seconds))
    if s < 90:
        return f"{s} second{'' if s == 1 else 's'}"
    m = round(s / 60)
    if m < 90:
        return f"{m} minute{'' if m == 1 else 's'}"
    h = round(s / 3600)
    return f"{h} hour{'' if h == 1 else 's'}"


def spoken_ago(seconds: float) -> str:
    """ "just now" under two seconds, "12 minutes ago" over it.

    "0 seconds ago" is what a duration formatter says and it is not what a person
    says; this line exists because the sentence is READ ALOUD.
    """
    return "just now" if seconds < 2 else f"{spoken_duration(seconds)} ago"


def in_quiet_hours(ts: str) -> bool:
    """23:30-08:30 in the speaking zone (see :mod:`jarvis.clock`)."""
    local = to_local(ts)
    minute = local.hour * 60 + local.minute
    return minute >= QUIET_START_MIN or minute < QUIET_END_MIN


def should_defer(state: PresenceState) -> bool:
    """Defer a blocking question only when nobody could possibly answer it.

    Everything else BLOCKS, because the runner is its own process and blocking
    costs nothing there. ``unknown`` deliberately does not defer: deferring on
    ignorance would turn a broken idle probe into a system that never asks.
    """
    return state in ("away", "asleep")


# ───────────────────────────── the pure decision ─────────────────────────────


def _idle_of(signals: Mapping[str, Signal], now_ts: str) -> float | None:
    sig = signals.get("idle")
    if sig is None:
        return None
    raw = sig.value.get("idle_s")
    if raw is None:
        return None
    # The reading is as old as the row: the poller wrote "300 seconds idle" up to
    # a TTL ago and nothing has touched the keyboard since, or there would be a
    # newer row. Carrying the row's own age forward is both more accurate and the
    # safe direction — it can only move the verdict toward away, which costs one
    # Telegram message, never toward a false present, which costs forty minutes.
    return float(raw) + max(0.0, sig.age_s(now_ts))


def _onset(s: Signal) -> str:
    """When this signal's CONDITION began, not when the row was last refreshed.

    The lock poller rewrites ``lock`` every five seconds while the screen stays
    locked, so the row's ``ts`` says "locked one second ago" forever. Comparing
    speech against THAT would mean a wake word could never beat a locked screen
    and the doc's instant path back to ``present`` would be unreachable for
    anyone who talks to Jarvis at a locked desk. The condition's onset is the
    honest thing to order against; refreshing a row is not new evidence.
    """
    since = s.value.get("since")
    return since if isinstance(since, str) else s.ts


def _newest(signals: list[Signal]) -> Signal | None:
    return max(signals, key=_onset) if signals else None


def _asleep_if_night(state: str, idle_s: float | None, now_ts: str) -> bool:
    """``away`` deepens to ``asleep`` only with all three legs of the doc's rule.

    Requiring a real idle reading of >= 30 minutes is what stops a decisive
    negative at 23:31 (one unanswered question) from removing ``phone`` from the
    ladder for the rest of the night.
    """
    if state != "away" or idle_s is None or idle_s < IDLE_ASLEEP_S:
        return False
    return in_quiet_hours(now_ts)


def _override_verdict(
    ov: Override, now_ts: str
) -> tuple[PresenceState, str, tuple[Channel, ...], frozenset[Channel]]:
    """(state, spoken reason, channels, channels FORBIDDEN whatever happens).

    The fourth element is why this returns a tuple of four rather than three.
    "Don't call me" has to survive the modifiers that follow: without a standing
    prohibition, a live-call signal would helpfully add ``phone`` back to the
    ladder of a user who just asked not to be called.
    """
    left = ov.remaining_s(now_ts)
    tail = f" for another {spoken_duration(left)}" if left is not None else ""
    who = ov.set_by or "you"
    none: frozenset[Channel] = frozenset()
    if ov.mode == "present":
        return "present", f"You told me you were back{tail}.", REACHABLE_BY_STATE["present"], none
    if ov.mode == "away":
        return "away", f"You told me you were going out{tail}.", REACHABLE_BY_STATE["away"], none
    if ov.mode == "dnd":
        # dnd is a REACHABILITY statement, not a presence one: the user is
        # probably right there, they just do not want the phone to ring.
        no_phone: frozenset[Channel] = frozenset({"phone"})
        return "maybe", f"You asked me not to call{tail}.", ("desk", "telegram"), no_phone
    # desk_only
    return "maybe", f"{who} set desk-only{tail}.", ("desk",), frozenset({"telegram", "phone"})


def decide(
    signals: Mapping[str, Signal],
    override: Override | None,
    now_ts: str,
) -> Presence:
    """The verdict, as a PURE function of live signals plus the override.

    No connection, no clock of its own, no writes. Every branch below is
    reachable from a three-line dict in a test, which is the entire reason this
    is separated from :func:`evaluate_presence`.

    Evidence order, strongest first:

    1. an unexpired override — it beats every sensor, that is what it is for;
    2. the NEWEST decisive signal, positive or negative. A wake word after a
       screen lock means you came back; a screen lock after a wake word means you
       left. Timestamp order is the only honest tie-break, and an exact tie goes
       to the NEGATIVE — see the asymmetry in the module docstring;
    3. the OS idle counter and its thresholds;
    4. nothing: ``unknown``.

    Then two modifiers that speak to reachability rather than to the room:
    ``call`` (you are on a Jarvis call, so you are not at the desk) and
    ``geofence`` (you are not in the building, so the desk cannot hear you).
    """
    live = {k: s for k, s in signals.items() if s.alive(now_ts)}
    idle_s = _idle_of(live, now_ts)

    if override is not None and override.alive(now_ts):
        state, reason, reach, forbidden = _override_verdict(override, now_ts)
        since = override.set_at or now_ts
        return _finish(
            state,
            since,
            1.0,
            reach,
            reason,
            live,
            idle_s,
            now_ts,
            override_set=True,
            forbidden=forbidden,
        )

    positives = [s for k, s in live.items() if k in POSITIVE_SOURCES]
    negatives: list[Signal] = []
    probe = live.get("probe")
    if probe is not None and probe.value.get("answered") is False:
        negatives.append(probe)
    lock = live.get("lock")
    if lock is not None and lock.value.get("locked") is True:
        negatives.append(lock)

    best_pos = _newest(positives)
    best_neg = _newest(negatives)

    # ">=" not ">": an exact tie is resolved toward absence. Ties are vanishingly
    # rare at millisecond resolution, but the direction of the rule is the whole
    # design and it should not be an accident of which way a comparison fell.
    if best_neg is not None and (best_pos is None or _onset(best_neg) >= _onset(best_pos)):
        began = _onset(best_neg)
        began_ago = (parse_ts(now_ts) - parse_ts(began)).total_seconds()
        if best_neg.source == "probe":
            reason = f"I asked you something {spoken_ago(began_ago)} and nobody answered."
            conf = 0.9
        else:
            reason = f"Your screen has been locked for {spoken_duration(began_ago)}."
            conf = 0.85
        state: PresenceState = "asleep" if _asleep_if_night("away", idle_s, now_ts) else "away"
        if state == "asleep":
            reason = f"{reason} It is the middle of the night, so I think you are asleep."
        return _finish(state, began, conf, REACHABLE_BY_STATE[state], reason, live, idle_s, now_ts)

    if best_pos is not None:
        age = spoken_ago(best_pos.age_s(now_ts))
        heard = "said my name" if best_pos.source == "wakeword" else "spoke to me"
        reason = f"You {heard} {age}."
        return _finish(
            "present",
            best_pos.ts,
            0.95,
            REACHABLE_BY_STATE["present"],
            reason,
            live,
            idle_s,
            now_ts,
        )

    if idle_s is None:
        # Two different blindnesses, and the difference matters when debugging at
        # 1am: nobody is reporting at all, versus the reporter is running and
        # says it cannot read the platform.
        reason = (
            "I can't read the idle time on this desktop."
            if "idle" in live
            else "Nothing has reported your keyboard recently, so I am guessing."
        )
        return _finish(
            "unknown", now_ts, 0.0, REACHABLE_BY_STATE["unknown"], reason, live, idle_s, now_ts
        )

    said = spoken_duration(idle_s)
    if idle_s < IDLE_MAYBE_S:
        return _finish(
            "present",
            shift_ts(now_ts, -idle_s),
            0.8,
            REACHABLE_BY_STATE["present"],
            f"You touched the keyboard {spoken_ago(idle_s)}.",
            live,
            idle_s,
            now_ts,
        )
    if idle_s < IDLE_AWAY_S:
        # since = when the MAYBE began, not when the input stopped.
        return _finish(
            "maybe",
            shift_ts(now_ts, -(idle_s - IDLE_MAYBE_S)),
            0.6,
            REACHABLE_BY_STATE["maybe"],
            f"Nothing has touched the keyboard for {said}, so you may still be here.",
            live,
            idle_s,
            now_ts,
        )

    state = "asleep" if _asleep_if_night("away", idle_s, now_ts) else "away"
    reason = f"Nothing has touched the keyboard for {said}."
    if state == "asleep":
        reason = f"{reason} It is the middle of the night, so I think you are asleep."
    return _finish(
        state,
        shift_ts(now_ts, -(idle_s - IDLE_AWAY_S)),
        0.8,
        REACHABLE_BY_STATE[state],
        reason,
        live,
        idle_s,
        now_ts,
    )


def _finish(
    state: PresenceState,
    since: str,
    confidence: float,
    reachable: tuple[Channel, ...],
    reason: str,
    live: Mapping[str, Signal],
    idle_s: float | None,
    now_ts: str,
    *,
    override_set: bool = False,
    forbidden: frozenset[Channel] = frozenset(),
) -> Presence:
    """Apply the reachability modifiers and assemble the verdict.

    Split out so every branch of :func:`decide` goes through the same modifiers.

    TWO RULES THAT LOOK ARBITRARY AND ARE NOT. An active override freezes the
    STATE — ``call`` and ``geofence`` are sensors, and the override exists
    precisely to beat sensors — but it does not freeze REACHABILITY, because a
    user who said "I'm back" and is nonetheless on a call still cannot be reached
    at a desk they are not sitting at. And ``forbidden`` is subtracted LAST, so
    no modifier can hand back a channel the user explicitly refused.
    """
    reach = set(reachable)
    extra: list[str] = []

    call = live.get("call")
    if call is not None and call.value.get("active", True):
        # On a Jarvis call: trivially reachable by phone, and not at the desk.
        # State is capped at maybe because "present" means the ROOM can hear me.
        reach.discard("desk")
        reach.add("phone")
        if state == "present" and not override_set:
            state = "maybe"
        extra.append("You are on a call with me.")

    geo = live.get("geofence")
    if geo is not None and geo.value.get("at_home") is False:
        reach.discard("desk")
        if state in ("present", "maybe", "unknown") and not override_set:
            state = "away"
        extra.append("Your phone says you are not at home.")

    tg = live.get("telegram")
    if tg is not None:
        reach.add("telegram")
        extra.append(f"You messaged me on Telegram {spoken_ago(tg.age_s(now_ts))}.")

    reach -= forbidden
    ordered = tuple(c for c in _CHANNEL_ORDER if c in reach)
    if extra:
        reason = " ".join([reason, *extra])
    return Presence(
        state=state, since=since, confidence=confidence, reachable=ordered, reason=reason
    )


# ───────────────────────────── signal writes ─────────────────────────────


def _check_source(source: str) -> None:
    if source not in SOURCES:
        raise UnknownSignal(f"unknown presence source {source!r}; expected one of {SOURCES}")


def record_signal(
    con: sqlite3.Connection,
    source: str,
    value: dict[str, Any],
    ttl_s: int | None = None,
    *,
    now_ts: str | None = None,
) -> bool:
    """Write one signal. Returns False when an OLDER reading lost a race.

    ``presence_signals`` is keyed by source, so every writer of ``idle`` is
    overwriting the same row. Two pollers (a restarted dispatch overlapping its
    predecessor, say) can interleave, and without the monotonic guard the SLOWER
    one wins and presence goes backwards in time. The upsert therefore refuses to
    move ``ts`` backwards, and says so rather than pretending it stored the row.

    ``source='override'`` is routed to :func:`set_override` instead of being
    stored as an ordinary signal: the schema enumerates it as a source, but the
    override has its own table and two places to read "am I overridden" from is
    how they drift apart. The signal row is still written, as a breadcrumb for
    :func:`active_signals`; :func:`decide` ignores it and reads the table.
    """
    _check_source(source)
    if not isinstance(value, dict):
        raise TypeError(f"signal value must be a dict, got {type(value).__name__}")
    ttl = DEFAULT_TTL_S[source] if ttl_s is None else ttl_s
    if ttl <= 0:
        raise ValueError(f"ttl_s must be positive, got {ttl}")
    ts = now_ts or now()

    if source == "override":
        mode = value.get("mode")
        set_override(con, mode, ttl, by=str(value.get("by", "unknown")), now_ts=ts)
        return True

    row = con.execute(
        """INSERT INTO presence_signals (source, value, ts, ttl_s) VALUES (?,?,?,?)
             ON CONFLICT(source) DO UPDATE SET
               value=excluded.value, ts=excluded.ts, ttl_s=excluded.ttl_s
             WHERE excluded.ts >= presence_signals.ts
           RETURNING source""",
        (source, canon(value), ts, int(ttl)),
    ).fetchone()
    return row is not None


#: The design doc's name for :func:`record_signal`. Same function, both spellings
#: appear in the architecture, and an alias is cheaper than a divergence.
set_signal = record_signal


def clear_signal(con: sqlite3.Connection, source: str) -> bool:
    """Delete a signal outright. Returns whether there was one."""
    _check_source(source)
    cur = con.execute("DELETE FROM presence_signals WHERE source=?", (source,))
    return bool(cur.rowcount)


def _to_signal(row: sqlite3.Row) -> Signal:
    return Signal(
        source=str(row["source"]),
        value=json.loads(row["value"]),
        ts=str(row["ts"]),
        ttl_s=int(row["ttl_s"]),
    )


def active_signals(con: sqlite3.Connection, now_ts: str | None = None) -> dict[str, Signal]:
    """Every UNEXPIRED signal, by source. Expired rows are not evidence.

    A row that will not parse is DROPPED rather than raised. ``value`` is plain
    TEXT in a file five processes write to, and this module's whole philosophy is
    that being blind degrades toward one redundant Telegram message: one
    malformed row must cost the evidence it carried, not take down presence for
    every reader in the system.
    """
    ts = now_ts or now()
    rows = con.execute("SELECT * FROM presence_signals").fetchall()
    live: dict[str, Signal] = {}
    for row in rows:
        try:
            sig = _to_signal(row)
        except (ValueError, TypeError):
            continue
        if isinstance(sig.value, dict) and sig.alive(ts):
            live[sig.source] = sig
    return live


def purge_expired(con: sqlite3.Connection, now_ts: str | None = None) -> int:
    """Housekeeping only. Correctness never depends on this having run.

    Expiry is evaluated at read time, so a row that outlives its TTL is already
    invisible to :func:`decide`. This exists so the table stays readable by a
    human at 1am, not so the verdict is right.
    """
    ts = now_ts or now()
    dead = [
        r["source"]
        for r in con.execute("SELECT * FROM presence_signals").fetchall()
        if not _to_signal(r).alive(ts)
    ]
    for source in dead:
        con.execute("DELETE FROM presence_signals WHERE source=?", (source,))
    return len(dead)


# ───────────────────────────── the free sensor ─────────────────────────────


def probe_outcome(
    con: sqlite3.Connection,
    *,
    asked_at: str,
    now_ts: str | None = None,
    window_s: float = UNANSWERED_PROBE_S,
) -> Literal["answered", "unanswered", "waiting"]:
    """Did anybody make a sound after we spoke? PURE: writes nothing.

    "Answered" is deliberately ANY audio from the user after ``asked_at``, not a
    correct answer to the question. The sensor is measuring whether the room is
    occupied; a grunt proves that as well as a sentence does, and requiring an
    on-topic reply would make an ignored question and an empty room the same
    reading.
    """
    ts = now_ts or now()
    heard = [
        s for k, s in active_signals(con, ts).items() if k in POSITIVE_SOURCES and s.ts > asked_at
    ]
    if heard:
        return "answered"
    if (parse_ts(ts) - parse_ts(asked_at)).total_seconds() >= window_s:
        return "unanswered"
    return "waiting"


def note_probe(
    con: sqlite3.Connection,
    *,
    asked_at: str,
    request_id: str | None = None,
    actor: str = "desk",
    now_ts: str | None = None,
    window_s: float = UNANSWERED_PROBE_S,
    ttl_s: int | None = None,
) -> Literal["answered", "unanswered", "waiting"]:
    """THE HOOK the requests layer calls after speaking a question.

    Call it once, ``window_s`` after the question was spoken (or on every poll —
    it is idempotent and cheap). On ``unanswered`` it writes the decisive
    negative that drops presence to ``away`` immediately, which is what makes the
    Telegram delivery due NOW instead of ninety seconds from now.

    On ``answered`` it CLEARS any previous negative. Without that, one unanswered
    question would keep the room "empty" for its full ten-minute TTL even though
    the user is demonstrably back and talking.
    """
    ts = now_ts or now()
    outcome = probe_outcome(con, asked_at=asked_at, now_ts=ts, window_s=window_s)
    if outcome == "unanswered":
        record_signal(
            con,
            "probe",
            {"answered": False, "request_id": request_id, "asked_at": asked_at, "by": actor},
            ttl_s,
            now_ts=ts,
        )
    elif outcome == "answered":
        clear_signal(con, "probe")
    return outcome


def note_heard(
    con: sqlite3.Connection,
    source: Literal["wakeword", "utterance"] = "utterance",
    *,
    text: str | None = None,
    actor: str = "desk",
    now_ts: str | None = None,
) -> bool:
    """A human made a noise at the desk. The instant path to ``present``.

    Also clears a stale ``probe`` negative, for the same reason :func:`note_probe`
    does: evidence of a person in the room retires evidence of an empty one.
    """
    if source not in POSITIVE_SOURCES:
        raise UnknownSignal(f"{source!r} is not a decisive positive")
    ts = now_ts or now()
    ok = record_signal(con, source, {"text": text, "by": actor}, now_ts=ts)
    clear_signal(con, "probe")
    return ok


# ───────────────────────────── the override ─────────────────────────────


def read_override(con: sqlite3.Connection) -> Override:
    row = con.execute("SELECT * FROM presence_override WHERE id=1").fetchone()
    if row is None:
        return Override(mode=None, until=None, set_by=None, set_at=None)
    mode = row["mode"]
    return Override(
        mode=mode if mode in OVERRIDE_MODES else None,
        until=row["until"],
        set_by=row["set_by"],
        set_at=row["set_at"],
    )


def set_override(
    con: sqlite3.Connection,
    mode: OverrideMode | None,
    ttl_s: int | None = None,
    *,
    by: str,
    now_ts: str | None = None,
) -> Override:
    """ "I'm going out" / "I'm back" / "don't call me". ``mode=None`` clears it.

    ALWAYS bounded. An override with no expiry is how a system ends up silent for
    a week because somebody said "I'm going out" on Friday; the schema has an
    ``until`` column and this function refuses to leave it NULL.

    The state row is refreshed in the same call, so an override spoken out loud
    takes effect on the next reader immediately rather than at the next poll.
    """
    if mode is not None and mode not in OVERRIDE_MODES:
        raise ValueError(f"unknown override mode {mode!r}; expected one of {OVERRIDE_MODES}")
    ts = now_ts or now()
    until = None if mode is None else shift_ts(ts, ttl_s or OVERRIDE_TTL_S[mode])

    with tx(con) as t:
        t.execute(
            """INSERT INTO presence_override (id, mode, until, set_by, set_at) VALUES (1,?,?,?,?)
                 ON CONFLICT(id) DO UPDATE SET
                   mode=excluded.mode, until=excluded.until,
                   set_by=excluded.set_by, set_at=excluded.set_at""",
            (mode, until, by, ts),
        )
        if mode is None:
            t.execute("DELETE FROM presence_signals WHERE source='override'")
        else:
            # The breadcrumb copy. Derived, never read by decide().
            t.execute(
                """INSERT INTO presence_signals (source, value, ts, ttl_s) VALUES (?,?,?,?)
                     ON CONFLICT(source) DO UPDATE SET
                       value=excluded.value, ts=excluded.ts, ttl_s=excluded.ttl_s""",
                (
                    "override",
                    canon({"mode": mode, "by": by, "until": until}),
                    ts,
                    int(ttl_s or OVERRIDE_TTL_S[mode]),
                ),
            )
    update_presence(con, actor=by, now_ts=ts)
    return read_override(con)


# ───────────────────────────── evaluate and persist ─────────────────────────────


def evaluate_presence(con: sqlite3.Connection, now_ts: str | None = None) -> Presence:
    """The live verdict. Reads two tables, writes NOTHING.

    Any process may call this as often as it likes; it takes no lock, publishes
    nothing, and cannot race with anything.
    """
    ts = now_ts or now()
    return decide(active_signals(con, ts), read_override(con), ts)


def read_presence(con: sqlite3.Connection) -> Presence | None:
    """The LAST PERSISTED verdict, or None if nobody has evaluated yet.

    Different from :func:`evaluate_presence` on purpose. This one carries a
    ``since`` that survived restarts — the real "how long have you been away" —
    at the cost of being as stale as the last poll. A tool answering "where do
    you think I am?" wants this; a router about to spend money wants the live
    one.
    """
    row = con.execute("SELECT * FROM presence_state WHERE id=1").fetchone()
    if row is None:
        return None
    return Presence(
        state=str(row["state"]),  # type: ignore[arg-type]
        since=str(row["since"]),
        confidence=float(row["confidence"]),
        reachable=tuple(json.loads(row["reachable"])),
        reason=str(row["reason"]),
    )


def update_presence(
    con: sqlite3.Connection,
    *,
    actor: str = "dispatch",
    now_ts: str | None = None,
) -> Presence:
    """Evaluate, persist, and publish ``presence.changed`` on a real change.

    ``since`` is PRESERVED across an unchanged state, which is the one thing the
    pure function cannot do: it has no history, so it derives ``since`` from the
    evidence in front of it. Here the stored row is the history, so "away since
    14:02" stays 14:02 across a hundred polls instead of walking forward.

    The event's idem_key names the transition by WHAT IT LEFT — the prior row's
    state and its ``updated_at`` — and not by the verdict alone. Two processes
    that notice the same change read the same prior row inside the same
    BEGIN IMMEDIATE, so they still publish it once between them; but a state the
    system genuinely returns to still gets its own event. Keying on
    ``(state, since, reachable)`` looked equivalent and was not: going
    present -> away -> present while the same wake word row is still the newest
    evidence reproduces ``since`` exactly, and the second "the user is back"
    silently deduped against the first and never reached the bus.
    """
    ts = now_ts or now()
    fresh = evaluate_presence(con, ts)
    with tx(con) as t:
        prior_row = t.execute("SELECT * FROM presence_state WHERE id=1").fetchone()
        prior_state = None if prior_row is None else str(prior_row["state"])
        prior_reach = None if prior_row is None else str(prior_row["reachable"])
        prior_at = None if prior_row is None else str(prior_row["updated_at"])
        reach_text = canon(list(fresh.reachable))
        changed = prior_state != fresh.state or prior_reach != reach_text
        since = fresh.since if changed or prior_row is None else str(prior_row["since"])
        t.execute(
            """INSERT INTO presence_state
                 (id, state, since, confidence, reachable, reason, updated_at)
                 VALUES (1,?,?,?,?,?,?)
                 ON CONFLICT(id) DO UPDATE SET
                   state=excluded.state, since=excluded.since, confidence=excluded.confidence,
                   reachable=excluded.reachable, reason=excluded.reason,
                   updated_at=excluded.updated_at""",
            (fresh.state, since, fresh.confidence, reach_text, fresh.reason, ts),
        )
        if changed:
            publish(
                t,
                "presence.changed",
                actor,
                {
                    "state": fresh.state,
                    "from": prior_state,
                    "since": since,
                    "confidence": fresh.confidence,
                    "reachable": list(fresh.reachable),
                    "reason": fresh.reason,
                },
                idem_key=f"presence:{prior_state}@{prior_at}->{fresh.state}:{since}:{reach_text}",
            )
    return Presence(
        state=fresh.state,
        since=since,
        confidence=fresh.confidence,
        reachable=fresh.reachable,
        reason=fresh.reason,
    )


# ───────────────────────────── the OS idle probes ─────────────────────────────
#
# Every probe returns None rather than raising: "this platform cannot tell me" is
# a normal answer, and a presence subsystem that crashes the dispatcher because
# gdbus is missing has failed worse than one that says unknown.


def _run(argv: list[str], timeout: float = 1.0) -> str | None:
    """Run a probe binary. None on any failure, including "not installed"."""
    if shutil.which(argv[0]) is None:
        return None
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, no shell, no user input
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def _gnome_idle_s() -> float | None:
    """Wayland/GNOME FIRST, because X11's probe LIES here rather than failing."""
    out = _run(
        [
            "gdbus",
            "call",
            "--session",
            "--dest",
            "org.gnome.Mutter.IdleMonitor",
            "--object-path",
            "/org/gnome/Mutter/IdleMonitor/Core",
            "--method",
            "org.gnome.Mutter.IdleMonitor.GetIdletime",
        ]
    )
    if not out:
        return None
    digits = "".join(c for c in out if c.isdigit())
    return int(digits) / 1000.0 if digits else None


def _logind_prop(prop: str) -> str | None:
    out = _run(["loginctl", "show-session", "self", "-p", prop, "--value"])
    return out.strip() if out else None


def _logind_idle_s() -> float | None:
    """Coarse: IdleSinceHint is microseconds since the epoch, 0 when not idle."""
    if _logind_prop("IdleHint") not in ("yes", "no"):
        return None
    since = _logind_prop("IdleSinceHint")
    if since is None or not since.isdigit():
        return None
    usec = int(since)
    if usec == 0:
        return 0.0
    return max(0.0, time.time() - usec / 1_000_000.0)


def _x11_idle_s() -> float | None:
    """LAST on Linux. Under Wayland/XWayland this returns a constant 0 forever."""
    if not os.environ.get("DISPLAY"):
        return None
    try:
        xss = ctypes.CDLL("libXss.so.1")
        x11 = ctypes.CDLL("libX11.so.6")
    except OSError:
        return None

    class _Info(ctypes.Structure):
        _fields_ = [
            ("window", ctypes.c_ulong),
            ("state", ctypes.c_int),
            ("kind", ctypes.c_int),
            ("til_or_since", ctypes.c_ulong),
            ("idle", ctypes.c_ulong),
            ("event_mask", ctypes.c_ulong),
        ]

    x11.XOpenDisplay.restype = ctypes.c_void_p
    xss.XScreenSaverAllocInfo.restype = ctypes.POINTER(_Info)
    dpy = x11.XOpenDisplay(None)
    if not dpy:
        return None
    try:
        info = xss.XScreenSaverAllocInfo()
        root = x11.XDefaultRootWindow(ctypes.c_void_p(dpy))
        if not xss.XScreenSaverQueryInfo(ctypes.c_void_p(dpy), root, info):
            return None
        return float(info.contents.idle) / 1000.0
    except (OSError, AttributeError, ValueError):
        return None
    finally:
        x11.XCloseDisplay(ctypes.c_void_p(dpy))


def _darwin_idle_s() -> float | None:
    try:
        cg = ctypes.CDLL(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
        )
    except OSError:
        return None
    try:
        cg.CGEventSourceSecondsSinceLastEventType.restype = ctypes.c_double
        cg.CGEventSourceSecondsSinceLastEventType.argtypes = [ctypes.c_int, ctypes.c_uint32]
        # kCGEventSourceStateCombinedSessionState = 0, kCGAnyInputEventType = 0xFFFFFFFF
        return float(cg.CGEventSourceSecondsSinceLastEventType(0, 0xFFFFFFFF))
    except (OSError, AttributeError, ValueError):
        return None


def _windows_idle_s() -> float | None:
    if os.name != "nt":
        return None
    try:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return None

    class _LastInput(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_ulong)]

    info = _LastInput()
    info.cbSize = ctypes.sizeof(_LastInput)
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        return None
    return max(0.0, (kernel32.GetTickCount() - info.dwTime) / 1000.0)


#: Probe order, and the order is the design. Wayland/GNOME first because the X11
#: probe does not FAIL under Wayland, it lies; logind second because it works on
#: any Linux session; X11 third; then the other platforms.
IDLE_PROBES: tuple[tuple[str, Callable[[], float | None]], ...] = (
    ("gnome", _gnome_idle_s),
    ("logind", _logind_idle_s),
    ("x11", _x11_idle_s),
    ("darwin", _darwin_idle_s),
    ("windows", _windows_idle_s),
)


def idle_seconds() -> float | None:
    """Seconds since the last human input, or None for "this desktop won't say".

    None is NOT zero and must never be coerced to it: zero means "someone is
    typing right now", which is the exact lie the Wayland trap tells.
    """
    for _name, probe in IDLE_PROBES:
        value = probe()
        if value is not None:
            return value
    return None


def screen_locked() -> bool | None:
    """Lock state, or None when unknowable. None is not False."""
    hint = _logind_prop("LockedHint")
    if hint == "yes":
        return True
    if hint == "no":
        return False
    out = _run(
        [
            "gdbus",
            "call",
            "--session",
            "--dest",
            "org.gnome.ScreenSaver",
            "--object-path",
            "/org/gnome/ScreenSaver",
            "--method",
            "org.gnome.ScreenSaver.GetActive",
        ]
    )
    if out is None:
        return None
    if "true" in out.lower():
        return True
    if "false" in out.lower():
        return False
    return None


def idle_probe_self_test(
    probe: Callable[[], float | None] = idle_seconds,
    *,
    pause_s: float = 2.0,
) -> bool:
    """Does the idle counter actually RISE across a pause? Run at startup.

    THIS IS THE ONLY THING that can tell a working probe from a lying one. A
    probe stuck at a constant value (XScreenSaver under Wayland is the famous
    case, but a stubbed one in a test is the same shape) reports a plausible
    number forever, and everything downstream believes it. Returns False rather
    than raising, because the caller's job is to degrade to ``unknown`` and SAY
    SO, not to refuse to boot.

    Obvious footgun, stated: this must run when nobody is touching the keyboard,
    or the second reading legitimately drops. It is a startup self-test.
    """
    first = probe()
    if first is None:
        return False
    time.sleep(pause_s)
    second = probe()
    if second is None:
        return False
    return second > first


def poll_idle(
    con: sqlite3.Connection,
    *,
    actor: str = "dispatch",
    idle_probe: Callable[[], float | None] = idle_seconds,
    lock_probe: Callable[[], bool | None] = screen_locked,
    now_ts: str | None = None,
) -> tuple[float | None, bool | None]:
    """The 5-second task in jarvis-dispatch. Writes ``idle`` and ``lock``.

    The probes are arguments so this is testable without a desktop session, and
    so a caller that already knows (the phone leg, say) can supply its own. They
    are plain functions, not state.

    An idle reading of None is STILL WRITTEN, carrying ``idle_s: null``. That is
    the difference between "the poller is alive and this platform won't say"
    (unknown, and we can name the reason) and "nobody is polling at all"
    (unknown, and something is broken) — two situations that look identical if
    the blind case writes nothing.

    The lock row carries ``since``: the instant the CURRENT lock state began,
    preserved across every refresh. Without it a screen locked an hour ago looks
    one poll old and outranks a wake word spoken five seconds ago — see
    :func:`_onset`. Read and write are in one transaction so two overlapping
    pollers cannot both read the old row and both restart the clock.

    This writes signals only. Publishing ``presence.changed`` is
    :func:`update_presence`, which the same 5-second task must call after this
    one; nothing else in the system will do it.
    """
    ts = now_ts or now()
    idle = idle_probe()
    locked = lock_probe()
    with tx(con) as t:
        record_signal(t, "idle", {"idle_s": idle}, now_ts=ts)
        if locked is not None:
            prior = t.execute("SELECT * FROM presence_signals WHERE source='lock'").fetchone()
            since = ts
            if prior is not None:
                was = _to_signal(prior)
                if was.alive(ts) and was.value.get("locked") == locked:
                    since = _onset(was)
            record_signal(t, "lock", {"locked": locked, "since": since}, now_ts=ts)
    return idle, locked
