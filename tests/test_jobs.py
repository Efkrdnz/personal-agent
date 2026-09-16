"""The job registry, tested where it breaks.

Three kinds of test here and almost nothing else. RACES use two real connections
to one file, which is exactly what two OS processes look like to SQLite — its
locking is per connection, not per process. RESTARTS write on one connection,
close it, and assert from another, because every reader of this table is in a
process that may have started after the writer died. FAILURE PATHS are the
point: the happy path of "a job ran and finished" is not where a system whose
state outlives its processes goes wrong.

The liveness tests spawn real processes and kill them. A mocked /proc would
assert that the parser does what the parser does; a killed `sleep` asserts the
contract, which is that a dead runner is recognised as dead.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from jarvis import jobs
from jarvis.db import connect, migrate
from jarvis.ids import now

# ───────────────────────────── fixtures ─────────────────────────────

NEEDS_PROC = pytest.mark.skipif(not jobs.have_proc(), reason="liveness proof needs /proc")


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """A throwaway database file. Never the real one at ~/.local/state."""
    p = tmp_path / "jarvis.db"
    con = connect(p)
    migrate(con)
    con.close()
    return p


@pytest.fixture
def con(db_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(db_path)
    yield c
    c.close()


@pytest.fixture
def other(db_path: Path) -> Iterator[sqlite3.Connection]:
    """A SECOND connection: the dispatcher, or a runner that outlived the first."""
    c = connect(db_path)
    yield c
    c.close()


def make_job(con: sqlite3.Connection, **kw: object) -> jobs.Job:
    kw.setdefault("kind", "claude_code")
    kw.setdefault("title", "the todo app build")
    kw.setdefault("created_by", "desk")
    return jobs.create_job(con, **kw)  # type: ignore[arg-type]


def dead_process() -> tuple[int, int | None]:
    """A pid that is genuinely gone, with the start-ticks it had while alive."""
    proc = subprocess.Popen(["sleep", "30"])
    ticks = jobs.proc_start_ticks(proc.pid)
    proc.kill()
    proc.wait()
    return proc.pid, ticks


# ───────────────────── dontAsk: the CHECK constraint ─────────────────────


def test_dontask_is_refused_with_the_reason_not_a_constraint_name(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    with pytest.raises(jobs.ForbiddenPermissionMode) as e:
        make_job(con, permission_mode="dontAsk")
    # The caller must learn WHY, not "CHECK constraint failed".
    assert "AskUserQuestion" in str(e.value)
    assert "plan" in str(e.value)
    # And nothing was written — visible from another connection, not just ours.
    assert other.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_dontask_is_refused_on_update_too(con: sqlite3.Connection) -> None:
    job = make_job(con, permission_mode="plan")
    with pytest.raises(jobs.ForbiddenPermissionMode):
        jobs.set_state(con, job.id, "starting", permission_mode="dontAsk")
    assert jobs.get(con, job.id) is not None
    assert jobs.get(con, job.id).permission_mode == "plan"  # type: ignore[union-attr]
    assert jobs.get(con, job.id).state == "queued"  # type: ignore[union-attr]


def test_schema_still_refuses_dontask_if_python_is_bypassed(con: sqlite3.Connection) -> None:
    """The Python guard is the message; the CHECK is the guarantee.

    A future writer that does not go through this module must still be stopped,
    so assert the constraint itself is live rather than trusting our own check.
    """
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO jobs (id, kind, title, state, created_at, updated_at, created_by,"
            " permission_mode) VALUES ('job_raw','claude_code','x','queued',?,?,'test','dontAsk')",
            (now(), now()),
        )


def test_other_permission_modes_are_allowed(con: sqlite3.Connection) -> None:
    for mode in ("plan", "default", "acceptEdits", "bypassPermissions"):
        job = make_job(con, permission_mode=mode)
        assert jobs.get(con, job.id).permission_mode == mode  # type: ignore[union-attr]


# ───────────────────────── transitions ─────────────────────────


@pytest.mark.parametrize("terminal", sorted(jobs.TERMINAL_STATES))
@pytest.mark.parametrize("target", ["running", "queued", "blocked", "starting", "orphaned"])
def test_a_terminal_job_never_moves_again(
    con: sqlite3.Connection, terminal: str, target: str
) -> None:
    job = make_job(con, state="running")
    jobs.set_state(con, job.id, terminal)  # type: ignore[arg-type]
    with pytest.raises(jobs.IllegalTransition):
        jobs.set_state(con, job.id, target)  # type: ignore[arg-type]
    assert jobs.get(con, job.id).state == terminal  # type: ignore[union-attr]


def test_a_done_job_cannot_be_resurrected_from_another_process(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    """The race the state machine exists for.

    ``other`` is opened BEFORE the job finishes — a runner that has not noticed
    it lost. Its write must still be refused, because the read and the write
    happen inside one BEGIN IMMEDIATE and SQLite serialises writers.
    """
    job = make_job(con, state="running")
    other.execute("SELECT 1").fetchone()  # this connection is live and has a snapshot
    jobs.set_state(con, job.id, "done", result_summary="all tests pass")

    with pytest.raises(jobs.IllegalTransition):
        jobs.set_state(other, job.id, "running")

    assert jobs.get(other, job.id).state == "done"  # type: ignore[union-attr]
    assert jobs.get(other, job.id).result_summary == "all tests pass"  # type: ignore[union-attr]


def test_repeating_a_state_is_free_and_silent(con: sqlite3.Connection) -> None:
    """reconcile() runs in every process; "no change" must not raise or log."""
    job = make_job(con, state="running")
    jobs.set_state(con, job.id, "orphaned")
    before = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    stamp = "2020-01-01T00:00:00.000Z"
    con.execute("UPDATE jobs SET updated_at=? WHERE id=?", (stamp, job.id))
    jobs.set_state(con, job.id, "orphaned")
    jobs.set_state(con, job.id, "orphaned")
    assert con.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before
    # Free AND silent: a no-op must not touch updated_at either, or every
    # reconcile in every process would refresh the clock the briefing reads.
    assert jobs.get(con, job.id).updated_at == stamp  # type: ignore[union-attr]
    # A no-op that carries a real field change is still a write.
    jobs.set_state(con, job.id, "orphaned", stop_reason="power cut")
    fresh = jobs.get(con, job.id)
    assert fresh is not None
    assert (fresh.stop_reason, fresh.updated_at > stamp) == ("power cut", True)


def test_expect_makes_the_write_a_compare_and_swap(con: sqlite3.Connection) -> None:
    job = make_job(con, state="running")
    jobs.set_state(con, job.id, "blocked", blocked_request_id="req_x")
    with pytest.raises(jobs.IllegalTransition) as e:
        jobs.set_state(con, job.id, "orphaned", expect="running")
    assert "another process moved it first" in str(e.value)
    assert jobs.get(con, job.id).state == "blocked"  # type: ignore[union-attr]


def test_unknown_state_and_unknown_job_are_distinguishable(con: sqlite3.Connection) -> None:
    job = make_job(con)
    with pytest.raises(ValueError, match="unknown job state"):
        jobs.set_state(con, job.id, "sleeping")  # type: ignore[arg-type]
    with pytest.raises(jobs.UnknownJob):
        jobs.set_state(con, "job_nope", "starting")


def test_a_state_this_module_does_not_know_refuses_instead_of_crashing(
    con: sqlite3.Connection,
) -> None:
    """reconcile() runs in EVERY process, so a strange row must not KeyError.

    A raw writer or a future migration can put a state in the column that this
    machine has never heard of. Refusing the move is correct; taking down the
    voice app on startup is not.
    """
    job = make_job(con)
    con.execute("UPDATE jobs SET state='haunted' WHERE id=?", (job.id,))
    with pytest.raises(jobs.IllegalTransition) as e:
        jobs.set_state(con, job.id, "running")
    assert "nothing" in str(e.value)
    assert jobs.get(con, job.id).state == "haunted"  # type: ignore[union-attr]


def test_a_caller_key_never_reaches_sql(con: sqlite3.Connection) -> None:
    """The **fields whitelist. The column name is interpolated; values are bound."""
    job = make_job(con)
    with pytest.raises(ValueError, match="not settable"):
        jobs.set_state(con, job.id, "starting", **{"title=?, state='done' --": "x"})
    with pytest.raises(ValueError, match="not settable"):
        jobs.set_state(con, job.id, "starting", created_at="1970-01-01T00:00:00.000Z")
    assert jobs.get(con, job.id).state == "queued"  # type: ignore[union-attr]


# ───────────────────────── blocking ─────────────────────────


def test_blocking_on_nothing_is_refused(con: sqlite3.Connection) -> None:
    """A job parked on no request can never be unparked by anyone."""
    job = make_job(con, state="running")
    with pytest.raises(ValueError, match="blocked_request_id"):
        jobs.set_state(con, job.id, "blocked")
    with pytest.raises(ValueError, match="blocking on nothing"):
        jobs.mark_blocked(con, job.id, "")


def test_a_job_cannot_be_born_parked_on_nothing(con: sqlite3.Connection) -> None:
    """The front door needs the same rule as the transition, or it is a way past it."""
    for state in ("blocked", "deferred"):
        with pytest.raises(ValueError, match="blocked_request_id"):
            make_job(con, state=state)
    born = make_job(con, state="deferred", blocked_request_id="req_1")
    assert born.state == "deferred"


def test_deferring_a_block_does_not_restart_the_clock(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    """blocked -> deferred is the path where the wait gets LONG.

    The user turns out to be away, so the runner exits and the job is parked on
    the same question it was already blocked on. If the handoff reset
    blocked_since, the ">15 minutes gets said out loud in the morning" backstop
    would be reset by the very transition that means nobody is at the desk — the
    one case it exists for.
    """
    job = make_job(con, state="running")
    jobs.mark_blocked(con, job.id, "req_1")
    old = "2020-01-01T00:00:00.000Z"
    con.execute("UPDATE jobs SET blocked_since=? WHERE id=?", (old, job.id))

    jobs.mark_blocked(other, job.id, "req_1", state="deferred")  # the runner exits

    fresh = jobs.get(con, job.id)
    assert fresh is not None
    assert (fresh.state, fresh.blocked_since) == ("deferred", old)
    assert [j.id for j in jobs.blocked_longer_than(con, 900)] == [job.id]
    # A real move is still logged, even though the clock kept running.
    kinds = [
        r[0]
        for r in con.execute(
            "SELECT kind FROM events WHERE job_id=? ORDER BY seq", (job.id,)
        ).fetchall()
    ]
    assert kinds == ["job.created", "job.blocked", "job.deferred"]


def test_re_announcing_the_same_block_does_not_restart_the_clock(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    """The footgun that would hide a stall from the morning briefing forever.

    A resumed driver re-fires the identical question (S1: tool_use_id is stable),
    so mark_blocked is called again for a block that started hours ago. If that
    reset blocked_since, the ">15 minutes means say it out loud" backstop would
    never fire and a question nobody heard would stay unheard.
    """
    job = make_job(con, state="running")
    jobs.mark_blocked(con, job.id, "req_1")
    old = "2020-01-01T00:00:00.000Z"
    con.execute("UPDATE jobs SET blocked_since=? WHERE id=?", (old, job.id))

    stamp = "2020-01-01T00:00:01.000Z"
    con.execute("UPDATE jobs SET updated_at=? WHERE id=?", (stamp, job.id))

    jobs.mark_blocked(other, job.id, "req_1")  # the resumed driver, another process

    fresh = jobs.get(con, job.id)
    assert fresh is not None
    assert fresh.blocked_since == old
    assert jobs.blocked_longer_than(con, 900) != []
    # Nothing changed, so nothing may look like it did: updated_at is what
    # reconcile measures silence with, and a re-announcement every thirty
    # seconds would keep a wedged runner looking freshly active forever.
    assert fresh.updated_at == stamp


def test_blocking_on_a_different_question_does_restart_the_clock(
    con: sqlite3.Connection,
) -> None:
    job = make_job(con, state="running")
    jobs.mark_blocked(con, job.id, "req_1")
    con.execute("UPDATE jobs SET blocked_since=? WHERE id=?", ("2020-01-01T00:00:00.000Z", job.id))
    jobs.mark_blocked(con, job.id, "req_2")
    fresh = jobs.get(con, job.id)
    assert fresh is not None
    assert fresh.blocked_request_id == "req_2"
    assert fresh.blocked_since > "2024-01-01T00:00:00.000Z"
    assert jobs.blocked_longer_than(con, 900) == []


def test_finishing_clears_the_block(con: sqlite3.Connection) -> None:
    """A finished job in the 'still waiting on you' list would be a lie forever."""
    job = make_job(con, state="running")
    jobs.mark_blocked(con, job.id, "req_1")
    jobs.set_state(con, job.id, "done")
    fresh = jobs.get(con, job.id)
    assert fresh is not None
    assert (fresh.blocked_request_id, fresh.blocked_since) == (None, None)
    assert jobs.blocked_longer_than(con, 0) == []


def test_unblock_returns_to_running(con: sqlite3.Connection) -> None:
    job = make_job(con, state="running")
    jobs.mark_blocked(con, job.id, "req_1")
    fresh = jobs.unblock(con, job.id)
    assert (fresh.state, fresh.blocked_request_id, fresh.blocked_since) == ("running", None, None)


# ───────────────────────── restart survival ─────────────────────────


def test_the_row_is_the_job(db_path: Path) -> None:
    """Write with one connection, close it, read with another. No memory anywhere."""
    first = connect(db_path)
    job = jobs.create_job(
        first, kind="claude_code", title="the scraper", created_by="desk", state="running"
    )
    jobs.mark_blocked(first, job.id, "req_7")
    first.close()

    second = connect(db_path)
    fresh = jobs.get(second, job.id)
    assert fresh is not None
    assert (fresh.state, fresh.blocked_request_id) == ("blocked", "req_7")
    assert fresh.title == "the scraper"
    assert [j.id for j in jobs.list_by_state(second, "blocked")] == [job.id]
    second.close()


def test_a_write_is_never_left_in_an_open_transaction(db_path: Path) -> None:
    """Spike S1's bug: a deferred transaction rolled back on close(), silently.

    A write that vanishes when the writing process exits is indistinguishable
    from a protocol failure, so assert autocommit at the seam rather than
    trusting it.
    """
    writer = connect(db_path)
    job = jobs.create_job(writer, kind="briefing", title="the morning briefing", created_by="cron")
    jobs.set_state(writer, job.id, "starting")
    assert not writer.in_transaction
    writer.close()  # no commit, no context manager, nothing

    reader = connect(db_path)
    assert jobs.get(reader, job.id).state == "starting"  # type: ignore[union-attr]
    reader.close()


def test_every_transition_reaches_the_activity_log(con: sqlite3.Connection) -> None:
    """The bus IS the log: a transition nobody can see afterwards did not happen."""
    job = make_job(con, state="running")
    jobs.mark_blocked(con, job.id, "req_1")
    jobs.unblock(con, job.id)
    jobs.set_state(con, job.id, "done")
    kinds = [
        r[0]
        for r in con.execute(
            "SELECT kind FROM events WHERE job_id=? ORDER BY seq", (job.id,)
        ).fetchall()
    ]
    assert kinds == ["job.created", "job.blocked", "job.started", "job.finished"]


# ───────────────────────── heartbeat ─────────────────────────


def test_heartbeat_refuses_to_warm_a_finished_job(con: sqlite3.Connection) -> None:
    """A runner that has not noticed it was killed must not keep the row warm."""
    job = make_job(con, state="running")
    assert jobs.heartbeat(con, job.id) is True
    jobs.set_state(con, job.id, "killed")
    assert jobs.heartbeat(con, job.id) is False
    assert jobs.heartbeat(con, "job_nope") is False


def test_heartbeat_does_not_look_like_a_state_change(con: sqlite3.Connection) -> None:
    """updated_at means "the state changed". The briefing measures staleness with it."""
    job = make_job(con, state="running")
    con.execute("UPDATE jobs SET updated_at=? WHERE id=?", ("2020-01-01T00:00:00.000Z", job.id))
    jobs.heartbeat(con, job.id)
    fresh = jobs.get(con, job.id)
    assert fresh is not None
    assert fresh.updated_at == "2020-01-01T00:00:00.000Z"
    assert fresh.heartbeat_at is not None and fresh.heartbeat_at > "2024-01-01T00:00:00.000Z"


# ───────────────────────── liveness ─────────────────────────


def test_stat_field_22_survives_a_process_name_full_of_parentheses() -> None:
    """comm can contain spaces AND ')'. Naive split() reads the wrong field.

    This is not a hypothetical: any program can set its own name. Parsing from
    the LAST ')' is the only correct way, and getting it wrong would make every
    job of such a process look dead — or worse, look alive when the number
    happened to match.
    """
    fields = ["S", "1", "4217", "4217", "0", "-1", "0"]  # fields 3..9
    fields += [str(n) for n in range(10, 22)]  # fields 10..21
    fields += ["9876543"]  # field 22: starttime
    evil = "4217 (my ) weird (app) ) " + " ".join(fields)
    assert jobs._parse_stat_starttime(evil) == 9876543
    # A naive whole-line split reads field 22 from the wrong offset entirely.
    assert evil.split()[21] != "9876543"
    assert jobs._parse_stat_starttime("no parens here") is None
    assert jobs._parse_stat_starttime("1 (sh) S 1 1") is None


@NEEDS_PROC
def test_start_ticks_agree_with_proc_for_this_process() -> None:
    mine = jobs.proc_start_ticks(os.getpid())
    assert mine is not None and mine > 0
    assert jobs.proc_start_ticks(os.getpid()) == mine  # stable, not a clock reading


@NEEDS_PROC
def test_a_pid_reused_after_a_reboot_is_not_alive() -> None:
    """THE reason boot_id is on the row.

    The pid below is this very test process, so it is unambiguously alive — but
    it was recorded in a previous boot, which means whatever we started then is
    gone and something else now holds the number. Anything that believed this
    pid would leave a dead build sitting in 'running' forever.
    """
    ident = jobs.process_identity()
    pid, boot, ticks = ident["pid"], ident["boot_id"], ident["proc_start_ticks"]
    assert jobs.process_liveness(pid, boot, ticks) == "alive"

    stale_boot = "00000000-0000-0000-0000-000000000000"
    assert stale_boot != boot
    assert jobs.process_liveness(pid, stale_boot, ticks) == "dead"
    assert jobs.process_alive(pid, stale_boot, ticks) is False


@NEEDS_PROC
def test_is_alive_is_false_for_a_row_from_a_previous_boot(con: sqlite3.Connection) -> None:
    job = make_job(
        con,
        state="running",
        pid=os.getpid(),
        pgid=os.getpgid(0),
        boot_id="00000000-0000-0000-0000-000000000000",
        proc_start_ticks=jobs.proc_start_ticks(os.getpid()),
    )
    fresh = jobs.get(con, job.id)
    assert fresh is not None
    assert jobs.is_alive(fresh) is False
    assert jobs.job_liveness(fresh) == "dead"


@NEEDS_PROC
def test_a_pid_reused_within_one_boot_is_caught_by_start_ticks() -> None:
    """Same boot, same pid, different process: the start time disagrees."""
    ident = jobs.process_identity()
    assert jobs.process_liveness(ident["pid"], ident["boot_id"], 1) == "dead"


@NEEDS_PROC
def test_a_killed_process_is_dead() -> None:
    pid, ticks = dead_process()
    assert jobs.process_liveness(pid, jobs.boot_id(), ticks) == "dead"


@NEEDS_PROC
def test_a_live_process_we_never_identified_is_unknown_not_alive() -> None:
    """Half the evidence proves nothing, and "probably" is not an answer here."""
    assert jobs.process_liveness(os.getpid(), None, None) == "unknown"
    assert jobs.process_liveness(os.getpid(), jobs.boot_id(), None) == "unknown"
    assert jobs.process_alive(os.getpid(), None, None) is False
    # Start-ticks with no boot id is the subtle half: ticks count FROM boot, so a
    # row written before a reboot can match a fresh process whose start offset
    # coincides, and nothing on the row can contradict it. Not "alive".
    assert jobs.process_liveness(os.getpid(), None, jobs.proc_start_ticks(os.getpid())) == "unknown"


def test_no_pid_recorded_is_dead_not_unknown() -> None:
    """A job claiming to run with no pid has provably no process of ours."""
    assert jobs.process_liveness(None, jobs.boot_id(), 1) == "dead"
    assert jobs.process_liveness(0, None, None) == "dead"


def test_without_proc_we_never_claim_a_process_is_ours(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The macOS path, stated rather than guessed.

    With no /proc there is no boot id and no start time, so a pid can be
    disproved (ESRCH) but never attributed. The honest answers are 'dead' and
    'unknown'; 'alive' is unreachable, and reconcile leaves 'unknown' alone.
    """
    monkeypatch.setattr(jobs, "PROC", tmp_path / "no-proc-here")
    assert jobs.have_proc() is False
    assert jobs.boot_id() is None

    ident_pid = os.getpid()
    assert jobs.process_liveness(ident_pid, None, None) == "unknown"
    # Even a boot id recorded on Linux earlier cannot be contradicted here.
    assert jobs.process_liveness(ident_pid, "some-old-boot", 1234) == "unknown"
    assert jobs.process_alive(ident_pid, None, None) is False

    pid, _ = dead_process()
    assert jobs.process_liveness(pid, None, None) == "dead"


