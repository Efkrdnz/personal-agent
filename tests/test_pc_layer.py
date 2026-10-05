"""jarvis.pc is a platform layer: standard library only, below the tools, never above them.

Until the lead adds ``jarvis/pc`` to ``tools/check_layers.py`` (see the WIRING
note), this file is what holds the line, with the same AST reader CI uses.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from jarvis.pc import NullDesktop, available, choose_desktop

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.check_layers import imports_of  # noqa: E402 - the repo root is not on sys.path


def test_the_platform_layer_imports_on_a_bare_interpreter() -> None:
    code = (
        f"import sys; sys.path.insert(0, {str(ROOT)!r}); "
        "import jarvis.pc, jarvis.pc.windows, jarvis.pc.posix, jarvis.pc.catalog, "
        "jarvis.pc.safety; print('ok')"
    )
    r = subprocess.run(
        [sys.executable, "-S", "-c", code],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONNOUSERSITE": "1", "PYTHONPATH": ""},
        timeout=60,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    assert "ok" in r.stdout


def test_the_platform_layer_knows_no_other_jarvis_layer() -> None:
    files = sorted((ROOT / "jarvis" / "pc").rglob("*.py"))
    assert len(files) >= 6
    reached = [
        f"{p.relative_to(ROOT).as_posix()} imports {name}"
        for p in files
        for name in sorted(imports_of(p, ROOT))
        if name.startswith("jarvis") and not name.startswith("jarvis.pc")
    ]
    assert not reached, "\n".join(reached)


def test_only_the_tool_module_reaches_the_platform_layer() -> None:
    # The window, the voice layer and the channels reach a PC action only
    # through the registry, which is where the channel gate and the yes live.
    reachers = sorted(
        p.relative_to(ROOT).as_posix()
        for p in (ROOT / "jarvis").rglob("*.py")
        if not p.is_relative_to(ROOT / "jarvis" / "pc")
        and any(n == "jarvis.pc" or n.startswith("jarvis.pc.") for n in imports_of(p, ROOT))
    )
    allowed = {"jarvis/tools/builtin/pc.py", "jarvis/__main__.py", "jarvis/app/selftest.py"}
    assert set(reachers) <= allowed, reachers
    assert "jarvis/tools/builtin/pc.py" in reachers


@pytest.mark.parametrize(
    ("platform", "env", "backend"),
    [
        ("win32", {}, "windows"),
        ("linux", {"DISPLAY": ":0"}, "linux"),
        ("linux", {}, "none"),
        ("darwin", {}, "macos"),
        ("sunos5", {}, "none"),
    ],
)
def test_the_backend_follows_the_platform(platform: str, env: dict[str, str], backend: str) -> None:
    d = choose_desktop(platform=platform, env=env, which=lambda n: None)
    assert d.backend == backend
    assert available(platform=platform, env=env, which=lambda n: None) is (backend != "none")


def test_a_null_desktop_refuses_everything_with_its_reason() -> None:
    from jarvis.pc.base import PcRefused

    d = NullDesktop("No desktop here.")
    for call in (d.apps, d.lock, d.windows, lambda: d.media("next"), lambda: d.power("shutdown")):
        with pytest.raises(PcRefused, match="No desktop here."):
            call()
