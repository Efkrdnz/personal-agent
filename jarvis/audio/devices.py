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

ONE DEVICE IS NOT ALWAYS ONE INDEX. Windows (and macOS, for built-in audio) lists
each direction of a device as its own endpoint: a USB headset is "Microphone
(Jabra Evolve2 40)" at one index and "Headphones (Jabra Evolve2 40)" at another,
in every host API. Requiring one index refused every Windows machine, so a pair
is accepted when :func:`same_hardware` says both ends are the same physical
device — same host API, same name in the parentheses Windows puts it in. Two
different devices are still refused, which is the case the clock rule exists for.

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
    "hardware_name",
    "open_duplex_stream",
    "same_hardware",
    "select_duplex_device",
    "stream_delay_ms",
    "usable_devices",
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
    """One physical device, good for both directions, with how we came to choose it.

    ``index`` is the capture end. ``output_index`` is set when the same device
    renders through a different endpoint (Windows always, macOS built-ins);
    None means one index does both.
    """

    index: int
    name: str
    hostapi: str
    samplerate: int
    is_system_default: bool
    output_index: int | None = None
    output_name: str | None = None

    @property
    def pair(self) -> tuple[int, int]:
        """``(input, output)`` as PortAudio's ``device=`` takes it."""
        return (self.index, self.index if self.output_index is None else self.output_index)

    def describe(self) -> str:
        how = "your system default" if self.is_system_default else "configured"
        ends = self.name if self.output_name is None else f"{self.name} + {self.output_name}"
        return f"{ends} ({self.hostapi}, {how}) at {self.samplerate} Hz"


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
        # sounddevice returns an _InputOutputPair here: indexable, NOT a tuple.
        # An isinstance check for list/tuple treated the whole pair as one
        # index and crashed `doctor` on the first real machine it met.
        try:
            inp, out = raw[0], raw[1]
        except (TypeError, IndexError, KeyError):
            inp = out = raw
        return _index_or_none(inp), _index_or_none(out)

    def hostapi_defaults(self) -> dict[str, tuple[int | None, int | None]]:
        """Each host API's own default input and output.

        Needed on Windows, where the system defaults are usually MME's "Microsoft
        Sound Mapper" aliases, which name no hardware; WASAPI's defaults name the
        real endpoints.
        """
        sd = self._sd()
        return {
            str(api["name"]): (
                _index_or_none(api.get("default_input_device")),
                _index_or_none(api.get("default_output_device")),
            )
            for api in sd.query_hostapis()
        }


def _index_or_none(value: Any) -> int | None:
    try:
        index = int(value)
    except (TypeError, ValueError):
        return None
    return index if index >= 0 else None


#: Endpoints that stand for "whatever the default is" and name no hardware.
_ALIASES = ("microsoft sound mapper", "primary sound driver", "primary sound capture driver")
#: MME truncates device names to this many characters.
_MME_NAME_LIMIT = 31
#: Which host API to open a matched pair through, best first. MME converts any
#: sample rate and is what Windows itself defaults to; WASAPI shared mode
#: refuses a rate that differs from the device's mix format, and WDM-KS is
#: exclusive. Host APIs not listed (ALSA, Core Audio, ...) rank after these.
_HOSTAPI_PREFERENCE = ("MME", "Windows DirectSound", "Windows WASAPI", "Windows WDM-KS")


def _is_alias(d: DeviceInfo) -> bool:
    return d.name.casefold().startswith(_ALIASES)


def hardware_name(name: str) -> str | None:
    """The physical device a Windows endpoint name refers to, or None.

    ``'Microphone (Jabra Evolve2 40)'`` -> ``'jabra evolve2 40'``. The device
    is what Windows puts in parentheses; MME may have cut the closing one off.
    A second identical device's ``'2- '`` prefix is KEPT: two headsets of the
    same model are two clocks.
    """
    shown = _hardware_label(name)
    return shown.casefold() if shown else None


def _hardware_label(name: str) -> str | None:
    """:func:`hardware_name` with its case kept, for messages a person reads."""
    _, sep, rest = name.partition(" (")
    if not sep:
        return None
    return (rest[:-1] if rest.endswith(")") else rest).strip() or None


