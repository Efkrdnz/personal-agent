""" "Hey Jarvis": the desk sleeps until it hears its name.

ASLEEP MEANS NOTHING LEAVES THE ROOM. While the desk is asleep the turn
controller opens no turn, so not one sample reaches Gemini; the microphone is
still read (rule 1 — it is never gated off), but only by this detector, locally.
Waking opens a conversation window, and every turn — the user's or Jarvis's —
holds it open; silence closes it again.

THE MODEL. openWakeWord's pretrained ``hey_jarvis``, run here directly on
onnxruntime rather than through the ``openwakeword`` package. The code of that
project is Apache-2.0; its pretrained MODELS are CC BY-NC-SA 4.0 — non-commercial
— which is why they are downloaded to the user's machine and never committed
(docs/adr/0012-wake-model-is-non-commercial.md). The package is the wrong
dependency for one phrase: importing it at all pulls in
scipy and scikit-learn (for a verifier this never trains), and on Linux its
metadata requires ``tflite-runtime``, which has no wheels past Python 3.11 —
the trap docs/architecture.md records. Three ONNX files and numpy are the whole
of what detection needs, so that is what this is:

    80 ms of 16 kHz int16 (1280 samples)
      -> melspectrogram.onnx over the last 1760 samples   -> 8 frames x 32 mels
         (scaled x/10 + 2, which is what the embedding model was trained on)
      -> embedding_model.onnx over the last 76 mel frames -> one 96-d embedding
      -> hey_jarvis.onnx over the last 16 embeddings      -> one score in [0, 1]

That arithmetic was checked against the models themselves, not the docs: 1760
samples in gives exactly 8 mel frames out, so each 80 ms chunk advances the
mel stream by exactly the 8 frames one embedding step consumes.

THE MODELS ARE DOWNLOADED, PINNED, AND NEVER IN THE TREE. ``python -m jarvis
wake download`` fetches them over HTTPS into ``$XDG_DATA_HOME/jarvis/wake`` and
refuses any file whose SHA-256 is not the one recorded below.

THE SELF-SPEECH VETO STILL APPLIES. A hit is offered to
:meth:`jarvis.audio.turn.TurnController.detector_hit` first, so Jarvis reading
"hey Jarvis" aloud from somebody's email does not wake him.
"""

from __future__ import annotations

import hashlib
import os
import threading
import urllib.request
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from jarvis.audio.micbus import BusClosed, MicReader

__all__ = [
    "CHUNK",
    "LICENCE",
    "MODELS",
    "PHRASES",
    "ModelsMissing",
    "OnnxWakeWord",
    "WakeDetector",
    "WakeModel",
    "WakeWatch",
    "chime",
    "default_model_dir",
    "download",
    "missing",
]

#: 80 ms at 16 kHz: the step every stage of the model advances by.
CHUNK = 1280
_MEL_INPUT = 1760  # CHUNK + 3 hops of 160: exactly 8 mel frames
_MEL_WINDOW = 76
_FEATURES = 16
_RELEASE = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/"

#: File -> SHA-256, measured from the v0.5.1 release. A file that does not
#: match is refused: a swapped model is a microphone listening for something else.
MODELS: dict[str, str] = {
    "melspectrogram.onnx": "ba2b0e0f8b7b875369a2c89cb13360ff53bac436f2895cced9f479fa65eb176f",
    "embedding_model.onnx": "70d164290c1d095d1d4ee149bc5e00543250a7316b59f31d056cff7bd3075c1f",
    "hey_jarvis_v0.1.onnx": "94a13cfe60075b132f6a472e7e462e8123ee70861bc3fb58434a73712ee0d2cb",
}
_SHARED = ("melspectrogram.onnx", "embedding_model.onnx")

#: Said wherever the models arrive. See docs/adr/0012-wake-model-is-non-commercial.md.
LICENCE = (
    "openWakeWord's pretrained models are CC BY-NC-SA 4.0: personal, non-commercial use. "
    "They are kept outside this repository; see docs/adr/0012."
)

#: Config name -> (model file, the phrase as the self-speech veto should match it).
PHRASES: dict[str, tuple[str, str]] = {"hey_jarvis": ("hey_jarvis_v0.1.onnx", "hey jarvis")}


