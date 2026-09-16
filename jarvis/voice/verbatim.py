"""The reader: a ladder of engines, a cache in front, and an honest refusal.

This is the object that makes "read me the options" a promise rather than a
hope. It never paraphrases, because nothing in its path can: the engines are
G2P-plus-acoustic-model or a fixed cloud voice, the cache is content-addressed
on the exact string, and the only thing that varies between two readings of
"Postgres" is which engine was reachable.

THE LADDER IS THE AVAILABILITY STORY. A local engine that does not speak Turkish
declines; a cloud engine whose endpoint is down fails; the next one is tried.
What must never happen is the interesting part: when the ladder is exhausted
this raises :class:`NoVerbatimEngine` and the caller REFUSES. It does not fall
back to the conversational voice, which would produce audio that sounds right
and may have translated, reordered or dropped an answer key. A silent downgrade
to paraphrase is the precise bug this whole subsystem exists to prevent, so it
is made structurally impossible rather than discouraged: there is no code path
from here to the Live track at all.

AN EARCON BRACKETS THE EPISODE. 150 ms, generated locally, costs nothing,
survives a phone codec, and marks the boundary in any recording. Within two days
the user has learned something useful — when the other voice speaks, those are
somebody else's exact words — which makes drift NOTICEABLE rather than merely
absent.

Everything is 24 kHz mono PCM16 and no resampler exists on this path. Engines
return bytes; only :class:`~jarvis.voice.router.PcmSink` — the audio bus — ever
touches a device.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

from jarvis.voice.cache import PcmCache
from jarvis.voice.engines import (
    RATE,
    Engine,
    EngineUnavailable,
    tone,
    validate_pcm,
)
from jarvis.voice.router import EarconMark, Fidelity, NoReader, PcmSink, Utterance

__all__ = [
    "EARCON_HZ",
    "EARCON_MS",
    "EngineAttempt",
    "NoVerbatimEngine",
    "VerbatimSpeaker",
    "duration_s",
    "earcon_pcm",
]

EARCON_MS = 150.0
EARCON_HZ = 880.0


def earcon_pcm(ms: float = EARCON_MS, hz: float = EARCON_HZ) -> bytes:
    """The bracket tone. Pure function of its arguments, so tests can predict it."""
    return tone(ms, hz=hz)


@dataclass(frozen=True, slots=True)
class EngineAttempt:
    """Why one rung of the ladder did not produce audio."""

    engine: str
    reason: str

    def __str__(self) -> str:
        return f"{self.engine}: {self.reason}"


class NoVerbatimEngine(RuntimeError):
    """Every engine declined or failed. The caller must REFUSE, never paraphrase.

    Carries the whole ladder because "the reader is broken" is the least useful
    possible incident report: what a person needs at 2am is which engine said
    what, in order.
    """

    def __init__(self, text: str, lang: str, attempts: Sequence[EngineAttempt]) -> None:
        self.text = text
        self.lang = lang
        self.attempts = tuple(attempts)
        detail = "; ".join(str(a) for a in self.attempts) or "no engines configured"
        super().__init__(f"no verbatim engine could say {text!r} in {lang!r} ({detail})")


@dataclass
class VerbatimSpeaker:
    """Text → bytes → a sink. One per leg; no singleton, no module state.

    ``sink`` is optional because synthesis and playback are separate concerns:
    the Telegram channel renders the same clips to a file and the phone leg
    writes them to a call track, so a speaker with no sink is a perfectly good
    pre-synthesiser.
    """

    engines: tuple[Engine, ...] = ()
    cache: PcmCache | None = None
    sink: PcmSink | None = None
    earcon_ms: float = EARCON_MS
    earcon_hz: float = EARCON_HZ
    on_engine_error: Callable[[str, str, BaseException], None] | None = None
    _earcon: bytes | None = field(default=None, repr=False, compare=False)

    # ───────────────────────────── synthesis ─────────────────────────────

    async def pcm_for(self, text: str, lang: str = "en", *, exact: bool = False) -> bytes:
        """Walk the ladder for one clip. Raises :class:`NoVerbatimEngine` at the end.

        ``exact=True`` additionally bars any engine that is not deterministic
        and not verified. A prompt-steerable model may read a plan body — a
        dropped adjective there is cosmetic — but it may not read an answer key
        until ``tools/fidelity_probe.py`` says it earned that, and "may" is
        spelled as a field on the engine rather than a comment in a runbook.
        """
        if not text.strip():
            raise ValueError("refusing to synthesise empty text")
        attempts: list[EngineAttempt] = []
        for engine in self.engines:
            name = engine.name
            if exact and not engine.deterministic and not engine.verified:
                attempts.append(EngineAttempt(name, "not deterministic and not verified"))
                continue
            try:
                voice = engine.voice_for(lang)
            except EngineUnavailable as exc:
                attempts.append(EngineAttempt(name, str(exc)))
                continue
            key = None
            if self.cache is not None:
                key = self.cache.key(engine=name, voice=voice, lang=lang, text=text)
                hit = self.cache.get(key)
                if hit is not None:
                    return hit
            try:
                # A thread, because an engine is synchronous by contract and may
                # block on a socket or an ONNX graph for a second. Blocking the
                # loop here would freeze the audio callbacks that are draining
                # the clip BEFORE this one.
                raw = await asyncio.to_thread(engine.synth, text, lang)
                pcm = validate_pcm(name, raw)
            except Exception as exc:  # noqa: BLE001 - any failure means "next rung"
                attempts.append(EngineAttempt(name, f"{type(exc).__name__}: {exc}"))
                if self.on_engine_error is not None:
                    self.on_engine_error(name, text, exc)
                continue
            if self.cache is not None and key is not None:
                self.cache.put(key, pcm)
            return pcm
        raise NoVerbatimEngine(text, lang, attempts)

    async def prefetch(self, texts: Iterable[str], lang: str = "en", *, exact: bool = False) -> int:
        """Synthesise a whole question at once, and swallow what fails.

        Fired the instant ``AskUserQuestion`` lands, while the conversational
        voice is still saying its framing sentence, so every clip is on disk
        before the user has heard "Claude Code has a question". Failures are
        deliberately not raised: a prefetch is an optimisation, and the decision
        to refuse belongs at the moment of speaking, where the caller knows what
        it is in the middle of.
        """
        wanted = list(dict.fromkeys(t for t in texts if t.strip()))
        if not wanted:
            return 0
        results = await asyncio.gather(
            *(self.pcm_for(t, lang, exact=exact) for t in wanted),
            return_exceptions=True,
        )
        return sum(1 for r in results if isinstance(r, bytes))

    def cached(self, text: str, lang: str = "en", *, exact: bool = False) -> bool:
        """Is this clip already on disk for the first engine that would serve it?"""
        if self.cache is None:
            return False
        for engine in self.engines:
            if exact and not engine.deterministic and not engine.verified:
                continue
            try:
                voice = engine.voice_for(lang)
            except EngineUnavailable:
                continue
            key = self.cache.key(engine=engine.name, voice=voice, lang=lang, text=text)
            return self.cache.get(key) is not None
        return False

    # ───────────────────────────── playback ─────────────────────────────

    def earcon(self) -> bytes:
        if self._earcon is None:
            self._earcon = earcon_pcm(self.earcon_ms, self.earcon_hz)
        return self._earcon

    async def say(
        self,
        sink: PcmSink,
        text: str,
        lang: str = "en",
        *,
        exact: bool = False,
        earcon: EarconMark = "none",
        tier: Fidelity | None = None,
    ) -> int:
        """Synthesise and write. Returns bytes written, earcons included.

        ``tier`` is handed on to the sink so the audio layer can enforce the
        same rule on the samples that this layer enforces on the text. It
        defaults from ``exact`` rather than being a second thing to remember.
        """
        pcm = await self.pcm_for(text, lang, exact=exact)
        content: Fidelity = tier if tier is not None else ("exact" if exact else "faithful")
        written = 0
        if earcon in ("open", "both"):
            # The tone carries no words, so it is free-tier on any track.
            written += await self._write(sink, self.earcon(), "free")
        written += await self._write(sink, pcm, content)
        if earcon in ("close", "both"):
            written += await self._write(sink, self.earcon(), "free")
        return written

    async def speak(self, utt: Utterance) -> int:
        """The :class:`~jarvis.voice.router.VerbatimSink` adapter.

        The router hands over whole utterances, so the tier travels with the
        text and ``exact`` is never a parameter somebody forgot to pass.
        """
        if self.sink is None:
            raise NoReader("this VerbatimSpeaker has no sink bound")
        return await self.say(
            self.sink,
            utt.text,
            utt.lang,
            exact=utt.fidelity == "exact",
            earcon=utt.earcon,
            tier=utt.fidelity,
        )

    @staticmethod
    async def _write(sink: PcmSink, pcm: bytes, tier: Fidelity) -> int:
        await sink.write(pcm, tier=tier)
        return len(pcm)


def duration_s(pcm: bytes) -> float:
    """Seconds of audio in a clip, at the one rate this system has."""
    return len(pcm) / (RATE * 2)
