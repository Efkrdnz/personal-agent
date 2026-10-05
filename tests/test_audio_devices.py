"""Device selection on a machine with no sound card.

``import sounddevice`` raises ``OSError("PortAudio library not found")`` here and
on every CI runner, so the first test is that importing this module does not.
Everything after it drives selection through a fake probe, which is the whole
reason the probe is a protocol.

The distinctions being tested are the ones the reference build loses: it returns
``None`` for "use the system default" AND for "the device you configured has been
unplugged", which need opposite responses. And it never checks that capture and
render are one device, which is the most common silent cause of AEC working for
two minutes and then not.
"""

from __future__ import annotations

import pytest

from jarvis.audio import DEV_RATE
from jarvis.audio.devices import (
    AmbiguousDevice,
    AudioStackMissing,
    ClockSplit,
    DeviceInfo,
    DeviceVanished,
    NoDefaultDevice,
    NotDuplex,
    PortAudioProbe,
    select_duplex_device,
    stream_delay_ms,
)

HEADSET = DeviceInfo(0, "Jabra Evolve2 40", "ALSA", 1, 2, 48000.0)
WEBCAM = DeviceInfo(1, "HD Pro Webcam C920", "ALSA", 2, 0, 32000.0)
MONITOR = DeviceInfo(2, "HDMI Output", "ALSA", 0, 2, 48000.0)
OTHER_JABRA = DeviceInfo(3, "Jabra Speak 750", "PulseAudio", 1, 2, 48000.0)


class FakeProbe:
    def __init__(self, devices: list[DeviceInfo], defaults: tuple[int | None, int | None]) -> None:
        self._devices = devices
        self._defaults = defaults

    def devices(self) -> list[DeviceInfo]:
        return self._devices

    def defaults(self) -> tuple[int | None, int | None]:
        return self._defaults


def test_this_module_imports_without_portaudio() -> None:
    """The point of the lazy import, asserted rather than assumed."""
    try:
        import sounddevice  # noqa: F401
    except (ImportError, OSError):
        pass
    else:
        pytest.skip("PortAudio is installed here (the Windows wheel bundles it)")
    with pytest.raises(AudioStackMissing) as exc:
        PortAudioProbe().devices()
    assert "PortAudio" in str(exc.value) or "portaudio" in str(exc.value).lower()
    assert "SyntheticLeg" in str(exc.value), "the message must say what still works"


def test_the_system_default_is_chosen_and_labelled_as_such() -> None:
    sel = select_duplex_device(FakeProbe([HEADSET, WEBCAM, MONITOR], (0, 0)))
    assert sel.index == 0
    assert sel.is_system_default
    assert sel.samplerate == DEV_RATE
    assert "your system default" in sel.describe()


def test_a_named_device_is_not_labelled_as_the_default() -> None:
    sel = select_duplex_device(FakeProbe([HEADSET, WEBCAM, MONITOR], (1, 2)), name="evolve2")
    assert sel.index == 0
    assert not sel.is_system_default
    assert "configured" in sel.describe()


def test_split_capture_and_render_is_refused_by_name() -> None:
    """Two devices are two crystals, and PortAudio does no drift correction.

    This is the failure that looks like "it worked yesterday": the AEC converges
    at startup and degrades over a couple of minutes as the clocks walk apart.
    """
    with pytest.raises(ClockSplit) as exc:
        select_duplex_device(FakeProbe([HEADSET, WEBCAM, MONITOR], (1, 2)))
    msg = str(exc.value)
    assert "HD Pro Webcam C920" in msg and "HDMI Output" in msg
    assert "drift" in msg


def test_a_vanished_device_is_not_the_same_as_no_default() -> None:
    probe = FakeProbe([WEBCAM, MONITOR], (None, None))
    with pytest.raises(DeviceVanished) as vanished:
        select_duplex_device(probe, name="Jabra Evolve2 40")
    # It says what IS there, because "not found" without a list is a dead end.
    assert "HD Pro Webcam C920" in str(vanished.value)

    with pytest.raises(NoDefaultDevice):
        select_duplex_device(probe)


def test_an_ambiguous_name_is_refused_rather_than_guessed() -> None:
    with pytest.raises(AmbiguousDevice) as exc:
        select_duplex_device(FakeProbe([HEADSET, OTHER_JABRA], (0, 0)), name="jabra")
    assert "Evolve2" in str(exc.value) and "Speak 750" in str(exc.value)


