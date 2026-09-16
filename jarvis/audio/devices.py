"""Device selection that asserts one hardware clock, and says which failure it hit.

ONE PHYSICAL DEVICE, FULL DUPLEX, ASSERTED AT STARTUP. Aggregating a USB mic with
separate speakers works on most host APIs, and it is the configuration everyone
ends up in by accident. PortAudio does NO drift correction between two devices,
and a few parts per million between two crystals is the classic reason AEC
quietly stops working after two minutes — not at startup, where you would notice,
but later, once the two clocks have walked far enough apart that the far-end
reference no longer lines up with the echo it is supposed to cancel. The symptom
is "it worked yesterday". So :func:`select_duplex_device` refuses, out loud, with
both device names in the message.

THE DISTINCTION THE REFERENCE BUILD CONFLATES. Its ``resolve()`` returns ``None``
for both "no device was configured, use the system default" and "the device you
configured has been unplugged". Those need opposite responses — one is normal and
one should stop startup and tell the user which device is missing — so here they
are :class:`NoDefaultDevice` and :class:`DeviceVanished`, and a successful
selection carries ``is_system_default`` so the caller can say "using your default
input" rather than naming a device the user never chose.

IMPORTING SOUNDDEVICE IS A SIDE EFFECT. ``import sounddevice`` raises
``OSError("PortAudio library not found")`` on a machine with no sound card, which
is every CI runner and this one. So it is imported inside the methods that need a
device and nowhere else, and every probe is behind a :class:`DeviceProbe`
protocol that the tests satisfy with a list of dataclasses. That is not a testing
convenience bolted on afterwards — it is what lets the whole graph be exercised
with synthetic audio on a box that has never had a sound card.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from jarvis.audio import BLOCK, DEV_RATE

__all__ = [
    "AmbiguousDevice",
    "AudioStackMissing",
    "ClockSplit",
    "DeviceError",
    "DeviceInfo",
    "DeviceProbe",
    "DeviceSelection",
    "DeviceVanished",
    "NoDefaultDevice",
    "NotDuplex",
    "PortAudioProbe",
    "open_duplex_stream",
    "select_duplex_device",
    "stream_delay_ms",
]


class DeviceError(RuntimeError):
    """Base for every way device selection can fail. Each subclass is a different fix."""


class AudioStackMissing(DeviceError):
    """There is no PortAudio here. Not a misconfiguration — a missing audio stack."""


class NoDefaultDevice(DeviceError):
    """No device was named and the host has no default. Nothing to fall back to."""


class DeviceVanished(DeviceError):
    """A device was named and it is not here. Almost always an unplugged USB headset."""


class AmbiguousDevice(DeviceError):
    """The name matched more than one device. Refuse rather than pick."""


class NotDuplex(DeviceError):
    """The device cannot both capture and render. One clock is impossible from here."""


class ClockSplit(DeviceError):
    """Capture and render resolved to different devices. See the module docstring."""


@dataclass(frozen=True)
class DeviceInfo:
    index: int
    name: str
    hostapi: str
    max_input_channels: int
    max_output_channels: int
    default_samplerate: float

    @property
    def duplex(self) -> bool:
        return self.max_input_channels >= 1 and self.max_output_channels >= 1


@dataclass(frozen=True)
class DeviceSelection:
    """One device, good for both directions, with how we came to choose it."""

    index: int
    name: str
    hostapi: str
    samplerate: int
    is_system_default: bool

    def describe(self) -> str:
        how = "your system default" if self.is_system_default else "configured"
        return f"{self.name} ({self.hostapi}, {how}) at {self.samplerate} Hz"


@runtime_checkable
class DeviceProbe(Protocol):
    """Everything selection needs to know about the machine, and nothing else."""

    def devices(self) -> Sequence[DeviceInfo]: ...

    def defaults(self) -> tuple[int | None, int | None]: ...


class PortAudioProbe:
    """The real probe. Every method imports sounddevice; the class itself does not."""

    @staticmethod
    def _sd() -> Any:
        try:
            import sounddevice
        except (ImportError, OSError) as exc:
            # OSError, not ImportError: sounddevice imports fine and then fails
            # to dlopen PortAudio, which is a different sentence to say to a user.
            raise AudioStackMissing(
                f"no usable PortAudio here ({exc}). The graph still runs on a "
                "SyntheticLeg; only the desk leg needs a device."
            ) from exc
        return sounddevice

    def devices(self) -> Sequence[DeviceInfo]:
        sd = self._sd()
        apis = sd.query_hostapis()
        out: list[DeviceInfo] = []
        for i, d in enumerate(sd.query_devices()):
            out.append(
                DeviceInfo(
                    index=i,
                    name=str(d["name"]),
                    hostapi=str(apis[d["hostapi"]]["name"]),
                    max_input_channels=int(d["max_input_channels"]),
                    max_output_channels=int(d["max_output_channels"]),
                    default_samplerate=float(d["default_samplerate"]),
                )
            )
        return out

    def defaults(self) -> tuple[int | None, int | None]:
        sd = self._sd()
        raw = sd.default.device
        inp = raw[0] if isinstance(raw, (list, tuple)) else raw
        out = raw[1] if isinstance(raw, (list, tuple)) else raw
        return (
            None if inp is None or inp < 0 else int(inp),
            None if out is None or out < 0 else int(out),
        )


def select_duplex_device(
    probe: DeviceProbe, *, name: str | None = None, samplerate: int = DEV_RATE
) -> DeviceSelection:
    """Pick ONE device that captures and renders, or explain precisely why not.

    With ``name`` omitted this takes the host's default input and output and
    REQUIRES them to be the same device. That requirement is the whole function:
    on most desktops the default input is a webcam mic and the default output is
    the monitor, which are two crystals and therefore a slow AEC failure.
    """
    devices = probe.devices()
    if not devices:
        raise NoDefaultDevice("the host reports no audio devices at all")

    if name is None:
        in_idx, out_idx = probe.defaults()
        if in_idx is None or out_idx is None:
            raise NoDefaultDevice(
                "this host has no default input/output device; name one explicitly"
            )
        if in_idx != out_idx:
            a = _by_index(devices, in_idx)
            b = _by_index(devices, out_idx)
            raise ClockSplit(
                f"capture is {a.name!r} and render is {b.name!r} — two devices, two "
                "clocks. PortAudio does no drift correction between them, so the AEC "
                "converges and then silently degrades over a couple of minutes. Pick "
                "one duplex device (a USB headset is the recommended default)."
            )
        chosen = _by_index(devices, in_idx)
        default = True
    else:
        needle = name.casefold()
        matches = [d for d in devices if needle in d.name.casefold()]
        if not matches:
            raise DeviceVanished(
                f"no device matching {name!r}. Present: " + ", ".join(repr(d.name) for d in devices)
            )
        if len(matches) > 1:
            raise AmbiguousDevice(
                f"{name!r} matches {len(matches)} devices: "
                + ", ".join(repr(d.name) for d in matches)
            )
        chosen = matches[0]
        default = False

    if not chosen.duplex:
        raise NotDuplex(
            f"{chosen.name!r} has {chosen.max_input_channels} in / "
            f"{chosen.max_output_channels} out; the graph needs one device doing both"
        )
    return DeviceSelection(
        index=chosen.index,
        name=chosen.name,
        hostapi=chosen.hostapi,
        samplerate=samplerate,
        is_system_default=default,
    )


def _by_index(devices: Sequence[DeviceInfo], index: int) -> DeviceInfo:
    for d in devices:
        if d.index == index:
            return d
    raise DeviceVanished(f"device index {index} is no longer present")


def stream_delay_ms(input_adc_time: float, output_dac_time: float) -> int:
    """The AEC delay hint, which is free in a duplex callback.

    ``(outputBufferDacTime - inputBufferAdcTime) * 1000``. AEC3 has its own
    estimator, so this only buys convergence in ~0.5 s instead of ~3 s. It is a
    pure function so it can be tested without a stream, and because the one thing
    that could go wrong with it — a sign error — is invisible in a running system
    and catastrophic for convergence.
    """
    return int(round((output_dac_time - input_adc_time) * 1000.0))


def open_duplex_stream(
    selection: DeviceSelection,
    callback: Callable[..., None],
    *,
    block: int = BLOCK,
    channels: int = 1,
) -> Any:
    """Open THE duplex stream. The only place in the package that touches a device.

    Returns an unstarted ``sd.Stream``; the caller starts it, because a leg that
    opened and started in one call has no window in which to claim the mixer's
    single output and would turn rule 2 into a race.
    """
    sd = PortAudioProbe._sd()
    return sd.Stream(
        device=(selection.index, selection.index),
        samplerate=selection.samplerate,
        blocksize=block,
        dtype="int16",
        channels=channels,
        callback=callback,
        latency="low",
    )
