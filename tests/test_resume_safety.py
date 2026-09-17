"""The resume path used to destroy the build it was meant to rescue.

``reconcile``'s phase 2 is kind-blind: it resumes anything deferred or orphaned.
The only spawner in the tree starts ``python -m jarvis.cc``, which drives Claude
Code. So a ``repo_setup`` row — a build request parked on its read-back — was
handed to the Claude Code driver, which failed it TERMINALLY.

And ``jarvis answer`` printed the command that did it, so the sentence a user saw
immediately after approving their build was the one that destroyed it.

These tests exist so that cannot come back. Each failure mode is asserted at the
layer that has to refuse it, because one guard is one commit away from being the
guard that was there.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from jarvis import __main__ as cli
from jarvis import jobs, reconcile
from jarvis import requests as rq
from jarvis.cc import gate
from jarvis.db import connect, migrate


@pytest.fixture
def con(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "jarvis.db")
    migrate(c)
    yield c
    c.close()


def parked_job(con: sqlite3.Connection, kind: str) -> jobs.Job:
    """A job of ``kind`` deferred on a question that has since been answered."""
    job = jobs.create_job(con, kind=kind, title="comment watcher", created_by="desk")
    jobs.set_state(con, job.id, "starting", actor="t")
    jobs.set_state(con, job.id, "running", actor="t")
    req = gate.ensure_request(
        con,
        tool_name="AskUserQuestion",
        input_data={
            "questions": [
                {
                    "question": "Shall I build that?",
                    "options": [{"label": "Build it"}, {"label": "No"}],
                }
            ]
        },
        job_id=job.id,
        tool_use_id=f"toolu_{kind}",
        actor="builder",
    )
    jobs.mark_blocked(con, job.id, req.id, actor="builder", state="deferred")
    rq.answer_request(con, req.id, {"answers": {"Shall I build that?": "Build it"}}, "cli", "hud")
    return jobs.get(con, job.id)


# ───────────────────── layer 1: reconcile must not claim it ─────────────────────


def test_a_build_request_is_never_handed_to_the_claude_code_spawner(
    con: sqlite3.Connection,
) -> None:
    spawned: list[tuple[str, str]] = []
    report = reconcile.reconcile(con, actor="cli", spawn=lambda j: spawned.append((j.id, j.kind)))
    build = parked_job(con, "repo_setup")
    spawned.clear()
    report = reconcile.reconcile(con, actor="cli", spawn=lambda j: spawned.append((j.id, j.kind)))

    assert spawned == [], "the Claude Code spawner was handed a build request"
    assert build.id in report["resumable"], "and it must still be reported as pickable-up"


def test_a_kind_it_cannot_start_is_not_claimed_at_all(con: sqlite3.Connection) -> None:
    """A claim spends one of three resume attempts before anything can refuse."""
    build = parked_job(con, "repo_setup")
    reconcile.reconcile(con, actor="cli", spawn=lambda j: None)
    fresh = jobs.get(con, build.id)
    assert fresh.resume_count == 0
    assert fresh.state == "deferred", "still parked, not moved to a state nothing scans"


def test_a_claude_code_job_is_still_resumed(con: sqlite3.Connection) -> None:
    """The guard must not break the thing it guards."""
    job = parked_job(con, "claude_code")
    spawned: list[str] = []
    report = reconcile.reconcile(con, actor="cli", spawn=lambda j: spawned.append(j.id))
    assert spawned == [job.id]
    assert job.id in report["resumed"]


def test_a_caller_may_widen_the_set_deliberately(con: sqlite3.Connection) -> None:
    build = parked_job(con, "repo_setup")
    spawned: list[str] = []
    reconcile.reconcile(
        con,
        actor="cli",
        spawn=lambda j: spawned.append(j.id),
        spawn_kinds=frozenset({"claude_code", "repo_setup"}),
    )
    assert spawned == [build.id]


# ───────────────────── layer 2: the driver must refuse it ─────────────────────


def test_the_driver_refuses_a_job_it_cannot_drive_before_touching_anything(
    con: sqlite3.Connection,
) -> None:
    """Defence in depth: one guard is one commit away from being the guard that was there."""
    from jarvis.cc.driver import ClaudeJobRunner, WrongJobKind

    build = parked_job(con, "repo_setup")
    runner = ClaudeJobRunner(con=con, job_id=build.id)
    with pytest.raises(WrongJobKind, match="repo_setup"):
        runner.job()
    assert jobs.get(con, build.id).state == "deferred", "nothing may have moved"


# ───────────────────── layer 3: the CLI must not suggest it ─────────────────────


def test_the_resume_command_is_chosen_by_kind(con: sqlite3.Connection) -> None:
    build = parked_job(con, "repo_setup")
    code = parked_job(con, "claude_code")
    assert cli.resume_command(build) == "python -m jarvis build"
    assert cli.resume_command(code) == "python -m jarvis run --resume"
    assert cli.resume_command(None) == "python -m jarvis run --resume"


def test_answering_a_build_never_prints_the_command_that_would_destroy_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole point. This sentence was printed to the user at the worst moment."""
    dbpath = tmp_path / "jarvis.db"
    con = connect(dbpath)
    try:
        migrate(con)
        job = jobs.create_job(con, kind="repo_setup", title="t", created_by="desk")
        jobs.set_state(con, job.id, "starting", actor="t")
        jobs.set_state(con, job.id, "running", actor="t")
        req = gate.ensure_request(
            con,
            tool_name="AskUserQuestion",
            input_data={
                "questions": [
                    {"question": "Shall I build that?", "options": [{"label": "Build it"}]}
                ]
            },
            job_id=job.id,
            tool_use_id="toolu_x",
            actor="builder",
        )
        jobs.mark_blocked(con, job.id, req.id, actor="builder", state="deferred")
    finally:
        con.close()

    assert cli.main(["--db", str(dbpath), "answer", req.id, "1"]) == 0
    out = capsys.readouterr().out
    assert "python -m jarvis build" in out
    assert "run --resume" not in out
