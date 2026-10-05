"""The Silero model file: pinned, found in the right order, fetched safely, and never fatal.

Nothing here touches the network: every download goes through an injected
``fetch``, and the pin is swapped for a fake payload's where the real bytes are
not needed. The decision the desk makes (:func:`choose`) is tested here, not in
the composition root, because the root holds wiring and no decisions.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
from pathlib import Path

import numpy as np
import pytest

from jarvis.audio import (
    BARGE_PREROLL_MS,
    IDLE_ONSET,
    IDLE_ONSET_FALLBACK,
    IDLE_PREROLL_MS,
    MAX_TURN_S,
    MIC_RATE,
    VAD_FRAME,
)
from jarvis.audio import vadmodel as vm
from jarvis.audio.dsp import (
    EnergyVad,
    Hearing,
    SileroVad,
    VadUnavailable,
    VoicedEnergyVad,
    synthetic_breath,
    synthetic_vowel,
)
from jarvis.audio.mixer import PlaybackMixer
from jarvis.audio.turn import RecordingUplink, TurnController

PAYLOAD = b"a model, as far as these tests are concerned" * 100


@pytest.fixture
def fake_pin(monkeypatch: pytest.MonkeyPatch) -> bytes:
    """Make PAYLOAD the pinned model, so download and verify run without the real 2 MB."""
    monkeypatch.setattr(vm, "SHA256", hashlib.sha256(PAYLOAD).hexdigest())
    monkeypatch.setattr(vm, "SIZE", len(PAYLOAD))
    return PAYLOAD


class Fetches:
    def __init__(self, data: bytes | Exception) -> None:
        self.data = data
        self.urls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.urls.append(url)
        if isinstance(self.data, Exception):
            raise self.data
        return self.data


# ───────────────────────────── the pin ─────────────────────────────


def test_the_pin_is_v6_2_3_from_a_tag_over_https() -> None:
    assert vm.SHA256 == "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3"
    assert vm.SIZE == 2_327_524
    assert vm.URL.startswith("https://raw.githubusercontent.com/snakers4/silero-vad/v6.2.3/")
    assert vm.URL.endswith("/src/silero_vad/data/silero_vad.onnx")
    assert "MIT" in vm.LICENCE


def test_only_https_is_fetched() -> None:
    with pytest.raises(vm.VadModelMissing, match="non-HTTPS"):
        vm._https_get("http://example.com/silero_vad.onnx")


# ───────────────────────────── where it is looked for ─────────────────────────────


def test_the_user_folder_is_beside_the_wake_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert vm.user_dir() == tmp_path / "jarvis" / "vad"


def test_a_frozen_app_looks_inside_itself_first(tmp_path: Path) -> None:
    not_a_checkout = tmp_path / "site"
    not_a_checkout.mkdir()
    dirs = vm.bundled_dirs(meipass=str(tmp_path / "bundle"), root=not_a_checkout)
    assert dirs == (tmp_path / "bundle" / "jarvis_models" / "vad",)


def test_a_source_checkout_looks_in_packaging_models_and_an_install_does_not(
    tmp_path: Path,
) -> None:
    """In site-packages, ``packaging/`` is somebody else's package."""
    assert vm.bundled_dirs(meipass="", root=tmp_path) == ()
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    assert vm.bundled_dirs(meipass="", root=tmp_path) == (tmp_path / "packaging" / "models",)


def test_the_bundle_comes_before_the_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "bundle"), raising=False)
    order = vm.search_dirs(tmp_path / "user")
    assert order[0] == tmp_path / "bundle" / vm.BUNDLE_DIR
    assert order[-1] == tmp_path / "user"


def test_verified_means_exactly_the_pinned_bytes(tmp_path: Path, fake_pin: bytes) -> None:
    path = tmp_path / vm.MODEL
    assert not vm.verified(path)
    path.write_bytes(fake_pin)
    assert vm.verified(path)
    path.write_bytes(fake_pin[:-1] + b"!")  # same size, other bytes
    assert not vm.verified(path)
    path.write_bytes(fake_pin + b"!")
    assert not vm.verified(path)


def test_find_takes_the_first_good_copy_and_skips_a_bad_one(
    tmp_path: Path, fake_pin: bytes
) -> None:
    bad, good, later = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    for d in (bad, good, later):
        d.mkdir()
    (bad / vm.MODEL).write_bytes(b"swapped")
    (good / vm.MODEL).write_bytes(fake_pin)
    (later / vm.MODEL).write_bytes(fake_pin)
    assert vm.find([bad, good, later]) == good / vm.MODEL
    assert vm.find([bad]) is None


# ───────────────────────────── the download ─────────────────────────────


def test_a_download_is_verified_then_installed_atomically(tmp_path: Path, fake_pin: bytes) -> None:
    fetch = Fetches(fake_pin)
    path = vm.download(tmp_path / "vad", fetch=fetch)
    assert path == tmp_path / "vad" / vm.MODEL and path.read_bytes() == fake_pin
    assert fetch.urls == [vm.URL]
    assert [p.name for p in (tmp_path / "vad").iterdir()] == [vm.MODEL], "no .part left behind"
    vm.download(tmp_path / "vad", fetch=fetch)
    assert len(fetch.urls) == 1, "a verified copy is not fetched again"


