"""Notes, reminders, and the scheduler that says a reminder when it is due.

The failures worth a test: a reminder said twice (two schedulers), a reminder
never said (a crash between raising and marking), a snooze that is lost or
doubled, and a spoken time resolved to a different instant than the user meant
without anyone saying so.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from jarvis import __main__ as cli
from jarvis import memory
from jarvis import requests as rq
from jarvis.db import connect, migrate
from jarvis.ids import now, parse_ts
from jarvis.live.profiles import DESK, PHONE_USER
from jarvis.schedule import loop, reminders
from jarvis.tools.builtin import general
from jarvis.tools.ctx import ToolCtx
from jarvis.tools.default import registry

IST = ZoneInfo("Europe/Istanbul")
MONDAY_3PM = datetime(2026, 10, 5, 15, 0, tzinfo=IST)


@pytest.fixture
def dbpath(tmp_path: Path) -> Path:
    p = tmp_path / "j.db"
    c = connect(p)
    migrate(c)
    c.close()
    return p


@pytest.fixture
def con(dbpath: Path) -> Iterator[sqlite3.Connection]:
    c = connect(dbpath)
    yield c
    c.close()


def ctx(con: sqlite3.Connection, **extra) -> ToolCtx:
    return ToolCtx(con=con, channel="desk", actor="desk", extra={"tz": "Europe/Istanbul", **extra})


# ───────────────────────────── spoken time ─────────────────────────────


@pytest.mark.parametrize(
    ("said", "expected"),
    [
        ("in 10 minutes", "Mon 15:10"),
        ("in an hour and a half", "Mon 16:30"),
        ("in half an hour", "Mon 15:30"),
        ("in a couple of hours", "Mon 17:00"),
        ("in two days", "Wed 15:00"),
        ("at 6", "Mon 18:00"),  # ambiguous, and 06:00 has passed: the evening
        ("at 6pm", "Mon 18:00"),
        ("at 18:30", "Mon 18:30"),
        ("at 9am", "Tue 09:00"),  # explicit and passed: tomorrow
        ("tomorrow", "Tue 09:00"),
        ("tomorrow at 9", "Tue 09:00"),
        ("tonight", "Mon 20:00"),
        ("this evening", "Mon 18:00"),
        ("on friday at 10", "Fri 10:00"),
        ("monday", "Mon 09:00"),  # said on a Monday: next week's
    ],
)
def test_spoken_times_resolve_to_the_instant_meant(said: str, expected: str) -> None:
    when = memory.parse_when(said, MONDAY_3PM)
    assert when is not None
    day = "Mon" if when.date() == MONDAY_3PM.date() else when.strftime("%a")
    if said == "monday":
        assert (when.date() - MONDAY_3PM.date()).days == 7
    assert f"{day} {when.strftime('%H:%M')}" == expected


@pytest.mark.parametrize("said", ["", "whenever", "at 25:00", "at 9:75", "in some time"])
def test_a_time_it_does_not_understand_is_refused_not_guessed(said: str) -> None:
    assert memory.parse_when(said, MONDAY_3PM) is None


def test_a_naive_now_is_refused() -> None:
    with pytest.raises(ValueError):
        memory.parse_when("in 5 minutes", datetime(2026, 1, 1))


# ───────────────────────────── notes ─────────────────────────────


def test_notes_are_the_users_words_and_recall_finds_them(con: sqlite3.Connection) -> None:
    memory.remember(con, "my  locker is 214", actor="desk", channel="desk")
    memory.remember(con, "Ali's birthday is in May", actor="telegram", channel="telegram")
    (hit,) = memory.recall(con, "what's my locker number")
    assert hit.text == "my locker is 214"
    assert [n.text for n in memory.recall(con)] == ["Ali's birthday is in May", "my locker is 214"]


def test_forgetting_takes_only_the_best_match_and_keeps_the_row(con: sqlite3.Connection) -> None:
    memory.remember(con, "my gym locker is 214", actor="desk", channel="desk")
    memory.remember(con, "the gym opens at 7", actor="desk", channel="desk")
    gone = memory.forget(con, "gym locker")
    assert [n.text for n in gone] == ["my gym locker is 214"]
    assert [n.text for n in memory.notes(con)] == ["the gym opens at 7"]
    assert con.execute("SELECT count(*) FROM notes").fetchone()[0] == 2


def test_notes_reach_every_conversation(con: sqlite3.Connection) -> None:
    from jarvis.config import Config

    memory.remember(con, "I take my coffee black", actor="cli", channel="cli")
    prof = cli.heard_profile(Config(), con, DESK)
    assert "I take my coffee black" in prof.system_instruction


# ───────────────────────────── reminders: the store ─────────────────────────────


def test_a_reminder_needs_a_zone(con: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="timezone"):
        memory.add_reminder(con, "x", datetime(2026, 1, 1), actor="t", channel="t")


def test_cancel_takes_the_best_match(con: sqlite3.Connection) -> None:
    memory.add_reminder(con, "call mum", MONDAY_3PM, actor="t", channel="t")
    memory.add_reminder(con, "water the plants", MONDAY_3PM, actor="t", channel="t")
    assert [r.text for r in memory.cancel_reminder(con, "mum")] == ["call mum"]
    assert [r.text for r in memory.pending_reminders(con)] == ["water the plants"]


# ───────────────────────────── reminders: the scheduler ─────────────────────────────


def _due_one(con: sqlite3.Connection, text: str = "call mum") -> memory.Reminder:
    return memory.add_reminder(
        con, text, datetime.now(IST) - timedelta(seconds=5), actor="desk", channel="desk"
    )


def test_a_due_reminder_becomes_a_routed_request_once(con: sqlite3.Connection) -> None:
    r = _due_one(con)
    first = loop.tick(con)
    assert [f.reminder.id for f in first.reminders] == [r.id]
    req = first.reminders[0].request
    assert req.kind == "free_text" and "call mum" in req.presentation["intro"]
    assert req.urgency == "normal"
    assert loop.tick(con).reminders == ()  # said once, not every tick


def test_a_crash_between_raising_and_marking_raises_the_same_request(
    con: sqlite3.Connection,
) -> None:
    r = _due_one(con)
    orphan = reminders.raise_reminder(con, r)  # ...and then the process died
    fired = reminders.fire_reminders(con)
    assert [f.request.id for f in fired] == [orphan.id]
    assert con.execute("SELECT count(*) FROM requests").fetchone()[0] == 1


def test_two_schedulers_never_both_say_it(dbpath: Path) -> None:
    c = connect(dbpath)
    try:
        for i in range(10):
            _due_one(c, f"thing {i}")
    finally:
        c.close()
    said: list[str] = []
    errors: list[BaseException] = []

    def worker() -> None:
        c = connect(dbpath)
        try:
            said.extend(f.reminder.id for f in reminders.fire_reminders(c))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            c.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(said) == len(set(said)) == 10


def _answer(con: sqlite3.Connection, req: rq.Request, label: str) -> None:
    assert rq.answer_request(
        con,
        req.id,
        {
            "answers": {reminders.REMINDER_QUESTION: label},
            "text": label,
            "sources": {reminders.REMINDER_QUESTION: "option"},
        },
        "desk",
        "voice",
    )


def test_got_it_settles_it(con: sqlite3.Connection) -> None:
    _due_one(con)
    (fired,) = loop.tick(con).reminders
    _answer(con, fired.request, reminders.GOT_IT)
    (settled,) = reminders.settle_reminders(con)
    assert settled.snoozed_to is None
    assert memory.pending_reminders(con) == []
    assert reminders.settle_reminders(con) == []


def test_snooze_is_one_new_reminder_ten_minutes_out(con: sqlite3.Connection) -> None:
    _due_one(con)
    (fired,) = loop.tick(con).reminders
    _answer(con, fired.request, reminders.SNOOZE)
    ts = now()
    (settled,) = reminders.settle_reminders(con, now_ts=ts)
    assert settled.snoozed_to is not None
    gap = (parse_ts(settled.snoozed_to) - parse_ts(ts)).total_seconds()
    assert 599 <= gap <= 601
    assert [r.text for r in memory.pending_reminders(con)] == ["call mum"]
    assert reminders.settle_reminders(con) == []  # not snoozed twice


def test_an_unheard_reminder_settles_itself_as_got_it(con: sqlite3.Connection) -> None:
    _due_one(con)
    (fired,) = loop.tick(con).reminders
    later = parse_ts(now()) + timedelta(seconds=reminders.REMINDER_EXPIRES_S + 5)
    rq.expire_due(con, now_ts=memory.to_ts(later))
    (settled,) = reminders.settle_reminders(con)
    assert settled.snoozed_to is None and memory.pending_reminders(con) == []


def test_the_daemon_says_what_it_reminded(con: sqlite3.Connection, capsys) -> None:
    from jarvis.schedule.__main__ import run

    _due_one(con)
    run(con, actor="scheduler", claimed_by="t", once=True)
    assert "reminder: call mum" in capsys.readouterr().out


# ───────────────────────────── the tools ─────────────────────────────


def test_remind_me_says_back_the_time_it_resolved(con: sqlite3.Connection) -> None:
    said = registry().dispatch("remind_me", {"what": "call mum", "when": "in 20 minutes"}, ctx(con))
    expected = (datetime.now(IST) + timedelta(minutes=20)).strftime("%H:%M")
    assert "call mum" in said and expected in said and "scheduler" in said
    (r,) = memory.pending_reminders(con)
    assert r.channel == "desk"


def test_remind_me_refuses_a_time_it_did_not_catch(con: sqlite3.Connection) -> None:
    said = registry().dispatch("remind_me", {"what": "x", "when": "whenever"}, ctx(con))
    assert "didn't catch when" in said and memory.pending_reminders(con) == []


def test_remember_recall_forget_by_voice(con: sqlite3.Connection) -> None:
    reg = registry()
    assert "locker is 214" in reg.dispatch("remember", {"fact": "my locker is 214"}, ctx(con))
    assert "214" in reg.dispatch("recall", {"about": "locker"}, ctx(con))
    assert "Forgotten" in reg.dispatch("forget_note", {"about": "locker"}, ctx(con))
    assert "haven't told me" in reg.dispatch("recall", {"about": "locker"}, ctx(con))


def test_web_search_without_a_key_says_so(con: sqlite3.Connection) -> None:
    said = registry().dispatch("web_search", {"query": "who won"}, ctx(con))
    assert "Gemini key" in said


def test_web_search_uses_the_search_it_was_handed(con: sqlite3.Connection) -> None:
    asked: list[str] = []

    def search(q: str) -> str:
        asked.append(q)
        return "Galatasaray won 2-1."

    said = registry().dispatch("web_search", {"query": "  who won  "}, ctx(con, search=search))
    assert said == "Galatasaray won 2-1." and asked == ["who won"]


def test_the_general_tools_are_offered_on_both_voice_legs_and_the_cli() -> None:
    reg = registry()
    for tool in general.TOOLS:
        assert tool.name in DESK.tools and tool.name in PHONE_USER.tools
        assert tool.name in reg.names("cli")


def test_the_cli_lists_and_cancels_reminders(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    path = tmp_path / "r.db"
    c = connect(path)
    migrate(c)
    memory.add_reminder(
        c, "call mum", datetime.now(IST) + timedelta(hours=1), actor="t", channel="t"
    )
    c.close()
    assert cli.main(["--db", str(path), "remind"]) == 0
    assert "call mum" in capsys.readouterr().out
    assert cli.main(["--db", str(path), "remind", "cancel", "mum"]) == 0
    assert "Cancelled: call mum" in capsys.readouterr().out


def test_tool_extra_carries_search_only_with_a_key() -> None:
    pytest.importorskip("google.genai")
    from jarvis.config import Config

    assert "search" not in cli.tool_extra(Config())
    assert callable(cli.tool_extra(Config(), "k")["search"])
    assert cli.tool_extra(Config())["tz"] == Config().tz


def test_memory_is_spine_and_imports_on_a_bare_interpreter() -> None:
    import os
    import subprocess
    import sys

    src = Path(__file__).parent.parent
    r = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            f"import sys; sys.path.insert(0, {str(src)!r}); import jarvis.memory; print('ok')",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONNOUSERSITE": "1", "PYTHONPATH": ""},
    )
    assert r.returncode == 0, r.stderr
