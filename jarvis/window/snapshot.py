"""What the window shows, read out of the rows other processes wrote. READ ONLY.

The window is a VIEWER. It is the one process in this tree whose whole job is
to look at everybody else's rows, so the first rule here is that looking never
moves anything: no ``reconcile.reconcile()`` (it reaps and re-routes), no
``bus.commit_cursor`` (the window is not a consumer and must not step over
events a real consumer has not handled), no ``presence.update_presence`` (it
publishes). Every function takes an open connection and issues SELECTs.

THE FEED IS A RENDERING OF THE ACTIVITY LOG, not a second log. ``events`` is
already the history of what was said and done; this module turns a window of it
into speech bubbles and one-line notices. Two rules shape it:

* An ALLOW-LIST of kinds. The log holds internal kinds (channel attaches, epoch
  bumps, delivery bookkeeping) that would be noise, and a kind nobody planned
  for must stay invisible rather than appear as a raw dotted name.
* Transcript FRAGMENTS become bubbles. The desk publishes what was heard and
  said in pieces as they stream in; consecutive pieces of one kind are one
  bubble until another SHOWN kind intervenes. Skipped kinds do not split a
  bubble, because the reader never sees them and a sentence broken in two by
  an invisible row reads as a glitch.

A section that fails to read is reported in ``errors`` and the rest of the
snapshot still renders: one malformed row in the spend table must not blank the
whole window.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jarvis import answers, ledger, liveness, memory, reconcile
from jarvis import hearing as hearing_
from jarvis import jobs as jobs_
from jarvis import presence as presence_
from jarvis import requests as rq
from jarvis.bus import last_seq
from jarvis.clock import day_month, local_tz
from jarvis.ids import now, parse_ts

__all__ = [
    "DEFAULT_FEED",
    "MAX_FEED",
    "MERGE_KINDS",
    "SHOWN_PROCESSES",
    "affects_snapshot",
    "changed_since",
    "desk_online",
    "feed",
    "feed_after",
    "fingerprint",
    "hearing",
    "jobs",
    "notes",
    "pending",
    "plain_sentence",
    "presence",
    "previous_kind",
    "processes",
    "projects",
    "recent",
    "reminders",
    "spend",
    "state",
]

#: The chips in the top bar. ``window`` is in :data:`jarvis.liveness.PROCESSES`
#: too, but a page that can render is its own proof that the window is up.
SHOWN_PROCESSES: tuple[str, ...] = ("desk", "schedule", "telegram")

#: Kinds whose consecutive fragments are one bubble, and whose bubble it is.
MERGE_KINDS: Mapping[str, str] = {
    "live.input_transcript": "user",
    "live.output_transcript": "jarvis",
}

DEFAULT_FEED = 200
MAX_FEED = 500

# Rows read per query, rows one feed call may look at in total, and how far back
# to look for the item a new fragment might continue. All three bound the work
# a single request can cause on a log that is kept forever (~110k rows a year).
_PAGE = 500
_SCAN_CAP = 5000
_LOOKBACK = 500

# One notice line, and one bubble. The feed renders whatever was said aloud;
# a pasted novel must not become a megabyte of DOM.
_MAX_LINE = 4000
_MAX_BUBBLE = 20000

_ROW_COLS = "seq, ts, kind, actor, payload, request_id"


# ───────────────────────────── the snapshot ─────────────────────────────


def state(
    con: sqlite3.Connection,
    *,
    tools: list[dict[str, Any]],
    wake_word: str,
    wake_threshold: float,
    spend_threshold_usd: float,
    tz: str,
    chat_available: bool,
    chat_why: str,
    speech_available: bool,
    speech_why: str,
    app: bool = False,
    now_ts: str | None = None,
) -> dict[str, Any]:
    """Everything ``GET /api/state`` returns, in the contract's shape.

    ``app`` says whether this window runs inside the Jarvis app, which starts
    and restarts the other processes itself. The page branches on it: inside
    the app it never tells anybody to type a command.
    """
    ts = now_ts or now()
    errors: list[str] = []

    def guarded(name: str, read: Callable[[], Any], fallback: Any) -> Any:
        try:
            return read()
        except Exception as exc:  # noqa: BLE001 - one bad section must not blank the window
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
            return fallback

    procs = guarded("processes", lambda: processes(con, now_ts=ts), _offline_processes())
    via = ("desk" if procs["desk"]["online"] else "local") if speech_available else None
    return {
        "now": ts,
        "last_seq": last_seq(con),
        "processes": procs,
        "presence": guarded(
            "presence", lambda: presence(con, now_ts=ts), {"state": "unknown", "reason": ""}
        ),
        "spend": guarded(
            "spend", lambda: spend(con, threshold_usd=spend_threshold_usd), {"line": ""}
        ),
        "projects": guarded("projects", lambda: projects(con, now_ts=ts), {"lines": []}),
        "jobs": guarded("jobs", lambda: jobs(con), []),
        "pending": guarded("pending", lambda: pending(con), []),
        "reminders": guarded("reminders", lambda: reminders(con, tz=tz, now_ts=ts), []),
        "notes": guarded("notes", lambda: notes(con), []),
        "hearing": guarded("hearing", lambda: hearing(con), []),
        "wake": {"word": wake_word, "threshold": wake_threshold},
        "chat": {"available": chat_available, "why": "" if chat_available else chat_why},
        "speech": {
            "available": speech_available,
            "via": via,
            "why": "" if speech_available else speech_why,
        },
        "tools": tools,
        "app": bool(app),
        "errors": errors,
    }


def processes(con: sqlite3.Connection, *, now_ts: str | None = None) -> dict[str, dict[str, Any]]:
    """The top bar's chips. ``online`` is false for a stale beat AND for a goodbye."""
    beats = liveness.read_all(con, now_ts=now_ts)
    out = _offline_processes()
    for name in SHOWN_PROCESSES:
        b = beats.get(name)
        if b is not None:
            out[name] = {
                "online": b.state != liveness.OFFLINE,
                "state": b.state,
                "since": b.since,
                "age_s": round(b.age_s, 1),
            }
    return out


