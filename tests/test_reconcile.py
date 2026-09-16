"""Reconcile and the briefing's project status.

Everything here is a restart or a race. ``reconcile()`` runs at the start of
EVERY process, which means the interesting cases are all "two of them at once"
and "the process that wrote this row is gone". The one thing it must never do is
guess: a job it cannot prove is dead stays exactly as it is, and the briefing
says so out loud instead of pretending.

``project_status`` is tested for what it SAYS, not for how it computes it — the
briefing is spoken to a person, and a section that is technically right and
unsayable is a bug.
"""

from __future__ import annotations

import sqlite3
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from jarvis import jobs, reconcile
from jarvis import requests as rq
from jarvis.db import connect, migrate
from jarvis.ids import now
from jarvis.jobs import shift_ts

# ───────────────────────────── fixtures ─────────────────────────────


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


def dead_identity() -> dict[str, object]:
    """Liveness columns for a process that really did exist and really is gone."""
    proc = subprocess.Popen(["sleep", "30"])
    ident = jobs.process_identity(proc.pid)
    proc.kill()
    proc.wait()
    return dict(ident)


def dead_job(con: sqlite3.Connection, title: str = "the todo app build", **kw: object) -> jobs.Job:
    job = jobs.create_job(
        con, kind="claude_code", title=title, created_by="desk", state="running", **dead_identity()
    )
    if kw:
        jobs.set_state(con, job.id, "running", **kw)
    return job


def live_job(con: sqlite3.Connection, title: str = "the live build") -> jobs.Job:
    return jobs.create_job(
        con,
        kind="claude_code",
        title=title,
        created_by="desk",
        state="running",
        **jobs.process_identity(),
    )


def a_request(con: sqlite3.Connection, job_id: str, label: str = "the database") -> rq.Request:
    return rq.create_request(
        con,
        kind="plan_question",
        short_label=label,
        presentation=rq.make_presentation(
            intro="How should todos be stored?", options=["SQLite", "JSON file"]
        ),
        payload={"questions": [{"question": "How should todos be stored?"}]},
        actor="runner",
        job_id=job_id,
    )


def ago(seconds: float) -> str:
    return jobs.shift_ts(now(), -seconds)


# ───────────────────────── orphaning ─────────────────────────


