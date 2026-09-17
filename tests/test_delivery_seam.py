"""The seam that was missing for a release: a question raised, and a channel told.

``jarvis/cc`` raises a ``requests`` row when Claude Code asks something and then
blocks. ``jarvis/telegram`` reads the ``deliveries`` table. Nothing wrote the row
in between, so the question was raised, ``/status`` reported it, and no channel
could ever present it. Every layer was green.

These tests are therefore CALLER tests, not callee tests. Deleting
``route_undelivered``'s call inside ``tick`` must fail here — that is the whole
point, and it is the shape this repo needs after shipping the same class of bug
three times.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from jarvis import jobs
from jarvis import requests as rq
from jarvis.cc import gate
from jarvis.db import connect, migrate
from jarvis.ids import now
from jarvis.schedule import loop


@pytest.fixture
def con(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "jarvis.db")
    migrate(c)
    yield c
    c.close()


QUESTIONS = {
    "questions": [
        {
            "question": "How should todos be stored?",
            "options": [
                {"label": "SQLite", "description": "one file"},
                {"label": "JSON file", "description": "no server"},
            ],
        }
    ]
}


def a_blocked_job(con: sqlite3.Connection) -> jobs.Job:
    job = jobs.create_job(con, kind="claude_code", title="the todo app build", created_by="cli")
    jobs.set_state(con, job.id, "starting", actor="t")
    jobs.set_state(con, job.id, "running", actor="t")
    return job


def ask(con: sqlite3.Connection, job_id: str | None = None) -> rq.Request:
    """Raise a question exactly the way the permission host does."""
    return gate.ensure_request(
        con,
        tool_name="AskUserQuestion",
        input_data=QUESTIONS,
        job_id=job_id,
        tool_use_id=f"toolu_{job_id or 'x'}",
        actor="runner",
    )


def rungs(con: sqlite3.Connection, request_id: str) -> list[str]:
    rows = con.execute(
        "SELECT channel_kind FROM deliveries WHERE request_id=? ORDER BY due_at, rowid",
        (request_id,),
    ).fetchall()
    return [r["channel_kind"] for r in rows]


# ───────────────────────── the seam itself ─────────────────────────


def test_a_question_nobody_raised_a_delivery_for_gets_one(con: sqlite3.Connection) -> None:
    job = a_blocked_job(con)
    req = ask(con, job.id)
    assert rungs(con, req.id) == [], "the driver must not deliver; that is the seam"

    loop.tick(con, actor="scheduler")
    assert rungs(con, req.id), "the question was raised and no channel was ever told"


def test_the_sweep_is_actually_called_by_tick(con: sqlite3.Connection) -> None:
    """THE CALLER TEST. The bug this file exists for is a function nobody calls.

    Asserted through ``tick`` rather than by calling ``route_undelivered``
    directly, because a green test of the callee is exactly what the last three
    bugs had.
    """
    job = a_blocked_job(con)
    req = ask(con, job.id)
    report = loop.tick(con, actor="scheduler")
    assert [r.request_id for r in report.routed] == [req.id]
    assert report.routed[0].kind == "plan_question"


def test_the_sweep_runs_after_the_others_so_a_snooze_is_not_stolen(
    con: sqlite3.Connection,
) -> None:
    """``handle_gate_answers`` re-asks a snoozed gate with ``deliver_after`` set.

    Routing first would reach that row, deliver it with no snooze, and ask the
    user immediately after they said "in five minutes".
    """
    source = loop.tick.__doc__ or ""
    del source
    import inspect

    body = inspect.getsource(loop.tick)
    assert body.index("route_undelivered") > body.index("notify_finished")


# ───────────────────────── what it must NOT route ─────────────────────────


def test_a_briefing_gate_is_not_this_sweeps_business(con: sqlite3.Connection) -> None:
    """``fire_due`` raises AND delivers it. Routing it again would steal its snooze."""
    from jarvis.schedule import gate as sgate

    req = sgate.raise_gate(con, schedule="morning_briefing", occurrence=now(), actor="scheduler")
    con.execute("DELETE FROM deliveries WHERE request_id=?", (req.id,))

    report = loop.tick(con, actor="scheduler")
    assert rungs(con, req.id) == []
    assert req.id not in [r.request_id for r in report.routed]


def test_a_question_whose_job_is_over_is_reported_not_asked(con: sqlite3.Connection) -> None:
    """Nobody is left to consume the answer, so asking would be asking for nobody."""
    job = a_blocked_job(con)
    req = ask(con, job.id)
    jobs.set_state(con, job.id, "killed", actor="t")

    report = loop.tick(con, actor="scheduler")
    assert rungs(con, req.id) == []
    assert report.routed[0].skipped == "the job is over"


def test_an_answered_question_is_not_routed_again(con: sqlite3.Connection) -> None:
    job = a_blocked_job(con)
    req = ask(con, job.id)
    loop.tick(con, actor="scheduler")
    before = rungs(con, req.id)

    rq.answer_request(con, req.id, {"answers": {"How should todos be stored?": "SQLite"}}, "cli")
    loop.tick(con, actor="scheduler")
    assert rungs(con, req.id) == before


# ───────────────────────── self-healing ─────────────────────────


def test_a_half_written_ladder_is_completed_rather_than_left_forever(
    con: sqlite3.Connection,
) -> None:
    """THE REGRESSION TEST for the design this replaced.

    ``deliver`` is not atomic across rungs: each opens its own transaction. A
    process killed between the desk rung and the Telegram rung leaves a half
    ladder — and a "skip rows that already have deliveries" guard would make that
    half ladder permanent, so the user would be asked only on a channel nobody is
    listening to, forever, with no error anywhere.
    """
    job = a_blocked_job(con)
    req = ask(con, job.id)
    rq.schedule_delivery(con, req.id, "desk", now())
    assert rungs(con, req.id) == ["desk"]

    loop.tick(con, actor="scheduler")
    assert "telegram" in rungs(con, req.id), "the interrupted ladder was never completed"


def test_routing_twice_does_not_ask_twice(con: sqlite3.Connection) -> None:
    """The idempotency the unconditional sweep rests on."""
    job = a_blocked_job(con)
    req = ask(con, job.id)
    loop.tick(con, actor="scheduler")
    once = rungs(con, req.id)
    for _ in range(3):
        loop.tick(con, actor="scheduler")
    assert rungs(con, req.id) == once


def test_two_schedulers_do_not_double_deliver(con: sqlite3.Connection, tmp_path: Path) -> None:
    """Two real connections, because "two processes are safe" is a claim."""
    job = a_blocked_job(con)
    req = ask(con, job.id)
    other = connect(tmp_path / "jarvis.db")
    try:
        loop.tick(con, actor="scheduler-a")
        loop.tick(other, actor="scheduler-b")
    finally:
        other.close()
    assert len(rungs(con, req.id)) == len(set(rungs(con, req.id)))


# ───────────────────────── containment ─────────────────────────


def test_one_bad_row_does_not_cost_the_other_four_sweeps(
    con: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A busy timeout under a second scheduler must not exit the daemon."""
    job = a_blocked_job(con)
    ask(con, job.id)

    def boom(*a: object, **k: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(loop, "deliver", boom)
    report = loop.tick(con, actor="scheduler")
    assert report.routed[0].skipped == "OperationalError"