def test_process_identity_records_all_four_columns() -> None:
    ident = jobs.process_identity()
    assert set(ident) == {"pid", "pgid", "boot_id", "proc_start_ticks"}
    assert ident["pid"] == os.getpid()
    assert ident["pgid"] == os.getpgid(0)


@NEEDS_PROC
def test_attach_process_makes_a_job_provably_alive(con: sqlite3.Connection) -> None:
    job = make_job(con)
    jobs.set_state(con, job.id, "starting")
    attached = jobs.attach_process(con, job.id)
    assert attached.state == "running"
    assert jobs.is_alive(attached) is True
    assert attached.pgid == os.getpgid(0)  # the kill switch signals this


# ───────────────────────── resume claim ─────────────────────────


def test_claim_resume_clears_the_dead_process_identity(con: sqlite3.Connection) -> None:
    """The old pid must not convince the next reconcile that a runner is alive."""
    pid, ticks = dead_process()
    job = make_job(
        con, state="running", pid=pid, pgid=pid, boot_id=jobs.boot_id(), proc_start_ticks=ticks
    )
    jobs.set_state(con, job.id, "orphaned")
    claimed = jobs.claim_resume(
        con, job.id, actor="t", expect_state="orphaned", expect_resume_count=0
    )
    assert claimed is not None
    assert (claimed.state, claimed.resume_count) == ("starting", 1)
    assert (claimed.pid, claimed.boot_id, claimed.proc_start_ticks) == (None, None, None)
    assert claimed.heartbeat_at is not None  # the grace window starts now


