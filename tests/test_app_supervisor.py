"""The supervisor: what an exit means, and that a child's words reach its log.

Two kinds of test, on purpose. A fake ``popen`` makes the exit-code and
backoff logic exact (no sleeping, an injected clock). A few REAL children
(``python -c``) prove the part a fake cannot: that a real process's stdout and
stderr land in the log file through real pipes, and that its real exit code is
classified the same way.
"""

from __future__ import annotations

import io
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from jarvis import liveness
from jarvis.app import supervisor as sup
from jarvis.app.supervisor import ProcessSpec, Supervisor
from jarvis.db import connect, migrate

# ───────────────────────────── a fake popen ─────────────────────────────


class FakeProc:
    def __init__(self, argv: list[str], *, out: bytes, err: bytes, pid: int, obeys: bool) -> None:
        self.argv = argv
        self.stdout = io.BytesIO(out)
        self.stderr = io.BytesIO(err)
        self.pid = pid
        self.returncode: int | None = None
        self.obeys = obeys
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        if self.obeys:
            self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise subprocess.TimeoutExpired(self.argv, timeout or 0)
        return self.returncode


class FakePopen:
    """Hands out FakeProcs; ``err`` and ``out`` are what the NEXT child will have printed."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.procs: list[FakeProc] = []
        self.err = b""
        self.out = b""
        self.obeys = True
        self.fail: Exception | None = None

    def __call__(self, argv: list[str], **kwargs: Any) -> FakeProc:
        self.calls.append((argv, kwargs))
        if self.fail is not None:
            raise self.fail
        proc = FakeProc(
            argv, out=self.out, err=self.err, pid=1000 + len(self.procs), obeys=self.obeys
        )
        self.procs.append(proc)
        return proc


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def make(
    tmp_path: Path,
    *specs: ProcessSpec,
    platform: str = "linux",
) -> tuple[Supervisor, FakePopen, Clock, list[tuple[str, int, str]]]:
    popen, clock, exits = FakePopen(), Clock(), []
    s = Supervisor(
        specs or (ProcessSpec("desk", ("-m", "jarvis", "desk")),),
        python="/py",
        env={"PATH": "/bin"},
        log_path=lambda name: tmp_path / f"{name}.log",
        popen=popen,
        clock=clock,
        on_exit=lambda *a: exits.append(a),
        platform=platform,
    )
    return s, popen, clock, exits


# ───────────────────────────── spawning ─────────────────────────────


def test_a_child_is_the_interpreter_and_its_argv_never_a_shell(tmp_path: Path) -> None:
    s, popen, _, _ = make(tmp_path)
    s.start()
    (argv, kw) = popen.calls[0]
    assert argv == ["/py", "-m", "jarvis", "desk"]
    assert "shell" not in kw
    assert kw["stdin"] is subprocess.DEVNULL
    assert kw["stdout"] is subprocess.PIPE and kw["stderr"] is subprocess.PIPE
    env = kw["env"]
    assert env["PYTHONIOENCODING"] == "utf-8" and env["JARVIS_APP"] == "1"
    assert env["PATH"] == "/bin", "the child must inherit the environment, not replace it"
    assert env["JARVIS_LOG"] == str(tmp_path / "desk.log")
    assert "creationflags" not in kw and kw["start_new_session"] is True


def test_on_windows_no_console_ever_flashes(tmp_path: Path) -> None:
    s, popen, _, _ = make(tmp_path, platform="win32")
    s.start()  # the job object cannot be made off Windows; that must not matter
    kw = popen.calls[0][1]
    assert kw["creationflags"] == sup.CREATE_NO_WINDOW == 0x08000000
    assert "start_new_session" not in kw


def test_status_has_the_contract_shape(tmp_path: Path) -> None:
    s, _, _, _ = make(tmp_path)
    s.start("desk")
    st = s.status()["desk"]
    assert set(st) == {"running", "pid", "restarts", "last_exit", "held", "reason", "log"}
    assert st["running"] is True and st["pid"] == 1000 and st["restarts"] == 0
    assert st["log"] == str(tmp_path / "desk.log")


def test_starting_twice_does_not_start_a_second_copy(tmp_path: Path) -> None:
    s, popen, _, _ = make(tmp_path)
    s.start()
    s.start("desk")
    assert len(popen.calls) == 1


def test_an_unknown_name_is_an_error_not_a_silent_no_op(tmp_path: Path) -> None:
    s, _, _, _ = make(tmp_path)
    with pytest.raises(KeyError):
        s.start("desk2")
    with pytest.raises(KeyError):
        s.restart("nobody")


# ───────────────────────────── exit 2: held ─────────────────────────────


def test_exit_2_is_a_refusal_held_with_its_stderr_as_the_reason(tmp_path: Path) -> None:
    s, popen, clock, exits = make(tmp_path)
    popen.out = b"ok  reader voice: system\n"
    popen.err = b"gemini_api_key is not set.\n  store it in Settings\n"
    s.start()
    popen.procs[0].returncode = 2
    s.poll()
    st = s.status()["desk"]
    assert st["held"] is True and st["running"] is False and st["last_exit"] == 2
    assert st["reason"] == "gemini_api_key is not set.\nstore it in Settings"
    assert "reader voice" not in st["reason"], "stdout is not the reason; stderr is"
    assert exits == [("desk", 2, st["reason"])]
    clock.t += 3600
    s.poll()
    assert len(popen.calls) == 1, "a refusal restarted is a loop that can never succeed"


def test_restart_clears_the_hold_and_starts_it_now(tmp_path: Path) -> None:
    s, popen, _, _ = make(tmp_path)
    popen.err = b"no microphone\n"
    s.start()
    popen.procs[0].returncode = 2
    s.poll()
    s.start("desk")
    assert len(popen.calls) == 1, "start() respects a hold; only restart() clears it"
    popen.err = b""
    s.restart("desk")
    st = s.status()["desk"]
    assert len(popen.calls) == 2 and st["held"] is False and st["running"] is True
    assert st["restarts"] == 1 and st["reason"] == ""


def test_a_long_refusal_is_clipped_to_400_characters(tmp_path: Path) -> None:
    s, popen, _, _ = make(tmp_path)
    popen.err = b"x" * 1000 + b"\n"
    s.start()
    popen.procs[0].returncode = 2
    s.poll()
    reason = s.status()["desk"]["reason"]
    assert len(reason) == sup.REASON_CHARS and reason.endswith("…")


# ───────────────────────────── crashes: backoff ─────────────────────────────


def test_a_crash_restarts_with_a_doubling_backoff_capped_at_a_minute(tmp_path: Path) -> None:
    s, popen, clock, exits = make(tmp_path)
    s.start()
    waits = []
    for _ in range(8):
        popen.procs[-1].returncode = 1
        s.poll()
        died = clock.t
        n = len(popen.calls)
        # Not one tick early...
        while len(popen.calls) == n:
            clock.t += 1
            s.poll()
        waits.append(clock.t - died)
    assert waits == [2, 4, 8, 16, 32, 60, 60, 60]
    assert s.status()["desk"]["restarts"] == 8
    assert [code for _, code, _ in exits] == [1] * 8


def test_a_crash_reason_is_the_exception_not_the_traceback(tmp_path: Path) -> None:
    s, popen, _, _ = make(tmp_path)
    popen.err = (
        b"Traceback (most recent call last):\n"
        b'  File "x.py", line 1, in <module>\n'
        b"RuntimeError: the session dropped\n"
    )
    s.start()
    popen.procs[0].returncode = 1
    s.poll()
    st = s.status()["desk"]
    assert st["reason"] == "RuntimeError: the session dropped" and st["held"] is False


def test_a_run_that_lasted_resets_the_backoff(tmp_path: Path) -> None:
    s, popen, clock, _ = make(tmp_path)
    s.start()
    for _ in range(3):  # three quick crashes: the next wait would be 8 s
        popen.procs[-1].returncode = 1
        s.poll()
        clock.t += 60
        s.poll()
    clock.t += sup.STABLE_S + 1  # this run was a healthy one
    popen.procs[-1].returncode = 1
    s.poll()
    n = len(popen.calls)
    clock.t += 2
    s.poll()
    assert len(popen.calls) == n + 1, "a crash after a stable run waits 2 s, not 16"


def test_exit_0_restarts_only_when_the_spec_says_so(tmp_path: Path) -> None:
    s, popen, clock, _ = make(
        tmp_path,
        ProcessSpec("desk", ("-m", "jarvis", "desk")),
        ProcessSpec("once", ("-c", "pass"), restart=False),
    )
    s.start()
    for p in popen.procs:
        p.returncode = 0
    s.poll()
    clock.t += 120
    s.poll()
    names = [argv[1:] for argv, _ in popen.calls]
    assert names.count(["-m", "jarvis", "desk"]) == 2
    assert names.count(["-c", "pass"]) == 1
    assert s.status()["once"]["reason"] == "" and s.status()["once"]["last_exit"] == 0


def test_a_child_that_cannot_be_started_is_retried_with_the_reason_shown(
    tmp_path: Path,
) -> None:
    s, popen, clock, exits = make(tmp_path)
    popen.fail = FileNotFoundError("no such file: /py")
    s.start()
    st = s.status()["desk"]
    assert st["running"] is False and "could not start" in st["reason"]
    assert exits == [], "nothing ran, so nothing exited"
    popen.fail = None
    clock.t += 2
    s.poll()
    assert s.status()["desk"]["running"] is True


def test_a_broken_on_exit_callback_does_not_stop_supervision(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    popen, clock = FakePopen(), Clock()

    def boom(*_: Any) -> None:
        raise RuntimeError("database locked")

    s = Supervisor(
        (ProcessSpec("desk", ("x",)),),
        log_path=lambda n: tmp_path / f"{n}.log",
        popen=popen,
        clock=clock,
        on_exit=boom,
        platform="linux",
    )
    s.start()
    popen.procs[0].returncode = 1
    s.poll()
    clock.t += 2
    s.poll()
    assert len(popen.calls) == 2
    assert "database locked" in capsys.readouterr().err


# ───────────────────────────── stopping ─────────────────────────────


def test_stop_terminates_and_is_not_a_crash(tmp_path: Path) -> None:
    s, popen, clock, exits = make(tmp_path)
    s.start()
    s.stop()
    proc = popen.procs[0]
    assert proc.terminated and not proc.killed
    st = s.status()["desk"]
    assert st["running"] is False and st["reason"] == "stopped" and st["held"] is False
    assert exits == [("desk", -15, "stopped")]
    clock.t += 3600
    s.poll()
    assert len(popen.calls) == 1, "a stopped process stays stopped"


def test_a_child_that_ignores_terminate_is_killed(tmp_path: Path) -> None:
    s, popen, _, exits = make(tmp_path)
    popen.obeys = False
    s.start()
    s.stop(timeout=0.01)
    assert popen.procs[0].terminated and popen.procs[0].killed
    assert exits == [("desk", -9, "stopped")]


def test_restart_of_a_running_child_stops_it_first(tmp_path: Path) -> None:
    s, popen, _, exits = make(tmp_path)
    s.start()
    s.restart("desk")
    assert popen.procs[0].terminated and len(popen.procs) == 2
    assert exits == [("desk", -15, "stopped")]
    assert s.status()["desk"]["running"] is True


def test_a_pending_backoff_is_cancelled_by_stop(tmp_path: Path) -> None:
    s, popen, clock, _ = make(tmp_path)
    s.start()
    popen.procs[0].returncode = 1
    s.poll()
    s.stop()
    clock.t += 120
    s.poll()
    assert len(popen.calls) == 1


# ───────────────────────────── reasons ─────────────────────────────


def test_the_reason_is_the_last_message_not_everything_said_before_it() -> None:
    said = [(0.0, "ALSA lib pcm.c: unknown PCM\n"), (3.0, "wake model missing\n"), (3.01, "\n")]
    said.append((3.02, 'Or set voice.wake_word = ""\n'))
    burst = sup.last_burst(said)
    assert burst == ["wake model missing\n", "\n", 'Or set voice.wake_word = ""\n']
    assert sup.reason_from(burst) == 'wake model missing\nOr set voice.wake_word = ""'
    assert sup.last_burst([]) == [] and sup.reason_from(["", "  ", "\n"]) == ""


def test_a_long_message_keeps_its_headline() -> None:
    lines = ["gemini_api_key is not set."] + [f"  detail {i}" for i in range(20)]
    reason = sup.reason_from(lines)
    assert reason.splitlines()[0] == "gemini_api_key is not set."
    assert len(reason.splitlines()) == 8


# ───────────────────────────── real children ─────────────────────────────


def real(tmp_path: Path, code: str, *, exits: list[Any] | None = None) -> Supervisor:
    return Supervisor(
        (ProcessSpec("child", ("-c", code)),),
        log_path=lambda name: tmp_path / f"{name}.log",
        on_exit=(lambda *a: exits.append(a)) if exits is not None else None,
    )


def wait_until(cond: Callable[[], bool], s: Supervisor, seconds: float = 20.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        s.poll()
        if cond():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out: {s.status()}")


def test_a_real_refusal_lands_in_the_log_and_is_held(tmp_path: Path) -> None:
    exits: list[Any] = []
    s = real(
        tmp_path,
        "import sys\n"
        "print('hello from the child')\n"
        "print('no gemini key; add it in Settings', file=sys.stderr)\n"
        "sys.exit(2)\n",
        exits=exits,
    )
    s.start()
    wait_until(lambda: s.status()["child"]["last_exit"] is not None, s)
    st = s.status()["child"]
    assert st["held"] is True and st["last_exit"] == 2
    assert st["reason"] == "no gemini key; add it in Settings"
    log = (tmp_path / "child.log").read_text(encoding="utf-8")
    assert "hello from the child" in log and "no gemini key; add it in Settings" in log
    assert "exited 2" in log
    assert exits == [("child", 2, "no gemini key; add it in Settings")]


def test_a_real_refusal_is_told_apart_from_an_earlier_warning(tmp_path: Path) -> None:
    s = real(
        tmp_path,
        "import sys, time\n"
        "print('ALSA lib pcm.c: unknown PCM cards.pcm.rear', file=sys.stderr)\n"
        "time.sleep(1.0)\n"
        "print('gemini_api_key is not set.\\n  store it in Settings', file=sys.stderr)\n"
        "sys.exit(2)\n",
    )
    s.start()
    wait_until(lambda: s.status()["child"]["held"], s)
    assert s.status()["child"]["reason"] == "gemini_api_key is not set.\nstore it in Settings"
    assert "ALSA" in (tmp_path / "child.log").read_text(encoding="utf-8"), "the log keeps it all"


def test_a_real_crash_is_scheduled_for_restart_not_held(tmp_path: Path) -> None:
    s = real(tmp_path, "raise SystemExit('the sound card went away')")
    s.start()
    wait_until(lambda: s.status()["child"]["last_exit"] is not None, s)
    st = s.status()["child"]
    assert st["last_exit"] == 1 and st["held"] is False
    assert st["reason"] == "the sound card went away"
    s.stop()  # cancels the pending restart


def test_a_real_child_is_stopped_and_its_output_is_utf8(tmp_path: Path) -> None:
    s = real(
        tmp_path,
        "import time\nprint('listening — 18:00 ✓', flush=True)\ntime.sleep(60)\n",
    )
    s.start()
    log = tmp_path / "child.log"
    deadline = time.monotonic() + 20
    while "listening" not in log.read_text(encoding="utf-8", errors="replace"):
        assert time.monotonic() < deadline, "the child's stdout never reached the log"
        time.sleep(0.05)
    started = time.monotonic()
    s.stop(timeout=5)
    assert time.monotonic() - started < 5
    st = s.status()["child"]
    assert st["running"] is False and st["reason"] == "stopped"
    assert "listening — 18:00 ✓" in log.read_text(encoding="utf-8")


def test_a_second_run_appends_to_the_same_log(tmp_path: Path) -> None:
    s = real(tmp_path, "import sys; print('run'); sys.exit(2)")
    s.start()
    wait_until(lambda: s.status()["child"]["held"], s)
    s.restart("child")
    wait_until(lambda: s.status()["child"]["held"], s)
    assert (tmp_path / "child.log").read_text(encoding="utf-8").count("starting child") == 2


# ───────────────────────────── what the app runs ─────────────────────────────


def test_the_app_knows_all_three_children_and_boots_telegram_only_with_a_token() -> None:
    specs = {s.name: s.argv for s in sup.app_specs()}
    assert specs == {
        "desk": ("-m", "jarvis", "desk"),
        "schedule": ("-m", "jarvis.schedule"),
        "telegram": ("-m", "jarvis.telegram"),
    }
    assert set(specs) <= set(liveness.PROCESSES)
    assert sup.boot_names(telegram_token=False, start_telegram=True) == ("desk", "schedule")
    assert sup.boot_names(telegram_token=True, start_telegram=False) == ("desk", "schedule")
    assert sup.boot_names(telegram_token=True, start_telegram=True) == (
        "desk",
        "schedule",
        "telegram",
    )


@pytest.fixture
def dbpath(tmp_path: Path) -> Path:
    p = tmp_path / "j.db"
    c = connect(p)
    migrate(c)
    c.close()
    return p


def test_an_exit_is_an_event_and_a_goodbye_the_child_could_not_write(dbpath: Path) -> None:
    con = connect(dbpath)
    try:
        liveness.beat(con, "desk", state="listening")
        record = sup.exit_recorder(lambda: connect(dbpath))
        record("desk", -9, "stopped")
        beat = liveness.read(con, "desk")
        assert beat is not None and beat.state == liveness.OFFLINE
        row = con.execute(
            "SELECT actor, payload FROM events WHERE kind='app.process_exited'"
        ).fetchone()
        assert row["actor"] == "app"
        import json

        assert json.loads(row["payload"]) == {"process": "desk", "code": -9, "reason": "stopped"}
    finally:
        con.close()


def test_the_exit_event_is_redacted(dbpath: Path) -> None:
    from jarvis.bus import Redactor

    record = sup.exit_recorder(
        lambda: connect(dbpath), redactor=Redactor.of(["sk-secret-value-123"])
    )
    record("telegram", 1, "bad token sk-secret-value-123")
    con: sqlite3.Connection = connect(dbpath)
    try:
        (payload,) = con.execute(
            "SELECT payload FROM events WHERE kind='app.process_exited'"
        ).fetchone()
    finally:
        con.close()
    assert "sk-secret-value-123" not in payload


def test_control_restarts_by_name_and_quit_only_sets_the_event(tmp_path: Path) -> None:
    s, popen, _, _ = make(tmp_path)
    ev = threading.Event()
    control = sup.Control(s, ev)
    s.start()
    control.restart("desk")
    assert len(popen.calls) == 2
    with pytest.raises(ValueError, match="no process called"):
        control.restart("rm -rf")
    assert control.status() == s.status()
    control.quit()
    assert ev.is_set() and s.status()["desk"]["running"], "teardown is the main thread's job"


def test_poll_forever_survives_a_bad_tick_and_ends_on_the_event(
    capsys: pytest.CaptureFixture[str],
) -> None:
    ticks: list[int] = []
    ev = threading.Event()

    class Flaky:
        def poll(self) -> None:
            ticks.append(1)
            if len(ticks) == 1:
                raise RuntimeError("one bad tick")
            if len(ticks) >= 3:
                ev.set()

    t = threading.Thread(target=sup.poll_forever, args=(Flaky(), ev), kwargs={"every": 0.01})
    t.start()
    t.join(5)
    assert not t.is_alive() and len(ticks) >= 3
    assert "one bad tick" in capsys.readouterr().err


def test_the_supervisor_uses_this_interpreter_by_default() -> None:
    s = Supervisor(())
    assert s._python == sys.executable
