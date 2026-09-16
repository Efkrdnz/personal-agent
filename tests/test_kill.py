"""The kill switch, tested across process boundaries because that is the point.

The DTMF handler lives in jarvis-phone. The runner it must stop was spawned
detached by jarvis-dispatch. Neither holds a reference to the other and neither
is the other's parent, so every test here either uses TWO CONNECTIONS to one
file or a REAL SUBPROCESS that the killing connection never had a handle to. A
test that issued and executed a kill through one object would prove nothing the
architecture cares about.

Three failure paths carry the module and they are all here:

* the ISSUER DIES before anyone reads the row (the spike's silent-rollback bug);
* the KILL ARRIVES LATE, at a process that woke up after the command expired —
  which is why the epoch exists and why it has no expiry;
* the RECORDED PID IS NO LONGER OURS, which without the identity check means the
  kill switch SIGKILLs whatever innocent process inherited that number.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path

import pytest

from jarvis import kill
from jarvis.db import connect, migrate
from jarvis.ids import now
from jarvis.jobs import create_job, get, have_proc, process_identity, set_state, shift_ts

needs_proc = pytest.mark.skipif(not have_proc(), reason="needs /proc for process identity")


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


def spawn(code: str) -> subprocess.Popen[bytes]:
    """A real process in its own session, so it has a process group of its own."""
    return subprocess.Popen([sys.executable, "-c", code], start_new_session=True)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = Path(f"/proc/{pid}/stat").read_text()
    return stat[stat.rfind(")") + 1 :].split()[0] != "Z"


def wait_gone(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.02)
    return False


# ───────────────────── one connection issues, another executes ─────────────────────


def test_a_command_issued_on_one_connection_is_seen_and_acked_on_another(
    db_path: Path,
) -> None:
    """The whole contract of this module in one test.

    The issuing connection is CLOSED before the reader opens, because in the real
    system the DTMF handler may well have hung up and exited by the time dispatch
    notices.
    """
    phone = connect(db_path)
    command_id = kill.stop_everything(phone, "dtmf", "user pressed star nine")
    phone.close()

    dispatch = connect(db_path)
    try:
        pending = kill.pending_commands(dispatch, actor="dispatch")
        assert [c.id for c in pending] == [command_id]
        assert pending[0].verb == "stop_all"
        assert pending[0].args["origin"] == "dtmf"

        assert kill.claim_command(dispatch, command_id, "dispatch") is True
        assert kill.ack_command(dispatch, command_id, "dispatch", result="killed 0 jobs") is True
    finally:
        dispatch.close()

    auditor = connect(db_path)
    try:
        acks = kill.acks_for(auditor, command_id)
        assert [(a.actor, a.result) for a in acks] == [("dispatch", "killed 0 jobs")]
        assert kill.pending_commands(auditor, actor="dispatch") == []
    finally:
        auditor.close()


def test_the_write_survives_a_connection_that_never_committed(db_path: Path) -> None:
    """The spike's bug, pinned forever.

    ``sqlite3.connect`` without ``isolation_level=None`` opens a deferred
    transaction and ``close()`` rolls the write back SILENTLY — indistinguishable
    from a protocol failure, and it cost the spike an afternoon. A kill that
    evaporates when its issuer exits is the worst possible version of this bug.
    """
    issuer = connect(db_path)
    command_id = kill.stop_everything(issuer, "telegram", "/kill")
    epoch = kill.current_epoch(issuer)
    issuer.close()  # no commit, no context manager, nothing

    after = connect(db_path)
    try:
        assert kill.get_command(after, command_id) is not None
        assert kill.current_epoch(after) == epoch == 1
    finally:
        after.close()


def test_pending_is_driven_by_acks_so_every_actor_sees_it_independently(
    con: sqlite3.Connection,
) -> None:
    command_id = kill.stop_everything(con, "voice", "jarvis full stop")

    for actor in ("runner:job_a", "runner:job_b", "voice"):
        assert [c.id for c in kill.pending_commands(con, actor=actor)] == [command_id]

    kill.ack_command(con, command_id, "runner:job_a", result="stopped")
    assert kill.pending_commands(con, actor="runner:job_a") == []
    assert [c.id for c in kill.pending_commands(con, actor="runner:job_b")] == [command_id]


# ───────────────────── three triggers, one path ─────────────────────


def test_the_three_triggers_differ_only_in_who_pulled_them(db_path: Path) -> None:
    """Not three code paths that agree — one code path with three callers."""
    rows = []
    for trigger in ("voice", "dtmf", "telegram"):
        c = connect(db_path)
        cmd = kill.get_command(c, kill.stop_everything(c, trigger, "stop"))
        assert cmd is not None
        rows.append(cmd)
        c.close()

    assert {r.verb for r in rows} == {"stop_all"}
    assert {r.target_kind for r in rows} == {"all"}
    assert [r.issued_by for r in rows] == ["voice", "dtmf", "telegram"]
    assert [r.args["origin"] for r in rows] == ["voice", "dtmf", "telegram"]
    # One path also means one epoch bump each, in order.
    assert [r.args["epoch"] for r in rows] == [1, 2, 3]


def test_a_trigger_must_name_itself(con: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="issued_by"):
        kill.stop_everything(con, "", "anonymous kill")


def test_stop_everything_hangs_up_every_live_call(con: sqlite3.Connection) -> None:
    """Killing a build does not end a call, and "stop" means both."""
    ts = now()
    for cid, state in (("ch_live", "attached"), ("ch_old", "detached")):
        con.execute(
            """INSERT INTO channels (id, kind, state, caps, attached_at, last_heartbeat_at)
                 VALUES (?, 'phone', ?, '{}', ?, ?)""",
            (cid, state, ts, ts),
        )
    stop_id = kill.stop_everything(con, "voice", "stop")

    hangups = [c for c in kill.pending_commands(con, actor="phone") if c.verb == "hangup"]
    assert [h.target_id for h in hangups] == ["ch_live"]
    assert hangups[0].args["stop_command_id"] == stop_id


# ───────────────────── the epoch ─────────────────────


def test_a_virgin_system_is_epoch_zero_without_a_seed_row(con: sqlite3.Connection) -> None:
    """No row and epoch 0 must mean the same thing, or two processes race to seed."""
    assert con.execute("SELECT * FROM kill_epoch").fetchall() == []
    assert kill.current_epoch(con) == 0
    assert kill.epoch_is_current(con, 0)


def test_concurrent_bumps_never_lose_one(db_path: Path) -> None:
    """Read-then-write would lose a bump, and a lost bump un-dooms a job.

    The user mashing ``*9`` while also saying the phrase is not hypothetical; it
    is what people do when something is running away from them.
    """
    rounds = 15
    ready = threading.Barrier(2)
    errors: list[BaseException] = []

    def go() -> None:
        c = connect(db_path)
        try:
            ready.wait(timeout=5)
            for _ in range(rounds):
                kill.bump_epoch(c, actor="racer", reason="mash")
        except BaseException as e:  # noqa: BLE001 - surfaced by the assert below
            errors.append(e)
        finally:
            c.close()

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    c = connect(db_path)
    try:
        assert kill.current_epoch(c) == 2 * rounds
    finally:
        c.close()


def test_a_process_spawned_before_the_kill_refuses_to_start(con: sqlite3.Connection) -> None:
    spawned_with = kill.current_epoch(con)
    kill.assert_epoch(con, spawned_with)  # no kill yet: fine

    kill.stop_everything(con, "telegram", "/kill")

    with pytest.raises(kill.KillEpochAdvanced) as caught:
        kill.assert_epoch(con, spawned_with)
    assert caught.value.spawned_with == 0
    assert caught.value.current == 1


def test_the_epoch_dooms_jobs_the_command_never_reached(db_path: Path) -> None:
    """The kill's DURABLE half: no delivery, no expiry, and it survives a reboot.

    This is the resurrection race. A job that was mid-spawn when the kill fired
    never sees the command row — it must still be doomed when some later process
    looks, including a process started days afterwards.
    """
    before = connect(db_path)
    old = create_job(
        before,
        kind="claude_code",
        title="the doomed build",
        created_by="test",
        state="running",
        kill_epoch=kill.current_epoch(before),
    )
    kill.stop_everything(before, "voice", "jarvis full stop")
    before.close()

    after = connect(db_path)
    try:
        assert [j.id for j in kill.doomed_jobs(after)] == [old.id]

        fresh = create_job(
            after,
            kind="claude_code",
            title="the new build",
            created_by="test",
            state="running",
            kill_epoch=kill.current_epoch(after),
        )
        assert fresh.id not in {j.id for j in kill.doomed_jobs(after)}

        # And a job created the naive way is born doomed until it is stamped —
        # which is exactly why stamp_job_epoch exists and is called at spawn.
        naive = create_job(
            after, kind="claude_code", title="unstamped", created_by="test", state="running"
        )
        assert naive.id in {j.id for j in kill.doomed_jobs(after)}
        kill.stamp_job_epoch(after, naive.id)
        assert naive.id not in {j.id for j in kill.doomed_jobs(after)}
    finally:
        after.close()


def test_a_finished_job_is_not_doomed_however_old_its_epoch(con: sqlite3.Connection) -> None:
    job = create_job(
        con, kind="claude_code", title="already done", created_by="test", state="running"
    )
    set_state(con, job.id, "done", actor="test")
    kill.stop_everything(con, "voice", "stop")
    assert kill.doomed_jobs(con) == []


# ───────────────────── claiming, acking, expiring ─────────────────────


def test_a_shared_actor_makes_the_claim_mutually_exclusive(db_path: Path) -> None:
    """Two reapers racing means SIGKILL landing before SIGTERM's grace has run.

    The primary key ``(command_id, actor)`` is the whole lock. Both racers are
    really started, on their own connections, and exactly one may win.
    """
    setup = connect(db_path)
    command_id = kill.stop_everything(setup, "voice", "stop")
    setup.close()

    ready = threading.Barrier(2)
    won: list[bool] = []
    lock = threading.Lock()

    def go() -> None:
        c = connect(db_path)
        try:
            ready.wait(timeout=5)
            result = kill.claim_command(c, command_id, "reaper")
            with lock:
                won.append(result)
        finally:
            c.close()

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert sorted(won) == [False, True], "exactly one reaper may win"


def test_a_per_process_actor_makes_the_same_row_a_broadcast(con: sqlite3.Connection) -> None:
    command_id = kill.stop_everything(con, "voice", "stop")
    assert kill.claim_command(con, command_id, "runner:job_a") is True
    assert kill.claim_command(con, command_id, "runner:job_b") is True
    assert kill.claim_command(con, command_id, "runner:job_a") is False


def test_the_first_result_sticks(con: sqlite3.Connection) -> None:
    """An ack states something that already happened; a second one cannot edit it."""
    command_id = kill.stop_everything(con, "voice", "stop")
    assert kill.ack_command(con, command_id, "dispatch", result="killed") is True
    assert kill.ack_command(con, command_id, "dispatch", result="actually fine") is False
    assert kill.acks_for(con, command_id)[0].result == "killed"


def test_acking_a_command_that_does_not_exist_is_an_error_not_a_stray_row(
    con: sqlite3.Connection,
) -> None:
    with pytest.raises(kill.UnknownCommand):
        kill.ack_command(con, "cmd_nope", "dispatch", result="ok")
    assert con.execute("SELECT count(*) c FROM command_acks").fetchone()["c"] == 0


def test_a_late_process_does_not_execute_a_stale_kill(db_path: Path) -> None:
    """The window the epoch covers and the command deliberately does not.

    A runner that was suspended for an hour must not wake up and reap a build the
    user started since. The command is invisible; the epoch still says the OLD
    job is doomed, which is the correct pair of answers.
    """
    issuer = connect(db_path)
    command_id = kill.stop_everything(issuer, "dtmf", "stop", ttl_s=60)
    issuer.close()

    late = shift_ts(now(), 3600)
    woke = connect(db_path)
    try:
        assert kill.pending_commands(woke, actor="runner:job_a", now_ts=late) == []
        assert kill.claim_command(woke, command_id, "runner:job_a", now_ts=late) is False
        assert kill.expire_commands(woke, now_ts=late) == 1
        cmd = kill.get_command(woke, command_id)
        assert cmd is not None and cmd.state == "expired"
    finally:
        woke.close()


def test_finishing_a_command_is_a_compare_and_swap(con: sqlite3.Connection) -> None:
    command_id = kill.stop_everything(con, "voice", "stop")
    assert kill.finish_command(con, command_id) is True
    assert kill.finish_command(con, command_id) is False
    assert kill.pending_commands(con, actor="anyone") == []


def test_an_unknown_verb_is_refused_before_it_reaches_sql(con: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="unknown verb"):
        kill.issue_command(con, verb="destroy", issued_by="voice")  # type: ignore[arg-type]


# ───────────────────── the signal half, with real processes ─────────────────────


@needs_proc
def test_a_real_process_dies_from_a_connection_that_never_spawned_it(db_path: Path) -> None:
    """The architecture's actual claim: database plus a signal, no shared object.

    The process is started here, recorded through the database, and then killed
    by a DIFFERENT connection opened afterwards, which has nothing but the row.
    """
    proc = spawn("import time; time.sleep(60)")
    try:
        starter = connect(db_path)
        job = create_job(
            starter,
            kind="claude_code",
            title="the runaway build",
            created_by="test",
            state="running",
            kill_epoch=kill.current_epoch(starter),
            **process_identity(proc.pid),
        )
        command_id = kill.stop_everything(starter, "dtmf", "star nine")
        starter.close()

        reaper = connect(db_path)
        try:
            outcomes = kill.reap_all(reaper, actor="dispatch", command_id=command_id)
            assert outcomes[job.id] == "term", "SIGTERM must be enough for a sleeping process"
            assert wait_gone(proc.pid)

            killed = get(reaper, job.id)
            assert killed is not None and killed.state == "killed"
            assert killed.stop_reason == "kill_switch"
            assert kill.acked_by(reaper, command_id, "dispatch")
            cmd = kill.get_command(reaper, command_id)
            assert cmd is not None and cmd.state == "done"
        finally:
            reaper.close()
    finally:
        proc.kill()
        proc.wait(timeout=5)


@needs_proc
def test_a_process_that_ignores_sigterm_is_escalated_to_sigkill(
    db_path: Path, tmp_path: Path
) -> None:
    """The grace period must actually escalate, or a wedged runner survives a kill."""
    # The handshake file is not ceremony: without it the SIGTERM can arrive during
    # interpreter startup, before SIG_IGN is installed, and the child dies of the
    # signal it is supposed to be ignoring — a green test measuring nothing.
    ready = tmp_path / "ignoring-sigterm"
    proc = spawn(
        "import signal, time, pathlib; signal.signal(signal.SIGTERM, signal.SIG_IGN);"
        f" pathlib.Path({str(ready)!r}).write_text('ok'); time.sleep(60)"
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), "child never armed its SIGTERM handler"

        c = connect(db_path)
        job = create_job(
            c,
            kind="claude_code",
            title="the stubborn build",
            created_by="test",
            state="running",
            **process_identity(proc.pid),
        )
        outcome = kill.reap_job(c, job.id, actor="dispatch", grace_s=0.3)
        assert outcome == "kill"
        assert wait_gone(proc.pid)
        c.close()
    finally:
        proc.kill()
        proc.wait(timeout=5)


@needs_proc
def test_the_kill_switch_refuses_a_pid_that_is_no_longer_ours(db_path: Path) -> None:
    """The footgun that makes a kill switch dangerous instead of merely broken.

    A recorded pid is not an identity. After a reboot — or a busy afternoon — the
    number belongs to somebody else, and signalling it kills an innocent process.
    The recorded start-ticks disagree, so nothing is signalled at all.
    """
    proc = spawn("import time; time.sleep(30)")
    try:
        identity = process_identity(proc.pid)
        identity["proc_start_ticks"] = int(identity["proc_start_ticks"] or 0) + 1

        c = connect(db_path)
        try:
            job = create_job(
                c,
                kind="claude_code",
                title="a pid we lost",
                created_by="test",
                state="running",
                **identity,
            )
            assert kill.reap_job(c, job.id, actor="dispatch", grace_s=0.2) == "stale"
            assert alive(proc.pid), "an innocent process must not be signalled"

            # The ROW is still marked killed: the user said stop, and a job left
            # running with no process of ours is what reconcile resurrects.
            killed = get(c, job.id)
            assert killed is not None and killed.state == "killed"
        finally:
            c.close()
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_a_job_with_no_process_is_still_stopped(con: sqlite3.Connection) -> None:
    """``deferred`` and ``queued`` jobs have nothing to signal and must still die.

    Otherwise the next reconcile helpfully respawns the very job the user just
    stopped, and the kill switch looks intermittent.
    """
    # A deferred job is BY DEFINITION parked on a request row — that row plus the
    # cc_session_id are its entire resumable state — and jobs.create_job enforces
    # it, so the kill path must be exercised with one.
    deferred = create_job(
        con,
        kind="claude_code",
        title="waiting for an answer",
        created_by="test",
        state="deferred",
        blocked_request_id="req_x",
    )
    queued = create_job(con, kind="claude_code", title="not started yet", created_by="test")

    command_id = kill.stop_everything(con, "telegram", "/kill")
    outcomes = kill.reap_all(con, actor="dispatch", command_id=command_id)

    assert outcomes[deferred.id] == "no_pid"
    assert outcomes[queued.id] == "no_pid"
    for job_id in (deferred.id, queued.id):
        job = get(con, job_id)
        assert job is not None and job.state == "killed"


def test_reaping_a_job_twice_does_not_fight_over_the_terminal_state(
    con: sqlite3.Connection,
) -> None:
    job = create_job(
        con, kind="claude_code", title="already gone", created_by="test", state="running"
    )
    assert kill.reap_job(con, job.id, actor="dispatch") == "no_pid"
    assert kill.reap_job(con, job.id, actor="dispatch") == "no_pid"
    assert get(con, job.id).state == "killed"  # type: ignore[union-attr]


def test_terminate_says_no_pid_rather_than_guessing() -> None:
    assert kill.terminate(None) == "no_pid"
    assert kill.terminate(0) == "no_pid"


def test_reaping_an_unknown_job_raises(con: sqlite3.Connection) -> None:
    with pytest.raises(KeyError):
        kill.reap_job(con, "job_nope", actor="dispatch")


# The guard below is tested in a CHILD SESSION on purpose. The failure it
# prevents is "the kill switch signals the process that is running it", and a
# test for that, run in-process, does not fail — it kills the test runner and
# every other test with it. So the probe calls setsid first: a regression can
# only reach itself, and the parent reads it as an exit code.
OWN_GROUP_PROBE = """
import os, subprocess, sys
os.setsid()
sys.path.insert(0, sys.argv[1])
from jarvis import kill
victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                          start_new_session=True)
print(victim.pid, flush=True)
outcome = kill.terminate(victim.pid, os.getpgrp(), grace_s=1.0)
print(outcome, flush=True)
"""


@needs_proc
def test_the_kill_switch_never_signals_its_own_process_group() -> None:
    """The recorded pgid can be OURS, and then a kill takes the system down.

    ``jobs.process_identity`` records ``os.getpgid(child)``. A child spawned
    without ``start_new_session`` — one fallback path that forgets ``setsid`` is
    enough — inherits the spawner's group, so the row names jarvis-dispatch's
    own group and ``killpg`` would SIGTERM and then SIGKILL the dispatcher, the
    scheduler and every sibling runner. The kill switch is the one component
    that must never be able to take the whole system down with it.

    The probe dying of its own signal (returncode -15) is the regression.
    """
    done = subprocess.run(
        [sys.executable, "-c", OWN_GROUP_PROBE, str(Path(__file__).resolve().parents[1])],
        capture_output=True,
        text=True,
        timeout=60,
    )
    lines = done.stdout.split()
    if lines:
        with suppress(ProcessLookupError, ValueError):
            os.kill(int(lines[0]), 9)

    assert done.returncode == 0, (
        f"the kill switch signalled its own process group: {done.returncode} {done.stderr}"
    )
    assert lines[1:] == ["term"], "the recorded pid must still be signalled"


@needs_proc
def test_a_stale_group_falls_back_to_the_pid_instead_of_claiming_success() -> None:
    """An empty group must not be reported as a successful kill.

    A pgid recorded before the runner called setsid (or by an older build) names
    a group that no longer exists while the process itself is very much alive.
    Reporting the ESRCH from killpg as "gone" is the kill switch declaring
    victory over something it never signalled, and the job would keep running
    with its row marked killed — invisible to the reconciler forever.
    """
    dead = spawn("pass")
    dead.wait(timeout=5)
    empty_group = dead.pid  # its session died with it; no process holds this group

    proc = spawn("import time; time.sleep(30)")
    try:
        outcome = kill.terminate(proc.pid, empty_group, grace_s=1.0)
        assert outcome == "term"
        assert wait_gone(proc.pid), "the process must actually be dead"
    finally:
        proc.kill()
        proc.wait(timeout=5)


@needs_proc
def test_escalation_still_happens_on_a_platform_without_proc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """macOS has no ``/proc``, and the corpse check must not become a yes-man.

    ``_zombie`` answering True whenever it cannot read ``/proc`` makes every live
    process look gone one poll after SIGTERM: terminate returns "term", never
    escalates, and a wedged runner survives a kill switch that reported success.
    Patching PROC away is the only honest way to exercise that platform here.
    """
    monkeypatch.setattr(kill, "PROC", tmp_path / "no-proc-here")

    ready = tmp_path / "ignoring-sigterm"
    proc = spawn(
        "import signal, time, pathlib; signal.signal(signal.SIGTERM, signal.SIG_IGN);"
        f" pathlib.Path({str(ready)!r}).write_text('ok'); time.sleep(60)"
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), "child never armed its SIGTERM handler"

        assert kill.terminate(proc.pid, grace_s=0.3) == "kill"
        assert wait_gone(proc.pid), "SIGTERM was ignored and SIGKILL never came"
    finally:
        proc.kill()
        proc.wait(timeout=5)