def test_a_capture_only_device_cannot_be_the_graph() -> None:
    with pytest.raises(NotDuplex) as exc:
        select_duplex_device(FakeProbe([WEBCAM], (0, 0)), name="webcam")
    assert "2 in / 0 out" in str(exc.value)


def test_a_host_with_no_devices_at_all_says_so() -> None:
    with pytest.raises(NoDefaultDevice):
        select_duplex_device(FakeProbe([], (None, None)))


def test_the_default_index_pointing_at_a_gone_device_is_a_vanish() -> None:
    with pytest.raises(DeviceVanished):
        select_duplex_device(FakeProbe([HEADSET], (7, 7)))


def test_the_delay_hint_has_the_sign_the_aec_expects() -> None:
    """A sign error here is invisible in a running system and ruins convergence.

    The DAC time for the block being written is LATER than the ADC time for the
    block just captured, so the hint is positive.
    """
    assert stream_delay_ms(input_adc_time=1.000, output_dac_time=1.030) == 30
    assert stream_delay_ms(input_adc_time=1.030, output_dac_time=1.000) == -30
    assert stream_delay_ms(input_adc_time=5.0, output_dac_time=5.0) == 0


# ───────────────────────────── Windows ─────────────────────────────
#
# What `python -m sounddevice` prints on a Windows laptop with a USB headset:
# every direction of every device is its own endpoint, in four host APIs; the
# system defaults are MME's "Sound Mapper" aliases, which name no hardware;
# MME cuts names at 31 characters. Requiring ONE index refused all of it.

WIN = [
    DeviceInfo(0, "Microsoft Sound Mapper - Input", "MME", 2, 0, 44100.0),
    DeviceInfo(1, "Microphone (Jabra Evolve2 40)", "MME", 1, 0, 44100.0),
    DeviceInfo(2, "Microphone Array (Realtek(R) Au", "MME", 2, 0, 44100.0),
    DeviceInfo(3, "Microsoft Sound Mapper - Output", "MME", 0, 2, 44100.0),
    DeviceInfo(4, "Headphones (Jabra Evolve2 40)", "MME", 0, 2, 44100.0),
    DeviceInfo(5, "Speakers (Realtek(R) Audio)", "MME", 0, 2, 44100.0),
    DeviceInfo(6, "Primary Sound Capture Driver", "Windows DirectSound", 2, 0, 44100.0),
    DeviceInfo(7, "Microphone (Jabra Evolve2 40)", "Windows DirectSound", 1, 0, 44100.0),
    DeviceInfo(8, "Primary Sound Driver", "Windows DirectSound", 0, 2, 44100.0),
    DeviceInfo(9, "Headphones (Jabra Evolve2 40)", "Windows DirectSound", 0, 2, 44100.0),
    DeviceInfo(10, "Headphones (Jabra Evolve2 40)", "Windows WASAPI", 0, 2, 48000.0),
    DeviceInfo(11, "Microphone (Jabra Evolve2 40)", "Windows WASAPI", 1, 0, 48000.0),
    DeviceInfo(12, "Speakers (Realtek(R) Audio)", "Windows WASAPI", 0, 2, 48000.0),
    DeviceInfo(13, "Microphone Array (Realtek(R) Audio)", "Windows WASAPI", 2, 0, 48000.0),
]


class WinProbe(FakeProbe):
    def __init__(self, wasapi: tuple[int | None, int | None] = (11, 10)) -> None:
        super().__init__(WIN, (0, 3))
        self._wasapi = wasapi

    def hostapi_defaults(self) -> dict[str, tuple[int | None, int | None]]:
        return {"MME": (0, 3), "Windows DirectSound": (6, 8), "Windows WASAPI": self._wasapi}


def test_windows_defaults_resolve_through_the_alias_to_one_headset() -> None:
    sel = select_duplex_device(WinProbe())
    # The real defaults come from WASAPI; the stream opens through MME.
    assert sel.pair == (1, 4) and sel.hostapi == "MME"
    assert sel.is_system_default
    assert "Microphone (Jabra Evolve2 40)" in sel.describe()
    assert "Headphones (Jabra Evolve2 40)" in sel.describe()


def test_windows_headset_mic_with_laptop_speakers_is_still_two_clocks() -> None:
    with pytest.raises(ClockSplit) as exc:
        select_duplex_device(WinProbe(wasapi=(11, 12)))
    msg = str(exc.value)
    # The refusal names what WOULD work and the line that selects it.
    assert "Jabra Evolve2 40" in msg and "Realtek(R) Audio" in msg
    assert "voice.input_device" in msg


