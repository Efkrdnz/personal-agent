"""The Silero model ships inside Jarvis.exe, and every link of that chain agrees with the next.

Workflow fetches into packaging/models -> git ignores that folder -> the spec
verifies and bundles it at vadmodel.BUNDLE_DIR with its MIT notice -> the
frozen app looks there first -> the exe's selftest proves it can hear. Each
link is a file nothing runs until a Windows runner does, so each is read here
as text and checked against its neighbour, the way tests/test_packaging.py
checks the rest of the build.
"""

from __future__ import annotations

import ast
import hashlib
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from jarvis.app import selftest
from jarvis.app.selftest import Skipped
from jarvis.audio import vadmodel as vm
from jarvis.audio.dsp import Hearing, SileroVad

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "packaging" / "jarvis.spec"
WORKFLOW = ROOT / ".github" / "workflows" / "windows-app.yml"
PAYLOAD = b"stand-in model bytes " * 64


@pytest.fixture
def fake_pin(monkeypatch: pytest.MonkeyPatch) -> bytes:
    monkeypatch.setattr(vm, "SHA256", hashlib.sha256(PAYLOAD).hexdigest())
    monkeypatch.setattr(vm, "SIZE", len(PAYLOAD))
    return PAYLOAD


def _spec_tree() -> ast.Module:
    return ast.parse(SPEC.read_text(encoding="utf-8"), filename=str(SPEC))


def _spec_constant(name: str) -> Any:
    for node in _spec_tree().body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"jarvis.spec has no literal {name}")


def _spec_function(name: str, namespace: dict[str, Any]) -> Any:
    for node in _spec_tree().body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            exec(compile(ast.Module([node], []), str(SPEC), "exec"), namespace)  # noqa: S102
            return namespace[name]
    raise AssertionError(f"jarvis.spec has no function {name}")


def _steps() -> list[dict[str, Any]]:
    import yaml

    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["build"]["steps"]


def _index(step_id: str) -> int:
    return next(i for i, s in enumerate(_steps()) if s.get("id") == step_id)


def _vad_model(root: Path, *, windows: bool, notes: list[str]) -> Any:
    return _spec_function(
        "_vad_model",
        {
            "ROOT": root,
            "WINDOWS": windows,
            "VAD_MODEL_DIR": _spec_constant("VAD_MODEL_DIR"),
            "VAD_NOTICE": _spec_constant("VAD_NOTICE"),
            "_note": notes.append,
        },
    )


# ───────────────────────────── the spec ─────────────────────────────


def test_a_windows_build_without_the_model_is_refused(tmp_path: Path) -> None:
    """An exe that silently fell back would let every headset breath through as a turn."""
    with pytest.raises(SystemExit, match="vadmodel download packaging/models"):
        _vad_model(tmp_path, windows=True, notes=[])()


def test_a_windows_build_with_a_swapped_model_is_refused(tmp_path: Path, fake_pin: bytes) -> None:
    (tmp_path / "packaging" / "models").mkdir(parents=True)
    (tmp_path / "packaging" / "models" / vm.MODEL).write_bytes(b"x" + fake_pin[1:])
    with pytest.raises(SystemExit, match="does not match its pinned SHA-256"):
        _vad_model(tmp_path, windows=True, notes=[])()


def test_elsewhere_a_build_without_it_is_noted_not_refused(tmp_path: Path) -> None:
    notes: list[str] = []
    assert _vad_model(tmp_path, windows=False, notes=notes)() == []
    assert notes and "voice activity model" in notes[0]


def test_the_model_and_its_notice_go_where_the_frozen_app_looks(
    tmp_path: Path, fake_pin: bytes
) -> None:
    models = tmp_path / "packaging" / "models"
    models.mkdir(parents=True)
    (models / vm.MODEL).write_bytes(fake_pin)
    entries = _vad_model(tmp_path, windows=True, notes=[])()
    assert entries == [
        (str(models / vm.MODEL), vm.BUNDLE_DIR),
        (str(tmp_path / _spec_constant("VAD_NOTICE")), vm.BUNDLE_DIR),
    ]
    # ...and that destination is the first place a frozen app searches.
    assert vm.bundled_dirs(meipass=str(tmp_path / "MEI"), root=tmp_path / "nowhere")[0] == (
        tmp_path / "MEI" / vm.BUNDLE_DIR
    )


def test_the_analysis_actually_collects_it() -> None:
    """The caller, not the callee: a _vad_model nobody calls bundles nothing."""
    analysis = next(
        node
        for node in ast.walk(_spec_tree())
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Analysis"
    )
    datas = next(k.value for k in analysis.keywords if k.arg == "datas")
    assert "_vad_model()" in ast.unparse(datas)


def test_the_wake_model_refusal_does_not_catch_silero() -> None:
    refuse = _spec_function(
        "_refuse_wake_models", {"Path": Path, "NEVER_BUNDLE": _spec_constant("NEVER_BUNDLE")}
    )
    refuse(
        [
            (f"{vm.BUNDLE_DIR}/{vm.MODEL}", f"/src/packaging/models/{vm.MODEL}", "DATA"),
            (f"{vm.BUNDLE_DIR}/silero-vad.txt", "/src/packaging/licenses/silero-vad.txt", "DATA"),
        ]
    )


