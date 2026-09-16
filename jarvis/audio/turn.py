"""Barge-in: duck on suspicion, then confirm. And the self-speech veto.

THIS IS THE MOVE THE WHOLE DESIGN TURNS ON. The naive barge-in needs AEC good
enough to deliver clean ASR-grade audio during double-talk — 30+ dB of ERLE, at
the mercy of the room, the speaker's nonlinearity and the clock drift between two
USB devices. This one needs AEC good enough to let a VAD make a decision in the
presence of echo, which is 15-20 dB. That is the difference between a solved
problem and a room-dependent one, and it is bought with a 200 ms dip.

THE CHAIN, with the numbers from the architecture and not from the Live guide:

    3 consecutive 32 ms VAD frames while Jarvis is speaking   (~96 ms of evidence)
      -> DUCK PLAYBACK 20 dB over one 20 ms block             (~150 ms to the dip)
      -> 200 ms CONFIRM WINDOW, nothing sent, Gemini keeps generating
      -> still firing with the echo 20 dB down?  REAL:
             flush the mixer (the local drop, the only reliable interrupt),
             activity_start, flush the 320 ms pre-roll, stream live
      -> stopped?  ECHO: ramp back over 50 ms and discard.

The cost of a false positive is a barely audible dip, NOT a lost turn. That
asymmetry is why the duck is on suspicion and the commit is on confirmation, and
never the other way round.

CLIENT-DRIVEN TURNS, RULE 4. ``automatic_activity_detection.disabled=True`` on
both profiles and we send ``activity_start``/``activity_end`` ourselves. This is
the one decision that unifies desk and telephone: the telephony path already
demanded local VAD, because server VAD tuned for a clean mic false-triggers on
line noise, and the desk needs explicit control of when a barge-in counts.
Hangover before ``activity_end`` is 700 ms, not the 100 ms in the Live guide —
that figure is for SERVER VAD, and Google's manual-VAD guidance says at least
500 ms or audio quality degrades.

THE SELF-SPEECH VETO, which is not optional. ``output_audio_transcription`` is
already on, so a rolling 9-second window of Jarvis's OWN output transcript is
free. A detector hit whose phrase appears in that window is discarded for the
utterance plus one second. Without it, reading aloud a GitHub issue that contains
the kill phrase halts the system — and the issue that contains it will be the one
about the kill switch.
"""

from __future__ import annotations

import re
import time
import unicodedata
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

import numpy as np

from jarvis.audio import (
    CONFIRM_MS,
    DUCK_DB,
    DUCK_RAMP_MS,
    HANGOVER_MS,
    MIC_RATE,
    ONSET_FRAMES,
    PREROLL_MS,
    RESTORE_RAMP_MS,
    SELF_SPEECH_TAIL_S,
    SELF_SPEECH_WINDOW_S,
)
from jarvis.audio.dsp import Vad
from jarvis.audio.mixer import PlaybackMixer

__all__ = [
    "NullUplink",
    "RecordingUplink",
    "TurnController",
    "TurnEvent",
    "TurnState",
    "Uplink",
    "normalise_phrase",
]


class TurnState(StrEnum):
    IDLE = "idle"
    SUSPECT = "suspect"
    USER_SPEAKING = "user_speaking"


@dataclass(frozen=True)
class TurnEvent:
    kind: str
    at: float
    detail: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Uplink(Protocol):
    """The Gemini Live send side, reduced to the three calls a turn needs.

    No ``mute``. The uplink is GATED ON ACTIVITY, never muted, because the mic is
    never gated off — the difference is that gating decides what Gemini is told
    about, and muting decides what the machine can hear at all. Only the first is
    ours to choose.
    """

    def activity_start(self) -> None: ...

    def send(self, pcm: np.ndarray) -> None: ...

    def activity_end(self) -> None: ...


class NullUplink:
    def activity_start(self) -> None: ...

    def send(self, pcm: np.ndarray) -> None: ...

    def activity_end(self) -> None: ...


class RecordingUplink:
    """Everything a turn would have sent, kept in memory. The test double, and the
    thing ``tools/aec_bench.py`` counts false barge-ins with."""

    def __init__(self) -> None:
        self.starts = 0
        self.ends = 0
        self.chunks: list[np.ndarray] = []

    def activity_start(self) -> None:
        self.starts += 1

    def send(self, pcm: np.ndarray) -> None:
        self.chunks.append(pcm.copy())

    def activity_end(self) -> None:
        self.ends += 1

    @property
    def samples(self) -> int:
        return sum(c.shape[0] for c in self.chunks)

    def audio(self) -> np.ndarray:
        if not self.chunks:
            return np.zeros(0, dtype=np.int16)
        return np.concatenate(self.chunks)


_PUNCT = re.compile(r"[^\w\s]", flags=re.UNICODE)
_SPACE = re.compile(r"\s+")


