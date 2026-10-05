"""The Silero VAD model: where it comes from, where it is found, and what the desk does without it.

THE MODEL IS PINNED. ``silero_vad.onnx`` from the v6.2.3 tag, 2,327,524 bytes,
by SHA-256. A file that does not match is refused wherever it is found: a
swapped model is a microphone deciding by somebody else's rule what counts as
the user speaking.

IT IS MIT, SO IT SHIPS INSIDE THE EXE, unlike openWakeWord's non-commercial
models (ADR 0012). The Windows workflow downloads it with :func:`download`,
the same verified path the app uses, into ``packaging/models/`` (gitignored:
no ``.onnx`` is ever committed); ``packaging/jarvis.spec`` refuses to build
without it and places it at :data:`BUNDLE_DIR`, where a frozen app looks
first. A pip install fetches it on first use into the user's data directory,
beside the wake models.

WITHOUT IT THE DESK STILL WORKS. A missing VAD model costs quality, not
privacy, so :func:`choose` falls back to :class:`VoicedEnergyVad` with a
warning rather than refusing to start. That is the opposite of the wake word's
rule, and deliberately so: a missing wake model would mean sending everything.
"""

from __future__ import annotations

import hashlib
import sys
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from jarvis.audio import (
    BARGE_PREROLL_MS,
    IDLE_ONSET,
    IDLE_ONSET_FALLBACK,
    IDLE_PREROLL_MS,
    MAX_TURN_S,
    PREROLL_MS,
)
from jarvis.audio.dsp import EnergyVad, SileroVad, Vad, VadUnavailable, VoicedEnergyVad
from jarvis.audio.wake import default_model_dir

__all__ = [
    "BUNDLE_DIR",
    "LICENCE",
    "MODEL",
    "MODES",
    "SHA256",
    "SIZE",
    "URL",
    "VERSION",
    "VadChoice",
    "VadModelMissing",
    "bundled_dirs",
    "choose",
    "diagnose",
    "download",
    "find",
    "load",
    "search_dirs",
    "user_dir",
    "verified",
]

MODEL = "silero_vad.onnx"
VERSION = "6.2.3"
SHA256 = "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3"
SIZE = 2_327_524
#: A tag, not a branch: the bytes at this URL cannot change under the pin.
URL = (
    f"https://raw.githubusercontent.com/snakers4/silero-vad/v{VERSION}/src/silero_vad/data/{MODEL}"
)

#: Inside a frozen app, relative to ``sys._MEIPASS``. The spec puts it here.
BUNDLE_DIR = "jarvis_models/vad"

LICENCE = f"Silero VAD v{VERSION}: MIT License, Copyright (c) 2020-present Silero Team."

#: ``auto``: Silero, else the numpy fallback. ``basic``: the fallback only.
#: ``energy``: loudness alone with three frames in a row, the rule every breath
#: got through; kept as the way back if the new rule ever proves deaf to a voice.
MODES = ("auto", "basic", "energy")

#: Long enough for 2.3 MB on a slow line, short enough that a desk with no
#: network starts on the fallback instead of hanging at "loading".
_TIMEOUT_S = 30.0


class VadModelMissing(VadUnavailable):
    """No verified model could be found or fetched. The message says why."""


def user_dir() -> Path:
    """Beside the wake models, so one folder holds everything that was downloaded."""
    return default_model_dir().parent / "vad"


def bundled_dirs(*, meipass: str | None = None, root: Path | None = None) -> tuple[Path, ...]:
    """Where a shipped copy can be: inside the frozen app, or in a checkout's packaging/models.

    The checkout path is what lets the Windows CI's test step, which runs from
    source after the workflow fetched the model, exercise the real thing. It is
    only trusted when ``pyproject.toml`` says it really is a checkout, because
    in an installed site-packages ``packaging/`` is somebody else's package.
    """
    out = []
    base = getattr(sys, "_MEIPASS", None) if meipass is None else meipass
    if base:
        out.append(Path(base) / BUNDLE_DIR)
    checkout = Path(__file__).resolve().parents[2] if root is None else root
    if (checkout / "pyproject.toml").is_file():
        out.append(checkout / "packaging" / "models")
    return tuple(out)