def same_hardware(a: DeviceInfo, b: DeviceInfo) -> bool:
    """Are these two endpoints one physical device, and so one clock?"""
    if a.index == b.index:
        return True
    if a.hostapi != b.hostapi:
        return False  # PortAudio cannot open a duplex stream across host APIs anyway
    ha, hb = hardware_name(a.name), hardware_name(b.name)
    if ha is None or hb is None:
        return False
    if ha == hb:
        return True
    # MME cuts names at 31 characters, so 'Microphone Array (Realtek(R) Au' is
    # 'Speakers (Realtek(R) Audio)'. Only a name that WAS cut gets the prefix
    # match; otherwise 'Jabra' would pair with 'Jabra Speak 750'.
    short, long_ = sorted(((ha, a.name), (hb, b.name)), key=lambda x: len(x[0]))
    return len(short[1]) >= _MME_NAME_LIMIT and len(short[0]) >= 4 and long_[0].startswith(short[0])


def select_duplex_device(
    probe: DeviceProbe, *, name: str | None = None, samplerate: int = DEV_RATE
) -> DeviceSelection:
    """Pick ONE physical device that captures and renders, or explain precisely why not.

    With ``name`` omitted this takes the host's default input and output and
    REQUIRES them to be the same device. That requirement is the whole function:
    on most desktops the default input is a webcam mic and the default output is
    the monitor, which are two crystals and therefore a slow AEC failure.
    """
    devices = probe.devices()
    if not devices:
        raise NoDefaultDevice("the host reports no audio devices at all")
    if name is None:
        return _from_defaults(probe, devices, samplerate)

    needle = name.casefold()
    matches = [d for d in devices if needle in d.name.casefold() and not _is_alias(d)]
    if not matches:
        raise DeviceVanished(
            f"no device matching {name!r}. Present: " + ", ".join(repr(d.name) for d in devices)
        )
    groups = _candidates(matches)
    if not groups:
        if len(matches) == 1:
            only = matches[0]
            raise NotDuplex(
                f"{only.name!r} has {only.max_input_channels} in / "
                f"{only.max_output_channels} out; the graph needs one device doing both"
            )
        raise NotDuplex(
            f"{name!r} matches {len(matches)} endpoints but no microphone and speaker of one "
            "device: " + ", ".join(repr(d.name) for d in matches)
        )
    if len(groups) > 1:
        raise AmbiguousDevice(
            f"{name!r} matches {len(groups)} devices: "
            + ", ".join(repr(label) for label in sorted(groups))
        )
    (pairs,) = groups.values()
    a, b = _preferred(pairs, _default_hostapi(probe, devices))
    return _selection(a, b, samplerate, default=False)


def _from_defaults(
    probe: DeviceProbe, devices: Sequence[DeviceInfo], samplerate: int
) -> DeviceSelection:
    in_idx, out_idx = probe.defaults()
    if in_idx is None or out_idx is None:
        raise NoDefaultDevice("this host has no default input/output device; name one explicitly")
    a, b = _by_index(devices, in_idx), _by_index(devices, out_idx)
    if _is_alias(a) or _is_alias(b):
        real = _real_defaults(probe, devices)
        if real is None:
            raise NoDefaultDevice(
                f"the system defaults are {a.name!r} and {b.name!r}, which stand for "
                "whatever Windows is set to and name no device. Set voice.input_device "
                "to your headset's name." + _suggest(devices)
            )
        a, b = real
    if a.index == b.index:
        if not a.duplex:
            raise NotDuplex(
                f"{a.name!r} has {a.max_input_channels} in / {a.max_output_channels} out; "
                "the graph needs one device doing both"
            )
        return _selection(a, a, samplerate, default=True)
    if not same_hardware(a, b):
        raise ClockSplit(
            f"capture is {a.name!r} and render is {b.name!r} — two devices, two "
            "clocks. PortAudio does no drift correction between them, so the AEC "
            "converges and then silently degrades over a couple of minutes. Pick "
            "one duplex device (a USB headset is the recommended default)." + _suggest(devices)
        )
    # The defaults are one device's two ends. Open them through the best host
    # API that lists both — on Windows MME, not WASAPI, which is merely where
    # the real defaults were read from and refuses a mismatched sample rate.
    same = [(i, o) for i, o in _all_pairs(devices) if _same_name(i, a) and _same_name(o, b)]
    best = _preferred(same, None) if same else (a, b)
    return _selection(best[0], best[1], samplerate, default=True)


def _same_name(x: DeviceInfo, y: DeviceInfo) -> bool:
    """The same endpoint as listed by another host API (allowing MME's truncation)."""
    nx, ny = x.name.casefold(), y.name.casefold()
    if nx == ny:
        return True
    short, long_ = sorted((nx, ny), key=len)
    return len(short) >= _MME_NAME_LIMIT and long_.startswith(short)


