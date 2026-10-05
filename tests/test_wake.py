"""The desk sleeps until it hears "hey Jarvis" — and sends nothing while it sleeps.

The failure that matters most here is not a missed wake word, it is the
opposite: a desk that the user believes is asleep and is in fact streaming the
room to Gemini. So the gate is tested from the uplink's side — what was SENT —
not from the controller's flags.

The model tests run the real openWakeWord models when they are on disk
(`python -m jarvis wake download`) and the OS voice can say the phrase; they
skip, saying why, when either is missing.
"""

from __future__ import annotations

import ast
import shutil
from pathlib import Path

import numpy as np
import pytest

from jarvis import __main__ as cli
from jarvis.audio import MIC_RATE, VAD_FRAME
from jarvis.audio import wake as wk
from jarvis.audio.dsp import EnergyVad
from jarvis.audio.graph import AudioGraph
from jarvis.audio.micbus import MicBus
from jarvis.audio.mixer import PlaybackMixer, Prio
from jarvis.audio.turn import RecordingUplink, TurnController, TurnEvent
from jarvis.config import Config, Voice

FRAME_S = VAD_FRAME / MIC_RATE


class Vad:
    """Speech when the frame is loud. The gate is under test, not the VAD."""

    frame_samples = VAD_FRAME

    def is_speech(self, frame: np.ndarray) -> bool:
        return bool(np.abs(frame).max() > 1000)

    def reset(self) -> None:
        return None


LOUD = np.full(VAD_FRAME, 5000, dtype=np.int16)
QUIET = np.zeros(VAD_FRAME, dtype=np.int16)


def gated(window: float | None = 20.0):
    mix = PlaybackMixer(rate=MIC_RATE)
    up = RecordingUplink()
    events: list[TurnEvent] = []
    turn = TurnController(
        mixer=mix, vad=Vad(), uplink=up, on_event=events.append, wake_window_s=window
    )
    return turn, mix, up, events


def feed(turn: TurnController, frames: list[np.ndarray], start: int = 0) -> int:
    for i, f in enumerate(frames, start):
        turn.feed(f, at=i * FRAME_S)
    return start + len(frames)


# ───────────────────────────── the gate ─────────────────────────────


def test_asleep_nothing_is_sent_however_long_you_talk() -> None:
    turn, _, up, _ = gated()
    feed(turn, [LOUD] * 300)  # ten seconds of speech
    assert up.starts == 0 and up.samples == 0


def test_without_a_wake_word_the_desk_listens_as_it_always_did() -> None:
    turn, _, up, _ = gated(window=None)
    feed(turn, [LOUD] * 5)
    assert up.starts == 1 and turn.awake(0.0)


def test_the_request_after_the_wake_word_opens_on_the_next_frame() -> None:
    turn, _, up, events = gated()
    i = feed(turn, [LOUD] * 20)  # "hey Jarvis, what's the..." — still asleep
    assert up.starts == 0
    turn.wake(i * FRAME_S)
    feed(turn, [LOUD], start=i)  # the very next frame: no new onset needed
    assert up.starts == 1
    # And the pre-roll went with it: the start of the request is not lost.
    assert up.samples >= VAD_FRAME * 5
    assert "wake.awake" in [e.kind for e in events]


def test_the_window_stays_open_while_anyone_talks_and_closes_after() -> None:
    turn, _, up, events = gated(window=2.0)
    turn.wake(0.0)
    i = feed(turn, [LOUD] * 10 + [QUIET] * 40)  # a turn, then 1.3 s of quiet
    assert up.starts == 1
    i = feed(turn, [LOUD] * 5, start=i)  # still inside the window: a second turn
    assert up.starts == 2
    i = feed(turn, [QUIET] * 200, start=i)  # 6.4 s of nothing
    assert not turn.awake(i * FRAME_S)
    assert [e.kind for e in events].count("wake.asleep") == 1
    feed(turn, [LOUD] * 10, start=i)
    assert up.starts == 2  # asleep again: nothing sent


