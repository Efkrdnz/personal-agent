"""The falsifiable test for stage 3, written down before the stage was built.

CLAUDE.md says: "Stage 3 (Telegram) must touch ZERO files in jarvis/audio/,
jarvis/voice/, jarvis/live/ and ZERO lines of jarvis/cc/driver.py."

A git diff proves that for one commit. This file proves the stronger and more
durable thing — that the channel COULD NOT have touched them, because it does
not depend on them — and it keeps proving it after the commit that introduced it
has scrolled out of sight. If somebody later reaches across the seam for a
convenience, this is what fails.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE = Path(__file__).parent.parent / "jarvis" / "telegram"

#: The desk. Voice, audio devices and the Gemini Live session. A channel that
#: imported any of these would be a channel that cannot run on a headless box —
#: which is every box this one will ever run on.
FORBIDDEN_PREFIXES = ("jarvis.audio", "jarvis.voice", "jarvis.live")

#: The driver is a process, not a library. Telegram reaches Claude Code through
#: rows in ``requests``; if it ever needs to import the driver, the request row
#: stopped being sufficient and the seam has failed.
FORBIDDEN_MODULES = ("jarvis.cc.driver", "jarvis.cc.permission_host", "jarvis.cc.sdk")


def _modules() -> list[Path]:
    return sorted(PACKAGE.glob("*.py"))


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.add(node.module)
    return found


def test_there_is_something_to_check() -> None:
    assert _modules(), "no telegram modules found; this test would pass vacuously"


@pytest.mark.parametrize("path", _modules(), ids=lambda p: p.name)
def test_no_module_reaches_across_the_seam(path: Path) -> None:
    for name in _imports(path):
        assert not name.startswith(FORBIDDEN_PREFIXES), f"{path.name} imports {name}"
        assert name not in FORBIDDEN_MODULES, f"{path.name} imports {name}"


def test_the_channel_imports_on_a_bare_interpreter() -> None:
    """No third-party dependency at all: the Bot API is HTTPS and JSON.

    Stronger than the house rule, which only binds the spine. It is worth having
    because the moment this channel needs a package, it needs an extra, a wheel
    and a deployment step on whatever machine is meant to be reachable.
    """
    src = Path(__file__).parent.parent
    r = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            f"import sys; sys.path.insert(0, {str(src)!r}); "
            "import jarvis.telegram.bot, jarvis.telegram.channel, jarvis.telegram.commands, "
            "jarvis.telegram.identity, jarvis.telegram.render, jarvis.telegram.transport, "
            "jarvis.telegram.voice; print('ok')",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONNOUSERSITE": "1", "PYTHONPATH": ""},
    )
    assert r.returncode == 0, r.stderr
    assert "ok" in r.stdout


def test_importing_the_channel_needs_no_token_and_no_network() -> None:
    """Nothing reads a credential at import time, and nothing dials out."""
    src = Path(__file__).parent.parent
    r = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import sys; sys.path.insert(0, {str(src)!r}); "
            "import jarvis.telegram.__main__ as m; "
            "print('ok' if m.resolve_token() is None else 'token found')",
        ],
        capture_output=True,
        text=True,
        env={k: v for k, v in os.environ.items() if k not in ("JARVIS_TELEGRAM_TOKEN",)},
    )
    assert r.returncode == 0, r.stderr
    assert "ok" in r.stdout


def test_rendering_a_question_needs_nothing_but_the_row() -> None:
    """A Presentation is the whole contract between the spine and a channel."""
    from jarvis.requests import make_presentation
    from jarvis.telegram import render

    pres = make_presentation(intro="Which database?", options=["SQLite", "Postgres"])
    message = render.render_request(pres, "req_aaaaaaaaaaaa")
    assert "1. SQLite" in message.text
    assert message.markup()["inline_keyboard"]
