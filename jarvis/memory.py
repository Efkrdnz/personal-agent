"""What the user asked Jarvis to remember, and what to remind them of, and when.

Two small stores and one parser, in the spine because every channel needs them
and none of them may need a network: "remember my locker is 214" typed into
Telegram must be in the desk's instructions an hour later, and "remind me at
six" said at the desk must reach the phone if that is where the user is at six.

NOTES are facts in the user's words. They are never paraphrased on the way in —
the user's sentence is the note — and they are FORGOTTEN rather than deleted, so
"what did I tell you about the locker?" still has an answer after "forget that".

REMINDERS are a request that does not exist yet. The scheduler raises it when it
is due, which is when presence should be asked where the user is (see
migration 005). This module only stores them and hands out the due ones under a
compare-and-swap; raising and routing belong to :mod:`jarvis.schedule`.

TIME. ``parse_when`` turns "in ten minutes", "at 6pm" or "tomorrow at 9" into
an instant. It takes the user's local "now" as an argument rather than reading a
clock, so it is testable, and it REFUSES anything it does not understand: a
reminder set for the wrong day is worse than "I didn't catch when".
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from jarvis.ids import nid, now

__all__ = [
    "Note",
    "Reminder",
    "add_reminder",
    "cancel_reminder",
    "due_reminders",
    "fired_reminders",
    "forget",
    "mark_fired",
    "notes",
    "parse_when",
    "pending_reminders",
    "recall",
    "remember",
    "settle",
    "to_ts",
]

#: How many notes go into a conversation's instructions. Facts beyond this are
#: still found by :func:`recall`; they just are not volunteered every turn.
NOTES_IN_CONTEXT = 30


@dataclass(frozen=True, slots=True)
class Note:
    id: str
    text: str
    actor: str
    channel: str
    created_at: str


@dataclass(frozen=True, slots=True)
class Reminder:
    id: str
    text: str
    due_at: str
    state: str
    actor: str
    channel: str
    request_id: str | None
    created_at: str


# ───────────────────────────── notes ─────────────────────────────


def remember(con: sqlite3.Connection, text: str, *, actor: str, channel: str) -> Note:
    clean = " ".join(text.split())
    if not clean:
        raise ValueError("there is nothing to remember")
    note = Note(nid("note"), clean, actor, channel, now())
    con.execute(
        "INSERT INTO notes(id, text, actor, channel, created_at) VALUES (?,?,?,?,?)",
        (note.id, note.text, note.actor, note.channel, note.created_at),
    )
    return note


def notes(con: sqlite3.Connection, *, limit: int = NOTES_IN_CONTEXT) -> list[Note]:
    """The live notes, newest first."""
    rows = con.execute(
        "SELECT id, text, actor, channel, created_at FROM notes WHERE forgotten_at IS NULL "
        # rowid, not created_at: two notes in one millisecond share a stamp,
        # and "newest first" must not then depend on a random id.
        "ORDER BY rowid DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [Note(*r) for r in rows]


def recall(con: sqlite3.Connection, query: str = "", *, limit: int = 5) -> list[Note]:
    """Notes that share words with the query, best first; newest first when it is empty.

    Word overlap rather than embeddings: the store is one person's few hundred
    sentences, and a result the user can predict ("it matched 'locker'") is
    worth more here than a clever one.
    """
    words = _words(query)
    every = notes(con, limit=10_000)
    if not words:
        return every[:limit]
    scored = []
    for age, n in enumerate(every):  # newest first, so a lower age is newer
        overlap = len(words & _words(n.text))
        if overlap:
            scored.append((-overlap, age, n))
    scored.sort(key=lambda x: (x[0], x[1]))
    return [n for _, _, n in scored[:limit]]


def forget(con: sqlite3.Connection, query: str) -> list[Note]:
    """Forget the notes a phrase points at. By id, or by the best word match.

    Only the BEST matches go: "forget the locker thing" must not also forget
    the note about the locker room at the gym that happened to share one word
    less.
    """
    q = query.strip()
    if not q:
        return []
    if q.startswith("note_"):
        targets = [n for n in notes(con, limit=10_000) if n.id == q]
    else:
        words = _words(q)
        scored = [(len(words & _words(n.text)), n) for n in notes(con, limit=10_000)]
        best = max((s for s, _ in scored), default=0)
        targets = [n for s, n in scored if best and s == best]
    stamp = now()
    for n in targets:
        con.execute(
            "UPDATE notes SET forgotten_at=? WHERE id=? AND forgotten_at IS NULL", (stamp, n.id)
        )
    return targets


# ───────────────────────────── reminders ─────────────────────────────


def add_reminder(
    con: sqlite3.Connection, text: str, due: datetime, *, actor: str, channel: str
) -> Reminder:
    clean = " ".join(text.split())
    if not clean:
        raise ValueError("a reminder needs something to remind you of")
    if due.tzinfo is None:
        # A naive time is somebody's local time and nobody's in particular; the
        # caller knows whose, so the caller attaches it.
        raise ValueError("a reminder's time must carry its timezone")
    stamp = now()
    r = Reminder(nid("rem"), clean, to_ts(due), "pending", actor, channel, None, stamp)
    con.execute(
        "INSERT INTO reminders(id, text, due_at, state, actor, channel, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (r.id, r.text, r.due_at, r.state, r.actor, r.channel, stamp, stamp),
    )
    return r


def pending_reminders(con: sqlite3.Connection) -> list[Reminder]:
    rows = con.execute(
        f"SELECT {_REMINDER_COLS} FROM reminders WHERE state='pending' ORDER BY due_at, id"
    ).fetchall()
    return [Reminder(*r) for r in rows]


def cancel_reminder(con: sqlite3.Connection, query: str) -> list[Reminder]:
    """Cancel pending reminders by id or by the best word match, like :func:`forget`."""
    q = query.strip()
    pending = pending_reminders(con)
    if q.startswith("rem_"):
        targets = [r for r in pending if r.id == q]
    else:
        words = _words(q)
        scored = [(len(words & _words(r.text)), r) for r in pending]
        best = max((s for s, _ in scored), default=0)
        targets = [r for s, r in scored if best and s == best]
    stamp = now()
    done = []
    for r in targets:
        hit = con.execute(
            "UPDATE reminders SET state='cancelled', updated_at=? WHERE id=? AND state='pending'",
            (stamp, r.id),
        ).rowcount
        if hit:
            done.append(r)
    return done


def due_reminders(con: sqlite3.Connection, *, now_ts: str | None = None) -> list[Reminder]:
    """Pending reminders whose time has come. Reading them claims nothing."""
    rows = con.execute(
        f"SELECT {_REMINDER_COLS} FROM reminders WHERE state='pending' AND due_at <= ? "
        "ORDER BY due_at, id",
        (now_ts or now(),),
    ).fetchall()
    return [Reminder(*r) for r in rows]


def mark_fired(con: sqlite3.Connection, reminder_id: str, request_id: str) -> bool:
    """``pending`` -> ``fired``, naming the request that carries it. True if WE moved it.

    The request is raised BEFORE this, with a dedupe key per reminder, so a
    process killed in between leaves a pending reminder whose request already
    exists — and the next tick raises the same row again rather than a second
    one. Claiming first would lose the reminder to that same crash.
    """
    return (
        con.execute(
            "UPDATE reminders SET state='fired', request_id=?, updated_at=? "
            "WHERE id=? AND state='pending'",
            (request_id, now(), reminder_id),
        ).rowcount
        == 1
    )


def settle(con: sqlite3.Connection, reminder_id: str) -> bool:
    """``fired`` -> ``done``. True if WE moved it, which is who may act on the answer."""
    return (
        con.execute(
            "UPDATE reminders SET state='done', updated_at=? WHERE id=? AND state='fired'",
            (now(), reminder_id),
        ).rowcount
        == 1
    )


def fired_reminders(con: sqlite3.Connection) -> list[Reminder]:
    """Fired and carried by a request, not yet settled: where snoozes are found."""
    rows = con.execute(
        f"SELECT {_REMINDER_COLS} FROM reminders WHERE state='fired' AND request_id IS NOT NULL "
        "ORDER BY due_at, id"
    ).fetchall()
    return [Reminder(*r) for r in rows]


_REMINDER_COLS = "id, text, due_at, state, actor, channel, request_id, created_at"


# ───────────────────────────── time, as people say it ─────────────────────────────

_NUMBERS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "forty-five": 45,
    "fortyfive": 45, "fifty": 50, "sixty": 60, "ninety": 90, "half": 0.5, "couple": 2,
    "a couple of": 2, "few": 3, "a few": 3,
}  # fmt: skip
_UNITS = {
    "second": 1, "seconds": 1, "sec": 1, "secs": 1,
    "minute": 60, "minutes": 60, "min": 60, "mins": 60,
    "hour": 3600, "hours": 3600, "hr": 3600, "hrs": 3600,
    "day": 86400, "days": 86400, "week": 604800, "weeks": 604800,
}  # fmt: skip
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_PARTS = {"morning": "09:00", "noon": "12:00", "afternoon": "15:00", "evening": "18:00",
          "tonight": "20:00", "night": "21:00", "midnight": "00:00"}  # fmt: skip


def parse_when(text: str, local_now: datetime) -> datetime | None:
    """An aware instant for a spoken time, in ``local_now``'s zone, or None.

    Understands: "in 10 minutes", "in an hour and a half", "in two days",
    "at 6", "at 6pm", "at 18:30", "6:30 pm", "tomorrow", "tomorrow at 9",
    "tonight", "this evening", "on friday at 10", "noon". A clock time with no
    am/pm and no day means the next time the clock shows it, so "at 6" said at
    19:00 is 06:00 tomorrow — and that is said back to the user, never assumed
    silently (the caller speaks the resolved time).
    """
    if local_now.tzinfo is None:
        raise ValueError("local_now must be timezone-aware")
    t = " ".join(text.lower().replace(",", " ").split())
    t = re.sub(r"^(remind me |me )", "", t)
    if not t:
        return None

    rel = _relative(t)
    if rel is not None:
        return local_now + rel

    day: datetime | None = None
    rest = t
    if m := re.search(r"\b(today|tonight|tomorrow|day after tomorrow)\b", rest):
        word = m.group(1)
        offset = {"today": 0, "tonight": 0, "tomorrow": 1, "day after tomorrow": 2}[word]
        day = local_now + timedelta(days=offset)
        if word == "tonight" and not re.search(r"\d", rest):
            rest = rest.replace("tonight", _PARTS["tonight"])
        rest = rest.replace(word, " ")
    elif m := re.search(r"\b(?:on |next |this )?(" + "|".join(_WEEKDAYS) + r")\b", rest):
        target = _WEEKDAYS.index(m.group(1))
        ahead = (target - local_now.weekday()) % 7 or 7
        day = local_now + timedelta(days=ahead)
        rest = rest.replace(m.group(0), " ")

    for part, clock in _PARTS.items():
        if re.search(rf"\b(this |in the )?{part}\b", rest) and not re.search(r"\d", rest):
            rest = clock
            break

    hm = _clock(rest)
    if hm is None:
        if day is not None and not rest.strip(" at"):
            hm = (9, 0, True)  # "tomorrow" alone: the morning, said back to the user
        else:
            return None
    hour, minute, explicit = hm
    base = day or local_now
    when = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if day is None and when <= local_now:
        if not explicit and hour < 12 and when + timedelta(hours=12) > local_now:
            when += timedelta(hours=12)  # "at 6" at 15:00 means 18:00, not tomorrow 06:00
        else:
            when += timedelta(days=1)
    return when


def _relative(t: str) -> timedelta | None:
    if not (t.startswith(("in ", "after ")) or t.endswith(" from now")):
        return None
    body = re.sub(r"^(in|after) ", "", t).removesuffix(" from now").strip()
    if body in ("half an hour", "a half hour", "half hour"):
        return timedelta(minutes=30)
    total = 0.0
    last_unit = 0  # "an hour and a half": the half is of the unit just said
    for chunk in re.split(r"\band\b", body):
        chunk = chunk.strip()
        if chunk in ("a half", "half"):
            if not last_unit:
                return None
            total += 0.5 * last_unit
            continue
        mm = re.fullmatch(r"(\d+(?:\.\d+)?|[a-z -]+?)\s+([a-z]+)", chunk)
        if not mm or mm.group(2) not in _UNITS:
            return None
        qty_s = mm.group(1).strip()
        qty = float(qty_s) if qty_s[0].isdigit() else _NUMBERS.get(qty_s)
        if qty is None:
            return None
        last_unit = _UNITS[mm.group(2)]
        total += qty * last_unit
    return timedelta(seconds=total) if total > 0 else None


def _clock(t: str) -> tuple[int, int, bool] | None:
    m = re.search(r"(?:\bat\s+)?\b(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?(?!\d)", t)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    suffix = (m.group(3) or "").replace(".", "")
    if minute > 59 or hour > 23 or (suffix and not 1 <= hour <= 12):
        return None
    if suffix == "pm" and hour != 12:
        hour += 12
    elif suffix == "am" and hour == 12:
        hour = 0
    # "at 6" is ambiguous between morning and evening; "6pm", "18:00" and "0:30"
    # are not, and only the ambiguous kind may be moved twelve hours.
    explicit = bool(suffix) or hour >= 13 or hour == 0
    return hour, minute, explicit


def to_ts(when: datetime) -> str:
    """An aware datetime in :func:`jarvis.ids.now`'s exact shape, so it sorts."""
    t = when.astimezone(UTC)
    return f"{t.strftime('%Y-%m-%dT%H:%M:%S')}.{t.microsecond // 1000:03d}Z"


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-zçğıöşü0-9]+", text.lower()) if len(w) > 2} - _STOP


_STOP = frozenset({"the", "and", "for", "that", "this", "with", "about", "my", "what", "thing"})