def test_jarvis_speaking_holds_the_window_so_a_question_can_be_answered() -> None:
    turn, mix, up, _ = gated(window=1.0)
    mix.track("reader", Prio.VERBATIM).write(np.full(MIC_RATE * 3, 100, dtype=np.int16))
    i = feed(turn, [QUIET] * 10)
    assert turn.awake(i * FRAME_S)  # never woken by name, but Jarvis is talking


def test_sleep_closes_the_window_at_once() -> None:
    turn, _, up, _ = gated()
    turn.wake(0.0)
    turn.sleep()
    feed(turn, [LOUD] * 10)
    assert up.starts == 0


# ───────────────────────────── the detector policy ─────────────────────────────


def test_one_phrase_wakes_once_not_once_per_high_chunk() -> None:
    d = wk.WakeDetector(threshold=0.5, refractory_s=2.0)
    hits = [d.feed(s, i * 0.08) for i, s in enumerate([0.1, 0.9, 0.95, 0.9, 0.2])]
    assert hits == [False, True, False, False, False]
    assert d.feed(0.9, 3.0) is True  # after the refractory period, again


def test_patience_needs_consecutive_chunks() -> None:
    d = wk.WakeDetector(threshold=0.5, patience=2)
    assert [d.feed(s, i) for i, s in enumerate([0.9, 0.1, 0.9, 0.9])] == [
        False,
        False,
        False,
        True,
    ]


# ───────────────────────────── the watch ─────────────────────────────


class Scripted:
    def __init__(self, scores: list[float]) -> None:
        self.scores = list(scores)

    def score(self, chunk: np.ndarray) -> float:
        return self.scores.pop(0) if self.scores else 0.0

    def reset(self) -> None:
        return None


def watch_with(scores: list[float], window: float = 20.0):
    bus = MicBus(rate=MIC_RATE, seconds=4.0)
    turn, _, up, _ = gated(window)
    woke: list[float] = []
    slept: list[float] = []
    watch = wk.WakeWatch(
        reader=bus.reader("wake"),
        model=Scripted(scores),
        turn=turn,
        on_wake=lambda at, s: woke.append(s),
        on_sleep=slept.append,
    )
    return bus, turn, watch, woke, slept


def pump(bus: MicBus, watch: wk.WakeWatch, chunks: int) -> None:
    for _ in range(chunks):
        bus.write(np.zeros(wk.CHUNK, dtype=np.int16))
        chunk = watch.reader.read(wk.CHUNK, timeout=0.0)
        assert chunk is not None
        watch.step(chunk)


def test_a_hit_wakes_the_desk_and_says_so() -> None:
    bus, turn, watch, woke, _ = watch_with([0.0, 0.0, 0.93])
    pump(bus, watch, 3)
    assert woke == [0.93] and watch.hits == 1
    assert turn.awake(watch.reader.cursor / MIC_RATE)


def test_jarvis_saying_his_own_name_does_not_wake_him() -> None:
    bus, turn, watch, woke, _ = watch_with([0.95])
    turn.note_output_transcript("Your email says: hey Jarvis, call me back", at=0.0)
    pump(bus, watch, 1)
    assert woke == [] and watch.vetoed == 1 and not turn.awake(0.1)


def test_the_name_mid_conversation_does_not_wake_again() -> None:
    bus, turn, watch, woke, _ = watch_with([0.95, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.95])
    pump(bus, watch, 10)
    assert woke == [0.95]  # the second "Jarvis" was a word in a sentence


def test_the_watch_reports_when_the_window_closes() -> None:
    bus, turn, watch, woke, slept = watch_with([0.95], window=0.5)
    pump(bus, watch, 20)  # 1.6 s
    assert len(woke) == 1 and len(slept) == 1