def test_losing_the_claim_is_normal_not_an_exception(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    job = make_job(con, state="running")
    jobs.set_state(con, job.id, "orphaned")
    won = jobs.claim_resume(con, job.id, actor="a", expect_state="orphaned", expect_resume_count=0)
    lost = jobs.claim_resume(
        other, job.id, actor="b", expect_state="orphaned", expect_resume_count=0
    )
    assert won is not None
    assert lost is None
    assert jobs.get(other, job.id).resume_count == 1  # type: ignore[union-attr]


def test_two_processes_claiming_at_once_produce_one_resume(db_path: Path) -> None:
    """The real race: reconcile runs at the start of EVERY process.

    Without the compare-and-swap both would spawn a runner and the same build
    would exist twice, writing to the same repository.
    """
    setup = connect(db_path)
    job = jobs.create_job(setup, kind="claude_code", title="the api", created_by="desk")
    jobs.set_state(setup, job.id, "starting")
    jobs.set_state(setup, job.id, "orphaned")
    setup.close()

    gate = threading.Barrier(2)
    results: list[jobs.Job | None] = []
    lock = threading.Lock()

    def claim(name: str) -> None:
        c = connect(db_path)
        try:
            gate.wait(timeout=5)
            got = jobs.claim_resume(
                c, job.id, actor=name, expect_state="orphaned", expect_resume_count=0
            )
            with lock:
                results.append(got)
        finally:
            c.close()

    threads = [threading.Thread(target=claim, args=(f"p{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert sum(1 for r in results if r is not None) == 1
    after = connect(db_path)
    assert jobs.get(after, job.id).resume_count == 1  # type: ignore[union-attr]
    resumed = after.execute(
        "SELECT COUNT(*) FROM events WHERE job_id=? AND kind='job.resumed'", (job.id,)
    ).fetchone()[0]
    assert resumed == 1
    after.close()


# ───────────────────────── creation details ─────────────────────────


def test_a_claude_code_job_owns_its_session_id_before_any_process_exists(
    con: sqlite3.Connection,
) -> None:
    """Resume needs the UUID to predate the runner; it is ours, not the CLI's."""
    job = make_job(con)
    assert job.cc_session_id is not None and len(job.cc_session_id) == 36
    given = make_job(con, cc_session_id="11111111-2222-3333-4444-555555555555")
    assert given.cc_session_id == "11111111-2222-3333-4444-555555555555"
    briefing = jobs.create_job(con, kind="briefing", title="the briefing", created_by="cron")
    assert briefing.cc_session_id is None


def test_list_by_state_rejects_a_state_that_does_not_exist(con: sqlite3.Connection) -> None:
    """A typo'd state would silently return nothing, which reads as "all clear"."""
    with pytest.raises(ValueError, match="unknown job state"):
        jobs.list_by_state(con, ["running", "runing"])
    assert jobs.list_by_state(con, []) == []


def test_running_includes_everything_underway(con: sqlite3.Connection) -> None:
    """ "What's running?" means "what have you not finished", including blocked."""
    a = make_job(con, title="a", state="running")
    b = make_job(con, title="b", state="running")
    jobs.mark_blocked(con, b.id, "req_1")
    c = make_job(con, title="c", state="running")
    jobs.set_state(con, c.id, "done")
    d = make_job(con, title="d")  # queued: nobody has started it
    assert {j.id for j in jobs.running(con)} == {a.id, b.id}
    assert d.state == "queued"


def test_a_shifted_timestamp_is_the_same_shape_as_a_written_one() -> None:
    """Every "how long has this been like that" compares these as STRINGS.

    shift_ts formats a timestamp of its own, so if jarvis.ids.now ever changed
    width — microseconds, an offset instead of Z — the comparisons in
    blocked_longer_than and in reconcile would go silently wrong rather than
    fail. Pin the two shapes together.
    """
    stamp = now()
    assert len(jobs.shift_ts(stamp, 0)) == len(stamp)
    assert jobs.shift_ts(stamp, 0) == stamp
    assert jobs.shift_ts(stamp, -90) < stamp < jobs.shift_ts(stamp, 90)
    # Across a day boundary the string order must still be the time order.
    midnight = "2026-09-17T00:00:00.500Z"
    assert jobs.shift_ts(midnight, -1) == "2026-09-16T23:59:59.500Z"
    assert jobs.shift_ts(midnight, -1) < midnight


def test_jobstore_is_a_facade_over_the_caller_s_connection(con: sqlite3.Connection) -> None:
    """ToolCtx.jobs needs an object; it must not own or open a connection."""
    store = jobs.JobStore(con)
    job = store.create(kind="claude_code", title="the todo app build", created_by="desk")
    assert store.get(job.id) is not None
    assert store.con is con