def desk_online(con: sqlite3.Connection, *, now_ts: str | None = None) -> bool:
    """Whether "say this" would go through the desk. The SAME rule ``speak`` uses."""
    b = liveness.read(con, "desk", now_ts=now_ts)
    return b is not None and b.state != liveness.OFFLINE


def presence(con: sqlite3.Connection, *, now_ts: str | None = None) -> dict[str, Any]:
    """The live verdict. ``evaluate_presence`` reads two tables and writes nothing."""
    v = presence_.evaluate_presence(con, now_ts)
    return {"state": v.state, "reason": v.reason, "reachable": list(v.reachable)}


def spend(con: sqlite3.Connection, *, threshold_usd: float) -> dict[str, str]:
    """The ledger's own sentence, which names every meter it could not price."""
    config = ledger.LedgerConfig(threshold_usd=threshold_usd)
    return {"line": ledger.spoken_status(ledger.status(con, "today", config=config))}


def projects(con: sqlite3.Connection, *, now_ts: str | None = None) -> dict[str, list[str]]:
    """``project_status`` READS. ``reconcile()`` would reap jobs — never from here."""
    return {"lines": list(reconcile.project_status(con, now_ts=now_ts).lines)}


def jobs(con: sqlite3.Connection, *, limit: int = 20) -> list[dict[str, Any]]:
    """The most recently changed jobs, whatever their state, newest first."""
    rows = con.execute(
        "SELECT * FROM jobs ORDER BY updated_at DESC, rowid DESC LIMIT ?", (limit,)
    ).fetchall()
    return [
        {"id": j.id, "title": j.title, "state": j.state, "kind": j.kind, "updated_at": j.updated_at}
        for j in (jobs_.to_job(r) for r in rows)
    ]