def test_the_thread_stops_when_asked_and_when_the_bus_closes() -> None:
    bus, _, watch, _, _ = watch_with([])
    watch.start()
    bus.close()
    watch.stop(timeout=2.0)
    assert watch._thread is not None and not watch._thread.is_alive()


# ───────────────────────────── the models on disk ─────────────────────────────


def fake_release() -> dict[str, bytes]:
    return {name: f"model {name}".encode() for name in wk.MODELS}


def test_download_installs_only_files_that_match_their_pinned_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = fake_release()
    monkeypatch.setattr(wk, "MODELS", {n: wk._sha256(b) for n, b in files.items()})
    got = wk.download("hey_jarvis", tmp_path, fetch=lambda url: files[url.rsplit("/", 1)[1]])
    assert set(got) == set(files) and wk.missing("hey_jarvis", tmp_path) == ()
    assert wk.download("hey_jarvis", tmp_path, fetch=lambda url: b"never called") == ()


def test_a_tampered_download_is_refused_and_nothing_is_installed(tmp_path: Path) -> None:
    with pytest.raises(wk.ModelsMissing, match="refusing it"):
        wk.download("hey_jarvis", tmp_path, fetch=lambda url: b"not the model")
    assert not any(tmp_path.iterdir())


def test_a_corrupted_file_on_disk_counts_as_missing(tmp_path: Path) -> None:
    for name in wk.MODELS:
        (tmp_path / name).write_bytes(b"truncated")
    assert set(wk.missing("hey_jarvis", tmp_path)) == set(wk.MODELS)
    with pytest.raises(wk.ModelsMissing, match="wake download"):
        wk.OnnxWakeWord("hey_jarvis", tmp_path)


def test_only_https_is_fetched() -> None:
    with pytest.raises(wk.ModelsMissing, match="non-HTTPS"):
        wk._https_get("http://example.com/x.onnx")


def test_the_chime_is_short_quiet_and_clickless() -> None:
    c = wk.chime()
    assert c.dtype == np.int16 and 0.1 < c.size / 24_000 < 0.25
    assert np.abs(c).max() < 0.25 * 32767 and abs(int(c[0])) < 200 and abs(int(c[-1])) < 200


# ───────────────────────────── the real model ─────────────────────────────


def _real_model() -> wk.OnnxWakeWord:
    pytest.importorskip("onnxruntime")
    if wk.missing("hey_jarvis", wk.default_model_dir()):
        pytest.skip("wake models not downloaded (python -m jarvis wake download)")
    if not shutil.which("espeak-ng"):
        pytest.skip("no espeak-ng to say the phrase")
    return wk.OnnxWakeWord("hey_jarvis")


def _say16k(text: str) -> np.ndarray:
    from jarvis.voice.engines import SystemEngine

    pcm = np.frombuffer(SystemEngine().synth(text, "en"), dtype="<i2").astype(np.float32)
    n = int(pcm.size * MIC_RATE / 24_000)
    return np.interp(np.linspace(0, pcm.size - 1, n), np.arange(pcm.size), pcm).astype(np.int16)


@pytest.mark.parametrize(
    ("text", "wakes"),
    [
        ("hey jarvis", True),
        ("hey jarvis, what's the weather", True),
        ("hello there, how are you", False),
        ("the jar is heavy", False),
        ("hey Travis", False),
    ],
)
def test_the_real_model_hears_its_name_and_nothing_else(text: str, wakes: bool) -> None:
    model = _real_model()
    best = cli._wake_score(model, _say16k(text).tobytes(), MIC_RATE)
    assert (best >= 0.5) is wakes, f"{text!r} scored {best:.3f}"


