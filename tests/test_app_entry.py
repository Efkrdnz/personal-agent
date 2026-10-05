"""The exe's one entry point: ``-m`` for children, the app for everything else.

Frozen, ``sys.executable`` IS Jarvis.exe, so every ``[sys.executable, "-m",
...]`` spawn in the tree lands here. These tests pin the table that answers
it, and that a windowed process with no stdout writes somewhere instead of
dying on its first ``print``.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from jarvis.app import entry

ROOT = Path(__file__).resolve().parents[1]


def test_the_table_is_exactly_the_four_process_modules() -> None:
    assert set(entry.CHILDREN) == {"jarvis", "jarvis.schedule", "jarvis.telegram", "jarvis.cc"}


@pytest.mark.parametrize(
    ("module", "target"),
    [
        ("jarvis", "jarvis.__main__"),
        ("jarvis.schedule", "jarvis.schedule.__main__"),
        ("jarvis.telegram", "jarvis.telegram.__main__"),
        ("jarvis.cc", "jarvis.cc.__main__"),
    ],
)
def test_dash_m_runs_that_modules_main_with_the_rest_of_the_arguments(
    monkeypatch: pytest.MonkeyPatch, module: str, target: str
) -> None:
    import importlib

    got: list[list[str]] = []
    mod = importlib.import_module(target)
    monkeypatch.setattr(mod, "main", lambda argv: got.append(list(argv)) or 7)
    assert entry.main(["-m", module, "--db", "x.db", "--once"]) == 7
    assert got == [["--db", "x.db", "--once"]]


def test_anything_else_is_the_app(monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvis import __main__ as cli

    got: list[list[str]] = []
    monkeypatch.setattr(cli, "main", lambda argv: got.append(list(argv)) or 0)
    assert entry.main([]) == 0
    assert entry.main(["--selftest", "--report", "r.json"]) == 0
    assert got == [["app"], ["app", "--selftest", "--report", "r.json"]]


def test_no_arguments_reads_sys_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvis.schedule import __main__ as sched

    got: list[list[str]] = []
    monkeypatch.setattr(sched, "main", lambda argv: got.append(list(argv)) or 0)
    monkeypatch.setattr(sys, "argv", ["Jarvis.exe", "-m", "jarvis.schedule", "--list"])
    assert entry.main() == 0
    assert got == [["--list"]]


def test_a_module_outside_the_table_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    assert entry.main(["-m", "os"]) == 2
    assert entry.main(["-m"]) == 2
    err = capsys.readouterr().err
    assert "cannot run -m os" in err and "jarvis.schedule" in err


def test_the_table_is_explicit_never_runpy() -> None:
    tree = ast.parse(Path(entry.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert "runpy" not in imported and "importlib" not in imported


def test_freeze_support_runs_before_anything_else(monkeypatch: pytest.MonkeyPatch) -> None:
    import multiprocessing

    from jarvis import __main__ as cli

    order: list[str] = []
    monkeypatch.setattr(multiprocessing, "freeze_support", lambda: order.append("freeze"))
    monkeypatch.setattr(entry, "redirect_missing_streams", lambda: order.append("streams"))
    monkeypatch.setattr(cli, "main", lambda argv: order.append("app") or 0)
    entry.main([])
    assert order == ["freeze", "streams", "app"]


# ───────────────────────────── no console, no streams ─────────────────────────────


def test_streams_that_exist_are_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    out, err = sys.stdout, sys.stderr
    assert entry.redirect_missing_streams(env={}) is None
    assert sys.stdout is out and sys.stderr is err


def test_a_windowed_process_writes_to_jarvis_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import faulthandler

    log = tmp_path / "desk.log"
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    stream = entry.redirect_missing_streams(env={"JARVIS_LOG": str(log)})
    try:
        assert sys.stdout is stream and sys.stderr is stream
        print("the desk is listening — ✓")
        print("and a refusal", file=sys.stderr)
    finally:
        faulthandler.disable()
        assert stream is not None
        stream.close()
    text = log.read_text(encoding="utf-8")
    assert "the desk is listening — ✓" in text and "and a refusal" in text


def test_without_jarvis_log_it_is_the_apps_own_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import faulthandler

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys, "stdout", None)
    stream = entry.redirect_missing_streams(env={})
    try:
        assert sys.stdout is stream and sys.stderr is not stream
        print("hello")
    finally:
        faulthandler.disable()
        assert stream is not None
        stream.close()
    assert (tmp_path / "jarvis" / "logs" / "app.log").read_text(encoding="utf-8") == "hello\n"


def run_python(code: str, **kw: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=120,
        check=False,
        **kw,
    )


def test_a_real_child_is_dispatched_in_a_real_process(tmp_path: Path) -> None:
    db = tmp_path / "j.db"
    done = run_python(
        "import sys\n"
        "from jarvis.app.entry import main\n"
        f"raise SystemExit(main(['-m', 'jarvis.schedule', '--db', {str(db)!r}, '--list']))\n"
    )
    assert done.returncode == 0, done.stderr
    assert db.exists(), "the scheduler opened the database it was handed"


def test_a_real_windowed_process_with_no_stdout_still_runs_and_logs(tmp_path: Path) -> None:
    """What pythonw and a console=False exe do: no streams at all. It must not crash."""
    log = tmp_path / "child.log"
    done = run_python(
        "import sys\n"
        "sys.stdout = None\n"
        "sys.stderr = None\n"
        "from jarvis.app.entry import main\n"
        "raise SystemExit(main(['-m', 'jarvis', 'tools']))\n",
        env={**__import__("os").environ, "JARVIS_LOG": str(log)},
    )
    assert done.returncode == 0, done.stderr
    assert "remember" in log.read_text(encoding="utf-8"), "the tool list went to the log"


def test_the_pyinstaller_script_only_calls_entry_main() -> None:
    script = ROOT / "packaging" / "jarvis_app.py"
    tree = ast.parse(script.read_text(encoding="utf-8"))
    body = [n for n in tree.body if not isinstance(n, ast.Expr)]  # skip the docstring
    assert len(body) == 2
    imp, call = body
    assert isinstance(imp, ast.ImportFrom) and imp.module == "jarvis.app.entry"
    assert [a.name for a in imp.names] == ["main"]
    assert isinstance(call, ast.Raise) and ast.unparse(call) == "raise SystemExit(main())"


# ───────────────────────────── a startup that dies says so ─────────────────────────────


def test_a_crash_at_startup_is_shown_not_swallowed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from jarvis import __main__ as cli

    def broken(argv: list[str]) -> int:
        raise RuntimeError("the database is on a drive that went away")

    shown: list[tuple[str, str]] = []
    monkeypatch.setattr(cli, "main", broken)
    assert entry.main([], alert=lambda title, text: shown.append((title, text))) == 1
    ((title, text),) = shown
    assert title == "Jarvis" and "the database is on a drive that went away" in text
    assert "RuntimeError" in capsys.readouterr().err, "the traceback still reaches the log"


def test_the_selftest_never_waits_on_a_dialog(monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvis import __main__ as cli

    def broken(argv: list[str]) -> int:
        raise RuntimeError("boom")

    shown: list[Any] = []
    monkeypatch.setattr(cli, "main", broken)
    assert entry.main(["--selftest"], alert=lambda *a: shown.append(a)) == 1
    assert shown == []


def test_an_ordinary_exit_is_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvis import __main__ as cli

    def usage(argv: list[str]) -> int:
        raise SystemExit(2)

    shown: list[Any] = []
    monkeypatch.setattr(cli, "main", usage)
    with pytest.raises(SystemExit):
        entry.main(["--bogus"], alert=lambda *a: shown.append(a))
    assert shown == []


def test_off_windows_there_is_no_dialog_to_show(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert entry._default_alert() is None
    monkeypatch.setattr(sys, "platform", "win32")
    assert callable(entry._default_alert())


def test_a_child_that_crashes_exits_with_its_traceback_in_the_log(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Escaping, the exception would reach the windowed bootloader, whose
    # dialog keeps the child alive behind it: never reaped, never restarted.
    from jarvis.schedule import __main__ as sched

    def broken(argv: list[str]) -> int:
        raise ConnectionError("Gemini could not be reached")

    shown: list[Any] = []
    monkeypatch.setattr(sched, "main", broken)
    assert entry.main(["-m", "jarvis.schedule"], alert=lambda *a: shown.append(a)) == 1
    assert shown == []
    err = capsys.readouterr().err
    assert "ConnectionError: Gemini could not be reached" in err and "Traceback" in err


def test_a_child_exit_code_and_usage_errors_pass_straight_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jarvis.schedule import __main__ as sched

    monkeypatch.setattr(sched, "main", lambda argv: 2)
    assert entry.main(["-m", "jarvis.schedule"]) == 2

    def usage(argv: list[str]) -> int:
        raise SystemExit(2)

    monkeypatch.setattr(sched, "main", usage)
    with pytest.raises(SystemExit):
        entry.main(["-m", "jarvis.schedule", "--bogus"])


def test_a_piped_child_writes_utf8_a_line_at_a_time(tmp_path: Path) -> None:
    # What a frozen child needs and cannot get from PYTHONIOENCODING or
    # PYTHONUNBUFFERED, which a frozen interpreter does not read.
    code = (
        "import sys; from jarvis.app import entry; "
        "sys.stdout.reconfigure(encoding='cp1252', line_buffering=False); "
        "entry.tune_piped_streams(); "
        "print(sys.stdout.encoding, sys.stdout.line_buffering); print('Gün aydın — sir')"
    )
    env = {k: v for k, v in __import__("os").environ.items() if not k.startswith("PYTHONIO")}
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, cwd=ROOT, env=env, timeout=60, check=True
    ).stdout.decode("utf-8")
    assert out.splitlines() == ["utf-8 True", "Gün aydın — sir"]