def test_naming_the_headset_finds_its_two_ends_in_one_host_api() -> None:
    sel = select_duplex_device(WinProbe(), name="Jabra")
    assert sel.pair == (1, 4) and sel.hostapi == "MME" and not sel.is_system_default


def test_mme_truncation_still_pairs_the_built_in_device() -> None:
    sel = select_duplex_device(WinProbe(), name="Realtek")
    assert sel.pair == (2, 5) and sel.hostapi == "MME"


def test_naming_only_one_direction_says_so() -> None:
    with pytest.raises(NotDuplex, match="no microphone and speaker of one device"):
        select_duplex_device(WinProbe(), name="Microphone")


def test_aliases_with_no_real_defaults_ask_for_a_name() -> None:
    probe = WinProbe(wasapi=(None, None))
    probe.hostapi_defaults = lambda: {"MME": (0, 3)}  # type: ignore[method-assign]
    with pytest.raises(NoDefaultDevice, match="Sound Mapper") as exc:
        select_duplex_device(probe)
    assert "Jabra Evolve2 40" in str(exc.value)


def test_a_cut_name_does_not_pair_with_a_different_device() -> None:
    from jarvis.audio.devices import same_hardware

    mic = DeviceInfo(1, "Microphone (Jabra Evolve2 40)", "MME", 1, 0, 44100.0)
    other = DeviceInfo(2, "Headphones (Jabra Speak 750)", "MME", 0, 2, 44100.0)
    second_unit = DeviceInfo(3, "Headphones (2- Jabra Evolve2 40)", "MME", 0, 2, 44100.0)
    wasapi_twin = DeviceInfo(4, "Headphones (Jabra Evolve2 40)", "Windows WASAPI", 0, 2, 48000.0)
    assert not same_hardware(mic, other)
    assert not same_hardware(mic, second_unit), "two headsets of one model are two clocks"
    assert not same_hardware(mic, wasapi_twin), "a duplex stream cannot span host APIs"


def test_doctor_lists_devices_not_endpoints() -> None:
    from jarvis.audio.devices import usable_devices

    assert usable_devices(WIN) == ["Jabra Evolve2 40", "Realtek(R) Audio"]


# ───────────────────────────── the real probe's parsing ─────────────────────────────


class _Pair:
    """sounddevice's _InputOutputPair: indexable, and NOT a tuple."""

    def __init__(self, i: object, o: object) -> None:
        self._v = [i, o]

    def __getitem__(self, k: int) -> object:
        return self._v[k]


def _fake_sd(monkeypatch: pytest.MonkeyPatch, device: object, apis: list[dict]) -> None:
    import types

    sd = types.SimpleNamespace(
        default=types.SimpleNamespace(device=device), query_hostapis=lambda: apis
    )
    monkeypatch.setattr(PortAudioProbe, "_sd", staticmethod(lambda: sd))


def test_the_default_device_pair_object_is_read_not_compared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The crash on the first Windows machine: '<' between _InputOutputPair and int."""
    _fake_sd(monkeypatch, _Pair(1, 4), [])
    assert PortAudioProbe().defaults() == (1, 4)
    _fake_sd(monkeypatch, _Pair(-1, 4), [])
    assert PortAudioProbe().defaults() == (None, 4)
    _fake_sd(monkeypatch, 7, [])
    assert PortAudioProbe().defaults() == (7, 7)


def test_host_api_defaults_are_read_per_api(monkeypatch: pytest.MonkeyPatch) -> None:
    apis = [
        {"name": "MME", "default_input_device": 0, "default_output_device": 3},
        {"name": "Windows WASAPI", "default_input_device": 11, "default_output_device": -1},
    ]
    _fake_sd(monkeypatch, _Pair(0, 3), apis)
    assert PortAudioProbe().hostapi_defaults() == {
        "MME": (0, 3),
        "Windows WASAPI": (11, None),
    }


def test_the_stream_opens_both_ends_of_a_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    import types

    from jarvis.audio.devices import open_duplex_stream

    opened: dict[str, object] = {}
    sd = types.SimpleNamespace(Stream=lambda **kw: opened.update(kw) or "stream")
    monkeypatch.setattr(PortAudioProbe, "_sd", staticmethod(lambda: sd))
    sel = select_duplex_device(WinProbe())
    assert open_duplex_stream(sel, lambda *a: None) == "stream"
    assert opened["device"] == (1, 4)