def _all_pairs(devices: Sequence[DeviceInfo]) -> list[tuple[DeviceInfo, DeviceInfo]]:
    """Every (capture, render) endpoint pair that is one physical device."""
    real = [d for d in devices if not _is_alias(d)]
    out: list[tuple[DeviceInfo, DeviceInfo]] = [(d, d) for d in real if d.duplex]
    for i in real:
        if i.max_input_channels < 1 or i.duplex:
            continue
        for o in real:
            if o.max_output_channels >= 1 and not o.duplex and same_hardware(i, o):
                out.append((i, o))
    return out


def _candidates(
    matches: Sequence[DeviceInfo],
) -> dict[str, list[tuple[DeviceInfo, DeviceInfo]]]:
    """Usable pairs among the matches, grouped by the physical device they are.

    Keyed by a label a person can read ("Jabra Evolve2 40"). One headset listed
    by MME, DirectSound and WASAPI is one group, not three: two pairs join when
    their capture ends are the same hardware in one host API, or the same
    endpoint name in two.
    """
    groups: list[list[tuple[DeviceInfo, DeviceInfo]]] = []
    for pair in _all_pairs(matches):
        for group in groups:
            rep = group[0][0]
            if same_hardware(pair[0], rep) or _same_name(pair[0], rep):
                group.append(pair)
                break
        else:
            groups.append([pair])

    def label(group: list[tuple[DeviceInfo, DeviceInfo]]) -> str:
        # The longest name wins: MME's is the one that may have been cut short.
        names = [
            i.name if i.index == o.index else (_hardware_label(i.name) or i.name) for i, o in group
        ]
        return max(names, key=len)

    return {label(g): g for g in groups}


def _rank(hostapi: str, first: str | None) -> tuple[int, int]:
    if first is not None and hostapi == first:
        return (0, 0)
    if hostapi in _HOSTAPI_PREFERENCE:
        return (1, _HOSTAPI_PREFERENCE.index(hostapi))
    return (2, 0)


def _preferred(
    pairs: Sequence[tuple[DeviceInfo, DeviceInfo]], first: str | None
) -> tuple[DeviceInfo, DeviceInfo]:
    return min(pairs, key=lambda p: (_rank(p[0].hostapi, first), p[0].index))


def _default_hostapi(probe: DeviceProbe, devices: Sequence[DeviceInfo]) -> str | None:
    try:
        in_idx, _ = probe.defaults()
        return _by_index(devices, in_idx).hostapi if in_idx is not None else None
    except DeviceError:
        return None


def _real_defaults(
    probe: DeviceProbe, devices: Sequence[DeviceInfo]
) -> tuple[DeviceInfo, DeviceInfo] | None:
    """The real default endpoints, when the system defaults are aliases.

    WASAPI's own defaults first: they are the endpoints Windows' sound settings
    show, under their full names.
    """
    per_api = getattr(probe, "hostapi_defaults", None)
    if not callable(per_api):
        return None
    table = per_api()
    order = sorted(table, key=lambda api: (api != "Windows WASAPI", api))
    for api in order:
        i, o = table[api]
        if i is None or o is None:
            continue
        try:
            a, b = _by_index(devices, i), _by_index(devices, o)
        except DeviceVanished:
            continue
        if not _is_alias(a) and not _is_alias(b):
            return a, b
    return None


def usable_devices(devices: Sequence[DeviceInfo]) -> list[str]:
    """Every physical device that can both capture and render, by a readable name."""
    return sorted(_candidates([d for d in devices if not _is_alias(d)]))


def _suggest(devices: Sequence[DeviceInfo]) -> str:
    """Name the devices that WOULD work, so the refusal comes with its fix."""
    labels = usable_devices(devices)
    if not labels:
        return ""
    return (
        " Devices that can do both: "
        + ", ".join(repr(x) for x in labels)
        + ('. Set voice.input_device = "<one of those>" in config.toml.')
    )


def _selection(a: DeviceInfo, b: DeviceInfo, samplerate: int, *, default: bool) -> DeviceSelection:
    return DeviceSelection(
        index=a.index,
        name=a.name,
        hostapi=a.hostapi,
        samplerate=samplerate,
        is_system_default=default,
        output_index=None if a.index == b.index else b.index,
        output_name=None if a.index == b.index else b.name,
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
        device=selection.pair,
        samplerate=selection.samplerate,
        blocksize=block,
        dtype="int16",
        channels=channels,
        callback=callback,
        latency="low",
    )