def test_a_job_whose_runner_died_becomes_orphaned(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    job = dead_job(con)
    spawned: list[jobs.Job] = []
    report = reconcile.reconcile(con, "dispatch", spawn=spawned.append)

    # ONE pass notices the death and picks the job back up. That convergence is
    # the point: a reboot and a four-hour phone answer take the same path.
    assert report["orphaned"] == [job.id]
    assert report["resumed"] == [job.id]
    assert [j.id for j in spawned] == [job.id]

    # From ANOTHER connection: the state is in the file, not in the reconciler.
    fresh = jobs.get(other, job.id)
    assert fresh is not None
    assert (fresh.state, fresh.resume_count) == ("starting", 1)
    assert fresh.stop_reason == "process gone"
    kinds = [
        r[0]
        for r in other.execute(
            "SELECT kind FROM events WHERE job_id=? ORDER BY seq", (job.id,)
        ).fetchall()
    ]
    assert kinds == ["job.created", "job.orphaned", "job.resumed"]


def test_a_process_that_cannot_spawn_never_spends_a_resume(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    """The voice app reconciles too, and it cannot start a runner.

    If a spawnerless pass claimed the resume it would burn one of the three
    attempts, park the job in 'starting' where nothing scans for a pickup, and
    leave the register claiming a runner exists. Three daemons booting after a
    power cut would exhaust RESUME_MAX between them and strand the build in
    needs_human without one runner ever having started.
    """
    job = dead_job(con)

    for actor in ("voice", "telegram", "runner"):
        report = reconcile.reconcile(con, actor)
        assert report["resumed"] == []
        assert report["resumable"] == [job.id]

    fresh = jobs.get(other, job.id)
    assert fresh is not None
    assert (fresh.state, fresh.resume_count) == ("orphaned", 0)
    assert (
        other.execute(
            "SELECT COUNT(*) FROM events WHERE job_id=? AND kind='job.resumed'", (job.id,)
        ).fetchone()[0]
        == 0
    )

    # And the dispatcher, arriving later, still finds it resumable.
    spawned: list[jobs.Job] = []
    assert reconcile.reconcile(con, "dispatch", spawn=spawned.append)["resumed"] == [job.id]
    assert [j.id for j in spawned] == [job.id]


def test_reconcile_is_idempotent_across_processes(db_path: Path) -> None:
    """It runs at the start of every process, so twice must equal once."""
    first = connect(db_path)
    job = dead_job(first)
    one = reconcile.reconcile(first, "dispatch", spawn=lambda j: None)
    first.close()

    second = connect(db_path)
    two = reconcile.reconcile(second, "dispatch", spawn=lambda j: None)
    assert one["orphaned"] == [job.id]
    assert two["orphaned"] == []
    # It was claimed for resume by the first pass, so the second must not touch it.
    assert jobs.get(second, job.id).resume_count == 1  # type: ignore[union-attr]
    three = reconcile.reconcile(second, "telegram")
    assert (three["orphaned"], three["resumed"], three["resumable"]) == ([], [], [])
    second.close()


def test_a_live_runner_is_left_alone(con: sqlite3.Connection) -> None:
    job = live_job(con)
    report = reconcile.reconcile(con)
    assert report["orphaned"] == []
    assert report["undetermined"] == []
    assert jobs.get(con, job.id).state == "running"  # type: ignore[union-attr]


def test_a_wedged_but_living_runner_is_reported_never_killed(con: sqlite3.Connection) -> None:
    """Going quiet during a long compile is not dying. Say it; do not act on it."""
    job = live_job(con)
    old = ago(reconcile.STALE_HEARTBEAT_S + 60)
    con.execute("UPDATE jobs SET heartbeat_at=?, updated_at=? WHERE id=?", (old, old, job.id))
    report = reconcile.reconcile(con)
    assert report["stale_heartbeat"] == [job.id]
    assert report["orphaned"] == []
    assert jobs.get(con, job.id).state == "running"  # type: ignore[union-attr]


def test_a_runner_that_just_did_something_is_not_wedged(con: sqlite3.Connection) -> None:
    """A stale heartbeat next to a fresh state change is not silence.

    A runner heartbeats at startup and then blocks on a question for ten
    minutes: it is alive, it is doing exactly what it should, and calling it
    wedged every pass would train the reader to ignore the report on the one
    morning it means something.
    """
    job = live_job(con)
    con.execute(
        "UPDATE jobs SET heartbeat_at=? WHERE id=?",
        (ago(reconcile.STALE_HEARTBEAT_S + 600), job.id),
    )
    req = a_request(con, job.id)
    jobs.mark_blocked(con, job.id, req.id)  # a real state change, just now

    report = reconcile.reconcile(con)
    assert report["stale_heartbeat"] == []
    assert report["orphaned"] == []


def test_a_job_still_being_spawned_is_given_its_grace(con: sqlite3.Connection) -> None:
    """Between the claim and the exec there is no pid. That is not an orphan yet."""
    job = jobs.create_job(
        con, kind="claude_code", title="the api", created_by="desk", state="starting"
    )
    assert reconcile.reconcile(con)["orphaned"] == []

    con.execute(
        "UPDATE jobs SET updated_at=?, heartbeat_at=NULL WHERE id=?",
        (ago(reconcile.STARTING_GRACE_S + 60), job.id),
    )
    report = reconcile.reconcile(con)
    assert report["orphaned"] == [job.id]
    assert jobs.get(con, job.id).stop_reason == "never reported a pid"  # type: ignore[union-attr]


def test_a_job_we_cannot_prove_is_dead_is_never_touched(
    con: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The macOS path. "I don't know" is a report, not a state change.

    Orphaning on unknown would respawn every live build on a platform with no
    /proc; leaving it silent would hide a genuinely dead one. So: leave it, and
    name it.
    """
    job = live_job(con)
    monkeypatch.setattr(jobs, "PROC", tmp_path / "no-proc-here")
    report = reconcile.reconcile(con)
    assert report["undetermined"] == [job.id]
    assert report["orphaned"] == []
    assert jobs.get(con, job.id).state == "running"  # type: ignore[union-attr]


def test_a_deferred_job_is_never_orphaned(con: sqlite3.Connection) -> None:
    """Its runner exited ON PURPOSE. "Gone" is the design, not a failure."""
    job = dead_job(con)
    req = a_request(con, job.id)
    jobs.mark_blocked(con, job.id, req.id, state="deferred")
    report = reconcile.reconcile(con)
    assert report["orphaned"] == []
    assert report["awaiting_answer"] == [job.id]
    assert jobs.get(con, job.id).state == "deferred"  # type: ignore[union-attr]


# ───────────────────────── resume convergence ─────────────────────────


def test_an_orphan_waiting_on_a_question_is_not_respawned(con: sqlite3.Connection) -> None:
    """Respawning would only re-ask a question already on somebody's phone."""
    job = dead_job(con)
    req = a_request(con, job.id)
    jobs.mark_blocked(con, job.id, req.id)

    report = reconcile.reconcile(con)
    assert report["orphaned"] == [job.id]
    assert report["awaiting_answer"] == [job.id]
    assert report["resumed"] == []
    assert jobs.get(con, job.id).resume_count == 0  # type: ignore[union-attr]


def test_the_answer_and_the_reboot_converge_on_one_path(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    """Four hours later, on the phone, in a different process.

    The deferred-with-an-answer path and the orphaned-after-a-reboot path are the
    same spawn. This is the one that makes "answer it tomorrow" work.
    """
    job = dead_job(con)
    req = a_request(con, job.id)
    jobs.mark_blocked(con, job.id, req.id, state="deferred")

    assert reconcile.reconcile(con)["resumed"] == []  # still pending: nothing to do

    # The phone answers, in its own process.
    assert rq.answer_request(other, req.id, {"text": "SQLite"}, "phone", "dtmf") is True

    spawned: list[jobs.Job] = []
    report = reconcile.reconcile(con, "dispatch", spawn=spawned.append)
    assert report["resumed"] == [job.id]
    assert [j.id for j in spawned] == [job.id]
    fresh = jobs.get(other, job.id)
    assert fresh is not None
    assert (fresh.state, fresh.resume_count) == ("starting", 1)
    # The dead runner's identity is gone, or the next reconcile would think the
    # new runner had already reported in.
    assert (fresh.pid, fresh.boot_id, fresh.proc_start_ticks) == (None, None, None)


def test_two_processes_reconciling_at_once_spawn_exactly_one_runner(db_path: Path) -> None:
    """Two daemons booting together is the normal case, not the exotic one.

    Without the claim both would spawn a runner for the same job, and two Claude
    Code processes would write to one repository.
    """
    setup = connect(db_path)
    job = dead_job(setup)
    jobs.set_state(setup, job.id, "orphaned")
    setup.close()

    gate = threading.Barrier(2)
    spawned: list[str] = []
    resumed: list[str] = []
    lock = threading.Lock()

    def boot(name: str) -> None:
        c = connect(db_path)
        try:
            gate.wait(timeout=5)
            report = reconcile.reconcile(c, name, spawn=lambda j: spawned.append(f"{name}:{j.id}"))
            with lock:
                resumed.extend(report["resumed"])
        finally:
            c.close()

    threads = [threading.Thread(target=boot, args=(f"p{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert resumed == [job.id]
    assert len(spawned) == 1
    after = connect(db_path)
    assert jobs.get(after, job.id).resume_count == 1  # type: ignore[union-attr]
    assert (
        after.execute(
            "SELECT COUNT(*) FROM events WHERE job_id=? AND kind='job.resumed'", (job.id,)
        ).fetchone()[0]
        == 1
    )
    after.close()


def test_a_job_that_keeps_dying_asks_for_a_human(con: sqlite3.Connection) -> None:
    """Three respawns is enough. A silent retry loop burns a rate-limit window."""
    job = dead_job(con)
    jobs.set_state(con, job.id, "orphaned", resume_count=reconcile.RESUME_MAX)
    report = reconcile.reconcile(con)
    assert report["needs_human"] == [job.id]
    assert report["resumed"] == []
    assert jobs.get(con, job.id).state == "orphaned"  # type: ignore[union-attr]


def test_resume_policy_manual_is_obeyed(con: sqlite3.Connection) -> None:
    job = dead_job(con)
    jobs.set_state(con, job.id, "orphaned", resume_policy="manual")
    report = reconcile.reconcile(con)
    assert report["needs_human"] == [job.id]
    assert jobs.get(con, job.id).state == "orphaned"  # type: ignore[union-attr]


def test_a_spawner_that_throws_cannot_stop_a_process_from_starting(
    con: sqlite3.Connection,
) -> None:
    """reconcile() is the first thing every process does. It must always return."""
    job = dead_job(con)

    def explode(j: jobs.Job) -> None:
        raise RuntimeError("systemd-run is not on PATH")

    report = reconcile.reconcile(con, "dispatch", spawn=explode)
    assert report["resumed"] == [job.id]
    assert report["spawn_errors"][0]["job_id"] == job.id
    assert "systemd-run" in report["spawn_errors"][0]["error"]
    # The claim stands, so the grace window will re-orphan and retry it.
    assert jobs.get(con, job.id).state == "starting"  # type: ignore[union-attr]


def test_a_finished_job_is_never_reconciled(con: sqlite3.Connection) -> None:
    """Its process is long gone; that is what finishing means."""
    job = dead_job(con)
    jobs.set_state(con, job.id, "done", result_summary="all tests pass")
    report = reconcile.reconcile(con)
    assert (report["orphaned"], report["resumed"], report["checked"]) == ([], [], 0)
    assert jobs.get(con, job.id).state == "done"  # type: ignore[union-attr]


# ───────────────────────── project status ─────────────────────────


def test_the_briefing_says_what_finished_what_failed_and_what_is_stuck(
    con: sqlite3.Connection,
) -> None:
    done = jobs.create_job(
        con, kind="claude_code", title="the todo app build", created_by="desk", state="running"
    )
    jobs.set_state(con, done.id, "done", result_summary="Seventeen tests pass.")
    broke = jobs.create_job(
        con, kind="claude_code", title="the scraper", created_by="desk", state="running"
    )
    jobs.set_state(con, broke.id, "failed", stop_reason="The build ran out of disk.")
    stuck = jobs.create_job(
        con, kind="claude_code", title="the api", created_by="desk", state="running"
    )
    req = a_request(con, stuck.id, "the database")
    jobs.mark_blocked(con, stuck.id, req.id)
    con.execute("UPDATE jobs SET blocked_since=? WHERE id=?", (ago(7500), stuck.id))

    status = reconcile.project_status(con, since=ago(86400))
    spoken = " ".join(status.lines)

    assert "the todo app build finished." in spoken
    assert "Seventeen tests pass." in spoken
    assert "the scraper failed." in spoken
    assert "The build ran out of disk." in spoken
    # How long, and what about — both, because "something is blocked" is useless.
    assert "the api has been waiting 2 hours and 5 minutes for an answer about the database." in (
        spoken
    )
    assert status.quiet is False
    assert [n.job_id for n in status.finished] == [done.id]
    assert [n.job_id for n in status.failed] == [broke.id]
    assert status.blocked[0].seconds is not None and status.blocked[0].seconds > 7000


def test_a_killed_job_is_not_called_a_failure(con: sqlite3.Connection) -> None:
    """The user almost always did it. "Failed" would be an accusation."""
    job = jobs.create_job(
        con, kind="claude_code", title="the api", created_by="desk", state="running"
    )
    jobs.set_state(con, job.id, "killed")
    spoken = " ".join(reconcile.project_status(con, since=ago(3600)).lines)
    assert "the api was stopped before it finished." in spoken
    assert "failed" not in spoken


def test_a_block_younger_than_the_threshold_is_kept_back_but_not_lost(
    con: sqlite3.Connection,
) -> None:
    """The briefing does not read out a question asked ninety seconds ago.

    It is still in the structured data, because "what else?" must be answerable.
    """
    job = jobs.create_job(
        con, kind="claude_code", title="the api", created_by="desk", state="running"
    )
    req = a_request(con, job.id)
    jobs.mark_blocked(con, job.id, req.id)
    con.execute("UPDATE jobs SET blocked_since=? WHERE id=?", (ago(90), job.id))

    status = reconcile.project_status(con, since=ago(3600))
    assert [n.job_id for n in status.blocked] == [job.id]
    assert not any("the api has been waiting" in line for line in status.lines)
    assert any("question is still waiting" in line for line in status.lines)


def test_nothing_to_report_is_one_honest_sentence(con: sqlite3.Connection) -> None:
    status = reconcile.project_status(con)
    assert status.quiet is True
    assert len(status.lines) == 1
    assert "Nothing has finished or failed" in status.lines[0]


def test_the_briefing_admits_what_it_cannot_determine(
    con: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The point of a three-valued liveness check, said out loud."""
    job = live_job(con, "the api")
    con.execute("UPDATE jobs SET heartbeat_at=? WHERE id=?", (ago(3600), job.id))
    monkeypatch.setattr(jobs, "PROC", tmp_path / "no-proc-here")

    status = reconcile.project_status(con, since=ago(86400))
    spoken = " ".join(status.lines)
    assert "I can't tell whether the api is still running" in spoken
    assert "1 hour" in spoken
    assert [n.job_id for n in status.undetermined] == [job.id]


def test_an_orphan_nobody_picked_up_is_admitted_too(con: sqlite3.Connection) -> None:
    job = dead_job(con, "the api")
    jobs.set_state(con, job.id, "orphaned", resume_policy="never")
    spoken = " ".join(reconcile.project_status(con, since=ago(3600)).lines)
    assert "the api stopped without finishing and I have not picked it back up." in spoken


def test_reading_the_briefing_does_not_consume_it(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    """A briefing composed and never delivered must be said again tomorrow.

    If project_status advanced the cursor, a dropped channel or an empty room
    would silently erase the only time that job was ever going to be mentioned.
    """
    job = jobs.create_job(
        con, kind="claude_code", title="the todo app build", created_by="desk", state="running"
    )
    jobs.set_state(con, job.id, "done")

    first = reconcile.project_status(con, since=ago(3600))
    assert [n.job_id for n in first.finished] == [job.id]
    assert reconcile.briefing_cursor(con) is None

    again = reconcile.project_status(con, since=ago(3600))
    assert [n.job_id for n in again.finished] == [job.id]

    # Only after it was actually spoken does the cursor move — and the move is
    # visible to every other process, because it is a row.
    #
    # The cursor is passed explicitly rather than defaulted to now(). The default
    # would collide with this job's updated_at about 80% of the time (measured),
    # because now() is millisecond-granular and the cursor is inclusive — which
    # made this assertion a coin flip rather than a statement about the cursor.
    # The collision behaviour itself is intended and has its own test below.
    reconcile.set_briefing_cursor(con, ts=shift_ts(jobs.get(con, job.id).updated_at, 0.001))
    assert reconcile.briefing_cursor(other) is not None
    assert reconcile.project_status(other).finished == ()


def test_a_job_finishing_on_the_cursor_boundary_is_repeated_not_dropped(
    con: sqlite3.Connection,
) -> None:
    """The cursor is inclusive on purpose. Repeating beats dropping.

    ``jobs.since`` matches ``updated_at >= cursor``, so a job that finishes in
    the same millisecond the cursor is written is mentioned again tomorrow.
    Hearing "the scraper build failed" twice is mildly annoying; the exclusive
    version would mean never hearing it at all.
    """
    job = jobs.create_job(
        con, kind="claude_code", title="the scraper", created_by="desk", state="running"
    )
    jobs.set_state(con, job.id, "failed")
    boundary = jobs.get(con, job.id).updated_at

    reconcile.set_briefing_cursor(con, ts=boundary)
    again = reconcile.project_status(con)
    assert [n.job_id for n in again.failed] == [job.id], (
        "a job on the cursor boundary must be repeated, never silently dropped"
    )


def test_the_default_window_is_the_briefing_cursor(con: sqlite3.Connection) -> None:
    old = jobs.create_job(
        con, kind="claude_code", title="yesterday's build", created_by="desk", state="running"
    )
    jobs.set_state(con, old.id, "done")
    con.execute("UPDATE jobs SET updated_at=? WHERE id=?", (ago(7200), old.id))
    reconcile.set_briefing_cursor(con, ago(3600))

    assert reconcile.project_status(con).finished == ()
    assert [n.job_id for n in reconcile.project_status(con, since=ago(86400)).finished] == [old.id]


def test_durations_are_said_the_way_a_person_would_say_them() -> None:
    phrase = reconcile._duration_phrase
    assert phrase(20) == "less than a minute"
    assert phrase(60) == "1 minute"
    assert phrase(2700) == "45 minutes"
    assert phrase(3600) == "1 hour"
    assert phrase(7500) == "2 hours and 5 minutes"
    assert phrase(86400) == "1 day"
    assert phrase(183600) == "2 days and 3 hours"
