"""What the window shows, read from real rows written by the real writers.

Every section is driven by the module that owns its table (``create_request``,
``memory``, ``hearing.teach``, ``jobs``, ``bus.publish``, ``liveness``), never by
hand-written INSERTs, so a writer that changes its shape breaks this test rather
than the window. The failures worth a test: a section that writes, a feed that
shows internal kinds or splits a sentence in two, a bad row that blanks the
page, and a continuation that forgets which bubble it was in.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from jarvis import answers, hearing, jobs, ledger, liveness, memory, reconcile
from jarvis import presence as presence_
from jarvis import requests as rq
from jarvis.bus import last_seq, publish
from jarvis.db import connect, migrate
from jarvis.ids import now
from jarvis.window import snapshot

IST = ZoneInfo("Europe/Istanbul")
#: 12:00 in Istanbul on Monday 5 October 2026.
NOON_IST = "2026-10-05T09:00:00.000Z"

QUESTIONS = {
    "questions": [
        {
            "question": "Which database should the app use?",
            "header": "Database",
            "multiSelect": False,
            "options": [
                {"label": "SQLite", "description": "one file, no server"},
                {"label": "Postgres", "description": "a server to run"},
            ],
        }
    ]
}


@pytest.fixture
def con(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "j.db")
    migrate(c)
    yield c
    c.close()


def _ask(con: sqlite3.Connection, payload: dict = QUESTIONS) -> rq.Request:
    return rq.create_request(
        con,
        kind="plan_question",
        short_label=answers.short_label(payload),
        presentation=answers.presentation(payload),
        payload=payload,
        actor="test",
        tool_name="AskUserQuestion",
    )


def _frag(con: sqlite3.Connection, kind: str, text: str) -> int:
    publish(con, kind, "desk", {"at": now(), "text": text})
    return last_seq(con)


def _state(con: sqlite3.Connection, **kw) -> dict:
    args = {
        "tools": [{"name": "weather", "description": "d", "parameters": {"type": "OBJECT"}}],
        "wake_word": "hey jarvis",
        "wake_threshold": 0.5,
        "spend_threshold_usd": 20.0,
        "tz": "Europe/Istanbul",
        "chat_available": True,
        "chat_why": "",
        "speech_available": True,
        "speech_why": "",
    }
    args.update(kw)
    return snapshot.state(con, **args)


# ───────────────────────────── the snapshot ─────────────────────────────


CONTRACT_KEYS = {
    "now",
    "last_seq",
    "processes",
    "presence",
    "spend",
    "projects",
    "jobs",
    "pending",
    "reminders",
    "notes",
    "hearing",
    "wake",
    "chat",
    "speech",
    "tools",
}


def test_state_has_every_key_the_contract_names(con: sqlite3.Connection) -> None:
    s = _state(con)
    assert set(s) >= CONTRACT_KEYS
    assert s["errors"] == []
    assert set(s["processes"]) == {"desk", "schedule", "telegram"}
    assert s["wake"] == {"word": "hey jarvis", "threshold": 0.5}
    assert s["chat"] == {"available": True, "why": ""}
    assert s["tools"][0]["name"] == "weather"
    assert isinstance(s["spend"]["line"], str) and s["spend"]["line"]
    assert {"state", "reason"} <= set(s["presence"])
    assert isinstance(s["projects"]["lines"], list)


def test_state_reads_and_never_writes(con: sqlite3.Connection, monkeypatch) -> None:
    def forbidden(*a, **k):
        raise AssertionError("the window called reconcile(), which reaps and re-routes")

    monkeypatch.setattr(reconcile, "reconcile", forbidden)
    _ask(con)
    memory.remember(con, "locker 214", actor="test", channel="cli")
    _frag(con, "live.input_transcript", "hello")
    before = con.total_changes
    _state(con)
    snapshot.feed(con)
    snapshot.feed(con, after=1)
    assert con.total_changes == before


def test_processes_online_stale_and_goodbye(con: sqlite3.Connection) -> None:
    liveness.beat(con, "desk", state="listening", now_ts="2026-10-05T09:00:00.000Z")
    liveness.beat(con, "schedule", state="running", now_ts="2026-10-05T08:00:00.000Z")
    liveness.beat(con, "telegram", state="polling", now_ts="2026-10-05T09:00:00.000Z")
    liveness.gone(con, "telegram", now_ts="2026-10-05T09:00:00.500Z")
    p = snapshot.processes(con, now_ts="2026-10-05T09:00:01.000Z")
    assert p["desk"] == {
        "online": True,
        "state": "listening",
        "since": "2026-10-05T09:00:00.000Z",
        "age_s": 1.0,
    }
    # An hour-old beat is a crash: shown exactly as if it never beat.
    assert p["schedule"] == {"online": False, "state": None, "since": None, "age_s": None}
    # A goodbye is shown as a goodbye, at once.
    assert p["telegram"]["online"] is False
    assert p["telegram"]["state"] == "offline"


def test_speech_via_follows_the_desk_and_is_null_when_unavailable(
    con: sqlite3.Connection,
) -> None:
    assert _state(con)["speech"] == {"available": True, "via": "local", "why": ""}
    liveness.beat(con, "desk", state="asleep")
    assert _state(con)["speech"]["via"] == "desk"
    assert snapshot.desk_online(con)
    liveness.gone(con, "desk")
    assert _state(con)["speech"]["via"] == "local"
    assert not snapshot.desk_online(con)
    off = _state(con, speech_available=False, speech_why="no reader voice installed")
    assert off["speech"] == {"available": False, "via": None, "why": "no reader voice installed"}
    nochat = _state(con, chat_available=False, chat_why="no Gemini key")
    assert nochat["chat"] == {"available": False, "why": "no Gemini key"}


def test_presence_is_the_live_verdict_and_writes_nothing(con: sqlite3.Connection) -> None:
    presence_.note_heard(con, "utterance", text="hey jarvis")
    before = con.total_changes
    p = snapshot.presence(con)
    assert con.total_changes == before
    want = presence_.evaluate_presence(con)
    assert p == {"state": want.state, "reason": want.reason, "reachable": list(want.reachable)}
    assert p["state"] == "present"
    assert "desk" in p["reachable"]


def test_spend_is_the_ledgers_own_sentence(con: sqlite3.Connection) -> None:
    line = snapshot.spend(con, threshold_usd=20.0)["line"]
    want = ledger.spoken_status(
        ledger.status(con, "today", config=ledger.LedgerConfig(threshold_usd=20.0))
    )
    assert line == want


def test_pending_carries_the_frozen_numbering(con: sqlite3.Connection) -> None:
    req = _ask(con)
    [p] = snapshot.pending(con)
    assert p["id"] == req.id
    assert p["kind"] == "plan_question"
    assert p["short_label"] == "database"
    assert p["question"] == "Which database should the app use?"
    assert p["intro"] == "Which database should the app use?"
    assert p["multi"] is False
    assert p["allows_free_text"] is True
    assert p["free_text_prompt"]
    assert [(o["index"], o["label"], o["description"]) for o in p["options"]] == [
        (1, "SQLite", "one file, no server"),
        (2, "Postgres", "a server to run"),
    ]
    assert p["options"][0]["question"] == "Which database should the app use?"
    assert p["created_at"] == req.created_at
    assert p["expires_at"] is None
    # Answered questions leave the list.
    rq.answer_request(
        con, req.id, answers.build_answer(req, picks=(1,)), answered_by="test", answer_mode="hud"
    )
    assert snapshot.pending(con) == []


def test_jobs_newest_first(con: sqlite3.Connection) -> None:
    a = jobs.create_job(con, kind="claude_code", title="the todo app", created_by="test")
    b = jobs.create_job(con, kind="claude_code", title="the weather widget", created_by="test")
    rows = snapshot.jobs(con)
    assert [r["id"] for r in rows] == [b.id, a.id]
    assert {r["title"] for r in rows} == {"the todo app", "the weather widget"}
    assert set(rows[0]) == {"id", "title", "state", "kind", "updated_at"}
    assert rows[0]["state"] == "queued"


def test_reminders_say_the_time_the_way_the_tool_does(con: sqlite3.Connection) -> None:
    memory.add_reminder(
        con, "call mum", datetime(2026, 10, 5, 18, 0, tzinfo=IST), actor="t", channel="cli"
    )
    memory.add_reminder(
        con, "bins out", datetime(2026, 10, 6, 9, 0, tzinfo=IST), actor="t", channel="cli"
    )
    memory.add_reminder(
        con, "dentist", datetime(2026, 10, 9, 10, 0, tzinfo=IST), actor="t", channel="cli"
    )
    rows = snapshot.reminders(con, tz="Europe/Istanbul", now_ts=NOON_IST)
    assert [(r["text"], r["due_local"]) for r in rows] == [
        ("call mum", "18:00 today"),
        ("bins out", "09:00 tomorrow"),
        ("dentist", "10:00 on Friday 9 October"),
    ]
    assert rows[0]["due_at"] == "2026-10-05T15:00:00.000Z"
    assert rows[0]["id"].startswith("rem")


def test_notes_newest_first_and_forgotten_ones_gone(con: sqlite3.Connection) -> None:
    memory.remember(con, "locker is 214", actor="t", channel="cli")
    memory.remember(con, "coffee black", actor="t", channel="cli")
    assert [n["text"] for n in snapshot.notes(con)] == ["coffee black", "locker is 214"]
    memory.forget(con, "locker")
    assert [n["text"] for n in snapshot.notes(con)] == ["coffee black"]
    assert set(snapshot.notes(con)[0]) == {"id", "text", "created_at"}


def test_hearing_lists_seeds_and_what_was_taught(con: sqlite3.Connection) -> None:
    seeds = {e["term"]: e for e in snapshot.hearing(con)}
    assert "quote" in seeds and "coat" in seeds["quote"]["heard_as"]
    assert seeds["quote"]["taught"] is False
    hearing.teach(con, "sequel", "sql", actor="test")
    taught = {e["term"]: e for e in snapshot.hearing(con)}
    assert taught["sql"]["heard_as"] == ["sequel"]
    assert taught["sql"]["taught"] is True


def test_projects_reads_project_status(con: sqlite3.Connection) -> None:
    j = jobs.create_job(con, kind="claude_code", title="the todo app", created_by="test")
    for state in ("starting", "running", "finishing", "done"):
        jobs.set_state(con, j.id, state, actor="test")
    lines = snapshot.projects(con)["lines"]
    assert any("todo app" in line for line in lines)


def test_one_failing_section_costs_only_itself(con: sqlite3.Connection, monkeypatch) -> None:
    memory.remember(con, "still here", actor="t", channel="cli")

    def broken(*a, **k):
        raise sqlite3.OperationalError("no such table: spend")

    monkeypatch.setattr(snapshot, "spend", broken)
    monkeypatch.setattr(snapshot, "hearing", broken)
    s = _state(con)
    assert s["spend"] == {"line": ""}
    assert s["hearing"] == []
    assert [n["text"] for n in s["notes"]] == ["still here"]
    assert len(s["errors"]) == 2
    assert any(e.startswith("spend: OperationalError") for e in s["errors"])


def test_a_broken_beat_section_falls_back_to_all_offline(
    con: sqlite3.Connection, monkeypatch
) -> None:
    monkeypatch.setattr(snapshot, "processes", lambda *a, **k: 1 / 0)
    s = _state(con)
    assert s["processes"]["desk"]["online"] is False
    assert s["speech"]["via"] == "local"
    assert s["errors"] and s["errors"][0].startswith("processes: ZeroDivisionError")


# ───────────────────────────── change detection ─────────────────────────────


def test_affects_snapshot_ignores_transcripts_and_audio() -> None:
    assert not snapshot.affects_snapshot("live.input_transcript")
    assert not snapshot.affects_snapshot("audio.wake.awake")
    assert not snapshot.affects_snapshot("speech.said")
    assert snapshot.affects_snapshot("tool.used")
    assert snapshot.affects_snapshot("request.created")


def test_changed_since(con: sqlite3.Connection) -> None:
    a = _frag(con, "live.input_transcript", "hel")
    b = _frag(con, "live.output_transcript", "lo")
    publish(con, "audio.wake.awake", "desk", {})
    c = last_seq(con)
    assert not snapshot.changed_since(con, 0, c)
    assert not snapshot.changed_since(con, c, c)
    publish(con, "tool.used", "desk", {"tool": "remember"})
    d = last_seq(con)
    assert snapshot.changed_since(con, c, d)
    assert snapshot.changed_since(con, 0, d)
    assert not snapshot.changed_since(con, a, b)
    # A gap too wide to scan is a yes, not a scan.
    assert snapshot.changed_since(con, 0, 10**9)


def test_fingerprint_moves_when_a_question_is_answered_silently(con: sqlite3.Connection) -> None:
    req = _ask(con)
    before = snapshot.fingerprint(con)
    rq.answer_request(
        con, req.id, answers.build_answer(req, picks=(2,)), answered_by="cli", answer_mode="hud"
    )
    assert snapshot.fingerprint(con) != before


# ───────────────────────────── the feed ─────────────────────────────


def _texts(items: list[dict]) -> list[tuple[str, str]]:
    return [(i["role"], i["text"]) for i in items]


def test_fragments_of_one_kind_are_one_bubble(con: sqlite3.Connection) -> None:
    first = _frag(con, "live.input_transcript", "What's the")
    _frag(con, "live.input_transcript", " weather")
    _frag(con, "live.input_transcript", " like?")
    _frag(con, "live.output_transcript", "Sunny,")
    _frag(con, "live.output_transcript", " 24 degrees.")
    out = snapshot.feed(con)
    assert _texts(out["items"]) == [
        ("user", "What's the weather like?"),
        ("jarvis", "Sunny, 24 degrees."),
    ]
    # The bubble is named after its first fragment, whichever way it was built.
    assert out["items"][0]["seq"] == first
    assert all(i["merge"] is False for i in out["items"])
    assert out["last_seq"] == last_seq(con)


def test_a_shown_kind_in_between_splits_the_bubble(con: sqlite3.Connection) -> None:
    _frag(con, "live.input_transcript", "one")
    publish(con, "window.said", "window", {"text": "typed"})
    _frag(con, "live.input_transcript", "two")
    assert _texts(snapshot.feed(con)["items"]) == [
        ("user", "one"),
        ("user", "typed"),
        ("user", "two"),
    ]
    items, _, _ = snapshot.feed_after(con, 0)
    assert _texts(items) == [("user", "one"), ("user", "typed"), ("user", "two")]


def test_unknown_kinds_are_skipped_and_do_not_split(con: sqlite3.Connection) -> None:
    _frag(con, "live.input_transcript", "hello")
    publish(con, "channel.attached", "desk", {"kind": "desk"})
    publish(con, "job.progress", "runner", {"title": "x"})
    _frag(con, "live.input_transcript", " there")
    items = snapshot.feed(con)["items"]
    assert _texts(items) == [("user", "hello there")]
    items, top, prev = snapshot.feed_after(con, 0)
    assert _texts(items) == [("user", "hello there")]
    assert top == last_seq(con)
    assert prev == "live.input_transcript"


def test_after_continues_the_clients_bubble(con: sqlite3.Connection) -> None:
    a = _frag(con, "live.input_transcript", "Remind me")
    publish(con, "channel.attached", "desk", {})
    _frag(con, "live.input_transcript", " at six")
    out = snapshot.feed(con, after=a)
    assert _texts(out["items"]) == [("user", " at six")]
    assert out["items"][0]["merge"] is True
    # A different kind last shown: a new bubble.
    b = _frag(con, "live.output_transcript", "Done.")
    _frag(con, "live.input_transcript", "thanks")
    out = snapshot.feed(con, after=b)
    assert out["items"][0]["merge"] is False
    # And a said line is never merged, whatever came before.
    c = last_seq(con)
    publish(con, "window.reply", "window", {"text": "Any time.", "tools": []})
    out = snapshot.feed(con, after=c)
    assert _texts(out["items"]) == [("jarvis", "Any time.")]
    assert out["items"][0]["merge"] is False


def test_after_at_or_past_the_top_returns_nothing_and_the_top(con: sqlite3.Connection) -> None:
    top = _frag(con, "live.input_transcript", "x")
    assert snapshot.feed(con, after=top) == {"items": [], "last_seq": top}
    # A seq from some other database: answered with this one's top, to resync.
    assert snapshot.feed(con, after=top + 100) == {"items": [], "last_seq": top}


def test_limit_keeps_the_newest_and_follows_a_bubble_to_its_head(
    con: sqlite3.Connection,
) -> None:
    publish(con, "window.said", "window", {"text": "old"})
    for word in ("a", "b", "c"):
        _frag(con, "live.input_transcript", word)
    publish(con, "window.reply", "window", {"text": "new", "tools": []})
    assert _texts(snapshot.feed(con, limit=2)["items"]) == [("user", "abc"), ("jarvis", "new")]
    assert _texts(snapshot.feed(con, limit=1)["items"]) == [("jarvis", "new")]
    assert len(snapshot.feed(con, limit=10_000)["items"]) == 3


def test_feed_after_stops_at_the_limit_and_resumes_cleanly(con: sqlite3.Connection) -> None:
    for i in range(5):
        publish(con, "window.said", "window", {"text": f"line {i}"})
    items, nxt, prev = snapshot.feed_after(con, 0, limit=2)
    assert _texts(items) == [("user", "line 0"), ("user", "line 1")]
    rest, top, _ = snapshot.feed_after(con, nxt, limit=10, prev_kind=prev)
    assert _texts(rest) == [("user", f"line {i}") for i in (2, 3, 4)]
    assert top == last_seq(con)


def test_allow_listed_kinds_become_short_sentences(con: sqlite3.Connection) -> None:
    req = _ask(con)
    publish(con, "request.created", "driver", {}, request_id=req.id)
    publish(con, "tool.used", "desk", {"tool": "weather", "channel": "desk"})
    publish(con, "tool.denied", "desk", {"tool": "push", "why": "not over the phone"})
    rem = memory.add_reminder(
        con, "call mum", datetime(2026, 10, 5, 18, 0, tzinfo=IST), actor="t", channel="cli"
    )
    publish(con, "reminder.fired", "scheduler", {"reminder_id": rem.id, "due_at": rem.due_at})
    publish(con, "audio.wake.awake", "desk", {})
    publish(con, "audio.wake.asleep", "desk", {})
    publish(con, "audio.activity_start", "desk", {"barge_in": False})
    publish(con, "audio.activity_start", "desk", {"barge_in": True})
    publish(con, "live.connected", "desk", {"resumed": False})
    publish(con, "window.error", "window", {"text": "the speaker is unplugged"})
    publish(con, "speech.said", "desk", {"track": "verbatim", "spoken": True, "text": "Option 1"})
    publish(con, "speech.said", "desk", {"track": "live", "spoken": True, "text": "dup"})
    items = snapshot.feed(con)["items"]
    assert _texts(items) == [
        ("system", "question: database"),
        ("tool", "used weather"),
        ("tool", "push: not over the phone"),
        ("system", "reminder: call mum"),
        ("system", "awake"),
        ("system", "asleep"),
        ("system", "you cut in"),
        ("system", "voice connected"),
        ("system", "the speaker is unplugged"),
        ("jarvis", "Option 1"),
    ]
    assert {i["kind"] for i in items} >= {"tool.used", "reminder.fired", "request.created"}
    assert all(set(i) == {"seq", "ts", "role", "text", "kind", "merge"} for i in items)


def test_job_lines_and_a_stop(con: sqlite3.Connection) -> None:
    j = jobs.create_job(con, kind="claude_code", title="the todo app", created_by="test")
    jobs.set_state(con, j.id, "starting", actor="test")
    jobs.set_state(con, j.id, "running", actor="test")
    jobs.set_state(con, j.id, "failed", actor="test", reason="tests failed")
    from jarvis import kill

    kill.stop_everything(con, "window", "the STOP button")
    texts = [t for _, t in _texts(snapshot.feed(con)["items"])]
    # job.started fires for 'starting' AND 'running'; one line is shown.
    assert texts.count("the todo app started") == 1
    assert "the todo app failed: tests failed" in texts
    assert any(t.startswith("STOP") and "window" in t for t in texts)


def test_a_malformed_row_costs_only_itself(con: sqlite3.Connection) -> None:
    publish(con, "window.said", "window", {"text": "before"})
    publish(con, "window.said", "window", {"text": "broken"})
    bad = last_seq(con)
    publish(con, "tool.used", "desk", {"tool": ["not", "a", "string"]})
    publish(con, "window.reply", "window", {"text": 42})
    publish(con, "reminder.fired", "scheduler", {"reminder_id": 7})
    publish(con, "window.said", "window", {"text": "after"})
    # A payload that is not JSON at all, as a hand-edited or truncated row would be.
    con.execute("UPDATE events SET payload='{not json' WHERE seq=?", (bad,))
    con.execute(
        "UPDATE events SET payload='[1, 2]' WHERE seq=(SELECT MIN(seq) FROM events "
        "WHERE kind='tool.used')"
    )
    assert _texts(snapshot.feed(con)["items"]) == [
        ("user", "before"),
        ("system", "reminder"),
        ("user", "after"),
    ]
    items, _, _ = snapshot.feed_after(con, 0)
    assert _texts(items) == [("user", "before"), ("system", "reminder"), ("user", "after")]


def test_a_nulled_detail_row_is_not_shown(con: sqlite3.Connection) -> None:
    publish(con, "window.said", "window", {"_jarvis_detail_nulled": True, "text": "old"})
    publish(con, "window.said", "window", {"text": "new"})
    assert _texts(snapshot.feed(con)["items"]) == [("user", "new")]


def test_previous_kind_is_the_last_shown_kind(con: sqlite3.Connection) -> None:
    assert snapshot.previous_kind(con, 0) is None
    _frag(con, "live.output_transcript", "hi")
    publish(con, "channel.attached", "desk", {})
    assert snapshot.previous_kind(con, last_seq(con)) == "live.output_transcript"