def test_the_notice_that_ships_is_the_mit_licence_for_this_exact_file() -> None:
    notice = (ROOT / _spec_constant("VAD_NOTICE")).read_text(encoding="utf-8")
    assert "MIT License" in notice and "Copyright (c) 2020-present Silero Team" in notice
    assert vm.SHA256 in notice and f"v{vm.VERSION}" in notice
    credits = (ROOT / "CREDITS.md").read_text(encoding="utf-8")
    assert "Silero VAD" in credits and "MIT" in credits


# ───────────────────────────── the workflow ─────────────────────────────


def test_the_workflow_fetches_the_model_where_the_spec_reads_it() -> None:
    step = _steps()[_index("vadmodel")]
    run = step["run"]
    assert f"python -m jarvis.audio.vadmodel download {_spec_constant('VAD_MODEL_DIR')}" in run
    assert "ci-logs/" in run and "Tee-Object" in run and "LASTEXITCODE" in run
    assert callable(vm.main), "the module the workflow runs has its entry point"


def test_it_is_fetched_after_install_and_before_the_tests_and_the_build() -> None:
    assert _index("install") < _index("vadmodel") < _index("tests") < _index("build")


def test_the_tests_still_run_if_the_fetch_failed() -> None:
    """One run should name every failure; the model's own tests then report the missing file."""
    cond = _steps()[_index("tests")].get("if", "")
    assert "steps.install.outcome == 'success'" in cond and "!cancelled()" in cond


def test_the_licence_check_on_the_bundle_still_lets_silero_through() -> None:
    """Broadening ADR 0012's check to every .onnx would fail every build from now on."""
    script = _steps()[_index("licence")]["run"]
    pattern = re.search(r"-match '([^']+)'", script)
    assert pattern, "the wake-model pattern moved"
    assert re.match(pattern.group(1), "hey_jarvis_v0.1.onnx")
    assert not re.match(pattern.group(1), vm.MODEL)


# ───────────────────────────── git ─────────────────────────────


def test_the_fetched_model_can_never_be_committed() -> None:
    hit = subprocess.run(
        ["git", "check-ignore", "-q", f"packaging/models/{vm.MODEL}"], cwd=ROOT, check=False
    )
    assert hit.returncode == 0, "packaging/models/ must be ignored"
    tracked = subprocess.run(
        ["git", "ls-files", "packaging"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    assert not [f for f in tracked if f.endswith(".onnx")]


# ───────────────────────────── the exe's selftest ─────────────────────────────


def test_the_selftest_has_the_check_and_it_is_critical_on_windows() -> None:
    win = {c.name: c for c in selftest.checks("win32")}
    lin = {c.name: c for c in selftest.checks("linux")}
    assert win["voice activity"].critical is True
    assert lin["voice activity"].critical is False


def test_a_frozen_app_without_its_bundled_model_fails_the_selftest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its OWN copy: a model an earlier pip install left in the profile does not count."""
    monkeypatch.setattr(vm, "search_dirs", lambda *a, **k: pytest.fail("looked outside"))
    with pytest.raises(RuntimeError, match="not inside the bundle"):
        selftest._voice_activity(meipass=str(tmp_path))


def test_from_source_with_no_model_anywhere_it_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vm, "search_dirs", lambda *a, **k: (tmp_path,))
    monkeypatch.setattr(sys, "_MEIPASS", "", raising=False)
    with pytest.raises(Skipped, match="downloads it on first use"):
        selftest._voice_activity()


def test_a_deaf_model_fails_the_selftest(
    tmp_path: Path, fake_pin: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    where = tmp_path / vm.BUNDLE_DIR
    where.mkdir(parents=True)
    (where / vm.MODEL).write_bytes(fake_pin)

    class Deaf:
        def __init__(self, path: Path, check: bool = True) -> None: ...

        def self_check(self) -> Hearing:
            return Hearing(vowel=0.001, breath=0.0, threshold=0.5)

    monkeypatch.setattr("jarvis.audio.dsp.SileroVad", Deaf)
    with pytest.raises(RuntimeError, match="cannot tell them apart"):
        selftest._voice_activity(meipass=str(tmp_path))


def test_the_check_runs_inside_the_real_selftest_and_never_fails_it_on_linux() -> None:
    (check,) = [c for c in selftest.checks("linux") if c.name == "voice activity"]
    result = selftest.run_checks([check])["voice activity"]
    assert result["ok"] is True, result


def test_a_frozen_app_with_the_real_model_inside_hears(tmp_path: Path) -> None:
    pytest.importorskip("onnxruntime")
    real = vm.find()
    if real is None:
        pytest.skip("no silero_vad.onnx here")
    where = tmp_path / vm.BUNDLE_DIR
    where.mkdir(parents=True)
    shutil.copy(real, where / vm.MODEL)
    assert "hears a vowel" in selftest._voice_activity(meipass=str(tmp_path))
    assert SileroVad(where / vm.MODEL).hearing is not None
