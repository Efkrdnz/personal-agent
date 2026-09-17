"""The morning gate, the snooze, and where the news goes. The claim, under test.

THE CLAIM IS THAT A BRIEFING SECTION IS JUST A REQUEST, and the gate is the first
place it has to hold. So these tests are mostly about what is NOT here: no second
way to ask a question, no second confirmation, no navigation mechanism of its
own. "Five minutes" is one more row in ``requests`` with a later ``due_at`` on its
delivery, and the test that matters most is the one asserting the schedules table
was not touched by it.

The other half is routing: presence decides, never a guess, and a briefing may
never ring a telephone.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from jarvis import jobs, presence
from jarvis import requests as rq
from jarvis.db import connect, migrate
from jarvis.ids import now
from jarvis.jobs import shift_ts
from jarvis.schedule import completion, gate, routing
from jarvis.schedule import recurrence as rec

TZ = "Europe/Istanbul"
AT = "10:00"
SCHEDULE = "morning_briefing"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "jarvis.db"
    c = connect(p)
    migrate(c)
    c.close()
    return p


@pytest.fixture
def con(db_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(db_path)
    yield c
    c.close()


@pytest.fixture
def other(db_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(db_path)
    yield c
    c.close()


def ten_am() -> str:
    return rec.last_at_or_before(now(), AT, TZ)


def tap(con: sqlite3.Connection, req: rq.Request, label: str, *, by: str = "telegram") -> None:
    """Answer the gate the way a channel does: by label, through the one CAS."""
    question = str(req.presentation["question"])
    rq.answer_request(
        con,
        req.id,
        {"answers": {question: label}, "text": label, "sources": {question: "option"}},
        by,
        "button",
    )


# ───────────────────────────── the question ─────────────────────────────


def test_the_gate_is_one_request_with_the_three_options_in_order(
    con: sqlite3.Connection,
) -> None:
    req = gate.raise_gate(con, schedule=SCHEDULE, occurrence=ten_am(), now_ts=ten_am())
    assert req.kind == "briefing_gate"
    assert [i["label"] for i in req.presentation["items"]] == [
        gate.NOW_LABEL,
        gate.FIVE_LABEL,
        gate.SKIP_LABEL,
    ]
    assert req.presentation["question"] == gate.GATE_QUESTION
    assert req.presentation["allows_free_text"] is False
    assert req.presentation["dtmf_map"] == {"1": 1, "2": 2, "3": 3}
    # Never anything a phone would ring for.
    assert req.urgency == "low"
    assert req.on_timeout == "default"
    assert req.payload["occurrence"] == ten_am()


def test_the_whole_gate_is_one_row_in_requests_and_nothing_else(
    con: sqlite3.Connection,
) -> None:
    """The coupling claim, counted. One question, no job, no second table."""
    gate.raise_gate(con, schedule=SCHEDULE, occurrence=ten_am(), now_ts=ten_am())
    assert con.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM effects").fetchone()[0] == 0


def test_two_daemons_firing_the_same_morning_ask_one_question(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    """job_id is NULL here, so the UNIQUE index does not bite; the lookup does."""
    first = gate.raise_gate(con, schedule=SCHEDULE, occurrence=ten_am(), now_ts=ten_am())
    second = gate.raise_gate(other, schedule=SCHEDULE, occurrence=ten_am(), now_ts=ten_am())
    assert second.id == first.id
    assert con.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1


def test_tomorrow_is_a_different_question(con: sqlite3.Connection) -> None:
    today = gate.raise_gate(con, schedule=SCHEDULE, occurrence=ten_am(), now_ts=ten_am())
    tomorrow_at = rec.next_after(ten_am(), AT, TZ)
    tomorrow = gate.raise_gate(con, schedule=SCHEDULE, occurrence=tomorrow_at, now_ts=tomorrow_at)
    assert tomorrow.id != today.id


# ───────────────────────────── reading the answer ─────────────────────────────


@pytest.mark.parametrize(
    ("label", "expected"),
    [(gate.NOW_LABEL, "now"), (gate.FIVE_LABEL, "five"), (gate.SKIP_LABEL, "skip")],
)
def test_each_label_maps_to_its_meaning(con: sqlite3.Connection, label: str, expected: str) -> None:
    req = gate.raise_gate(con, schedule=SCHEDULE, occurrence=ten_am(), now_ts=ten_am())
    answer: rq.Answer = {"answers": {gate.GATE_QUESTION: label}}
    assert gate.choice_of(req, answer) == expected


def test_a_label_nobody_offered_is_not_a_choice(con: sqlite3.Connection) -> None:
    """The frozen options array is the only vocabulary. "Later" is not in it."""
    req = gate.raise_gate(con, schedule=SCHEDULE, occurrence=ten_am(), now_ts=ten_am())
    assert gate.choice_of(req, {"answers": {gate.GATE_QUESTION: "Later"}}) is None
    assert gate.choice_of(req, {"text": "maybe after lunch"}) is None


def test_free_text_is_never_read_as_one_of_the_three(con: sqlite3.Connection) -> None:
    req = gate.raise_gate(con, schedule=SCHEDULE, occurrence=ten_am(), now_ts=ten_am())
    typed: rq.Answer = {
        "answers": {gate.GATE_QUESTION: gate.NOW_LABEL},
        "sources": {gate.GATE_QUESTION: "free_text"},
    }
    assert gate.choice_of(req, typed) is None


# ───────────────────────────── the three outcomes ─────────────────────────────


def test_now_starts_the_briefing_and_says_so_on_the_bus(con: sqlite3.Connection) -> None:
    req = gate.raise_gate(con, schedule=SCHEDULE, occurrence=ten_am(), now_ts=ten_am())
    tap(con, req, gate.NOW_LABEL)

    outcome = gate.handle_answer(con, rq.get_request(con, req.id), now_ts=ten_am())
    assert outcome.action == "start"
    kinds = [r[0] for r in con.execute("SELECT kind FROM events")]
    assert "briefing.started" in kinds
    assert rq.get_request(con, req.id).state == "consumed"


def test_skip_today_ends_the_morning(con: sqlite3.Connection) -> None:
    req = gate.raise_gate(con, schedule=SCHEDULE, occurrence=ten_am(), now_ts=ten_am())
    tap(con, req, gate.SKIP_LABEL)

    outcome = gate.handle_answer(con, rq.get_request(con, req.id), now_ts=ten_am())
    assert outcome.action == "skipped"
    assert "briefing.started" not in [r[0] for r in con.execute("SELECT kind FROM events")]


def test_nobody_answering_resolves_through_the_same_swap_a_tap_uses(
    con: sqlite3.Connection,
) -> None:
    """The timeout path IS the human path. There is no second mechanism for silence."""
    req = gate.raise_gate(con, schedule=SCHEDULE, occurrence=ten_am(), now_ts=now())
    later = shift_ts(now(), gate.GATE_EXPIRES_S + 1)

    expiries = rq.expire_due(con, later)
    assert [e.outcome for e in expiries] == ["defaulted"]

    settled = rq.get_request(con, req.id)
    assert settled.state == "answered"
    assert settled.answered_by == "timeout"
    outcome = gate.handle_answer(con, settled, now_ts=later)
    assert outcome.action == "skipped", "an unanswered morning is a skipped one, by default"


def test_handling_an_unanswered_gate_does_nothing_at_all(con: sqlite3.Connection) -> None:
    req = gate.raise_gate(con, schedule=SCHEDULE, occurrence=ten_am(), now_ts=ten_am())
    assert gate.handle_answer(con, req, now_ts=ten_am()).action == "not_answered"
    assert rq.get_request(con, req.id).state == "pending"


def test_an_answer_that_is_none_of_the_three_leaves_the_briefing_alone(
    con: sqlite3.Connection,
) -> None:
    req = gate.raise_gate(con, schedule=SCHEDULE, occurrence=ten_am(), now_ts=ten_am())
    rq.answer_request(con, req.id, {"text": "what? no"}, "desk", "voice")
    outcome = gate.handle_answer(con, rq.get_request(con, req.id), now_ts=ten_am())
    assert outcome.action == "unreadable"
    assert "briefing.started" not in [r[0] for r in con.execute("SELECT kind FROM events")]


# ───────────────────────────── five minutes ─────────────────────────────


def _schedules_snapshot(con: sqlite3.Connection) -> list[tuple]:
    return [tuple(r) for r in con.execute("SELECT * FROM schedules ORDER BY id")]


def test_five_minutes_writes_a_due_time_and_does_not_touch_the_schedules_table(
    con: sqlite3.Connection,
) -> None:
    """The roadmap is specific: a snooze re-queues, it does not reschedule."""
    from jarvis.schedule import store

    store.ensure_schedule(
        con,
        name=SCHEDULE,
        fires="briefing_gate",
        at_local=AT,
        tz=TZ,
        now_ts=shift_ts(ten_am(), -3600),
    )
    before = _schedules_snapshot(con)

    req = gate.raise_gate(con, schedule=SCHEDULE, occurrence=ten_am(), now_ts=ten_am())
    tap(con, req, gate.FIVE_LABEL)
    outcome = gate.handle_answer(con, rq.get_request(con, req.id), now_ts=ten_am())

    assert outcome.action == "snoozed"
    assert outcome.deliver_after == shift_ts(ten_am(), gate.SNOOZE_S)
    assert outcome.next_request is not None
    assert outcome.next_request.id != req.id
    assert outcome.next_request.attempt == 2
    assert outcome.next_request.payload["snoozes"] == 1
    assert _schedules_snapshot(con) == before, "a snooze rescheduled something"


def test_the_snoozed_question_is_delivered_later_not_now(con: sqlite3.Connection) -> None:
    req = gate.raise_gate(con, schedule=SCHEDULE, occurrence=ten_am(), now_ts=ten_am())
    tap(con, req, gate.FIVE_LABEL)
    outcome = gate.handle_answer(con, rq.get_request(con, req.id), now_ts=ten_am())

    rows = routing.deliver(
        con, outcome.next_request, now_ts=ten_am(), deliver_after=outcome.deliver_after
    )
    assert rows, "a snoozed question with nowhere to go is a lost briefing"
    assert min(r.due_at for r in rows) == shift_ts(ten_am(), gate.SNOOZE_S)
    assert rq.due_deliveries(con, ten_am()) == [], "the re-ask is not due yet"
    assert rq.due_deliveries(con, shift_ts(ten_am(), gate.SNOOZE_S))


def test_snoozing_forever_is_not_a_loop(con: sqlite3.Connection) -> None:
    """After N postponements it SAYS SO and leaves the day, rather than asking at midnight."""
    req = gate.raise_gate(con, schedule=SCHEDULE, occurrence=ten_am(), now_ts=ten_am())
    at = ten_am()
    for expected in range(1, gate.MAX_SNOOZES + 1):
        tap(con, req, gate.FIVE_LABEL)
        outcome = gate.handle_answer(con, rq.get_request(con, req.id), now_ts=at)
        assert outcome.action == "snoozed"
        assert outcome.next_request.payload["snoozes"] == expected
        req = outcome.next_request
        at = shift_ts(at, gate.SNOOZE_S)

    tap(con, req, gate.FIVE_LABEL)
    final = gate.handle_answer(con, rq.get_request(con, req.id), now_ts=at)
    assert final.action == "exhausted"
    assert final.next_request is None
    assert str(gate.MAX_SNOOZES) in final.spoken
    assert con.execute("SELECT COUNT(*) FROM requests WHERE state='pending'").fetchone()[0] == 0


def test_acting_on_the_same_answer_twice_is_harmless(con: sqlite3.Connection) -> None:
    """A daemon that dies between acting and consuming repeats itself on the next tick."""
    req = gate.raise_gate(con, schedule=SCHEDULE, occurrence=ten_am(), now_ts=ten_am())
    tap(con, req, gate.FIVE_LABEL)
    answered = rq.get_request(con, req.id)

    first = gate.handle_answer(con, answered, now_ts=ten_am())
    second = gate.handle_answer(con, answered, now_ts=ten_am())
    assert first.next_request.id == second.next_request.id
    assert con.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 2


# ───────────────────────────── where it goes ─────────────────────────────


def test_the_ladder_is_pure_and_puts_the_first_reachable_channel_first() -> None:
    assert routing.ladder(("desk", "telegram")) == (
        ("desk", 0),
        ("telegram", routing.ESCALATE_TELEGRAM_S),
    )
    # Away: Telegram is not an escalation from the desk, it is the only way in.
    assert routing.ladder(("telegram", "phone")) == (("telegram", 0),)


def test_a_briefing_never_rings_a_telephone() -> None:
    """Structural, not a promise: the phone rung needs an urgency the gate never has."""
    assert routing.ladder(("telegram", "phone"), urgency="low") == (("telegram", 0),)
    assert routing.ladder(("telegram", "phone"), urgency="critical") == (
        ("telegram", 0),
        ("phone", routing.ESCALATE_PHONE_S),
    )


def test_at_the_desk_it_is_spoken_and_away_it_is_tapped(con: sqlite3.Connection) -> None:
    at = ten_am()
    presence.record_signal(con, "utterance", {"text": "morning"}, now_ts=at)
    here = gate.raise_gate(con, schedule=SCHEDULE, occurrence=at, now_ts=at)
    assert [d.channel_kind for d in routing.deliver(con, here, now_ts=at)] == [
        "desk",
        "telegram",
    ]

    presence.clear_signal(con, "utterance")
    presence.record_signal(con, "idle", {"idle_s": 900.0}, now_ts=at)
    assert presence.evaluate_presence(con, at).state == "away"
    away = gate.raise_gate(con, schedule=SCHEDULE, occurrence=rec.next_after(at, AT, TZ), now_ts=at)
    assert [d.channel_kind for d in routing.deliver(con, away, now_ts=at)] == ["telegram"]


def test_routing_is_idempotent_so_a_retried_fire_does_not_double_ask(
    con: sqlite3.Connection,
) -> None:
    req = gate.raise_gate(con, schedule=SCHEDULE, occurrence=ten_am(), now_ts=ten_am())
    first = routing.deliver(con, req, now_ts=ten_am())
    second = routing.deliver(con, req, now_ts=ten_am())
    assert [d.id for d in first] == [d.id for d in second]


# ───────────────────────────── task completion ─────────────────────────────


def finished_job(
    con: sqlite3.Connection, *, state: str = "done", title: str = "the todo app"
) -> str:
    job = jobs.create_job(con, kind="claude_code", title=title, created_by="desk")
    jobs.set_state(con, job.id, "starting", actor="desk")
    jobs.set_state(con, job.id, "running", actor="desk")
    if state == "done":
        jobs.set_state(con, job.id, "finishing", actor="desk")
        jobs.set_state(con, job.id, "done", actor="desk", result_summary="Seven tests pass.")
    else:
        jobs.set_state(con, job.id, state, actor="desk", stop_reason="the build broke")
    return job.id


def test_a_finished_job_is_routed_by_presence_like_everything_else(
    con: sqlite3.Connection,
) -> None:
    at = ten_am()
    presence.record_signal(con, "idle", {"idle_s": 900.0}, now_ts=at)
    job_id = finished_job(con)

    notice = completion.raise_completion(con, job_id, now_ts=at)
    assert notice is not None
    assert "the todo app" in notice.line
    rows = routing.deliver(con, notice.request, now_ts=at)
    assert [d.channel_kind for d in rows] == ["telegram"], "away means Telegram, not a guess"


def test_the_sentence_is_the_one_the_briefing_would_have_used(con: sqlite3.Connection) -> None:
    """One wording for one event, or the 10:00 version drifts from the 15:00 one."""
    from jarvis.reconcile import project_status

    job_id = finished_job(con, state="failed", title="the scraper")
    notice = completion.raise_completion(con, job_id, now_ts=ten_am())
    job = jobs.get(con, job_id)
    status = project_status(con, since=job.updated_at, now_ts=ten_am())
    assert notice.line in [n.line for n in status.failed]


def test_the_same_finished_job_is_only_announced_once(con: sqlite3.Connection) -> None:
    job_id = finished_job(con)
    first = completion.raise_completion(con, job_id, now_ts=ten_am())
    second = completion.raise_completion(con, job_id, now_ts=ten_am())
    assert first.request.id == second.request.id
    assert con.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1


def test_a_job_that_has_not_finished_is_not_news(con: sqlite3.Connection) -> None:
    job = jobs.create_job(con, kind="claude_code", title="still going", created_by="desk")
    jobs.set_state(con, job.id, "starting", actor="desk")
    jobs.set_state(con, job.id, "running", actor="desk")
    assert completion.raise_completion(con, job.id, now_ts=ten_am()) is None


def test_an_unanswered_notice_resolves_itself_rather_than_waiting_forever(
    con: sqlite3.Connection,
) -> None:
    job_id = finished_job(con)
    notice = completion.raise_completion(con, job_id, now_ts=now())
    later = shift_ts(now(), completion.COMPLETION_EXPIRES_S + 1)
    assert [e.outcome for e in rq.expire_due(con, later)] == ["defaulted"]
    assert rq.get_request(con, notice.request.id).answer["text"] == completion.LEAVE_IT_LABEL