def search_dirs(model_dir: Path | None = None) -> tuple[Path, ...]:
    """The lookup order: what shipped with the app first, then what was downloaded."""
    return (*bundled_dirs(), model_dir or user_dir())


def verified(path: Path) -> bool:
    """True when ``path`` is exactly the pinned model. Size first: it is free."""
    try:
        return (
            path.is_file() and path.stat().st_size == SIZE and _sha256(path.read_bytes()) == SHA256
        )
    except OSError:
        return False


def find(dirs: Sequence[Path] | None = None) -> Path | None:
    """The first verified copy in ``dirs`` (default :func:`search_dirs`), or None."""
    for d in search_dirs() if dirs is None else dirs:
        if verified(d / MODEL):
            return d / MODEL
    return None


def download(model_dir: Path | None = None, *, fetch: Callable[[str], bytes] | None = None) -> Path:
    """Fetch, verify and atomically install the model. Returns its path.

    Checked BEFORE it is put in place and written under a temporary name, so a
    cut connection leaves the old file or none, never half of one.
    """
    where = model_dir or user_dir()
    target = where / MODEL
    if verified(target):
        return target
    data = (fetch or _https_get)(URL)
    got = _sha256(data)
    if got != SHA256:
        raise VadModelMissing(
            f"{MODEL} downloaded with SHA-256 {got[:16]}…, expected {SHA256[:16]}…; "
            "refusing it. Nothing was installed."
        )
    where.mkdir(parents=True, exist_ok=True)
    tmp = where / f".{MODEL}.part"
    tmp.write_bytes(data)
    tmp.replace(target)
    return target