def pending(con: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every question waiting on a human, numbered exactly as every channel numbers it.

    The options are the presentation's frozen array, copied through: the window
    sends back INDICES, and :func:`jarvis.answers.build_answer` turns them into
    labels by local lookup, the same as the CLI and Telegram do.
    """
    out: list[dict[str, Any]] = []
    for req in rq.open_requests(con):
        pres = req.presentation
        options = [
            {
                "index": int(item["index"]),
                "label": str(item["label"]),
                "description": str(item.get("description") or ""),
            }
            for item in pres.get("items", [])
        ]
        entry: dict[str, Any] = {
            "id": req.id,
            "kind": req.kind,
            "short_label": req.short_label,
            "intro": str(pres.get("intro") or ""),
            "question": str(pres.get("question") or ""),
            "multi": bool(pres.get("multi")),
            "allows_free_text": bool(pres.get("allows_free_text")),
            "free_text_prompt": str(pres.get("free_text_prompt") or ""),
            "options": options,
            "created_at": req.created_at,
            "expires_at": req.expires_at,
        }
        if req.kind == "plan_question":
            _label_questions(entry, req.payload)
        out.append(entry)
    return out


def _label_questions(entry: dict[str, Any], payload: dict[str, Any]) -> None:
    """Tag each option with the question it answers, for a multi-question batch.

    The numbering runs across the whole batch (see :mod:`jarvis.answers`), so a
    screen that shows two questions needs to know where one ends; the indices
    themselves are untouched.
    """
    try:
        slots = answers.slots(payload)
    except answers.MalformedQuestions:
        return
    owner = {s.index: s.question for s in slots}
    for option in entry["options"]:
        option["question"] = owner.get(option["index"], "")
    entry["questions"] = list(dict.fromkeys(s.question for s in slots))


def reminders(
    con: sqlite3.Connection, *, tz: str, now_ts: str | None = None
) -> list[dict[str, Any]]:
    """Pending reminders with the time said the way the reminder tool says it back."""
    zone = _zone(tz)
    today = parse_ts(now_ts or now()).astimezone(zone)
    return [
        {"id": r.id, "text": r.text, "due_at": r.due_at, "due_local": _when(r.due_at, zone, today)}
        for r in memory.pending_reminders(con)
    ]


def notes(con: sqlite3.Connection, *, limit: int = 50) -> list[dict[str, Any]]:
    return [
        {"id": n.id, "text": n.text, "created_at": n.created_at}
        for n in memory.notes(con, limit=limit)
    ]


def hearing(con: sqlite3.Connection) -> list[dict[str, Any]]:
    """The words Jarvis corrects, seeds and taught alike. ``lexicon`` only reads."""
    return [
        {"term": e.term, "heard_as": list(e.heard_as), "taught": e.taught}
        for e in hearing_.lexicon(con).entries
    ]


def fingerprint(con: sqlite3.Connection) -> tuple[Any, ...]:
    """A cheap digest of the tables the snapshot shows, for changes no event announces.

    ``answer_request`` writes no event, and neither does the CLI's ``answer``:
    without this a question answered at the terminal would sit in the window's
    Questions tab until something else happened to publish.
    """
    return tuple(con.execute(_FINGERPRINT).fetchone())


_FINGERPRINT = """SELECT
  (SELECT COUNT(*) FROM requests WHERE state='pending'),
  (SELECT COALESCE(MAX(rowid), 0) FROM requests),
  (SELECT COUNT(*) FROM reminders WHERE state='pending'),
  (SELECT COALESCE(MAX(updated_at), '') FROM reminders),
  (SELECT COUNT(*) FROM notes WHERE forgotten_at IS NULL),
  (SELECT COALESCE(MAX(rowid), 0) FROM notes),
  (SELECT COALESCE(MAX(updated_at), '') FROM jobs),
  (SELECT COALESCE(MAX(updated_at), '') FROM lexicon)"""


def affects_snapshot(kind: str) -> bool:
    """Could an event of this kind change what ``/api/state`` returns?

    Transcript fragments arrive four times a second while anybody is talking;
    re-reading the whole snapshot for each one would cost a ledger sum and a
    project status per syllable and change nothing on screen.
    """
    return not kind.startswith(("live.", "audio.", "speech."))


def changed_since(con: sqlite3.Connection, after: int, top: int) -> bool:
    """Did any event with ``after < seq <= top`` change what ``/api/state`` returns?

    The distinct KINDS in the range, judged by :func:`affects_snapshot`, so the
    rule lives in one place and no payload is parsed. A gap wider than the scan
    cap is answered yes without reading it: after a laptop sleep, one spare
    snapshot is cheaper than a DISTINCT over a day of transcript fragments.
    """
    if top <= after:
        return False
    if top - after > _SCAN_CAP:
        return True
    rows = con.execute(
        "SELECT DISTINCT kind FROM events WHERE seq > ? AND seq <= ?", (after, top)
    ).fetchall()
    return any(affects_snapshot(str(r[0])) for r in rows)


# ───────────────────────────── the feed ─────────────────────────────


def feed(
    con: sqlite3.Connection, *, after: int | None = None, limit: int = DEFAULT_FEED
) -> dict[str, Any]:
    """``GET /api/feed``: the newest ``limit`` items, or everything after ``after``."""
    if after is None:
        items, top = recent(con, limit=limit)
        return {"items": items, "last_seq": top}
    items, last, _ = feed_after(con, after, limit=limit, prev_kind=previous_kind(con, after))
    return {"items": items, "last_seq": last}


def recent(
    con: sqlite3.Connection, *, limit: int = DEFAULT_FEED, upto: int | None = None
) -> tuple[list[dict[str, Any]], int]:
    """The newest ``limit`` items, oldest first, each starting a bubble of its own.

    Read backwards, so a bubble whose head is older than the page is followed
    back to its head rather than shown from the middle of a sentence. Every
    returned item therefore has ``merge`` false: there is nothing on the page
    before the first one for it to continue.
    """
    limit = _clamp(limit)
    top = last_seq(con) if upto is None else upto
    newest_first: list[dict[str, Any]] = []
    cursor = top + 1
    scanned = 0
    while scanned < _SCAN_CAP:
        rows = con.execute(
            f"SELECT {_ROW_COLS} FROM events WHERE seq < ? ORDER BY seq DESC LIMIT ?",
            (cursor, _PAGE),
        ).fetchall()
        for row in rows:
            scanned += 1
            cursor = int(row["seq"])
            shown = _render(con, row)
            if shown is None:
                continue
            kind = str(row["kind"])
            if newest_first and _continues(newest_first[-1], kind):
                _prepend(newest_first[-1], shown[1], row)
                continue
            if len(newest_first) >= limit:
                return list(reversed(newest_first)), top
            newest_first.append(_item(row, shown, merge=False))
        if len(rows) < _PAGE:
            break
    return list(reversed(newest_first)), top


def feed_after(
    con: sqlite3.Connection,
    after: int,
    *,
    limit: int = DEFAULT_FEED,
    prev_kind: str | None = None,
    upto: int | None = None,
) -> tuple[list[dict[str, Any]], int, str | None]:
    """Items after ``after``, the seq to ask from next time, and the last shown kind.

    ``prev_kind`` is the kind of the last item the CLIENT already has, which is
    what decides whether the first fragment here continues its bubble. A
    long-lived stream carries it from call to call instead of re-deriving it.

    The returned seq covers skipped rows too, so a client never re-reads a
    stretch of internal events it was shown nothing from. It stops short of the
    end only when ``limit`` items or the scan cap were reached first.
    """
    limit = _clamp(limit)
    top = last_seq(con) if upto is None else upto
    if after >= top:
        # after > top means the client's seq is from a different database (a
        # reset, a restore); answering with this one's top lets it resync.
        return [], top, prev_kind
    items: list[dict[str, Any]] = []
    cursor = after
    scanned = 0
    while scanned < _SCAN_CAP:
        rows = con.execute(
            f"SELECT {_ROW_COLS} FROM events WHERE seq > ? AND seq <= ? ORDER BY seq LIMIT ?",
            (cursor, top, _PAGE),
        ).fetchall()
        for row in rows:
            shown = _render(con, row)
            if shown is not None:
                kind = str(row["kind"])
                if items and _continues(items[-1], kind):
                    _append(items[-1], shown[1])
                elif len(items) >= limit:
                    return items, cursor, prev_kind
                else:
                    merge = kind in MERGE_KINDS and prev_kind == kind
                    items.append(_item(row, shown, merge=merge))
                prev_kind = kind
            scanned += 1
            cursor = int(row["seq"])
        if len(rows) < _PAGE:
            return items, top, prev_kind
    return items, cursor, prev_kind


def previous_kind(con: sqlite3.Connection, seq: int) -> str | None:
    """The kind of the last SHOWN event at or before ``seq``, within a bounded look back."""
    rows = con.execute(
        f"SELECT {_ROW_COLS} FROM events WHERE seq <= ? ORDER BY seq DESC LIMIT ?",
        (seq, _LOOKBACK),
    ).fetchall()
    for row in rows:
        if _render(con, row) is not None:
            return str(row["kind"])
    return None


def _clamp(limit: int) -> int:
    return max(1, min(int(limit), MAX_FEED))


def _continues(item: dict[str, Any], kind: str) -> bool:
    return kind in MERGE_KINDS and item["kind"] == kind


def _item(row: sqlite3.Row, shown: tuple[str, str], *, merge: bool) -> dict[str, Any]:
    role, text = shown
    return {
        "seq": int(row["seq"]),
        "ts": str(row["ts"]),
        "role": role,
        "text": text,
        "kind": str(row["kind"]),
        "merge": merge,
    }


def _append(item: dict[str, Any], text: str) -> None:
    joined = item["text"] + text
    item["text"] = joined if len(joined) <= _MAX_BUBBLE else "…" + joined[-_MAX_BUBBLE:]


def _prepend(item: dict[str, Any], text: str, row: sqlite3.Row) -> None:
    # The bubble is named after its FIRST fragment, whichever direction it was
    # assembled in, so the same bubble has the same seq on every page.
    joined = text + item["text"]
    item["text"] = joined if len(joined) <= _MAX_BUBBLE else "…" + joined[-_MAX_BUBBLE:]
    item["seq"] = int(row["seq"])
    item["ts"] = str(row["ts"])


# ───────────────────────────── one row -> one line ─────────────────────────────

Shown = tuple[str, str] | None
_Renderer = Callable[[dict[str, Any], sqlite3.Row, sqlite3.Connection], Shown]


def _render(con: sqlite3.Connection, row: sqlite3.Row) -> Shown:
    """``(role, text)`` for an allow-listed kind, or None. Never raises on a bad row.

    These rows were written by five processes over months. A payload that is
    not an object, a field of the wrong type or a nulled detail costs that one
    line, never the feed.
    """
    renderer = _RENDERERS.get(str(row["kind"]))
    if renderer is None:
        return None
    try:
        body = json.loads(row["payload"])
    except (TypeError, ValueError):
        return None
    if not isinstance(body, dict) or body.get("_jarvis_detail_nulled"):
        return None
    try:
        shown = renderer(body, row, con)
    except (sqlite3.Error, TypeError, ValueError, KeyError):
        return None
    if shown is None:
        return None
    role, text = shown
    if row["kind"] in MERGE_KINDS:
        return (role, text) if text else None
    text = text.strip()
    if not text:
        return None
    return role, text if len(text) <= _MAX_LINE else text[: _MAX_LINE - 1] + "…"


def _s(body: Mapping[str, Any], key: str) -> str:
    v = body.get(key)
    return v if isinstance(v, str) else ""


def _fragment(role: str) -> _Renderer:
    # NOT stripped: fragments carry their own spacing, and " world" after
    # "Hello" is how the words stay apart once they are joined.
    return lambda body, row, con: (role, _s(body, "text"))


def _said(role: str) -> _Renderer:
    return lambda body, row, con: (role, _s(body, "text"))


def _notice(role: str, text: str) -> _Renderer:
    return lambda body, row, con: (role, text)


def _speech(body: Mapping[str, Any], row: sqlite3.Row, con: sqlite3.Connection) -> Shown:
    # Only the reader's voice. What Gemini's own voice says already arrives as
    # live.output_transcript, and showing both would print every reply twice.
    if body.get("track") != "verbatim" or not body.get("spoken"):
        return None
    return "jarvis", _s(body, "text")


def _tool_used(body: Mapping[str, Any], row: sqlite3.Row, con: sqlite3.Connection) -> Shown:
    tool = _s(body, "tool")
    return ("tool", f"used {tool}") if tool else None


def _tool_denied(body: Mapping[str, Any], row: sqlite3.Row, con: sqlite3.Connection) -> Shown:
    tool = _s(body, "tool") or "a tool"
    why = _s(body, "why") or _s(body, "error")
    return "tool", f"{tool}: {why}" if why else f"{tool} was refused"


def _tool_failed(body: Mapping[str, Any], row: sqlite3.Row, con: sqlite3.Connection) -> Shown:
    name = _s(body, "name") or "a tool"
    error = _s(body, "error")
    return "tool", f"{name} failed ({error})" if error else f"{name} failed"


def _connected(body: Mapping[str, Any], row: sqlite3.Row, con: sqlite3.Connection) -> Shown:
    return "system", "reconnected to Gemini Live" if body.get("resumed") else "voice connected"


def _disconnected(body: Mapping[str, Any], row: sqlite3.Row, con: sqlite3.Connection) -> Shown:
    reason = _s(body, "reason")
    return "system", "voice disconnected" + (f" ({reason})" if reason else "")


def _connect_failed(body: Mapping[str, Any], row: sqlite3.Row, con: sqlite3.Connection) -> Shown:
    attempt = body.get("attempt")
    tail = f" (attempt {attempt})" if isinstance(attempt, int) else ""
    return "system", f"couldn't reach Gemini Live{tail}"


def _cut_in(body: Mapping[str, Any], row: sqlite3.Row, con: sqlite3.Connection) -> Shown:
    # Every utterance starts an activity; only a barge-in is worth a line.
    return ("system", "you cut in") if body.get("barge_in") is True else None


def _label(con: sqlite3.Connection, body: Mapping[str, Any], row: sqlite3.Row) -> str:
    """A request's short label: from the payload, else from its own row."""
    label = _s(body, "short_label")
    if label or row["request_id"] is None:
        return label
    hit = con.execute(
        "SELECT short_label FROM requests WHERE id=?", (row["request_id"],)
    ).fetchone()
    return str(hit[0]) if hit is not None else ""


def _request(verb: str) -> _Renderer:
    def render(body: Mapping[str, Any], row: sqlite3.Row, con: sqlite3.Connection) -> Shown:
        label = _label(con, body, row)
        channel = _s(body, "channel")
        where = f" on {channel}" if channel and verb == "answered" else ""
        return "system", f"{verb}{where}: {label}" if label else f"{verb}{where}"

    return render


def _reminder(body: Mapping[str, Any], row: sqlite3.Row, con: sqlite3.Connection) -> Shown:
    # The scheduler's payload carries the id, not the words; the words are on
    # the reminder's own row, which outlives the firing.
    text = _s(body, "text")
    rid = _s(body, "reminder_id")
    if not text and rid:
        hit = con.execute("SELECT text FROM reminders WHERE id=?", (rid,)).fetchone()
        text = str(hit[0]) if hit is not None else ""
    return "system", f"reminder: {text}" if text else "reminder"


def _project_requested(body: Mapping[str, Any], row: sqlite3.Row, con: sqlite3.Connection) -> Shown:
    title = _s(body, "title")
    return ("system", f"build requested: {title}") if title else None


def _project_started(body: Mapping[str, Any], row: sqlite3.Row, con: sqlite3.Connection) -> Shown:
    where = _s(body, "repo") or _s(body, "cwd")
    return "system", f"build started in {where}" if where else "build started"


def _job(phrase: str, *, only_to: str | None = None) -> _Renderer:
    def render(body: Mapping[str, Any], row: sqlite3.Row, con: sqlite3.Connection) -> Shown:
        # job.started is published for both 'starting' and 'running'; one line
        # per build is what a person wants, so only one of them is shown.
        if only_to is not None and body.get("to") not in (only_to, None):
            return None
        title = _s(body, "title")
        if not title:
            return None
        reason = _s(body, "reason")
        return "system", f"{title} {phrase}" + (f": {reason}" if reason else "")

    return render


#: How the feed names a process the app supervises.
_PROCESS_WORDS: Mapping[str, str] = {
    "desk": "desk",
    "schedule": "scheduler",
    "telegram": "Telegram",
}


def _desk_refused(body: Mapping[str, Any], row: sqlite3.Row, con: sqlite3.Connection) -> Shown:
    why = plain_sentence(_s(body, "sentence"))
    return "system", f"desk couldn't start: {why}" if why else "desk couldn't start"


def _process_exited(body: Mapping[str, Any], row: sqlite3.Row, con: sqlite3.Connection) -> Shown:
    name = _s(body, "process")
    if not name:
        return None
    who = _PROCESS_WORDS.get(name, name)
    code = body.get("code")
    why = plain_sentence(_s(body, "reason"))
    if code == 0:
        head = f"{who} stopped"
    elif code == 2:
        # Exit 2 is a refusal: the process said why and waits to be fixed.
        head = f"{who} is waiting for you"
    else:
        head = f"{who} stopped unexpectedly" + (f" (exit {code})" if isinstance(code, int) else "")
    return "system", f"{head}: {why}" if why else head


def _command(body: Mapping[str, Any], row: sqlite3.Row, con: sqlite3.Connection) -> Shown:
    if body.get("verb") != "stop_all":
        return None
    origin = _s(body, "origin")
    return "system", f"STOP — everything was told to stop ({origin})" if origin else "STOP"


_RENDERERS: Mapping[str, _Renderer] = {
    "live.input_transcript": _fragment("user"),
    "live.output_transcript": _fragment("jarvis"),
    "window.said": _said("user"),
    "window.reply": _said("jarvis"),
    "window.error": _said("system"),
    "speech.said": _speech,
    "tool.used": _tool_used,
    "tool.denied": _tool_denied,
    "live.tool_failed": _tool_failed,
    "live.connected": _connected,
    "live.disconnected": _disconnected,
    "live.connect_failed": _connect_failed,
    "audio.wake.awake": _notice("system", "awake"),
    "audio.wake.asleep": _notice("system", "asleep"),
    "audio.activity_start": _cut_in,
    "reminder.fired": _reminder,
    "request.created": _request("question"),
    "request.answered": _request("answered"),
    "request.expired": _request("question expired"),
    "project.requested": _project_requested,
    "project.started": _project_started,
    "job.started": _job("started", only_to="running"),
    "job.finished": _job("finished"),
    "job.failed": _job("failed"),
    "job.killed": _job("was stopped"),
    "job.blocked": _job("is waiting for you"),
    "job.deferred": _job("is parked until you answer"),
    "command.issued": _command,
    "desk.refused": _desk_refused,
    "app.process_exited": _process_exited,
    "app.started": _notice("system", "Jarvis is online"),
}


# ───────────────────────────── sentences without commands ─────────────────────────────

# What makes a fragment an instruction to a terminal: a quoted command, or an
# interpreter or package manager invoked. The window inside the app has a
# button for every one of those fixes.
_COMMANDISH = re.compile(
    r"`[^`]*`|\bpython3?(?:\.exe)?\s+-[mc]\b|\bpy\s+-m\b|\bpip\s+install\b"
    r"|\buv\s+(?:pip|venv|run)\b|\bsudo\b|\bapt(?:-get)?\s+install\b|\bbrew\s+install\b"
    r"|\.venv\b|\bexport\s+[A-Z_]+=|\bjarvis\s+(?:secrets|wake|desk|window|doctor)\b",
    re.I,
)
_PARENS = re.compile(r"\s*\([^()]*\)")
_COMMAND_TAIL = re.compile(
    r"\s*(?:[:;\u2014\u2013]|\s-)\s*(?:run|try|use|start it with|store it with|with|or)?\s*:?\s*"
    r"`?(?:python3?|py|pip|uv|sudo|apt)\b.*$",
    re.I,
)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
# A sentence cut at its command and left ending on one of these was ALL
# instruction ("Store it with: python -m ..."), and its stump says nothing.
_STUMP = re.compile(r"\b(?:with|run|try|use|using|via|by|to|set|it|is)$", re.I)


def plain_sentence(text: str, fallback: str = "") -> str:
    """The headline of a refusal, with every terminal instruction taken out.

    Refusals in this tree end with the command that fixes them, which is right
    in a terminal and wrong in a window whose user has never opened one. This
    keeps the first line that says something once its commands are gone —
    "Chat needs the Gemini key: python -m ..." becomes "Chat needs the Gemini
    key." — and returns ``fallback`` when nothing is left.
    """
    if not isinstance(text, str):
        return fallback
    kept: list[str] = []
    for raw in text.splitlines():
        line = _PARENS.sub(lambda m: "" if _COMMANDISH.search(m.group()) else m.group(), raw)
        for sentence in _SENTENCE_END.split(line.strip()):
            cut = _COMMAND_TAIL.sub("", sentence).strip()
            if cut != sentence.strip() and (kept or _STUMP.search(cut)):
                continue  # past the headline, a sentence that led to a command WAS the command
            if cut and not _COMMANDISH.search(cut):
                kept.append(cut)
        if kept:
            break  # the first line that says something is the headline
    out = " ".join(kept).strip(" ,;:")
    if not out:
        return fallback
    first = out.split(" ", 1)[0]
    if first.isalpha() and first.islower():
        out = out[0].upper() + out[1:]
    return out if out[-1] in ".!?…" else out + "."


# ───────────────────────────── time, said back ─────────────────────────────


def _zone(name: str) -> ZoneInfo:
    if isinstance(name, str) and name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return local_tz()


def _when(ts: str, zone: ZoneInfo, today: datetime) -> str:
    """'18:00 today', '09:00 tomorrow', '10:00 on Friday 9 October' — what the tool says."""
    local = parse_ts(ts).astimezone(zone)
    days = (local.date() - today.date()).days
    clock = local.strftime("%H:%M")
    if days == 0:
        return f"{clock} today"
    if days == 1:
        return f"{clock} tomorrow"
    return f"{clock} on {day_month(local)}"


def _offline_processes() -> dict[str, dict[str, Any]]:
    return {
        name: {"online": False, "state": None, "since": None, "age_s": None}
        for name in SHOWN_PROCESSES
    }
