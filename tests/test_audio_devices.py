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
