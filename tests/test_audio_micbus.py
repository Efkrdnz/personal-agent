"""The one microphone tap, and the reader that falls behind.

The property under test is not "the ring works". It is that a SLOW READER IS
DETECTABLE. Losing audio is survivable; a detector that silently reads a torn
frame is the bug that shows up months later as "barge-in is flaky sometimes",
and there is no log line to find. So every test here either proves a reader was
TOLD what it lost, or proves the writer was never made to wait.
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from jarvis.audio import MIC_RATE
from jarvis.audio.micbus import BusClosed, MicBus


def ramp(start: int, n: int) -> np.ndarray:
    """A signal whose every sample says where it came from, so a torn read shows."""
    return (np.arange(start, start + n) % 30000).astype(np.int16)


def test_reader_sees_exactly_what_was_written() -> None:
    bus = MicBus(rate=MIC_RATE, seconds=1.0)
    r = bus.reader("uplink")
    bus.write(ramp(0, 512))
    got = r.read(512, timeout=0.0)
    assert got is not None
    np.testing.assert_array_equal(got, ramp(0, 512))
    assert r.dropped == 0


def test_five_readers_have_independent_cursors() -> None:
    """Wake word, kill spotter, nav spotter, VAD and uplink. One tap, five views."""
    bus = MicBus(rate=MIC_RATE, seconds=1.0)
    names = ["wake", "kill", "nav", "vad", "uplink"]
    readers = {n: bus.reader(n) for n in names}
    bus.write(ramp(0, 1600))

    # Each one consumes a different amount; nobody's progress affects anyone else.
    assert readers["wake"].read(1600, timeout=0.0) is not None
    assert readers["vad"].read(512, timeout=0.0) is not None
    assert readers["kill"].read(160, timeout=0.0) is not None

    assert readers["nav"].available == 1600
    assert readers["uplink"].available == 1600
    assert readers["vad"].available == 1600 - 512
    assert readers["wake"].available == 0
    assert all(r.dropped == 0 for r in readers.values())


def test_a_slow_reader_is_fast_forwarded_and_told_what_it_lost() -> None:
    bus = MicBus(rate=1000, seconds=1.0)  # 1000 samples of ring
    slow = bus.reader("slow")
    fast = bus.reader("fast")

    for block in range(4):
        bus.write(ramp(block * 500, 500))
        fast.read(500, timeout=0.0)

    # 2000 samples went past a 1000-sample ring; the slow reader can only be
    # served the last 1000, so it lost exactly the first 1000.
    got = slow.read(500, timeout=0.0)
    assert got is not None
    assert slow.dropped == 1000
    assert slow.lagging
    assert slow.lag_events == 1
    # And what it got is genuinely the oldest still-available audio, not a
    # wrapped-around mixture of two eras.
    np.testing.assert_array_equal(got, ramp(1000, 500))

    assert fast.dropped == 0
    assert not fast.lagging


def test_the_writer_never_waits_for_a_reader() -> None:
    """The one property that cannot be traded away: the audio callback must return.

    A reader is parked inside read() with a long timeout while the writer fills
    the ring several times over. If the writer could ever block on a reader, this
    hangs; instead the writer finishes promptly and the reader learns it lagged.
    """
    bus = MicBus(rate=1000, seconds=0.5)  # 500-sample ring
    parked = bus.reader("parked")
    started = threading.Event()
    result: dict[str, object] = {}

    def park() -> None:
        started.set()
        # Ask for more than the ring holds after the writer has moved on.
        result["pcm"] = parked.read(400, timeout=2.0)

    t = threading.Thread(target=park, daemon=True)
    t.start()
    assert started.wait(1.0)

    t0 = time.monotonic()
    for block in range(20):
        bus.write(ramp(block * 500, 500))
    elapsed = time.monotonic() - t0

    t.join(timeout=2.0)
    assert not t.is_alive()
    # 20 writes of a 500-sample ring, with a reader parked the whole time.
    assert elapsed < 0.5, f"the writer waited {elapsed:.3f}s on a reader"
    assert result["pcm"] is not None
    assert parked.dropped > 0


def test_a_timed_out_read_still_reports_the_lag() -> None:
    """A reader that is BOTH behind and starved must still learn it is behind."""
    bus = MicBus(rate=1000, seconds=0.5)
    r = bus.reader("starved")
    for block in range(3):
        bus.write(ramp(block * 500, 500))
    # 1500 written into a 500 ring; oldest available is 1000, and only 500 are
    # there, so a 500-sample read succeeds but a 501 one cannot.
    pcm, cursor, dropped = bus.read(r.cursor, 400, timeout=0.0)
    assert pcm is not None and dropped == 1000
    pcm2, _, dropped2 = bus.read(cursor, 400, timeout=0.01)
    assert pcm2 is None
    assert dropped2 == 0  # already fast-forwarded once; not double-counted


def test_write_wraps_the_ring_without_tearing() -> None:
    bus = MicBus(rate=1000, seconds=0.3)  # 300 samples
    r = bus.reader("r")
    bus.write(ramp(0, 200))
    bus.write(ramp(200, 80))  # crosses the wrap point
    got = r.read(280, timeout=0.0)
    assert got is not None
    np.testing.assert_array_equal(got, ramp(0, 280))


def test_an_oversized_block_keeps_the_newest_audio() -> None:
    """A block bigger than the ring is an upstream bug; drop the PAST, not the present."""
    bus = MicBus(rate=1000, seconds=0.1)  # 100 samples
    r = bus.reader("r")
    bus.write(ramp(0, 250))
    got = r.read(100, timeout=0.0)
    assert got is not None
    np.testing.assert_array_equal(got, ramp(150, 100))
    assert r.dropped == 150


def test_close_wakes_a_waiting_reader() -> None:
    """A session that drops mid-turn must not leave detector threads parked forever."""
    bus = MicBus(rate=MIC_RATE, seconds=0.5)
    r = bus.reader("uplink")
    errors: list[BaseException] = []

    def wait() -> None:
        try:
            r.read(512, timeout=5.0)
        except BaseException as exc:  # noqa: BLE001 - the test is about which one
            errors.append(exc)

    t = threading.Thread(target=wait, daemon=True)
    t.start()
    time.sleep(0.05)
    bus.close()
    t.join(timeout=2.0)
    assert not t.is_alive()
    assert errors and isinstance(errors[0], BusClosed)


def test_rejects_the_wrong_shape_rather_than_reinterpreting_it() -> None:
    bus = MicBus(rate=MIC_RATE, seconds=0.5)
    with pytest.raises(TypeError):
        bus.write(np.zeros(320, dtype=np.float32))
    with pytest.raises(ValueError):
        bus.write(np.zeros((320, 2), dtype=np.int16))
    with pytest.raises(ValueError):
        bus.read(0, MIC_RATE * 10)


def test_a_reader_attaches_at_the_live_edge_by_default() -> None:
    """A spotter armed halfway through a briefing hears what happens NEXT, not the past."""
    bus = MicBus(rate=MIC_RATE, seconds=1.0)
    bus.write(ramp(0, 1600))
    late = bus.reader("nav")
    assert late.available == 0
    from_start = bus.reader("audit", from_start=True)
    assert from_start.available == 1600
