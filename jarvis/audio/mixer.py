"""The ONE output stream: priority arbitration, a fader, and a fidelity refusal.

RULE 2 OF THE GRAPH LIVES HERE. Exactly one output stream exists, this object
owns it, and the post-fader post-mix array returned by :meth:`PlaybackMixer.pull`
is simultaneously what goes to the device and what goes to the echo canceller as
the far-end reference. That identity is the whole point: anything that plays
audio by another route is invisible to the AEC and WILL be heard as uncancellable
echo. The reference build could not have this property — its TTS engines each
synthesised AND played, and ``TTSPlayer.stop()`` called the global ``sd.stop()``,
which would have killed the Live session's output stream as collateral damage.

TWO INVARIANTS ARE ENFORCED HERE RATHER THAN DOCUMENTED.

  :meth:`claim_output` refuses a second claim. One mixer, one device stream, and
  the second thing that tries to open one gets a :class:`SecondOutputStream`
  naming who already holds it.

  :meth:`Track.write` REFUSES EXACT-tier content on a track that cannot carry it.
  The LIVE track is Gemini's; Gemini is a generative model; load-bearing text
  never passes through a generative model on its way to the user. That is the
  fidelity guarantee of the whole system, and a rule that lives in a docstring is
  a rule that gets violated by the third feature that needs to ship on a Friday.
  So it is a TypeError-shaped refusal at the only door the audio can come
  through, and the caller's options are to route it to the VERBATIM track or to
  fail loudly — never to silently paraphrase.

PRIORITY ARBITRATION, not summation. SYSTEM(40) > VERBATIM(30) > LIVE(20) all
play EXCLUSIVELY: while the reader is speaking option labels, Gemini's audio is
held, not mixed under it, because two voices over each other is exactly the
failure that makes verbatim reading pointless. MONITOR(10) is the only MIXED tier
and sits 12 dB down. Held audio is not lost immediately — it is subject to the
track's TTL, which is how a barge-in's late arrivals get dropped rather than
played two seconds after the user interrupted.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum
from typing import Literal

import numpy as np

from jarvis.audio import BUS_RATE, LIVE_TTL_S, MONITOR_DB
from jarvis.audio.dsp import Resampler, make_resampler

__all__ = [
    "AudioBus",
    "FidelityViolation",
    "OutputClaim",
    "PlaybackMixer",
    "Prio",
    "SecondOutputStream",
    "Tier",
    "Track",
    "UnknownTrack",
]

# The three fidelity tiers. EXACT is option labels, the ordinal-to-label binding,
# confirmed requirement items, the chosen-option read-back and the AI-disclosure
# line — answer keys and contract text, where one wrong word silently builds the
# wrong thing. FAITHFUL is question text and plan bodies. FREE is everything else.
Tier = Literal["exact", "faithful", "free"]


class Prio(IntEnum):
    MONITOR = 10
    LIVE = 20
    VERBATIM = 30
    SYSTEM = 40


class FidelityViolation(RuntimeError):
    """EXACT-tier audio was routed to a track that cannot promise the exact words."""


class SecondOutputStream(RuntimeError):
    """Something tried to open a second output stream. See rule 2."""


class UnknownTrack(KeyError):
    pass


@dataclass
class _Chunk:
    pcm: np.ndarray
    at: float
    tier: Tier


class Track:
    """One source's queue into the mixer. Never touches a device."""

    def __init__(
        self,
        name: str,
        prio: Prio,
        *,
        rate: int,
        content_rate: int,
        gain: float = 1.0,
        ttl_s: float | None = None,
        mixed: bool = False,
        carries_exact: bool = True,
    ) -> None:
        self.name = name
        self.prio = prio
        self.rate = rate
        self.content_rate = content_rate
        self.gain = gain
        self.ttl_s = ttl_s
        self.mixed = mixed
        self.carries_exact = carries_exact
        self._queue: deque[_Chunk] = deque()
        self._resampler: Resampler = make_resampler(content_rate, rate)
        self._dropped_stale = 0
        self._lock = threading.Lock()

    @property
    def resamples(self) -> bool:
        """False when the content rate already matches the mixer, which on a
        24 kHz bus fed by Kokoro, edge-tts, Gemini TTS and Gemini Live is every
        track — the rate collision is designed out, not managed."""
        return self.content_rate != self.rate

    def write(self, pcm: np.ndarray, *, tier: Tier = "free", at: float | None = None) -> None:
        if pcm.dtype != np.int16:
            raise TypeError(f"tracks carry int16, got {pcm.dtype}")
        if pcm.ndim != 1:
            raise ValueError(f"tracks are mono, got shape {pcm.shape}")
        if tier == "exact" and not self.carries_exact:
            raise FidelityViolation(
                f"EXACT-tier audio was routed to track {self.name!r} (prio "
                f"{self.prio.name}), which passes through a generative model. "
                "Route it to the VERBATIM track or refuse and defer — never "
                "paraphrase load-bearing text."
            )
        converted = self._resampler.process(pcm)
        if converted.size == 0:
            return
        with self._lock:
            self._queue.append(_Chunk(converted, time.monotonic() if at is None else at, tier))

    @property
    def pending(self) -> int:
        with self._lock:
            return sum(c.pcm.shape[0] for c in self._queue)

    @property
    def dropped_stale(self) -> int:
        return self._dropped_stale

    def flush(self) -> int:
        """Drop everything queued. Returns how many samples were discarded."""
        with self._lock:
            n = sum(c.pcm.shape[0] for c in self._queue)
            self._queue.clear()
        return n

    def fade_out(self, samples: int) -> None:
        """Keep a short fade and drop the rest — a flush with no click on the end."""
        with self._lock:
            kept: list[_Chunk] = []
            budget = samples
            for chunk in self._queue:
                if budget <= 0:
                    break
                take = min(budget, chunk.pcm.shape[0])
                kept.append(_Chunk(chunk.pcm[:take], chunk.at, chunk.tier))
                budget -= take
            total = sum(c.pcm.shape[0] for c in kept)
            if total:
                ramp = np.linspace(1.0, 0.0, total, endpoint=False)
                pos = 0
                for chunk in kept:
                    n = chunk.pcm.shape[0]
                    faded = chunk.pcm.astype(np.float32) * ramp[pos : pos + n]
                    chunk.pcm = faded.astype(np.int16)
                    pos += n
            self._queue.clear()
            self._queue.extend(kept)

    def prune(self, now: float) -> int:
        """Drop chunks older than the TTL. Returns how many samples went."""
        if self.ttl_s is None:
            return 0
        dropped = 0
        with self._lock:
            while self._queue and now - self._queue[0].at > self.ttl_s:
                dropped += self._queue.popleft().pcm.shape[0]
        self._dropped_stale += dropped
        return dropped

    def take(self, n: int) -> np.ndarray:
        """Pop up to ``n`` samples, zero-padded. Called by the mixer and nobody else:
        a track that could be drained from outside would be a second output path."""
        out = np.zeros(n, dtype=np.int16)
        filled = 0
        with self._lock:
            while filled < n and self._queue:
                chunk = self._queue[0]
                take = min(n - filled, chunk.pcm.shape[0])
                out[filled : filled + take] = chunk.pcm[:take]
                filled += take
                if take == chunk.pcm.shape[0]:
                    self._queue.popleft()
                else:
                    chunk.pcm = chunk.pcm[take:]
        return out


