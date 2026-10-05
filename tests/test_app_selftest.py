"""``--selftest``: the build checks itself, and Linux is not mistaken for a broken Windows.

The real run below is the point: every check executes here, against this
install, over real HTTP. It must pass on Linux with the Windows-only check
marked skipped, and with PortAudio — a system library on Linux, bundled in
the wheel on Windows — allowed to be absent.
"""

from __future__ import annotations

import json
import sys
import threading
import types
from pathlib import Path

import pytest

from jarvis.app import selftest
from jarvis.app.selftest import Check, Skipped


@pytest.fixture(scope="module")
def linux_report(tmp_path_factory: pytest.TempPathFactory) -> tuple[int, dict]:
    report = tmp_path_factory.mktemp("selftest") / "report.json"
    code = selftest.main(report=str(report), platform="linux")
    return code, json.loads(report.read_text(encoding="utf-8"))


def test_the_real_selftest_passes_on_linux(linux_report: tuple[int, dict]) -> None:
    code, results = linux_report
    failed = {n: r for n, r in results.items() if not r["ok"] and r["critical"]}
    assert code == 0, failed
    for name, r in results.items():
        assert {"ok", "detail", "critical"} <= set(r), name
        assert isinstance(r["ok"], bool) and isinstance(r["detail"], str)


def test_the_windows_voice_is_skipped_not_passed(linux_report: tuple[int, dict]) -> None:
    _, results = linux_report
    sapi = results["windows voice"]
    assert sapi["ok"] is True and sapi.get("skipped") is True
    assert sapi["detail"].startswith("skipped")


def test_the_contract_checks_are_all_there(linux_report: tuple[int, dict]) -> None:
    _, results = linux_report
    assert {
        "numpy",
        "portaudio",
        "soxr",
        "onnxruntime",
        "google.genai",
        "keyring",
        "claude code",
        "migrations",
        "window files",
        "database",
        "window server",
        "windows voice",
        "time zones",
        "british voice",
        "tray",
    } <= set(results)
    assert results["window server"]["ok"], results["window server"]
    assert results["database"]["ok"] and results["claude code"]["ok"]
    assert results["british voice"]["critical"] is False
    assert results["tray"]["critical"] is False


def test_portaudio_missing_on_linux_is_reported_honestly(linux_report: tuple[int, dict]) -> None:
    _, results = linux_report
    pa = results["portaudio"]
    assert pa["critical"] is False
    try:
        import sounddevice  # noqa: F401
    except OSError:
        assert pa["ok"] is False and "PortAudio" in pa["detail"]
    else:
        assert pa["ok"] is True


def test_on_windows_portaudio_and_the_voice_are_critical() -> None:
    by_name = {c.name: c for c in selftest.checks("win32")}
    assert by_name["portaudio"].critical and by_name["windows voice"].critical
    assert by_name["keyring"].critical
    assert not by_name["tray"].critical and not by_name["british voice"].critical


def test_a_bundled_build_with_no_portaudio_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    # None in sys.modules makes the import itself fail, as a build that left
    # the module out would.
    monkeypatch.setitem(sys.modules, "sounddevice", None)
    results = selftest.run_checks([c for c in selftest.checks("win32") if c.name == "portaudio"])
    assert results["portaudio"]["ok"] is False and results["portaudio"]["critical"] is True
    assert not selftest.passed(results)


def test_portaudio_with_no_devices_is_fine(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = types.ModuleType("sounddevice")

    def no_devices() -> list:
        raise RuntimeError("Error querying device -1")

    fake.get_portaudio_version = lambda: (1, "PortAudio V19.7.0")  # type: ignore[attr-defined]
    fake.query_devices = no_devices  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    results = selftest.run_checks([c for c in selftest.checks("win32") if c.name == "portaudio"])
    assert results["portaudio"]["ok"] is True
    assert "no devices" in results["portaudio"]["detail"]


def test_a_windows_build_without_a_keyring_backend_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    import keyring
    from keyring.backends import fail

    monkeypatch.setattr(keyring, "get_keyring", lambda: fail.Keyring())
    (check,) = [c for c in selftest.checks("win32") if c.name == "keyring"]
    assert selftest.run_checks([check])["keyring"]["ok"] is False
    (check,) = [c for c in selftest.checks("linux") if c.name == "keyring"]
    assert selftest.run_checks([check])["keyring"]["ok"] is True


# ───────────────────────────── the machinery ─────────────────────────────


def test_a_hung_check_fails_on_its_deadline_and_the_rest_still_run() -> None:
    gate = threading.Event()
    results = selftest.run_checks(
        [Check("hangs", lambda: str(gate.wait(30))), Check("fine", lambda: "ok")],
        timeout_s=0.2,
    )
    gate.set()
    assert results["hangs"] == {"ok": False, "detail": "did not finish in 0s", "critical": True}
    assert results["fine"]["ok"] is True


def test_only_critical_failures_fail_the_run() -> None:
    def boom() -> str:
        raise ImportError("no module named edge_tts")

    def skip() -> str:
        raise Skipped("Windows only")

    soft = selftest.run_checks([Check("soft", boom, critical=False), Check("na", skip)])
    assert selftest.passed(soft)
    hard = selftest.run_checks([Check("hard", boom)])
    assert not selftest.passed(hard)
    assert hard["hard"]["detail"] == "ImportError: no module named edge_tts"


def test_main_writes_the_report_and_exits_by_the_verdict(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom() -> str:
        raise RuntimeError("no migrations")

    report = tmp_path / "r.json"
    assert selftest.main(report=str(report), items=[Check("migrations", boom)]) == 1
    assert json.loads(report.read_text(encoding="utf-8"))["migrations"]["ok"] is False
    assert "selftest FAILED: migrations" in capsys.readouterr().out
    assert selftest.main(items=[Check("fine", lambda: "ok")]) == 0


def test_the_app_command_runs_the_selftest_without_taking_the_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from jarvis import __main__ as cli
    from jarvis.app import instance

    def no_lock(**_: object) -> None:
        raise AssertionError("--selftest must not start an app")

    seen: dict[str, object] = {}
    monkeypatch.setattr(instance, "acquire", no_lock)
    monkeypatch.setattr(selftest, "main", lambda **kw: seen.update(kw) or 0)
    report = str(tmp_path / "r.json")
    assert cli.main(["app", "--selftest", "--report", report]) == 0
    assert seen == {"report": report}