class ModelsMissing(RuntimeError):
    """The wake models are not on disk. The message is the command that fixes it."""


def default_model_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "jarvis" / "wake"


def _files_for(phrase: str) -> tuple[str, ...]:
    if phrase not in PHRASES:
        raise ValueError(f"no wake model for {phrase!r}; have {', '.join(sorted(PHRASES))}")
    return (*_SHARED, PHRASES[phrase][0])


def missing(phrase: str, model_dir: Path) -> tuple[str, ...]:
    """Files for this phrase that are absent or do not match their pinned hash."""
    out = []
    for name in _files_for(phrase):
        path = model_dir / name
        if not path.is_file() or _sha256(path.read_bytes()) != MODELS[name]:
            out.append(name)
    return tuple(out)


def download(
    phrase: str,
    model_dir: Path,
    *,
    fetch: Callable[[str], bytes] | None = None,
) -> tuple[str, ...]:
    """Fetch, verify and atomically install the models for ``phrase``. Returns what was written.

    Every file is checked BEFORE it is put in place, and written to a temporary
    name then renamed, so a cut connection leaves the old file or none — never
    half of one that onnxruntime would load and score garbage with.
    """
    get = fetch or _https_get
    model_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name in missing(phrase, model_dir):
        data = get(_RELEASE + name)
        got = _sha256(data)
        if got != MODELS[name]:
            raise ModelsMissing(
                f"{name} downloaded with SHA-256 {got[:16]}…, expected {MODELS[name][:16]}…; "
                "refusing it. Nothing was installed."
            )
        tmp = model_dir / f".{name}.part"
        tmp.write_bytes(data)
        tmp.replace(model_dir / name)
        written.append(name)
    return tuple(written)