@dataclass
class OutputClaim:
    """Proof that the holder is THE output stream. Released explicitly or by context."""

    owner: str
    _mixer: PlaybackMixer

    def release(self) -> None:
        self._mixer._release_output(self)

    def __enter__(self) -> OutputClaim:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


class PlaybackMixer:
    """Owns every sample that leaves this process as sound.

    Multi-instantiable on purpose. v1 runs exactly one, but the phone leg is a
    second one in another process and the tests run several at once; a singleton
    here would have to be unpicked the week the phone lands, which is precisely
    the class of retrofit the seams exist to avoid.
    """

    def __init__(
        self,
        *,
        rate: int = BUS_RATE,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.rate = rate
        self._clock = clock
        self._tracks: dict[str, Track] = {}
        self._claim: OutputClaim | None = None
        self._fader = 1.0
        self._fader_target = 1.0
        self._fader_step = 0.0
        self._stopped = False
        self._last_pull: np.ndarray | None = None
        self._pull_seq = 0
        self._lock = threading.Lock()

    # -- tracks ---------------------------------------------------------------

    def track(
        self,
        name: str,
        prio: Prio,
        *,
        gain: float = 1.0,
        content_rate: int | None = None,
        ttl_s: float | None = None,
    ) -> Track:
        """Create a track. Defaults come from the priority, so the caller cannot
        accidentally create a LIVE track that accepts exact text."""
        if name in self._tracks:
            raise ValueError(f"track {name!r} already exists on this mixer")
        if prio is Prio.LIVE:
            carries_exact, mixed = False, False
            ttl_s = LIVE_TTL_S if ttl_s is None else ttl_s
        elif prio is Prio.MONITOR:
            carries_exact, mixed = False, True
            gain = gain * (10.0 ** (MONITOR_DB / 20.0))
        else:
            carries_exact, mixed = True, False
        tr = Track(
            name,
            prio,
            rate=self.rate,
            content_rate=self.rate if content_rate is None else content_rate,
            gain=gain,
            ttl_s=ttl_s,
            mixed=mixed,
            carries_exact=carries_exact,
        )
        self._tracks[name] = tr
        return tr

    def get(self, name: str) -> Track:
        try:
            return self._tracks[name]
        except KeyError as exc:
            raise UnknownTrack(name) from exc

    @property
    def tracks(self) -> dict[str, Track]:
        return dict(self._tracks)

    # -- the one output stream ------------------------------------------------

    def claim_output(self, owner: str) -> OutputClaim:
        """Take the single output-stream claim. RULE 2, enforced rather than greped."""
        with self._lock:
            if self._claim is not None:
                raise SecondOutputStream(
                    f"{owner!r} tried to open an output stream but {self._claim.owner!r} "
                    "already owns this mixer's. Exactly one output stream exists; "
                    "anything playing by another route is uncancellable echo."
                )
            self._claim = OutputClaim(owner, self)
            return self._claim

    def _release_output(self, claim: OutputClaim) -> None:
        with self._lock:
            if self._claim is claim:
                self._claim = None

    @property
    def output_owner(self) -> str | None:
        return self._claim.owner if self._claim is not None else None

    # -- the fader ------------------------------------------------------------

    def duck(self, db: float, *, ramp_ms: int = 20) -> None:
        """Attenuate PLAYBACK. Note what is absent: any effect on the microphone.

        Rule 1 of the graph is that the mic is never gated off, and the reason it
        holds is that this is the only lever barge-in has.
        """
        self.ramp_to(10.0 ** (db / 20.0), ramp_ms=ramp_ms)

    def ramp_to(self, gain: float, *, ramp_ms: int = 50) -> None:
        with self._lock:
            self._fader_target = max(0.0, gain)
            samples = max(1, int(self.rate * ramp_ms / 1000))
            self._fader_step = (self._fader_target - self._fader) / samples

    @property
    def fader(self) -> float:
        return self._fader

    @property
    def ducked(self) -> bool:
        return self._fader_target < 1.0

    # -- arbitration ----------------------------------------------------------

    def preempt_for(self, prio: Prio, *, fade_ms: int = 10) -> int:
        """Fade and drop every track below ``prio``. Returns samples discarded."""
        fade = max(1, int(self.rate * fade_ms / 1000))
        dropped = 0
        for tr in self._tracks.values():
            if tr.prio < prio and not tr.mixed:
                before = tr.pending
                tr.fade_out(fade)
                dropped += max(0, before - tr.pending)
        return dropped

    def flush(self, *, below: Prio | None = None) -> int:
        """Drop queued audio. This is the LOCAL DROP half of barge-in.

        There is no way to command the Gemini Live model to stop — ``AsyncSession``
        has no ``interrupt()`` — so the local drop is the only 100%-reliable lever
        and the synthetic ``activity_start`` is an optimisation that must never be
        depended on.
        """
        dropped = 0
        for tr in self._tracks.values():
            if below is None or tr.prio < below:
                dropped += tr.flush()
        return dropped

    def hard_stop(self) -> None:
        """Zero-fill the in-flight buffer and keep it zero. The kill switch's lever."""
        with self._lock:
            self._stopped = True
            self._fader = 0.0
            self._fader_target = 0.0
            self._fader_step = 0.0
        self.flush()

    def resume(self) -> None:
        with self._lock:
            self._stopped = False
            self._fader = 1.0
            self._fader_target = 1.0
            self._fader_step = 0.0

    @property
    def stopped(self) -> bool:
        return self._stopped

    @property
    def is_playing(self) -> bool:
        """Is Jarvis making a sound right now? The TurnController's speaking flag."""
        return any(tr.pending > 0 for tr in self._tracks.values() if not tr.mixed)

    def speaking_track(self) -> Track | None:
        candidates = [t for t in self._tracks.values() if not t.mixed and t.pending > 0]
        return max(candidates, key=lambda t: t.prio) if candidates else None

    # -- the pull -------------------------------------------------------------

    def pull(self, n: int, *, at: float | None = None) -> np.ndarray:
        """Produce the next ``n`` output samples.

        The returned array is THE output buffer. Hand this same object to the
        device and to the echo canceller; do not copy it, re-mix it or rebuild it
        from the tracks, because then the reference stops being what the speaker
        actually played and the AEC quietly stops working.
        """
        now = self._clock() if at is None else at
        acc = np.zeros(n, dtype=np.float32)
        if not self._stopped:
            for tr in self._tracks.values():
                tr.prune(now)
            # Exclusive, not summed: while the reader speaks option labels,
            # Gemini's audio is HELD. Two voices over each other is exactly the
            # failure that makes reading them verbatim pointless.
            exclusive = self.speaking_track()
            if exclusive is not None:
                acc += exclusive.take(n).astype(np.float32) * exclusive.gain
            for tr in self._tracks.values():
                if tr.mixed and tr.pending > 0:
                    acc += tr.take(n).astype(np.float32) * tr.gain
        out = self._apply_fader(acc, n)
        self._last_pull = out
        self._pull_seq += 1
        return out

    def _apply_fader(self, acc: np.ndarray, n: int) -> np.ndarray:
        """Per-sample gain, interpolated across the block.

        A gain step applied whole-block is a click, and a click on the far-end
        reference is broadband energy the AEC cannot model — the duck would
        itself cause the false barge-in it exists to reject.
        """
        with self._lock:
            start, step, target = self._fader, self._fader_step, self._fader_target
            if step == 0.0 or math.isclose(start, target, abs_tol=1e-9):
                self._fader = target
                self._fader_step = 0.0
                env: np.ndarray | None = None
            else:
                env = np.clip(
                    start + step * np.arange(1, n + 1, dtype=np.float32),
                    min(start, target),
                    max(start, target),
                )
                self._fader = float(env[-1])
                if math.isclose(self._fader, target, abs_tol=1e-6):
                    self._fader = target
                    self._fader_step = 0.0
            flat = self._fader
        faded = acc * (flat if env is None else env)
        return np.clip(faded, -32768, 32767).astype(np.int16)

    def last_pull(self) -> np.ndarray | None:
        """The literal array last handed out. The AEC far-end reference, by identity."""
        return self._last_pull

    @property
    def pull_seq(self) -> int:
        return self._pull_seq


# The architecture names this object twice, once per role it plays. Keep both
# spellings so neither half of the design reads like it is describing something
# that does not exist.
AudioBus = PlaybackMixer
