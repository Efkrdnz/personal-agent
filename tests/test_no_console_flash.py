"""Every console program Jarvis runs is run hidden on Windows, or says why not.

The app is a windowed exe with no console. A console program started from such
a process gets a console window of its own: git, ffmpeg or PowerShell each flash
a black window over whatever the user was doing. Nothing raises and no test of
the callee fails, which is this repo's bug class — so the CALLERS are checked.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "jarvis"

#: (file, function) -> why a direct subprocess call there may show a console.
ALLOWED = {
    # Linux/macOS probe binaries (gdbus, loginctl, ioreg); none exists on Windows.
    ("presence.py", "_run"): "never runs on Windows",
    # `run` and `build` are terminal commands: the runner inherits that console
    # on purpose, so the user sees its refusals.
    ("__main__.py", "_spawn_runner"): "inherits the terminal it was started from",
}

_SPAWNERS = {"run", "Popen", "call", "check_call", "check_output"}


def _spawn_calls() -> list[tuple[str, str, ast.Call]]:
    found = []
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in _SPAWNERS
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "subprocess"
                ):
                    found.append((rel, fn.name, node))
    return found


def _hides_its_console(call: ast.Call) -> bool:
    # An explicit creationflags, or a **mapping built for the purpose
    # (jarvis/voice/engines.py's _hidden()).
    return any(kw.arg == "creationflags" or kw.arg is None for kw in call.keywords)


def test_the_scan_finds_the_calls_it_is_meant_to_check() -> None:
    # A scan that found nothing would pass forever.
    names = {(rel, fn) for rel, fn, _ in _spawn_calls()}
    assert ("project/workspace.py", "run") in names
    assert ("__main__.py", "_check_claude_cli") in names


@pytest.mark.parametrize(
    ("rel", "fn", "call"), _spawn_calls(), ids=lambda v: v if isinstance(v, str) else ""
)
def test_every_console_program_is_run_hidden_on_windows(rel: str, fn: str, call: ast.Call) -> None:
    if (rel, fn) in ALLOWED:
        return
    assert _hides_its_console(call), (
        f"jarvis/{rel}:{call.lineno} in {fn}() runs a console program without "
        "creationflags; from the windowed app it flashes a console window. Pass "
        'creationflags=0x08000000 if sys.platform == "win32" else 0, or add it to '
        "ALLOWED with the reason it never runs there."
    )


def test_the_allow_list_has_no_stale_entries() -> None:
    names = {(rel, fn) for rel, fn, _ in _spawn_calls()}
    assert set(ALLOWED) <= names, set(ALLOWED) - names