def _https_get(url: str) -> bytes:
    if not url.startswith("https://"):
        raise ModelsMissing(f"refusing a non-HTTPS model URL: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "jarvis-wake/1"})
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - https enforced above
        return resp.read()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ───────────────────────────── the model ─────────────────────────────


class WakeModel(Protocol):
    def score(self, chunk: np.ndarray) -> float:
        """One CHUNK of 16 kHz int16 in, a score in [0, 1] out."""
        ...

    def reset(self) -> None: ...


class OnnxWakeWord:
    """The three-stage openWakeWord pipeline, streaming, on onnxruntime alone."""

    def __init__(self, phrase: str = "hey_jarvis", model_dir: Path | None = None) -> None:
        directory = model_dir or default_model_dir()
        gone = missing(phrase, directory)
        if gone:
            raise ModelsMissing(
                f"wake model files missing or wrong in {directory}: {', '.join(gone)}. "
                "Run `python -m jarvis wake download`."
            )
        try:
            import onnxruntime as ort
        except ImportError as exc:
            raise ModelsMissing("onnxruntime is not installed: pip install -e '.[wake]'") from exc
        opts = ort.SessionOptions()
        # One thread each: this runs beside a real-time audio callback, and a
        # wake model that grabs every core is a dropout in the speaker.
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1

        def session(name: str) -> Any:
            return ort.InferenceSession(
                str(directory / name), sess_options=opts, providers=["CPUExecutionProvider"]
            )

        self.phrase = phrase
        self._mel = session("melspectrogram.onnx")
        self._embed = session("embedding_model.onnx")
        self._word = session(PHRASES[phrase][0])
        self._word_input = self._word.get_inputs()[0].name
        self.reset()

    def reset(self) -> None:
        self._raw = np.zeros(0, dtype=np.int16)
        # Ones, as the reference pipeline starts: the first embeddings see a
        # little padding, and no score is reported until _FEATURES real ones
        # exist, by which time the padding has scrolled out of the window.
        self._mels = np.ones((_MEL_WINDOW, 32), dtype=np.float32)
        self._features: deque[np.ndarray] = deque(maxlen=_FEATURES)

    def score(self, chunk: np.ndarray) -> float:
        if chunk.dtype != np.int16 or chunk.shape != (CHUNK,):
            got = f"{chunk.dtype}{chunk.shape}"
            raise ValueError(f"wake chunks are {CHUNK} int16 samples, got {got}")
        self._raw = np.concatenate((self._raw, chunk))[-_MEL_INPUT:]
        if self._raw.shape[0] < _MEL_INPUT:
            return 0.0
        mel = self._mel.run(None, {"input": self._raw[None].astype(np.float32)})[0]
        mel = np.squeeze(mel) / 10.0 + 2.0
        self._mels = np.vstack((self._mels, mel))[-_MEL_WINDOW:]
        window = self._mels[None, :, :, None].astype(np.float32)
        self._features.append(self._embed.run(None, {"input_1": window})[0].reshape(-1))
        if len(self._features) < _FEATURES:
            return 0.0
        feats = np.stack(self._features)[None].astype(np.float32)
        return float(np.squeeze(self._word.run(None, {self._word_input: feats})[0]))


# ───────────────────────────── deciding ─────────────────────────────


@dataclass
class WakeDetector:
    """Scores in, hits out. Pure, so the policy is testable without a model.

    ``patience`` consecutive chunks at or above ``threshold`` make a hit; then
    nothing fires for ``refractory_s``, because one "hey Jarvis" scores high for
    several chunks in a row and must wake the desk once, not five times.
    """

    threshold: float = 0.5
    patience: int = 1
    refractory_s: float = 2.0
    _run: int = 0
    _quiet_until: float = -1.0

    def feed(self, score: float, at: float) -> bool:
        if score >= self.threshold:
            self._run += 1
        else:
            self._run = 0
        if self._run >= self.patience and at >= self._quiet_until:
            self._run = 0
            self._quiet_until = at + self.refractory_s
            return True
        return False


class _Turn(Protocol):
    def detector_hit(self, phrase: str, *, at: float | None = None) -> bool: ...

    def wake(self, at: float) -> None: ...

    def awake(self, at: float) -> bool: ...


@dataclass
class WakeWatch:
    """The detector's thread: read the mic tap, score it, wake the turn controller.

    Its own thread and its own MicBus cursor (rule 3), so a slow model can lose
    wake audio but can never make the VAD miss an onset. Time is the reader's
    own sample count, the same audio clock the graph stamps turns with.
    """

    reader: MicReader
    model: WakeModel
    turn: _Turn
    phrase: str = "hey jarvis"
    detector: WakeDetector = field(default_factory=WakeDetector)
    #: Called from THIS thread with (at, score) after the desk is woken.
    on_wake: Callable[[float, float], None] | None = None
    #: Called from this thread when the conversation window closes.
    on_sleep: Callable[[float], None] | None = None
    rate: int = 16_000
    hits: int = 0
    vetoed: int = 0
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _thread: threading.Thread | None = field(default=None, repr=False)
    _was_awake: bool = field(default=False, repr=False)

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("wake watch already started")
        self._thread = threading.Thread(target=self.run, name="wake-watch", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                chunk = self.reader.read(CHUNK, timeout=0.25)
            except BusClosed:
                return
            if chunk is None:
                continue
            self.step(chunk)

    def step(self, chunk: np.ndarray) -> bool:
        """One chunk. Returns True if it woke the desk. Public so tests need no thread."""
        at = self.reader.cursor / self.rate
        awake = self.turn.awake(at)
        if self._was_awake and not awake and self.on_sleep is not None:
            self.on_sleep(at)
        self._was_awake = awake
        score = self.model.score(chunk)
        if not self.detector.feed(score, at):
            return False
        if awake:
            return False  # already listening; the name mid-conversation is just a word
        if not self.turn.detector_hit(self.phrase, at=at):
            self.vetoed += 1
            return False
        self.turn.wake(at)
        self._was_awake = True
        self.hits += 1
        if self.on_wake is not None:
            self.on_wake(at, score)
        return True


def chime(rate: int = 24_000) -> np.ndarray:
    """A soft rising two-note blip, 160 ms: "I'm listening", without words."""
    out = []
    for freq, ms in ((660.0, 70), (880.0, 90)):
        n = int(rate * ms / 1000)
        t = np.arange(n) / rate
        env = np.sin(np.pi * np.arange(n) / n)  # no click at either end
        out.append(0.18 * env * np.sin(2 * np.pi * freq * t))
    return (np.concatenate(out) * 32767).astype(np.int16)