def _https_get(url: str) -> bytes:
    if not url.startswith("https://"):
        raise VadModelMissing(f"refusing a non-HTTPS model URL: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "jarvis-vad/1"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:  # noqa: S310 - https enforced above
        return resp.read()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load(path: Path | None = None) -> SileroVad:
    """A Silero VAD that has proved it can hear. Raises :class:`VadUnavailable` with a reason."""
    where = path or find()
    if where is None:
        raise VadModelMissing(
            f"{MODEL} is not in {', '.join(str(d) for d in search_dirs())}; "
            "the desk downloads it when it starts with a network connection"
        )
    if not verified(where):
        raise VadModelMissing(f"{where} is not the pinned {MODEL} v{VERSION}; refusing it")
    return SileroVad(where)


# ───────────────────────────── the desk's choice ─────────────────────────────


@dataclass(frozen=True)
class VadChoice:
    """Everything the turn controller needs from the VAD decision, and what to tell the user."""

    vad: Vad
    name: str
    idle_onset: tuple[int, int] | None
    idle_preroll_ms: int
    barge_preroll_ms: int
    max_turn_s: float | None
    #: One line for the terminal and the log: what is listening, with its numbers.
    detail: str
    #: Set when this is not the detector the user should have, with the fix.
    warning: str | None = None


def choose(
    *,
    mode: str = "auto",
    model_dir: Path | None = None,
    dirs: Sequence[Path] | None = None,
    fetch: Callable[[str], bytes] | None = None,
    allow_download: bool = True,
) -> VadChoice:
    """Pick the desk's VAD BEFORE the session starts. Never raises for a missing model.

    Run before the audio stream opens, because the download (pip installs, first
    run only) must not happen in the callback, and the model's own self-check
    is its warm-up. Anything that stops Silero (no file, no network, a tampered
    download, a model that cannot hear a vowel) ends in the fallback and a
    warning naming why, because a desk that cannot start is worse than one
    that hears a little less well.
    """
    if mode not in MODES:
        raise ValueError(f"voice activity mode must be one of {', '.join(MODES)}, not {mode!r}")
    if mode == "energy":
        return VadChoice(
            vad=EnergyVad(),
            name="energy",
            idle_onset=None,
            idle_preroll_ms=PREROLL_MS,
            barge_preroll_ms=PREROLL_MS,
            max_turn_s=MAX_TURN_S,
            detail="loudness only, three frames in a row: breaths and clicks will start turns",
            warning="voice activity is set to loudness alone; breaths will start turns",
        )
    why = "the basic detector was asked for"
    if mode == "auto":
        try:
            vad = SileroVad(_get_model(model_dir, dirs, fetch, allow_download))
        except Exception as exc:  # noqa: BLE001 - every way of not getting Silero ends the same
            why = str(exc) or type(exc).__name__
        else:
            heard = vad.hearing or vad.self_check()
            return VadChoice(
                vad=vad,
                name="silero",
                idle_onset=IDLE_ONSET,
                idle_preroll_ms=IDLE_PREROLL_MS,
                barge_preroll_ms=BARGE_PREROLL_MS,
                max_turn_s=MAX_TURN_S,
                detail=(
                    f"Silero v{VERSION}: {heard.describe()}; a turn needs "
                    f"{IDLE_ONSET[0]} of {IDLE_ONSET[1]} speech frames"
                ),
            )
    return VadChoice(
        vad=VoicedEnergyVad(),
        name="voiced",
        idle_onset=IDLE_ONSET_FALLBACK,
        idle_preroll_ms=IDLE_PREROLL_MS,
        barge_preroll_ms=BARGE_PREROLL_MS,
        max_turn_s=MAX_TURN_S,
        detail=(
            f"loud and voiced (no Silero model); a turn needs "
            f"{IDLE_ONSET_FALLBACK[0]} of {IDLE_ONSET_FALLBACK[1]} speech frames"
        ),
        warning=(
            f"no Silero voice detector ({why}). Using the basic one: breaths are ignored, "
            "but a hum or a TV may still start a turn. The model is fetched the next time "
            "the desk starts with a network connection."
        )
        if mode == "auto"
        else None,
    )


def _get_model(
    model_dir: Path | None,
    dirs: Sequence[Path] | None,
    fetch: Callable[[str], bytes] | None,
    allow_download: bool,
) -> Path:
    found = find(search_dirs(model_dir) if dirs is None else dirs)
    if found is not None:
        return found
    if not allow_download:
        raise VadModelMissing(f"{MODEL} is not downloaded")
    return download(model_dir, fetch=fetch)


def diagnose(dirs: Sequence[Path] | None = None) -> tuple[bool, str]:
    """For ``doctor``: (healthy, one sentence). Never downloads, never raises."""
    path = find(dirs)
    if path is None:
        return False, (
            f"Silero voice detector not downloaded yet: the desk fetches it when it starts "
            f"with a network connection, and until then filters breaths with the basic "
            f"detector. Looked in {', '.join(str(d) for d in (dirs or search_dirs()))}"
        )
    try:
        heard = SileroVad(path, check=False).self_check()
    except Exception as exc:  # noqa: BLE001 - doctor reports, it does not crash
        return False, f"Silero voice detector at {path} would not load: {exc}"
    if not heard.ok:
        return False, f"Silero voice detector at {path} is deaf: it {heard.describe()}"
    return True, f"Silero v{VERSION} {heard.describe()} ({path})"


# ───────────────────────────── for the build ─────────────────────────────


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m jarvis.audio.vadmodel download DIR``: what the Windows workflow runs.

    The same download and the same pin as the app, so CI cannot bundle a file
    the app itself would refuse, and then the same self-check the app runs.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or args[0] != "download":
        print("usage: python -m jarvis.audio.vadmodel download DIR", file=sys.stderr)
        return 2
    try:
        path = download(Path(args[1]))
        heard = SileroVad(path).self_check()
    except Exception as exc:  # noqa: BLE001 - the build log needs the sentence, not a traceback
        print(f"could not get {MODEL}: {exc}", file=sys.stderr)
        return 1
    print(f"{path} ({SIZE} bytes, sha256 {SHA256})")
    print(heard.describe())
    print(LICENCE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
