"""Three legs, one graph. The phone leg is three boxes swapped, and that is the test.

A LEG IS A DSP FRONT END AND AN ARMED DETECTOR SET. Nothing more. That claim is
falsifiable — stage 6 must touch zero files in ``jarvis/cc/`` and zero lines of
``jarvis/requests.py`` — and this file is where it is either true or a lie, so
each leg is deliberately small enough that you can see there is nowhere for a
second code path to hide.

  DeskLeg       48 kHz duplex on ONE physical device, AEC3 in the callback,
                soxr down to 16 kHz, confirm_ms=200, everything armed.
  PhoneLeg      16 kHz in from a track, NO AEC (the carrier and the handset
                already cancel, and a second canceller on a line that already
                has one makes things worse), NS level 2 and AGC on because PSTN
                levels vary by 30 dB, confirm_ms=0 because there is no local
                echo to confirm against.
  SyntheticLeg  in-memory frames in, played blocks out, no device and no clock.

THE SYNTHETIC LEG IS NOT A TEST FIXTURE THAT ESCAPED INTO THE PACKAGE. It is how
CI runs at all on a machine where ``import sounddevice`` raises
``OSError("PortAudio library not found")``, it is how ``tools/aec_bench.py``
replays a recording through the real duck-confirm chain, and it is how a
barge-in that is actually echo gets tested — it can inject the far-end signal
back into the near path at a chosen gain and delay, which is precisely what a
room does and precisely what the confirm window exists to reject.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Sequence
from typing import Any, Protocol, runtime_checkable

import numpy as np

from jarvis.audio import BLOCK, CONFIRM_MS, DEV_RATE, MIC_RATE
from jarvis.audio.devices import (
    DeviceProbe,
    DeviceSelection,
    PortAudioProbe,
    open_duplex_stream,
    select_duplex_device,
    stream_delay_ms,
)
from jarvis.audio.dsp import (
    AecUnavailable,
    EchoCanceller,
    NoiseSuppressor,
    NullEchoCanceller,
    NullNoiseSuppressor,
    WebRtcEchoCanceller,
)
from jarvis.audio.graph import AudioGraph

__all__ = [
    "DESK_DETECTORS",
    "PHONE_DETECTORS",
    "DeskLeg",
    "Leg",
    "ListSink",
    "PhoneLeg",
    "Sink",
    "Source",
    "SyntheticLeg",
    "frames_from",
]

# Which detectors a leg arms. The desk hears everything; the phone has no wake
# word (the call IS the wake) and no local kill spotter worth the name, because
# DTMF *9 is language-free and more reliable than any spoken phrase — which
# matters more than usual here, since sherpa-onnx ships no Turkish KWS model and
# the spoken phrases are therefore English-only.
DESK_DETECTORS = frozenset({"wake", "kill", "nav", "vad", "uplink"})
PHONE_DETECTORS = frozenset({"nav", "vad", "uplink"})


@runtime_checkable
class Source(Protocol):
    rate: int
    block: int

    def read(self) -> np.ndarray | None: ...


@runtime_checkable
class Sink(Protocol):
    rate: int

    def write(self, pcm: np.ndarray) -> None: ...


@runtime_checkable
class Leg(Protocol):
    """A leg supplies its front end and says how the turn should be tuned."""

    name: str
    device_rate: int
    block: int
    has_hardware_echo_control: bool
    confirm_ms: int
    armed: frozenset[str]

    def make_aec(self) -> EchoCanceller: ...

    def make_ns(self) -> NoiseSuppressor: ...


class SyntheticLeg:
    """A leg made of arrays. No device, no network, no clock, no API key.

    ``echo_gain`` and ``echo_delay_blocks`` are what make it interesting: with
    them the near signal contains a delayed, attenuated copy of whatever the
    mixer actually played, which is the one thing a passthrough canceller cannot
    remove and therefore the exact input the confirm window must reject.
    """

    has_hardware_echo_control = False
    armed = DESK_DETECTORS

    def __init__(
        self,
        frames: Iterable[np.ndarray] | None = None,
        *,
        name: str = "synthetic",
        rate: int = MIC_RATE,
        block: int = 320,
        confirm_ms: int = CONFIRM_MS,
        echo_gain: float = 0.0,
        echo_delay_blocks: int = 1,
        aec: EchoCanceller | None = None,
    ) -> None:
        self.name = name
        self.device_rate = rate
        self.block = block
        self.confirm_ms = confirm_ms
        self.echo_gain = echo_gain
        # At least one block: a zero-block echo would be the mixer's CURRENT
        # output arriving before it has been pulled, which no room does and no
        # canceller has to handle.
        self.echo_delay_blocks = max(1, echo_delay_blocks)
        self._aec = aec
        self._frames: list[np.ndarray] = [np.asarray(f, dtype=np.int16) for f in (frames or [])]
        self.played: list[np.ndarray] = []
        self._delay_line: list[np.ndarray] = []

    def make_aec(self) -> EchoCanceller:
        return self._aec if self._aec is not None else NullEchoCanceller(self.device_rate)

    def make_ns(self) -> NoiseSuppressor:
        return NullNoiseSuppressor(level=0)

    # -- driving the graph ----------------------------------------------------

    def feed(self, frames: Iterable[np.ndarray]) -> None:
        self._frames.extend(np.asarray(f, dtype=np.int16) for f in frames)

    def silence(self, blocks: int) -> None:
        self._frames.extend(np.zeros(self.block, dtype=np.int16) for _ in range(blocks))

    def run(self, graph: AudioGraph, *, blocks: int | None = None) -> list[np.ndarray]:
        """Push frames through the graph until they run out (or ``blocks`` of them).

        Returns what the mixer actually played, block by block, so a test can
        assert on the duck depth, the flush and the ramp back rather than on the
        internal state that produced them.
        """
        out: list[np.ndarray] = []
        for near in self._pending(blocks):
            out.append(self.step(graph, near))
        return out

    def step(self, graph: AudioGraph, near: np.ndarray) -> np.ndarray:
        played = graph.step(self._with_echo(near))
        self.played.append(played.copy())
        # Trimmed to exactly the delay, so _delay_line[0] is always the block
        # played `echo_delay_blocks` steps ago.
        self._delay_line.append(played.copy())
        while len(self._delay_line) > self.echo_delay_blocks:
            self._delay_line.pop(0)
        return played

    def _pending(self, blocks: int | None) -> Iterator[np.ndarray]:
        take = len(self._frames) if blocks is None else min(blocks, len(self._frames))
        head, self._frames = self._frames[:take], self._frames[take:]
        yield from head

    def _with_echo(self, near: np.ndarray) -> np.ndarray:
        if self.echo_gain <= 0.0 or len(self._delay_line) < self.echo_delay_blocks:
            return near
        echo = self._delay_line[0]
        n = min(near.shape[0], echo.shape[0])
        mixed = near.astype(np.float32).copy()
        mixed[:n] += echo[:n].astype(np.float32) * self.echo_gain
        return np.clip(mixed, -32768, 32767).astype(np.int16)


class DeskLeg:
    """48 kHz duplex, one physical device, AEC3 in the callback.

    ``has_hardware_echo_control`` is False, which is what tells the
    TurnController it must use duck-confirm. If that ever becomes True — an OS
    AEC via ``libpipewire-module-echo-cancel``, or a hardware speakerphone — the
    confirm window can drop to zero and nothing else changes.
    """

    device_rate = DEV_RATE
    block = BLOCK
    has_hardware_echo_control = False
    armed = DESK_DETECTORS

    def __init__(
        self,
        *,
        name: str = "desk",
        device_name: str | None = None,
        probe: DeviceProbe | None = None,
        confirm_ms: int = CONFIRM_MS,
        require_aec: bool = False,
    ) -> None:
        self.name = name
        self.device_name = device_name
        self.confirm_ms = confirm_ms
        self.require_aec = require_aec
        self._probe = probe if probe is not None else PortAudioProbe()
        self._selection: DeviceSelection | None = None
        self._stream: Any = None
        self._claim: Any = None
        self.aec_degraded = False

    def select(self) -> DeviceSelection:
        """Resolve the device. Separate from :meth:`open` so a startup self-test
        can say 'using your default input' — or refuse — before anything is opened."""
        self._selection = select_duplex_device(
            self._probe, name=self.device_name, samplerate=self.device_rate
        )
        return self._selection

    def make_aec(self) -> EchoCanceller:
        try:
            return WebRtcEchoCanceller(self.device_rate)
        except AecUnavailable:
            if self.require_aec:
                raise
            # Degrading is allowed and SAID OUT LOUD, because what is lost is
            # open-speaker barge-in and nothing else: the kill switch keeps the
            # hotkey and DTMF, navigation keeps push-to-talk, and the wake word
            # is unaffected since nothing is playing while Jarvis is asleep.
            self.aec_degraded = True
            return NullEchoCanceller(self.device_rate)

    def make_ns(self) -> NoiseSuppressor:
        # Level 1, AGC off: AEC+NS+AGC is tuned for a human listener and distorts
        # speech in ways that hurt keyword spotting, and AGC pumping on an open
        # desk mic causes more trouble than it fixes.
        return NullNoiseSuppressor(level=1)

    def open(self, graph: AudioGraph) -> Any:
        """Open THE duplex stream, claim the mixer's single output, and START it.

        The claim is taken BEFORE the stream exists, so a second leg on the same
        mixer fails here rather than after PortAudio has grabbed the device.

        STARTING IS THIS METHOD'S JOB AND IT SHIPPED WITHOUT IT.
        :func:`~jarvis.audio.devices.open_duplex_stream` returns an UNSTARTED
        stream on purpose — that gap is the window in which the mixer claim
        happens — and for one release nothing in the tree ever closed it. Every
        unit test drove ``graph.step`` directly, so the whole graph was green
        while the only thing that calls it in production, the PortAudio
        callback, was never armed: ``python -m jarvis desk`` printed
        "listening.", opened a Gemini socket, and sat deaf and mute in both
        directions, because ``graph.step`` is also the only caller of
        ``mixer.pull``. Nothing raises, nothing logs, and the failure is
        SILENCE — which is what an idle assistant sounds like anyway.
        """
        selection = self._selection or self.select()
        self._claim = graph.mixer.claim_output(self.name)

        def callback(indata: Any, outdata: Any, frames: int, tinfo: Any, status: Any) -> None:
            del frames, status
            hint = stream_delay_ms(tinfo.inputBufferAdcTime, tinfo.outputBufferDacTime)
            if hint >= 0:
                graph.aec.stream_delay_ms = hint
            played = graph.step(np.asarray(indata, dtype=np.int16).reshape(-1))
            outdata[:, 0] = played

        self._stream = open_duplex_stream(selection, callback, block=self.block)
        try:
            self._stream.start()
        except Exception:
            # A started stream owns the device; a half-open one that failed to
            # start would hold the mixer claim forever and make the next attempt
            # look like "something else is already using the speaker".
            self.close()
            raise
        return self._stream

    def close(self) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None
        if self._claim is not None:
            self._claim.release()
            self._claim = None


class PhoneLeg:
    """16 kHz in from a call track, out to a call track. No AEC at all.

    ``confirm_ms = 0``. The confirm window exists to reject our own echo; on a
    call there is none to reject, so waiting 200 ms would be pure added latency
    on the one leg that can least afford it.
    """

    device_rate = MIC_RATE
    has_hardware_echo_control = True
    armed = PHONE_DETECTORS

    def __init__(
        self,
        source: Callable[[], np.ndarray | None],
        sink: Sink | None = None,
        *,
        name: str = "phone",
        block: int = 320,
    ) -> None:
        self.name = name
        self.block = block
        self.confirm_ms = 0
        self._source = source
        self._sink = sink

    def make_aec(self) -> EchoCanceller:
        # Deliberately none. The carrier and the handset already cancel; a second
        # AEC on a line that already has one makes things worse, and there is no
        # local speaker for it to reference anyway.
        return NullEchoCanceller(self.device_rate)

    def make_ns(self) -> NoiseSuppressor:
        # Level 2 with AGC on, because PSTN levels vary by 30 dB.
        return NullNoiseSuppressor(level=2)

    def run(self, graph: AudioGraph, *, blocks: int | None = None) -> int:
        """Pump the call track through the same graph the desk uses."""
        done = 0
        while blocks is None or done < blocks:
            near = self._source()
            if near is None:
                break
            played = graph.step(np.asarray(near, dtype=np.int16).reshape(-1))
            if self._sink is not None:
                self._sink.write(played)
            done += 1
        return done


class ListSink:
    """A sink that keeps what it was given. The phone leg's test double, and the
    shape the real one has: 24 kHz in, mu-law out, resampled locally — never
    tagged ``rate=8000`` for the server to upsample."""

    def __init__(self, rate: int = MIC_RATE) -> None:
        self.rate = rate
        self.blocks: list[np.ndarray] = []

    def write(self, pcm: np.ndarray) -> None:
        self.blocks.append(np.asarray(pcm, dtype=np.int16).copy())

    def audio(self) -> np.ndarray:
        if not self.blocks:
            return np.zeros(0, dtype=np.int16)
        return np.concatenate(self.blocks)


def frames_from(pcm: np.ndarray, block: int) -> Sequence[np.ndarray]:
    """Cut a signal into whole blocks, dropping a short tail.

    The tail is dropped rather than zero-padded: a padded final block is silence
    the graph never actually captured, and it lands in the VAD's hangover count.
    """
    n = (pcm.shape[0] // block) * block
    return [pcm[i : i + block] for i in range(0, n, block)]