def normalise_phrase(text: str) -> str:
    """NFKC, casefold, strip punctuation, collapse whitespace.

    Casefold rather than lower() because the user is Turkish and ``"I".lower()``
    is not ``"ı"`` — a dotted-I mismatch would make the veto silently miss every
    phrase containing an I, which is most of them.
    """
    folded = unicodedata.normalize("NFKC", text).casefold()
    return _SPACE.sub(" ", _PUNCT.sub(" ", folded)).strip()


class TurnController:
    """One conversation's turn state. Multi-instantiable; holds no globals."""

    def __init__(
        self,
        *,
        mixer: PlaybackMixer,
        vad: Vad,
        uplink: Uplink,
        on_event: Callable[[TurnEvent], None] | None = None,
        rate: int = MIC_RATE,
        confirm_ms: int = CONFIRM_MS,
        onset_frames: int = ONSET_FRAMES,
        hangover_ms: int = HANGOVER_MS,
        preroll_ms: int = PREROLL_MS,
        duck_db: float = DUCK_DB,
        self_speech_window_s: float = SELF_SPEECH_WINDOW_S,
        self_speech_tail_s: float = SELF_SPEECH_TAIL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._mixer = mixer
        self._vad = vad
        self._uplink = uplink
        self._on_event = on_event
        self.rate = rate
        self.confirm_ms = confirm_ms
        self.onset_frames = onset_frames
        self.hangover_ms = hangover_ms
        self.duck_db = duck_db
        self._clock = clock
        self._frame_ms = 1000 * vad.frame_samples / rate
        self._hangover_frames = max(1, round(hangover_ms / self._frame_ms))
        self._preroll: deque[np.ndarray] = deque(maxlen=max(1, round(preroll_ms / self._frame_ms)))
        self._state = TurnState.IDLE
        self._onset_run = 0
        self._silence_run = 0
        self._suspect_until = 0.0
        self._suspect_started_at = 0.0
        self._confirm_frames = 0
        self._speech_in_window = 0
        self._self_speech: deque[tuple[float, str]] = deque()
        self._self_speech_window_s = self_speech_window_s
        self._self_speech_tail_s = self_speech_tail_s
        self._veto_until = 0.0
        self._utterance_started_at = 0.0
        self.barge_ins = 0
        self.echo_rejections = 0

    # -- introspection --------------------------------------------------------

    @property
    def state(self) -> TurnState:
        return self._state

    @property
    def frame_samples(self) -> int:
        return self._vad.frame_samples

    @property
    def preroll_samples(self) -> int:
        return sum(f.shape[0] for f in self._preroll)

    @property
    def jarvis_speaking(self) -> bool:
        return self._mixer.is_playing

    # -- the chain ------------------------------------------------------------

    def feed(self, frame: np.ndarray, *, at: float | None = None) -> None:
        """One VAD frame. The only entry point; everything else is a consequence."""
        if frame.shape[0] != self._vad.frame_samples:
            raise ValueError(
                f"turn frames are {self._vad.frame_samples} samples, got {frame.shape[0]}"
            )
        now = self._clock() if at is None else at
        # The pre-roll is filled ALWAYS, including while Jarvis is speaking and
        # while we are still deciding whether this is echo. By the time a
        # barge-in is confirmed the first 300 ms of the word is already past, and
        # a ring that only fills once we are interested would be empty exactly
        # when it is needed.
        self._preroll.append(frame.copy())
        speech = self._vad.is_speech(frame)

        if self._state is TurnState.IDLE:
            self._feed_idle(speech, now)
        elif self._state is TurnState.SUSPECT:
            self._feed_suspect(speech, now)
        else:
            self._feed_speaking(speech, now)

    def _feed_idle(self, speech: bool, now: float) -> None:
        self._onset_run = self._onset_run + 1 if speech else 0
        if self._onset_run < self.onset_frames:
            return
        if not self.jarvis_speaking:
            self._commit(now, barge_in=False)
            return
        # Suspicion, not commitment: Gemini keeps generating and nothing is sent.
        self._mixer.duck(self.duck_db, ramp_ms=DUCK_RAMP_MS)
        self._state = TurnState.SUSPECT
        self._suspect_started_at = now
        self._suspect_until = now + self.confirm_ms / 1000.0
        self._confirm_frames = 0
        self._speech_in_window = 0
        self._emit("barge_in.suspected", now, {"confirm_ms": self.confirm_ms})
        if self.confirm_ms <= 0:
            # The phone leg. There is no local echo to confirm against, so
            # confirmation would only add latency to a decision already made.
            self._commit(now, barge_in=True)

    def _feed_suspect(self, speech: bool, now: float) -> None:
        self._confirm_frames += 1
        if speech:
            self._speech_in_window += 1
        if now < self._suspect_until:
            return
        # "Still firing" is evaluated on the frame AT the decision point, not
        # on a majority of the window: residual echo decays, so a window that
        # started loud and ended quiet is exactly the case being rejected.
        if speech:
            self._commit(now, barge_in=True)
        else:
            # Residual echo. Cost of being wrong here: one 200 ms dip nobody
            # mentions, versus a lost turn if we had committed.
            self._mixer.ramp_to(1.0, ramp_ms=RESTORE_RAMP_MS)
            self._state = TurnState.IDLE
            self._onset_run = 0
            self.echo_rejections += 1
            self._emit(
                "barge_in.echo",
                now,
                {
                    "window_ms": round((now - self._suspect_started_at) * 1000),
                    "speech_frames": self._speech_in_window,
                    "frames": self._confirm_frames,
                },
            )

    def _feed_speaking(self, speech: bool, now: float) -> None:
        if speech:
            self._silence_run = 0
            self._uplink.send(self._preroll[-1])
            return
        self._silence_run += 1
        self._uplink.send(self._preroll[-1])
        if self._silence_run >= self._hangover_frames:
            self._uplink.activity_end()
            self._state = TurnState.IDLE
            self._onset_run = 0
            self._silence_run = 0
            self._emit("activity_end", now, {"hangover_ms": self.hangover_ms})

    def _commit(self, now: float, *, barge_in: bool) -> None:
        dropped = 0
        if barge_in:
            # LOCAL DROP. The only 100%-reliable interrupt: AsyncSession exposes
            # no interrupt() at all, and the synthetic activity_start that
            # LiveKit's plugin relies on is unverified on this model. Never
            # depend on the server lever.
            dropped = self._mixer.flush()
            self._mixer.ramp_to(1.0, ramp_ms=RESTORE_RAMP_MS)
            self.barge_ins += 1
        preroll = list(self._preroll)
        self._preroll.clear()
        self._uplink.activity_start()
        for frame in preroll:
            self._uplink.send(frame)
        self._state = TurnState.USER_SPEAKING
        self._onset_run = 0
        self._silence_run = 0
        self._emit(
            "activity_start",
            now,
            {
                "barge_in": barge_in,
                "preroll_samples": sum(f.shape[0] for f in preroll),
                "dropped_samples": dropped,
                "latency_ms": round((now - self._suspect_started_at) * 1000)
                if barge_in and self._suspect_started_at
                else 0,
            },
        )

    def abort(self, reason: str, *, at: float | None = None) -> None:
        """The session dropped mid-turn. Close the turn honestly and reset.

        Not the same as an ``activity_end``: the uplink may be gone, so the end
        is best-effort and the state machine resets regardless. A controller that
        stayed in USER_SPEAKING after a reconnect would never open another turn.
        """
        now = self._clock() if at is None else at
        was = self._state
        if was is TurnState.USER_SPEAKING:
            try:
                self._uplink.activity_end()
            except Exception as exc:  # noqa: BLE001 - a dead socket must not wedge the graph
                self._emit("turn.end_failed", now, {"error": repr(exc)})
        if was is TurnState.SUSPECT:
            self._mixer.ramp_to(1.0, ramp_ms=RESTORE_RAMP_MS)
        self._state = TurnState.IDLE
        self._onset_run = 0
        self._silence_run = 0
        self._preroll.clear()
        self._vad.reset()
        self._emit("turn.aborted", now, {"reason": reason, "was": was.value})

    # -- the self-speech veto -------------------------------------------------

    def note_output_transcript(self, text: str, *, at: float | None = None) -> None:
        """Jarvis said this. Feed it every output-transcript fragment as it arrives."""
        now = self._clock() if at is None else at
        if now > self._veto_until:
            # A gap longer than the tail means the previous utterance finished,
            # so this fragment opens a new one.
            self._utterance_started_at = now
        norm = normalise_phrase(text)
        if norm:
            self._self_speech.append((now, norm))
        self._veto_until = now + self._self_speech_tail_s
        self._prune_self_speech(now)

    def _prune_self_speech(self, now: float) -> None:
        cutoff = now - self._self_speech_window_s
        if now <= self._veto_until:
            # "For the utterance plus one second": a read-aloud longer than the
            # window must not expire its own opening line while still speaking
            # it, or a 40-second issue body vetoes only its last nine seconds.
            cutoff = min(cutoff, self._utterance_started_at)
        while self._self_speech and self._self_speech[0][0] < cutoff:
            self._self_speech.popleft()

    def vetoed(self, phrase: str, *, at: float | None = None) -> bool:
        """Did Jarvis just say this himself?"""
        now = self._clock() if at is None else at
        self._prune_self_speech(now)
        norm = normalise_phrase(phrase)
        if not norm:
            return False
        return any(norm in said for _, said in self._self_speech)

    def detector_hit(self, phrase: str, *, at: float | None = None) -> bool:
        """A spotter fired. Returns True if the hit should be acted on.

        Every detector — wake word, kill phrase, briefing navigation — goes
        through here, because all three can be spoken by Jarvis reading somebody
        else's text aloud and all three have consequences.
        """
        now = self._clock() if at is None else at
        if self.vetoed(phrase, at=now):
            self._emit("detector.vetoed", now, {"phrase": phrase})
            return False
        self._emit("detector.hit", now, {"phrase": phrase})
        return True

    # -- events ---------------------------------------------------------------

    def _emit(self, kind: str, at: float, detail: dict[str, Any]) -> None:
        if self._on_event is not None:
            self._on_event(TurnEvent(kind=kind, at=at, detail=detail))
