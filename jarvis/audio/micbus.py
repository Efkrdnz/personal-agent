"""The single microphone tap: one writer, N cursors, nobody blocks the writer.

RULE 3 OF THE GRAPH LIVES HERE. Wake word, kill spotter, navigation spotter, VAD
and the Gemini uplink are all READERS of one 16 kHz int16 mono ring with their
own cursors. The reference build gave each detector its own queue fed by the
callback, which means the callback's cost scales with the number of detectors and
a detector that stalls backs up into the audio thread. Here the callback does one
memcpy no matter how many things are listening.

THE FAILURE THIS IS DESIGNED AROUND. A reader that falls behind — the uplink
waiting on a socket, a spotter on a busy core — will eventually be pointing at
ring space the writer has already reused. Three things could happen there and two
of them are unacceptable: block the writer (audio glitches, and in a PortAudio
callback that is a dropout you can hear), or hand back whatever bytes are now in
those slots (a silently torn frame, which shows up weeks later as a detector that
"sometimes" misfires). So this ring does the third thing: it FAST-FORWARDS the
lagging cursor to the oldest sample it can still serve honestly and REPORTS how
many samples were skipped. Lost audio is a fact you can act on — re-arm the
detector, log it, show it in the TUI. A torn frame is a fact you cannot even see.

WHY A LOCK IN THE AUDIO CALLBACK. Purists would use a lock-free ring. But the
writer only ever contends with readers for the duration of a memcpy of at most
one block, it never waits on a reader's progress, and the graph already runs
Python in the callback. Measured against the 20 ms budget, an uncontended
``threading.Condition`` acquire is noise. The property that matters is that no
code path exists in which the writer waits for a reader to catch up.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import numpy as np

from jarvis.audio import MIC_RATE

__all__ = ["BusClosed", "MicBus", "MicReader"]


class BusClosed(RuntimeError):
    """The bus was closed while a reader was waiting on it.

    Raised rather than returned so a leg that drops mid-turn unwinds its reader
    threads instead of spinning on an empty ring forever.
    """


class MicBus:
    """A single-writer, many-cursor ring of int16 mono samples.

    Cursors live in an absolute sample-count space that never wraps, so a stale
    cursor is arithmetically detectable rather than ambiguous — the classic ring
    bug is a wrapped index that looks valid.
    """

    def __init__(self, *, rate: int = MIC_RATE, seconds: float = 4.0) -> None:
        capacity = int(rate * seconds)
        if capacity <= 0:
            raise ValueError(f"capacity must be positive, got {capacity}")
        self._rate = rate
        self._cap = capacity
        self._buf = np.zeros(capacity, dtype=np.int16)
        self._written = 0
        self._closed = False
        self._cond = threading.Condition()

    @property
    def rate(self) -> int:
        return self._rate

    @property
    def capacity(self) -> int:
        return self._cap

    @property
    def written(self) -> int:
        """Total samples ever written. Also the cursor a reader attaching now gets."""
        with self._cond:
            return self._written

    def cursor(self) -> int:
        """A cursor positioned at the live edge: a new reader hears what happens next."""
        return self.written

    def oldest(self) -> int:
        with self._cond:
            return max(0, self._written - self._cap)

    def write(self, pcm: np.ndarray) -> None:
        """Append one block. Called from the audio callback and nowhere else."""
        if pcm.dtype != np.int16:
            raise TypeError(f"MicBus carries int16, got {pcm.dtype}")
        if pcm.ndim != 1:
            raise ValueError(f"MicBus is mono, got shape {pcm.shape}")
        n = pcm.shape[0]
        if n == 0:
            return
        # A block longer than the whole ring can only be a bug upstream. Dropping
        # the call would hide it and raising would kill the audio thread, so keep
        # the newest capacity samples — and ADVANCE THE CURSOR SPACE BY THE FULL
        # LENGTH, so the samples that were thrown away show up in every reader's
        # drop counter. Advancing only by what was kept would make the loss
        # arithmetically invisible, which is the exact failure this ring exists
        # to prevent.
        skipped = 0
        if n > self._cap:
            skipped = n - self._cap
            pcm = pcm[-self._cap :]
            n = self._cap
        with self._cond:
            if self._closed:
                raise BusClosed("write to a closed MicBus")
            start = (self._written + skipped) % self._cap
            end = start + n
            if end <= self._cap:
                self._buf[start:end] = pcm
            else:
                split = self._cap - start
                self._buf[start:] = pcm[:split]
                self._buf[: end - self._cap] = pcm[split:]
            self._written += n + skipped
            self._cond.notify_all()

    def read(self, cursor: int, n: int, timeout: float = 0.5) -> tuple[np.ndarray | None, int, int]:
        """Read ``n`` samples from ``cursor``.

        Returns ``(pcm, next_cursor, dropped)``. ``pcm`` is None when the timeout
        expired with fewer than ``n`` samples available; ``dropped`` is how many
        samples this reader lost by being too slow, and it is reported on the
        timeout path too, because a reader that is both lagging AND starved must
        learn about the lag.

        The caller MUST adopt ``next_cursor``. Reusing the cursor it passed in
        after a fast-forward re-reads the same gap forever.
        """
        if n <= 0:
            raise ValueError(f"read size must be positive, got {n}")
        if n > self._cap:
            raise ValueError(f"read of {n} exceeds ring capacity {self._cap}")
        deadline = time.monotonic() + max(0.0, timeout)
        dropped = 0
        with self._cond:
            while True:
                oldest = max(0, self._written - self._cap)
                if cursor < oldest:
                    dropped += oldest - cursor
                    cursor = oldest
                if self._written - cursor >= n:
                    return self._slice(cursor, n), cursor + n, dropped
                if self._closed:
                    raise BusClosed("MicBus closed while a reader was waiting")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None, cursor, dropped
                self._cond.wait(remaining)

    def _slice(self, cursor: int, n: int) -> np.ndarray:
        start = cursor % self._cap
        end = start + n
        if end <= self._cap:
            return self._buf[start:end].copy()
        split = self._cap - start
        out = np.empty(n, dtype=np.int16)
        out[:split] = self._buf[start:]
        out[split:] = self._buf[: end - self._cap]
        return out

    def close(self) -> None:
        """Wake every waiting reader. Idempotent, because several legs may unwind at once."""
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    @property
    def closed(self) -> bool:
        with self._cond:
            return self._closed

    def reader(self, name: str, *, from_start: bool = False) -> MicReader:
        """Attach an independent cursor. Detectors are created, not registered."""
        return MicReader(name=name, bus=self, cursor=self.oldest() if from_start else self.cursor())


@dataclass
class MicReader:
    """One detector's view of the tap: its own cursor and its own drop ledger."""

    name: str
    bus: MicBus
    cursor: int
    dropped: int = 0
    frames: int = 0
    _lag_events: int = field(default=0, repr=False)

    def read(self, n: int, timeout: float = 0.5) -> np.ndarray | None:
        pcm, self.cursor, dropped = self.bus.read(self.cursor, n, timeout)
        if dropped:
            self.dropped += dropped
            self._lag_events += 1
        if pcm is not None:
            self.frames += 1
        return pcm

    def drain(self, n: int) -> list[np.ndarray]:
        """Every whole ``n``-sample frame available right now, without waiting.

        This is how the graph pumps detectors from inside a block: take what is
        there, leave the remainder for the next block. A detector that wants to
        block instead calls :meth:`read` with a timeout.
        """
        out: list[np.ndarray] = []
        while self.available >= n:
            frame = self.read(n, timeout=0.0)
            if frame is None:
                break
            out.append(frame)
        return out

    @property
    def available(self) -> int:
        """How many samples this reader can actually still be served.

        Clamped to the ring, because the raw backlog of a reader that fell behind
        is a number it can never obtain — reporting it would have `drain` loop on
        audio that no longer exists. Use :attr:`behind` for the raw backlog.
        """
        return min(max(0, self.bus.written - self.cursor), self.bus.capacity)

    @property
    def behind(self) -> int:
        """Raw distance from the live edge, INCLUDING samples already overwritten."""
        return max(0, self.bus.written - self.cursor)

    @property
    def lagging(self) -> bool:
        """True once this reader has provably lost audio. Never resets itself."""
        return self.dropped > 0

    @property
    def lag_events(self) -> int:
        return self._lag_events