def test_a_tampered_download_installs_nothing(tmp_path: Path, fake_pin: bytes) -> None:
    with pytest.raises(vm.VadModelMissing, match="refusing it"):
        vm.download(tmp_path / "vad", fetch=Fetches(b"evil" + fake_pin))
    assert not (tmp_path / "vad" / vm.MODEL).exists()


def test_a_bad_file_on_disk_is_replaced_by_a_good_download(tmp_path: Path, fake_pin: bytes) -> None:
    where = tmp_path / "vad"
    where.mkdir()
    (where / vm.MODEL).write_bytes(b"half a file")
    assert vm.download(where, fetch=Fetches(fake_pin)).read_bytes() == fake_pin


# ───────────────────────────── the desk's choice ─────────────────────────────


class FakeSilero:
    """Stands in for the real detector where only the decision is under test."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.frame_samples = 512
        self.hearing = Hearing(vowel=0.97, breath=0.12, threshold=0.5)

    def is_speech(self, frame: object) -> bool:
        return False

    def reset(self) -> None: ...

    def self_check(self) -> Hearing:
        return self.hearing


def test_with_a_model_the_desk_gets_silero_and_the_measured_numbers(
    tmp_path: Path, fake_pin: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vm, "SileroVad", FakeSilero)
    (tmp_path / vm.MODEL).write_bytes(fake_pin)
    choice = vm.choose(dirs=[tmp_path], allow_download=False)
    assert choice.name == "silero" and isinstance(choice.vad, FakeSilero)
    assert choice.idle_onset == IDLE_ONSET == (4, 5)
    assert choice.idle_preroll_ms == IDLE_PREROLL_MS == 640
    assert choice.barge_preroll_ms == BARGE_PREROLL_MS == 416
    assert choice.max_turn_s == MAX_TURN_S
    assert choice.warning is None
    assert "hears a vowel (0.97)" in choice.detail and "4 of 5" in choice.detail


def test_a_missing_model_is_downloaded_before_the_session_starts(
    tmp_path: Path, fake_pin: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vm, "SileroVad", FakeSilero)
    fetch = Fetches(fake_pin)
    choice = vm.choose(model_dir=tmp_path / "user", dirs=[tmp_path / "empty"], fetch=fetch)
    assert choice.name == "silero"
    assert choice.vad.path == tmp_path / "user" / vm.MODEL
    assert fetch.urls == [vm.URL]


@pytest.mark.parametrize(
    ("fetch", "why"),
    [
        (Fetches(OSError("Temporary failure in name resolution")), "name resolution"),
        (Fetches(b"a captive portal's login page"), "refusing it"),
        (Fetches(TimeoutError("timed out")), "timed out"),
    ],
)
def test_no_model_falls_back_to_the_voiced_detector_with_the_reason(
    tmp_path: Path, fake_pin: bytes, fetch: Fetches, why: str
) -> None:
    """Costs quality, not privacy: the desk starts, and says why it hears less well."""
    choice = vm.choose(model_dir=tmp_path / "user", dirs=[tmp_path / "empty"], fetch=fetch)
    assert choice.name == "voiced" and isinstance(choice.vad, VoicedEnergyVad)
    assert choice.idle_onset == IDLE_ONSET_FALLBACK == (3, 4)
    assert choice.idle_preroll_ms == IDLE_PREROLL_MS
    assert choice.warning is not None and why in choice.warning
    assert "next time the desk starts" in choice.warning, "the fix is named"


def test_no_download_allowed_is_also_a_fallback_not_a_refusal(tmp_path: Path) -> None:
    choice = vm.choose(dirs=[tmp_path], allow_download=False)
    assert choice.name == "voiced" and "not downloaded" in (choice.warning or "")


def test_a_model_that_cannot_hear_is_a_fallback(
    tmp_path: Path, fake_pin: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    def deaf(path: Path) -> SileroVad:
        raise VadUnavailable("the Silero model loaded but cannot tell speech from a breath")

    monkeypatch.setattr(vm, "SileroVad", deaf)
    (tmp_path / vm.MODEL).write_bytes(fake_pin)
    choice = vm.choose(dirs=[tmp_path], allow_download=False)
    assert choice.name == "voiced" and "cannot tell" in (choice.warning or "")


def test_the_modes(tmp_path: Path) -> None:
    basic = vm.choose(mode="basic", dirs=[tmp_path])
    assert basic.name == "voiced" and basic.warning is None
    energy = vm.choose(mode="energy")
    assert isinstance(energy.vad, EnergyVad) and energy.idle_onset is None
    assert energy.idle_preroll_ms == energy.barge_preroll_ms == 320, "exactly the old desk"
    assert energy.warning and "breaths" in energy.warning
    with pytest.raises(ValueError, match="auto, basic, energy"):
        vm.choose(mode="silero-please")


def test_diagnose_never_downloads_and_says_what_to_do(tmp_path: Path) -> None:
    ok, line = vm.diagnose([tmp_path])
    assert ok is False and "not downloaded" in line and "fetches it when it starts" in line
    assert str(tmp_path) in line, "and says where it looked"


def test_the_cli_refuses_bad_arguments(capsys: pytest.CaptureFixture[str]) -> None:
    assert vm.main([]) == 2
    assert vm.main(["fetch", "x"]) == 2
    assert "usage" in capsys.readouterr().err


def test_the_cli_reports_a_failed_download_as_a_sentence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(vm, "_https_get", Fetches(OSError("no route to host")))
    assert vm.main(["download", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "could not get silero_vad.onnx" in err and "no route to host" in err
    assert "Traceback" not in err


def _drive(choice: vm.VadChoice, pcm: np.ndarray) -> RecordingUplink:
    """Build the turn controller exactly as the desk's wiring note does, and feed it."""
    up = RecordingUplink()
    turn = TurnController(
        mixer=PlaybackMixer(rate=MIC_RATE),
        vad=choice.vad,
        uplink=up,
        idle_onset=choice.idle_onset,
        idle_preroll_ms=choice.idle_preroll_ms,
        preroll_ms=choice.barge_preroll_ms,
        max_turn_s=choice.max_turn_s,
    )
    room = np.zeros(MIC_RATE // 2, dtype=np.int16)
    clip = np.concatenate((room, pcm, room))
    for i in range(clip.size // VAD_FRAME):
        turn.feed(clip[i * VAD_FRAME : (i + 1) * VAD_FRAME], at=i * VAD_FRAME / MIC_RATE)
    return up


def test_the_fallback_choice_drives_a_turn_controller_end_to_end() -> None:
    """The desk passes these five fields; together they must drop a breath and keep a vowel."""
    assert _drive(vm.choose(mode="basic"), synthetic_breath(level_dbfs=-26.0)).starts == 0
    assert _drive(vm.choose(mode="basic"), synthetic_vowel()).starts == 1
    assert _drive(vm.choose(mode="energy"), synthetic_breath(level_dbfs=-26.0)).starts == 1, (
        "the escape hatch really is the old rule"
    )


# ───────────────────────────── the real model ─────────────────────────────


def _real() -> Path:
    pytest.importorskip("onnxruntime")
    path = vm.find()
    if path is None:
        if sys.platform == "win32" and os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail("the workflow's 'Fetch the voice activity model' step left no model")
        pytest.skip(
            "no silero_vad.onnx (python -m jarvis.audio.vadmodel download packaging/models)"
        )
    return path


def test_choose_with_the_real_model(tmp_path: Path) -> None:
    shutil.copy(_real(), tmp_path / vm.MODEL)
    choice = vm.choose(dirs=[tmp_path], allow_download=False)
    assert choice.name == "silero" and isinstance(choice.vad, SileroVad)
    assert "hears a vowel" in choice.detail and choice.warning is None


def test_the_silero_choice_drives_a_turn_controller_end_to_end(tmp_path: Path) -> None:
    shutil.copy(_real(), tmp_path / vm.MODEL)

    def choice() -> vm.VadChoice:
        return vm.choose(dirs=[tmp_path], allow_download=False)

    assert _drive(choice(), synthetic_breath(level_dbfs=-26.0)).starts == 0
    assert _drive(choice(), synthetic_vowel()).starts == 1


def test_diagnose_with_the_real_model(tmp_path: Path) -> None:
    shutil.copy(_real(), tmp_path / vm.MODEL)
    ok, line = vm.diagnose([tmp_path])
    assert ok and "hears a vowel" in line and "ignores a breath" in line


def test_the_cli_the_workflow_runs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    shutil.copy(_real(), tmp_path / vm.MODEL)  # already there: verified, not fetched
    assert vm.main(["download", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert vm.SHA256 in out and "hears a vowel" in out and "MIT" in out


# ───────────────────────────── Windows, on the windows-latest runner ─────────────────


windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


@windows_only
def test_on_windows_the_fetched_model_loads_and_hears() -> None:
    """onnxruntime.dll from the wheel, the model from packaging/models, one 576-sample frame."""
    vad = vm.load(_real())
    assert vad.hearing is not None and vad.hearing.ok, vad.hearing


@windows_only
def test_on_windows_the_download_folder_is_in_the_users_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert Path.home() in vm.user_dir().parents
    assert vm.user_dir().name == "vad"


@windows_only
def test_on_windows_an_install_replaces_a_bad_file_in_place(
    tmp_path: Path, fake_pin: bytes
) -> None:
    """os.replace over an existing file: the rename half of the atomic install, on NTFS."""
    where = tmp_path / "vad"
    where.mkdir()
    (where / vm.MODEL).write_bytes(b"x" * len(fake_pin))
    assert vm.download(where, fetch=Fetches(fake_pin)).read_bytes() == fake_pin
    assert not (where / f".{vm.MODEL}.part").exists()