@pytest.mark.parametrize(
    ("text", "opens_a_turn"),
    [("hey jarvis, what's the weather in Ankara", True), ("what's the weather in Ankara", False)],
)
def test_end_to_end_through_the_real_graph(text: str, opens_a_turn: bool) -> None:
    """Speech in at the device, the wake thread's step, the uplink out: the whole chain."""
    model = _real_model()
    mix = PlaybackMixer(rate=MIC_RATE)
    bus = MicBus(rate=MIC_RATE, seconds=4.0)
    up = RecordingUplink()
    turn = TurnController(
        mixer=mix,
        vad=EnergyVad(frame_samples=VAD_FRAME, threshold_dbfs=-45.0),
        uplink=up,
        wake_window_s=20.0,
    )
    graph = AudioGraph(mixer=mix, micbus=bus, turn=turn, device_rate=MIC_RATE, block=320)
    watch = wk.WakeWatch(reader=graph.reader("wake"), model=model, turn=turn)
    silence = np.zeros(MIC_RATE * 2, dtype=np.int16)
    audio = np.concatenate((silence, _say16k(text), silence))
    for i in range(0, audio.size - 320 + 1, 320):
        graph.step(audio[i : i + 320])
        for chunk in watch.reader.drain(wk.CHUNK):
            watch.step(chunk)
    assert (up.starts >= 1) is opens_a_turn
    if opens_a_turn:
        assert watch.hits == 1 and up.samples > MIC_RATE // 2  # the request went up


# ───────────────────────────── the composition root ─────────────────────────────


def test_no_wake_word_means_no_watch(capsys: pytest.CaptureFixture[str]) -> None:
    cfg = Config(voice=Voice(wake_word=""))
    assert cli._desk_wake(cfg, None, None, None, None) is None
    assert "listening all the time" in capsys.readouterr().out


def test_a_wake_word_without_its_model_refuses_rather_than_listening_to_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    with pytest.raises(cli.StartupRefused, match="wake download") as err:
        cli._desk_wake(Config(), None, None, None, None)
    assert 'wake_word = ""' in str(err.value)
    with pytest.raises(cli.StartupRefused, match="no wake model called"):
        cli._desk_wake(Config(voice=Voice(wake_word="ok_computer")), None, None, None, None)


def test_the_desk_builds_and_starts_the_watch() -> None:
    """The caller, not the callee: a WakeWatch nobody starts is a deaf desk."""
    tree = ast.parse(Path(cli.__file__).read_text())
    fns = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}

    def calls(fn: ast.FunctionDef) -> set[str]:
        out = set()
        for n in ast.walk(fn):
            if isinstance(n, ast.Call):
                f = n.func
                out.add(f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", ""))
        return out

    assert "_desk_wake" in calls(fns["_build_desk"])
    turn_call = next(
        n
        for n in ast.walk(fns["_build_desk"])
        if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "TurnController"
    )
    assert "wake_window_s" in {k.arg for k in turn_call.keywords}
    assert {"start", "stop"} <= calls(fns["cmd_desk"])


def test_the_cli_downloads_and_tests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    files = fake_release()
    monkeypatch.setattr(wk, "MODELS", {n: wk._sha256(b) for n, b in files.items()})
    monkeypatch.setattr(wk, "_https_get", lambda url: files[url.rsplit("/", 1)[1]])
    assert cli.main(["--db", str(tmp_path / "d.db"), "wake", "download"]) == 0
    out = capsys.readouterr().out
    assert "hey_jarvis_v0.1.onnx" in out
    assert "CC BY-NC-SA" in out  # the licence is said where the model arrives
    monkeypatch.setattr(wk, "_https_get", lambda url: b"tampered")
    monkeypatch.setattr(wk, "MODELS", {n: "0" * 64 for n in files})
    assert cli.main(["--db", str(tmp_path / "d.db"), "wake", "download"]) == 1
    assert "refusing it" in capsys.readouterr().err


def test_no_model_file_is_ever_committed() -> None:
    """ADR 0012: the models are non-commercial, and the tree is MIT."""
    import subprocess

    root = Path(__file__).parent.parent
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    assert not [f for f in tracked if f.endswith((".onnx", ".tflite"))]
