"""Running a command: the right shell, hidden, bounded, stoppable, and killed as a tree.

Real processes on POSIX (``/bin/sh``), fakes for every Windows decision, and a
few Windows-only tests that run real PowerShell on the windows-latest CI job.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest

from jarvis.shell import run as runmod
from jarvis.shell.command import ENV_KEY, WRAPPER
from jarvis.shell.run import (
    CREATE_NO_WINDOW,
    Outcome,
    Shell,
    ShellSpec,
    code_pages,
    kill_tree,
    resolve,
    run,
    scrubbed_env,
)

ROOT = Path(__file__).resolve().parents[1]
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="runs /bin/sh")
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="runs real PowerShell")
SH = ShellSpec("sh", "the shell", ("/bin/sh", "-c"))


# ───────────────────────────── which shell ─────────────────────────────


def test_off_windows_it_is_sh_with_the_command_as_an_argument() -> None:
    spec = resolve("linux")
    assert spec.argv == ("/bin/sh", "-c") and spec.env_key is None
    assert spec.argv_for("echo hi") == ["/bin/sh", "-c", "echo hi"]


def test_on_windows_powershell_7_is_preferred_by_absolute_path() -> None:
    env = {"ProgramFiles": r"C:\Program Files", "SystemRoot": r"C:\Windows"}
    pwsh = r"C:\Program Files\PowerShell\7\pwsh.exe"
    spec = resolve("win32", environ=env, which=lambda name: None, exists=lambda p: p == pwsh)
    assert spec.argv[0] == pwsh and spec.name == "pwsh" and spec.spoken == "PowerShell"
    assert spec.argv[1:] == (
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-OutputFormat",
        "Text",
        "-Command",
        WRAPPER,
    )
    # The command never reaches the command line.
    assert spec.argv_for("Remove-Item x") == list(spec.argv)
    assert spec.env_for("Remove-Item x", {})[ENV_KEY] == "Remove-Item x"


def test_without_powershell_7_it_is_windows_powershell_from_system32() -> None:
    spec = resolve(
        "win32", environ={"SystemRoot": r"D:\Win"}, which=lambda n: None, exists=lambda p: False
    )
    assert spec.argv[0] == r"D:\Win\System32\WindowsPowerShell\v1.0\powershell.exe"
    assert spec.name == "powershell"


def test_a_relative_pwsh_found_in_the_current_folder_is_never_used() -> None:
    # shutil.which on Windows looks in the current directory first.
    spec = resolve("win32", environ={}, which=lambda n: r".\pwsh.exe", exists=lambda p: True)
    assert spec.argv[0].endswith(r"System32\WindowsPowerShell\v1.0\powershell.exe")
    assert spec.argv[0].startswith("C:\\")


def test_the_environment_loses_every_jarvis_secret_and_pyinstallers_variables() -> None:
    env = {
        "PATH": "/usr/bin",
        "JARVIS_GEMINI_API_KEY": "AIza-secret",
        "jarvis_github_token": "ghp_x",  # Windows names are case-insensitive
        "JARVIS_TELEGRAM_TOKEN": "1:x",
        "_MEIPASS2": r"C:\Temp\_MEI1234",
        "_PYI_APPLICATION_HOME_DIR": r"C:\Temp\_MEI1234",
        ENV_KEY: "stale",
        "HOME": "/home/u",
    }
    assert scrubbed_env(env) == {"PATH": "/usr/bin", "HOME": "/home/u"}


def test_terminal_colour_is_switched_off() -> None:
    env = SH.env_for("ls", {"TERM": "xterm-256color"})
    assert env["TERM"] == "dumb" and env["NO_COLOR"] == "1" and ENV_KEY not in env


# ───────────────────────────── hidden, and its own group ─────────────────────────────


def test_on_windows_the_process_is_created_without_a_console(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def fake_popen(argv: list[str], **kw: Any) -> object:
        seen.update(kw, argv=argv)
        return object()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    runmod._spawn(["powershell.exe"], cwd="C:\\Users\\u", env={}, platform="win32")
    assert seen["creationflags"] == CREATE_NO_WINDOW == 0x08000000
    assert seen["start_new_session"] is False
    assert seen["stdin"] is subprocess.DEVNULL and seen["stderr"] is subprocess.STDOUT
    assert seen["cwd"] == "C:\\Users\\u"

    runmod._spawn(["/bin/sh"], cwd="/home/u", env={}, platform="linux")
    assert seen["creationflags"] == 0 and seen["start_new_session"] is True


def test_on_windows_the_tree_is_killed_by_a_hidden_taskkill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: calls.append((argv, kw)))
    runmod._taskkill(4242)
    ((argv, kw),) = calls
    assert argv[0].endswith("System32\\taskkill.exe") and argv[1:] == ["/PID", "4242", "/T", "/F"]
    assert kw["creationflags"] == CREATE_NO_WINDOW

    class Proc:
        pid = 4242
        killed = False

        def kill(self) -> None:
            self.killed = True

    proc, killed = Proc(), []
    kill_tree(proc, platform="win32", taskkill=killed.append)
    assert killed == [4242] and proc.killed


def test_code_pages_come_from_the_console_then_the_ansi_page() -> None:
    class Kernel32:
        def GetOEMCP(self) -> int:  # noqa: N802 - the Win32 name
            return 857

        def GetACP(self) -> int:  # noqa: N802
            return 1254

    assert code_pages("win32", kernel32=Kernel32()) == ("cp857", "cp1254")
    assert code_pages("linux") == ()

    class Utf8:
        def GetOEMCP(self) -> int:  # noqa: N802
            return 65001

        def GetACP(self) -> int:  # noqa: N802
            return 99999  # no such codec

    assert code_pages("win32", kernel32=Utf8()) == ()


def test_a_shell_that_cannot_start_is_an_outcome_not_an_exception() -> None:
    def broken(argv: list[str], **kw: Any) -> Any:
        raise FileNotFoundError(2, "No such file", argv[0])

    out = run(SH, "echo hi", cwd="/", env={}, timeout_s=5, popen=broken)
    assert out.exit_code is None and "FileNotFoundError" in out.error


# ───────────────────────────── real processes ─────────────────────────────


@posix_only
def test_exit_codes_and_merged_output(tmp_path: Path) -> None:
    out = run(SH, "echo out; echo err >&2; exit 3", cwd=str(tmp_path), env={}, timeout_s=10)
    assert out.exit_code == 3
    assert out.text() == "out\nerr\n"
    assert not (out.timed_out or out.stopped or out.incomplete or out.truncated)


@posix_only
def test_it_runs_where_it_is_told_with_the_environment_it_is_given(tmp_path: Path) -> None:
    out = run(SH, 'pwd; echo "$TERM $NO_COLOR $SECRET"', cwd=str(tmp_path), env={}, timeout_s=10)
    assert out.text().split("\n")[:2] == [str(tmp_path.resolve()), "dumb 1 "]


@posix_only
def test_a_timeout_kills_the_whole_tree(tmp_path: Path) -> None:
    pidfile = tmp_path / "child.pid"
    started = time.monotonic()
    out = run(
        SH,
        f"sleep 30 & echo $! > {pidfile}; wait",
        cwd=str(tmp_path),
        env=dict(os.environ),
        timeout_s=1,
        poll_s=0.05,
    )
    assert out.timed_out and time.monotonic() - started < 10
    child = int(pidfile.read_text())
    assert _gone(child), "the grandchild outlived the timeout"


@posix_only
def test_stopping_mid_run_kills_it(tmp_path: Path) -> None:
    polls: list[int] = []

    def stop() -> bool:
        polls.append(1)
        return len(polls) >= 2

    started = time.monotonic()
    out = run(
        SH, "sleep 30", cwd=str(tmp_path), env={}, timeout_s=60, should_stop=stop, poll_s=0.05
    )
    assert out.stopped and not out.timed_out and time.monotonic() - started < 10


@posix_only
def test_a_broken_kill_switch_does_not_orphan_the_command(tmp_path: Path) -> None:
    def broken() -> bool:
        raise RuntimeError("database is locked")

    out = run(SH, "echo fine", cwd=str(tmp_path), env={}, timeout_s=10, should_stop=broken)
    assert out.exit_code == 0 and out.text() == "fine\n"


@posix_only
def test_something_left_holding_the_output_does_not_hang_the_tool(tmp_path: Path) -> None:
    started = time.monotonic()
    out = run(
        SH,
        "sleep 30 & echo started",
        cwd=str(tmp_path),
        env=dict(os.environ),
        timeout_s=20,
        drain_s=0.3,
        poll_s=0.05,
    )
    try:
        assert out.exit_code == 0 and out.incomplete
        assert out.text() == "started\n"
        assert time.monotonic() - started < 10
    finally:
        # The background sleep is still in the command's process group.
        if out.pid:
            with suppress(ProcessLookupError):
                os.killpg(out.pid, signal.SIGKILL)


@posix_only
def test_huge_output_is_bounded_at_both_ends(tmp_path: Path) -> None:
    out = run(
        SH,
        "i=0; while [ $i -lt 20000 ]; do echo line$i; i=$((i+1)); done",
        cwd=str(tmp_path),
        env={},
        timeout_s=30,
        head_bytes=1000,
        tail_bytes=500,
    )
    assert out.exit_code == 0 and out.truncated
    assert len(out.head) == 1000 and len(out.tail) == 500 and out.total_bytes > 100_000
    text = out.text()
    assert text.startswith("line0\nline1\n") and text.endswith("line19999\n")
    assert "bytes of output not kept" in text


def test_a_clipped_end_is_cut_back_to_whole_characters() -> None:
    word = "ğüşı".encode()
    out = Outcome(
        exit_code=0, head=b"ok " + word[:3], tail=word[1:], total_bytes=999, truncated=True
    )
    text = out.text()
    assert "\ufffd" not in text and text.startswith("ok ğ")


@posix_only
def test_this_machines_shell_runs_a_command_in_the_home_folder() -> None:
    shell = Shell.here()
    assert shell.spoken == "the shell"
    out = shell.run("pwd", timeout_s=10)
    assert Path(shell.text(out).strip()).resolve() == Path.home().resolve()


def test_the_package_imports_with_no_third_party_packages() -> None:
    r = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            f"import sys; sys.path.insert(0, {str(ROOT)!r}); import jarvis.shell; print('ok')",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONNOUSERSITE": "1", "PYTHONPATH": ""},
        check=False,
    )
    assert r.returncode == 0, r.stderr
    assert "ok" in r.stdout


def test_the_package_knows_nothing_above_the_spine() -> None:
    sys.path.insert(0, str(ROOT))
    from tools.check_layers import imports_of, modules_of

    reached = {
        name
        for path in modules_of("jarvis/shell", ROOT)
        for name in imports_of(path, ROOT)
        if name.startswith("jarvis.") and not name.startswith(("jarvis.shell", "jarvis.secrets"))
    }
    assert reached == set()


def _gone(pid: int, wait_s: float = 5.0) -> bool:
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        if _zombie(pid):
            return True
        time.sleep(0.05)
    return False


def _zombie(pid: int) -> bool:
    try:
        return " Z" in Path(f"/proc/{pid}/stat").read_text().split(")")[-1][:3]
    except OSError:
        return False


# ───────────────────────────── Windows, for real ─────────────────────────────


@windows_only
def test_windows_powershell_round_trips_turkish_and_keeps_exit_codes() -> None:
    shell = Shell.here()
    assert shell.spec.argv[0].lower().endswith(("powershell.exe", "pwsh.exe"))
    out = shell.run("Write-Output 'Türkçe: ğüşıöç İ'", timeout_s=60)
    assert out.exit_code == 0, shell.text(out)
    assert "Türkçe: ğüşıöç İ" in shell.text(out)
    assert "CLIXML" not in shell.text(out)
    assert shell.run("cmd /c exit 5", timeout_s=60).exit_code == 5


@windows_only
def test_windows_the_command_cannot_read_itself_back_from_the_environment() -> None:
    shell = Shell.here()
    out = shell.run(f"Write-Output ([bool]$env:{ENV_KEY})", timeout_s=60)
    assert shell.text(out).strip() == "False"


@windows_only
def test_windows_a_timeout_kills_what_the_command_started(tmp_path: Path) -> None:
    shell = Shell.here()
    pidfile = tmp_path / "ping.pid"
    started = time.monotonic()
    out = shell.run(
        "$p = Start-Process -FilePath ping.exe -ArgumentList '-n','60','127.0.0.1' "
        f"-NoNewWindow -PassThru; Set-Content -Path '{pidfile}' -Value $p.Id; $p.WaitForExit()",
        timeout_s=5,
    )
    assert out.timed_out and time.monotonic() - started < 30
    child = int(pidfile.read_text().strip())
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        listed = subprocess.run(
            ["tasklist", "/FI", f"PID eq {child}", "/NH"],
            capture_output=True,
            text=True,
            check=False,
            creationflags=CREATE_NO_WINDOW,
        )
        if str(child) not in listed.stdout:
            break
        time.sleep(0.2)
    assert str(child) not in listed.stdout, "ping outlived the timeout: the tree was not killed"
