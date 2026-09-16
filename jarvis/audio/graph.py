"""The one code path. Everything above ``clean16`` is this file.

RULE 5. A leg differs from another leg in three things and no more: its DSP front
end, its device rate, and which detectors are armed. The desk runs 48 kHz duplex
with AEC3 and soxr down to 16 kHz; the phone runs 16 kHz straight in with no AEC;
the synthetic leg runs whatever a test hands it. All three call :meth:`AudioGraph.step`
and all three get the same MicBus, the same TurnController, the same pre-roll, the
same self-speech veto and the same events. That is the claim the phone stage will
falsify or confirm, and it is cheap to make true now and a rewrite to retrofit.

THE ORDER IS NOT NEGOTIABLE, because two of the steps are the same array::

    far  = mixer.pull(BLOCK)        <- post-fader, post-mix
    out  = far                      -> the device.       SAME OBJECT.
    clean = aec.process(near, far)  -> the canceller.    SAME OBJECT.
    clean16 = resample(clean)
    micbus.write(clean16)
    detectors read micbus with independent cursors

Pull first, because the reference must be the block being played DURING this
capture, not the previous one. Hand the same object to both, because a reference
that was copied before the fade, or rebuilt from the tracks, or mixed a second
time, is not what the speaker played — and an AEC given a reference that is nearly
right converges on nearly-cancelling, which is indistinguishable from working
until the room gets loud. :meth:`reference_is_output` is that identity, asserted.

TIME IS COUNTED IN SAMPLES, NOT READ FROM A CLOCK. The graph derives ``at`` from
how many samples it has processed, and passes it to the mixer's TTL and the turn
controller's confirm window. So a test can push 40 blocks through and assert on a
200 ms confirm window without sleeping, and — more usefully — the desk leg's
timings stop depending on how long the callback was descheduled for.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from jarvis.audio import BLOCK, DEV_RATE
from jarvis.audio.dsp import EchoCanceller, NullEchoCanceller, Resampler, make_resampler
from jarvis.audio.micbus import MicBus, MicReader
from jarvis.audio.mixer import PlaybackMixer
from jarvis.audio.turn import TurnController, TurnEvent

__all__ = [
    "AudioEvent",
    "AudioGraph",
    "EventSink",
    "QueuedEventSink",
    "ReferenceDesync",
]


class ReferenceDesync(RuntimeError):
    """The AEC reference was not the array handed to the device. See rule 2."""


@dataclass(frozen=True)
class AudioEvent:
    kind: str
    at: float
    detail: dict[str, Any] = field(default_factory=dict)


EventSink = Callable[[AudioEvent], None]


class QueuedEventSink:
    """Buffer events in the audio thread; publish them from a worker with a connection.

    THE BUS IS NOT CALLABLE FROM THE CALLBACK. ``jarvis.bus.publish`` opens a
    write transaction on SQLite, and a BEGIN IMMEDIATE that blocks on another
    writer inside a 20 ms audio callback is a dropout you can hear. So the graph
    appends to a bounded deque and a worker thread drains it. Dropping the oldest
    event under pressure is the right failure: an event log with a hole and a
    count of what it lost is honest, and a stalled audio thread is not.
    """

    def __init__(self, *, limit: int = 4096) -> None:
        self._events: list[AudioEvent] = []
        self._limit = limit
        self._dropped = 0
        self._lock = threading.Lock()

    def __call__(self, event: AudioEvent) -> None:
        with self._lock:
            if len(self._events) >= self._limit:
                self._events.pop(0)
                self._dropped += 1
            self._events.append(event)

    @property
    def dropped(self) -> int:
        return self._dropped

    def pending(self) -> list[AudioEvent]:
        with self._lock:
            return list(self._events)

    def drain(self, con: sqlite3.Connection, actor: str) -> int:
        """Publish everything buffered. Takes an open connection, per house rule 3."""
        from jarvis.bus import publish

        with self._lock:
            batch, self._events = self._events, []
        for ev in batch:
            publish(con, f"audio.{ev.kind}", actor, {"at": ev.at, **ev.detail})
        return len(batch)


class AudioGraph:
    """One leg's signal path. Multi-instantiable; owns no globals and no device."""

    def __init__(
        self,
        *,
        mixer: PlaybackMixer,
        micbus: MicBus,
        turn: TurnController,
        aec: EchoCanceller | None = None,
        device_rate: int = DEV_RATE,
        block: int = BLOCK,
        on_event: EventSink | None = None,
    ) -> None:
        if mixer.rate != device_rate:
            # The graph pulls `block` samples from the mixer and hands that same
            # array to the device, so the two rates ARE the same rate. A 24 kHz
            # mixer on a 48 kHz leg plays at half speed and, worse, hands the
            # AEC a far-end reference that is not what the speaker emitted —
            # which the canceller cannot detect and the user hears as echo that
            # "sometimes" comes back. The defaults collide on purpose-built
            # constants (BUS_RATE 24000, DEV_RATE 48000), so this is the pairing
            # a caller falls into by accident.
            raise ValueError(
                f"mixer runs at {mixer.rate} Hz and the device at {device_rate} Hz. "
                "The array the mixer produces IS the device buffer and IS the AEC "
                "reference, so build the mixer at the leg's rate and let its tracks "
                "resample their 24 kHz content on write."
            )
        self.mixer = mixer
        self.micbus = micbus
        self.turn = turn
        self.device_rate = device_rate
        self.block = block
        self.aec: EchoCanceller = aec if aec is not None else NullEchoCanceller(device_rate)
        self._to_mic: Resampler = make_resampler(device_rate, micbus.rate)
        self._on_event = on_event
        self._vad_reader = micbus.reader("vad")
        self._samples = 0
        self._blocks = 0
        self._last_far: np.ndarray | None = None
        self._readers: dict[str, MicReader] = {"vad": self._vad_reader}
        self._vad_dropped_seen = 0

    # -- readers --------------------------------------------------------------

    def reader(self, name: str) -> MicReader:
        """Attach a detector. Wake word, kill spotter and nav spotter each get one.

        They are independent by construction: a spotter that blocks on a model
        load cannot make the VAD miss an onset, because the only thing they share
        is a ring the writer never waits on.
        """
        if name in self._readers:
            raise ValueError(f"reader {name!r} already attached to this graph")
        r = self.micbus.reader(name)
        self._readers[name] = r
        return r

    @property
    def readers(self) -> dict[str, MicReader]:
        return dict(self._readers)

    # -- clocks ---------------------------------------------------------------

    @property
    def elapsed(self) -> float:
        """Seconds of audio processed. The graph's only notion of 'now'."""
        return self._samples / self.device_rate

    @property
    def blocks(self) -> int:
        return self._blocks

    # -- the path -------------------------------------------------------------

    def step(self, near: np.ndarray) -> np.ndarray:
        """One block in, one block out. The array returned goes to the device."""
        if near.dtype != np.int16:
            raise TypeError(f"the near path carries int16, got {near.dtype}")
        if near.ndim != 1:
            raise ValueError(f"the near path is mono, got shape {near.shape}")
        n = near.shape[0]
        at = self.elapsed

        far = self.mixer.pull(n, at=at)
        self._last_far = far
        clean = self.aec.process(near, far)
        clean16 = self._to_mic.process(clean)
        if clean16.size:
            self.micbus.write(clean16)

        self._samples += n
        self._blocks += 1
        self._pump_turn(at)
        return far

    def _pump_turn(self, at: float) -> None:
        frame_n = self.turn.frame_samples
        frames = self._vad_reader.drain(frame_n)
        if self._vad_reader.dropped > self._vad_dropped_seen:
            # Emitted on the CHANGE, not on the condition: `lagging` never resets,
            # and an event per block thereafter would bury the moment it started.
            # A VAD that lost audio may have missed an onset, and "barge-in felt
            # unreliable last Tuesday" is unanswerable without this line.
            lost = self._vad_reader.dropped - self._vad_dropped_seen
            self._vad_dropped_seen = self._vad_reader.dropped
            self._emit(
                "mic.lagged",
                at,
                {
                    "reader": "vad",
                    "dropped": lost,
                    "dropped_total": self._vad_reader.dropped,
                },
            )
        step = frame_n / self.micbus.rate
        # Frames drained in one block are contiguous in time; stamp them so the
        # confirm window measures audio, not scheduler jitter.
        base = at - (len(frames) - 1) * step if frames else at
        for i, frame in enumerate(frames):
            self.turn.feed(frame, at=base + i * step)

    # -- the invariant --------------------------------------------------------

    def reference_is_output(self) -> bool:
        """Was the AEC's far-end reference the literal array handed to the device?

        Identity, not equality: two arrays with the same contents would pass an
        equality check and still mean somebody rebuilt the reference, which is
        the bug this is here to catch.
        """
        return self._last_far is not None and self._last_far is self.mixer.last_pull()

    def assert_reference_is_output(self) -> None:
        if not self.reference_is_output():
            raise ReferenceDesync(
                "the AEC far-end reference is not the array the mixer handed out. "
                "Something is playing audio by another route, and it will be heard "
                "as uncancellable echo."
            )

    # -- events ---------------------------------------------------------------

    def on_turn_event(self, event: TurnEvent) -> None:
        """Adapter so a TurnController can be wired straight into the graph's sink."""
        self._emit(event.kind, event.at, event.detail)

    def _emit(self, kind: str, at: float, detail: dict[str, Any]) -> None:
        if self._on_event is not None:
            self._on_event(AudioEvent(kind=kind, at=at, detail=detail))
