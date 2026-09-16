"""The hooks, tested where they are load-bearing: the defer path and its fallback.

The defer is a REQUEST, not an outcome — the shipped CLI drops it silently in
three measured cases — so what matters here is that the row is durable before the
defer is asked for, that the job's state still tells the truth afterwards, and
that a dropped defer is detectable rather than mysterious.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from jarvis import jobs
from jarvis import requests as rq
from jarvis.bus import read_since
from jarvis.cc.hooks import DeferLedger, Hooks
from jarvis.db import connect, migrate

JOB = "job_hookhookhoo"

QUESTIONS = {
    "questions": [
        {
            "header": "Storage",
            "question": "How should todos be stored?",
            "options": [{"label": "SQLite"}, {"label": "JSON file"}],
            "multiSelect": False,
        }
    ]
}


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "jarvis.db"
    con = connect(p)
    migrate(con)
    jobs.create_job(
        con,
        kind="claude_code",
        title="the todo app build",
        created_by="test",
        job_id=JOB,
        state="running",
        cwd=str(tmp_path),
    )
    con.close()
    return p


@pytest.fixture
def con(db_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(db_path)
    yield c
    c.close()


def pre_tool_input(tool: str = "AskUserQuestion", tool_input: dict | None = None) -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "session_id": "sess-1",
        "tool_name": tool,
        "tool_input": tool_input if tool_input is not None else QUESTIONS,
    }


def kinds(con: sqlite3.Connection) -> list[str]:
    return [e.kind for e in read_since(con, 0)]


def away(_: sqlite3.Connection) -> str:
    return "away"


def present(_: sqlite3.Connection) -> str:
    return "present"


async def test_every_tool_call_is_logged_once_per_tool_use_id(con: sqlite3.Connection) -> None:
    hooks = Hooks(con, JOB, presence_state=present)
    out = await hooks.pre_tool(pre_tool_input("Read", {"file_path": "a.py"}), "toolu_read1")
    assert out == {"continue_": True}
    # The same tool_use_id re-fires after a resume (S1). Logging it twice would
    # make the activity log claim the tool ran twice.
    await hooks.pre_tool(pre_tool_input("Read", {"file_path": "a.py"}), "toolu_read1")
    assert kinds(con).count("tool.used") == 1


async def test_at_the_desk_the_hook_gets_out_of_the_way(con: sqlite3.Connection) -> None:
    hooks = Hooks(con, JOB, presence_state=present)
    out = await hooks.pre_tool(pre_tool_input(), "toolu_present")
    # continue_, NOT an allow: a hook allow does not short-circuit the deny and
    # ask rules that follow it, and the question must reach can_use_tool.
    assert out == {"continue_": True}
    assert "permissionDecision" not in str(out)


async def test_the_question_is_durable_before_the_defer_is_even_asked_for(
    con: sqlite3.Connection, db_path: Path
) -> None:
    hooks = Hooks(con, JOB, presence_state=away)
    out = await hooks.pre_tool(pre_tool_input(), "toolu_defer1")
    assert out["hookSpecificOutput"]["permissionDecision"] == "defer"

    # Defer fires BEFORE can_use_tool and the process may be gone a millisecond
    # later, so the away channel's only copy of the question is this row — read
    # here on a SECOND connection, which is what the phone worker really is.
    other = connect(db_path)
    try:
        req = rq.find_answer(other, "toolu_defer1")
        assert req is None  # nobody has answered yet
        open_rows = rq.open_requests(other, JOB)
        assert [r.tool_use_id for r in open_rows] == ["toolu_defer1"]
        assert open_rows[0].payload == QUESTIONS
        assert [i["label"] for i in open_rows[0].presentation["items"]] == ["SQLite", "JSON file"]
    finally:
        other.close()


async def test_asking_for_a_defer_does_not_pretend_the_job_is_deferred(
    con: sqlite3.Connection,
) -> None:
    hooks = Hooks(con, JOB, presence_state=away)
    await hooks.pre_tool(pre_tool_input(), "toolu_defer2")
    job = jobs.get(con, JOB)
    assert job is not None
    # The CLI may ignore the defer. Recording the intention as a fact would make
    # the state machine lie, and 'deferred' has no legal move to 'blocked', so
    # the fall-back-to-blocking path could not even run.
    assert job.state == "running"
    assert "job.deferred" in kinds(con)


async def test_a_dropped_defer_is_detectable_exactly_once(con: sqlite3.Connection) -> None:
    ledger = DeferLedger()
    hooks = Hooks(con, JOB, deferrals=ledger, presence_state=away)
    await hooks.pre_tool(pre_tool_input(), "toolu_defer3")
    assert "toolu_defer3" in ledger
    assert ledger.take("toolu_defer3") is True
    # Read once: the same tool_use_id re-fires after a HONOURED defer too, and a
    # sticky flag would report "the defer was rejected" on the replay.
    assert ledger.take("toolu_defer3") is False


async def test_a_question_nobody_can_narrate_does_not_kill_the_run(
    con: sqlite3.Connection,
) -> None:
    hooks = Hooks(con, JOB, presence_state=away)
    out = await hooks.pre_tool(pre_tool_input("AskUserQuestion", {"questions": []}), "toolu_bad")
    # can_use_tool denies it with a message Claude can act on; raising here would
    # end the whole session over a payload we merely could not read aloud.
    assert out == {"continue_": True}
    assert rq.open_requests(con, JOB) == []
    assert "tool.denied" in kinds(con)


async def test_the_defer_decision_is_the_exact_shape_the_spike_measured(
    con: sqlite3.Connection,
) -> None:
    hooks = Hooks(con, JOB, presence_state=away)
    out = await hooks.pre_tool(pre_tool_input(), "toolu_shape")
    assert out == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "defer",
            "permissionDecisionReason": out["hookSpecificOutput"]["permissionDecisionReason"],
        }
    }
    assert isinstance(out["hookSpecificOutput"]["permissionDecisionReason"], str)


async def test_only_askuserquestion_is_ever_deferred(con: sqlite3.Connection) -> None:
    hooks = Hooks(con, JOB, presence_state=away)
    # Deferring a Bash call would orphan it: there is no question for a channel
    # to present and nothing for a human to answer.
    out = await hooks.pre_tool(pre_tool_input("Bash", {"command": "pytest"}), "toolu_bash")
    assert out == {"continue_": True}


async def test_asleep_defers_and_unknown_does_not(con: sqlite3.Connection) -> None:
    asleep = Hooks(con, JOB, presence_state=lambda _: "asleep")
    assert "hookSpecificOutput" in await asleep.pre_tool(pre_tool_input(), "toolu_asleep")
    # 'unknown' means every idle probe failed. Deferring on ignorance would turn
    # a broken probe into a system that never asks anything.
    unknown = Hooks(con, JOB, presence_state=lambda _: "unknown")
    assert await unknown.pre_tool(pre_tool_input(), "toolu_unknown") == {"continue_": True}


async def test_stop_and_task_completed_leave_a_trigger_behind(con: sqlite3.Connection) -> None:
    hooks = Hooks(con, JOB, presence_state=present)
    assert await hooks.stop({"hook_event_name": "Stop", "session_id": "s"}) == {"continue_": True}
    assert await hooks.task_completed({"hook_event_name": "TaskCompleted"}) == {"continue_": True}
    logged = kinds(con)
    assert "job.stopped" in logged
    assert "task.completed" in logged
    # Never {"decision": "block"}: that makes the model keep going, which is a
    # routing decision, and a hook does not get to make those.
    assert "block" not in str(logged)
