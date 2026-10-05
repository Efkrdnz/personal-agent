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
    # A short temp dir: a socket path past sun_path's 108 bytes is renamed to
    # a hash, and a CI runner's own temp dir is long enough to trigger that.
    short = Path(tmp_path.anchor) / "t"
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(short))
    d = bus.poke_dir()
    assert d.parent.name.startswith("jarvis-") and d.parent.parent == short
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


# ───────────────────────────── processes ─────────────────────────────


def test_no_process_groups_means_no_pgid_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    """`python -m jarvis run` died here on Windows: os.getpgid does not exist."""
    from jarvis import jobs

    monkeypatch.delattr(os, "getpgid", raising=False)
    assert jobs.pgid_of(os.getpid()) is None
    jobs.process_identity(os.getpid())  # the runner's first call; must not raise


class FakeKernel32:
    def __init__(self, handle: int, exit_code: int = 259, ok: bool = True) -> None:
        self.handle, self.exit_code, self.ok = handle, exit_code, ok
        self.closed: list[int] = []

    def OpenProcess(self, access: int, inherit: bool, pid: int) -> int:  # noqa: N802
        return self.handle

    def GetExitCodeProcess(self, handle: int, out: object) -> bool:  # noqa: N802
        out._obj.value = self.exit_code  # type: ignore[attr-defined]
        return self.ok

    def CloseHandle(self, handle: int) -> bool:  # noqa: N802
        self.closed.append(handle)
        return True


@pytest.mark.parametrize(
    ("kernel", "error", "expected"),
    [
        (FakeKernel32(0), 87, "dead"),  # no such process
        (FakeKernel32(0), 5, "unknown"),  # exists, not ours
        (FakeKernel32(7, exit_code=259), 0, "unknown"),  # still running: never "alive"
        (FakeKernel32(7, exit_code=0), 0, "dead"),  # exited
        (FakeKernel32(7, ok=False), 0, "unknown"),
    ],
)
def test_windows_liveness_asks_the_kernel_and_closes_what_it_opened(
    kernel: FakeKernel32, error: int, expected: str
) -> None:
    from jarvis import jobs

    got = jobs._pid_exists_windows(1234, kernel32=kernel, last_error=lambda: error)
    assert got == expected
    assert kernel.closed == ([7] if kernel.handle else [])


def test_on_windows_the_probe_never_calls_os_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    """os.kill(pid, 0) on Windows is Ctrl+C to the console, and on 3.11 can kill the pid."""
    from jarvis import jobs, kill

    def no_kill(*_: object) -> None:
        raise AssertionError("os.kill must never run as a probe on Windows")

    monkeypatch.setattr(os, "kill", no_kill)
    monkeypatch.setattr(jobs.os, "name", "nt")
    monkeypatch.setattr(jobs, "_pid_exists_windows", lambda pid: "dead")
    assert jobs._pid_exists(4242) == "dead"
    assert jobs.pid_is_gone(4242) is True
    assert kill._gone(4242) is True


def test_terminate_works_without_groups_or_sigkill(monkeypatch: pytest.MonkeyPatch) -> None:
    import signal

    from jarvis import kill

    for name in ("getpgrp", "killpg"):
        monkeypatch.delattr(os, name, raising=False)
    monkeypatch.delattr(signal, "SIGKILL", raising=False)
    sent: list[int] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append(sig))
    monkeypatch.setattr(kill, "process_liveness", lambda *a: "unknown")
    monkeypatch.setattr(kill, "_gone", lambda pid: False)
    assert kill.terminate(4242, pgid=99, grace_s=0.0) == "kill"
    assert sent == [signal.SIGTERM, signal.SIGTERM]  # no group, no SIGKILL: the pid, twice


# ───────────────────────────── text and time ─────────────────────────────


def test_migrations_are_read_as_utf8_whatever_the_locale() -> None:
    """A Turkish Windows code page cannot decode the migrations' em dashes and Turkish."""
    import subprocess
    import sys

    src = Path(__file__).parent.parent
    r = subprocess.run(
        [
            sys.executable,
            "-X",
            "warn_default_encoding",
            "-W",
            "error::EncodingWarning",
            "-c",
            "from jarvis import db; c = db.connect(':memory:'); db.migrate(c); print('ok')",
        ],
        cwd=src,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr


def test_a_config_saved_with_a_bom_loads(tmp_path: Path) -> None:
    from jarvis import config

    f = tmp_path / "config.toml"
    f.write_bytes(b"\xef\xbb\xbf" + b'tz = "Europe/Istanbul"\n')
    assert config.load(f).tz == "Europe/Istanbul"


def test_a_utf16_config_says_how_to_fix_it(tmp_path: Path) -> None:
    from jarvis import config

    f = tmp_path / "config.toml"
    f.write_text('tz = "Europe/Istanbul"\n', encoding="utf-16")
    with pytest.raises(ValueError, match="not UTF-8"):
        config.load(f)


def test_missing_zone_data_names_the_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    from zoneinfo import ZoneInfoNotFoundError

    from jarvis.clock import local_tz
    from jarvis.schedule.recurrence import zone

    monkeypatch.setenv("JARVIS_TZ", "Nowhere/Atlantis")
    with pytest.raises(ZoneInfoNotFoundError, match="tzdata"):
        local_tz()
    with pytest.raises(ValueError, match="tzdata"):
        zone("Nowhere/Atlantis")


def test_windows_installs_get_zone_data() -> None:
    import tomllib

    meta = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text("utf-8"))
    deps = meta["project"]["dependencies"]
    assert any(d.replace(" ", "").startswith("tzdata;sys_platform=='win32'") for d in deps)


def test_doctor_reports_missing_zone_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from jarvis import __main__ as cli

    cfg = tmp_path / "c.toml"
    cfg.write_text('tz = "Nowhere/Atlantis"\n', encoding="utf-8")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    cli.main(["--db", str(tmp_path / "j.db"), "--config", str(cfg), "doctor"])
    assert "uv pip install tzdata" in capsys.readouterr().out


def test_unencodable_output_is_replaced_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    import sys

    from jarvis import __main__ as cli

    raw = io.BytesIO()
    cp1252 = io.TextIOWrapper(raw, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", cp1252)
    cli._tolerant_output()
    print("── reader voice ── ş")
    cp1252.flush()
    assert b"reader voice" in raw.getvalue()
