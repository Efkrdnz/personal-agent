"""The verbatim speech path: the second, deterministic voice.

LOAD-BEARING TEXT NEVER PASSES THROUGH A GENERATIVE MODEL ON ITS WAY TO THE
USER. Gemini Live has no verbatim path — everything it says is a paraphrase of
an injected fake user turn — so option labels, confirmed requirements and the
disclosure line are read by an engine that structurally cannot rewrite them,
and everything in this package exists to keep that true under failure.

Layout:

``router``
    :class:`~jarvis.voice.router.Utterance` and
    :class:`~jarvis.voice.router.OutputRouter`. Nothing calls ``speak()``;
    everything enqueues an utterance carrying its fidelity tier, and the router
    picks the voice. Exact text aimed at the Gemini track raises.
``engines``
    ``synth(text, lang) -> bytes``. No engine may touch a device.
``cache``
    Content-addressed PCM on disk, so a repeated label is a file read.
``verbatim``
    The engine ladder, the earcon, and :class:`NoVerbatimEngine` — the refusal.
``chunk``
    One clip per option, plus a streaming sentence splitter for narration.

Nothing here imports an audio device, a model client or numpy at module scope,
so the whole package imports and its tests run on a machine with no sound card
and no API key.
"""

from __future__ import annotations

from jarvis.voice.cache import PcmCache, cache_key, default_cache_dir
from jarvis.voice.chunk import (
    SentenceSplitter,
    disclosure_clip,
    narration_clips,
    option_clips,
    presynthesise,
    readback_clips,
    sentences,
)
from jarvis.voice.engines import (
    RATE,
    EdgeEngine,
    Engine,
    EngineFailed,
    EngineUnavailable,
    FakeEngine,
    KokoroEngine,
)
from jarvis.voice.router import (
    LIVE_TRACK,
    VERBATIM_TRACK,
    Fidelity,
    FidelityViolation,
    NoReader,
    OutputRouter,
    Refused,
    Spoken,
    Track,
    TrackSink,
    Utterance,
    assert_routable,
    publish_said,
    track_for,
)
from jarvis.voice.verbatim import NoVerbatimEngine, VerbatimSpeaker, earcon_pcm

__all__ = [
    "LIVE_TRACK",
    "RATE",
    "VERBATIM_TRACK",
    "EdgeEngine",
    "Engine",
    "EngineFailed",
    "EngineUnavailable",
    "FakeEngine",
    "Fidelity",
    "FidelityViolation",
    "KokoroEngine",
    "NoReader",
    "NoVerbatimEngine",
    "OutputRouter",
    "PcmCache",
    "Refused",
    "SentenceSplitter",
    "Spoken",
    "Track",
    "TrackSink",
    "Utterance",
    "VerbatimSpeaker",
    "assert_routable",
    "cache_key",
    "default_cache_dir",
    "disclosure_clip",
    "earcon_pcm",
    "narration_clips",
    "option_clips",
    "presynthesise",
    "publish_said",
    "readback_clips",
    "sentences",
    "track_for",
]
