"""Code paths that were Linux-only by accident, pinned so they stay portable.

Each test removes or simulates the thing Windows lacks, rather than skipping
off Linux: the suite runs on Linux CI, and a portability test that only runs
on the platform it protects never runs.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import pytest

from jarvis import bus
from jarvis.clock import day_month, spoken_date


def test_poke_paths_need_no_getuid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Windows has AF_UNIX on newer Pythons but no os.getuid; the bind path met both."""
    monkeypatch.delattr(os, "getuid", raising=False)
    monkeypatch.delenv("JARVIS_POKE_DIR", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    d = bus.poke_dir()
    assert d.parent.name.startswith("jarvis-") and d.parent.parent == tmp_path
    assert bus.poke_path("peer").name == "peer.sock"
    assert bus.poke_path("x" * 300).name.startswith("jarvis-")


def test_the_user_tag_is_stable_and_names_no_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(os, "getuid", raising=False)
    monkeypatch.setattr("getpass.getuser", lambda: "Efkan")
    a, b = bus._user_tag(), bus._user_tag()
    assert a == b and "Efkan" not in a and len(a) == 8


def test_dates_are_spoken_without_glibc_strftime_flags() -> None:
    """'%-d' raises ValueError on Windows; the day is formatted by hand instead."""
    assert day_month(datetime(2026, 10, 5)) == "Monday 5 October"
    assert spoken_date("2026-10-05T09:00:00.000Z").endswith(" October")


def test_no_glibc_only_strftime_flags_anywhere() -> None:
    import re

    root = Path(__file__).parent.parent / "jarvis"
    flag = re.compile(r"strftime\([^)]*%-[a-zA-Z]")
    offenders = [
        f"{p.relative_to(root)}:{i}"
        for p in root.rglob("*.py")
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if flag.search(line) and not line.lstrip().startswith(("#", "Not ``"))
    ]
    assert offenders == []
