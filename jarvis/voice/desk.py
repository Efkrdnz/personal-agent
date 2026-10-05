"""What the desk tells the rest of the system while it runs: its state, its events, and "say this".

THE DESK IS A PROCESS OTHER PROCESSES CANNOT SEE INTO. The window wants to show
whether Jarvis is asleep or listening and what was just said; the only way one
process learns anything about another here is a row. So the desk writes rows:

* a HEARTBEAT twice a second (:mod:`jarvis.liveness`) carrying its state —
  ``asleep``, ``awake``, ``listening`` or ``speaking`` — which is what the
  window's orb renders;
* its EVENTS, through the two queues that existed from the start and that
  nothing ever drained: the Live session's (transcripts, tool calls, connects)
  and the audio graph's (wake, sleep, turns, barge-ins). Both were built to be
  drained "by something holding a connection"; :class:`DeskPublisher` is that
  something, on its own thread with its own connection, so a slow write never
  stalls the audio callback or the receive loop.

And it READS one kind of row: a ``say`` command (:mod:`jarvis.kill`). Only the
desk owns the speaker while it runs — rule 2, one output stream, because audio
from any other process is echo the AEC was never shown — so a window that wants
something said aloud asks the desk to say it.
"""

from __future__ import annotations

import sqlite3
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from jarvis import kill, liveness
from jarvis.audio.turn import TurnState

__all__ = ["SAY_ACTOR", "DeskPublisher", "consume_say_commands", "desk_state"]

#: The command actor for the desk. SHARED, not per-process: two desks racing to
#: say the same sentence must produce one voice, and a shared actor makes the
#: claim mutually exclusive (see :func:`jarvis.kill.claim_command`).
SAY_ACTOR = "desk"


def desk_state(turn: Any, mixer: Any) -> str:
    """One of :data:`jarvis.liveness.DESK_STATES`, from the objects that know.

    Speaking wins over listening: during a barge-in both are briefly true, and
    the orb should show the voice that is actually coming out of the speaker.
    """
    if mixer.is_playing:
        return "speaking"
    if turn.state is TurnState.USER_SPEAKING:
        return "listening"
    return "awake" if turn.awake() else "asleep"


@dataclass
class DeskPublisher:
    """Drain the desk's event queues and beat its heartbeat, on a thread of its own.

    ``drains`` are callables taking a connection and returning how many events
    they published — :meth:`jarvis.live.session.QueuedLiveEvents.drain` and
    :meth:`jarvis.audio.graph.QueuedEventSink.drain`, partially applied. Kept
    abstract so this module needs neither the Live SDK nor a sound card to test.
    """

    open_db: Callable[[], sqlite3.Connection]
    state: Callable[[], str]
    drains: Sequence[Callable[[sqlite3.Connection], int]]
    every_s: float = 0.25
    #: A beat every half second, and at once whenever the state changes, so the
    #: orb follows the voice rather than lagging it by up to a beat.
    beat_every_s: float = 0.5
    clock: Callable[[], float] = time.monotonic
    published: int = 0
    failures: int = 0
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _thread: threading.Thread | None = field(default=None, repr=False)
    _last_beat: float = field(default=float("-inf"), repr=False)
    _last_state: str | None = field(default=None, repr=False)

    def step(self, con: sqlite3.Connection) -> int:
        """One pass: publish what is queued, then beat if due. Returns events published."""
        n = 0
        for drain in self.drains:
            n += drain(con)
        self.published += n
        current = self.state()
        now = self.clock()
        if current != self._last_state or now - self._last_beat >= self.beat_every_s:
            liveness.beat(con, "desk", state=current)
            self._last_beat, self._last_state = now, current
        return n

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("the desk publisher is already running")
        self._thread = threading.Thread(target=self._run, name="desk-publisher", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        """Final drain, then say "offline" so the window does not wait ten seconds."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self) -> None:
        con = self.open_db()
        try:
            while not self._stop.is_set():
                self._guarded(con)
                self._stop.wait(self.every_s)
            self._guarded(con)
            liveness.gone(con, "desk")
        finally:
            con.close()

    def _guarded(self, con: sqlite3.Connection) -> None:
        # A locked database or one bad payload must cost one pass, not the
        # thread: a publisher that died silently is a window that shows the
        # desk as gone while the user is talking to it.
        try:
            self.step(con)
        except Exception as exc:  # noqa: BLE001 - see above
            self.failures += 1
            if self.failures in (1, 10, 100) or self.failures % 1000 == 0:
                print(f"[desk publisher] {type(exc).__name__}: {exc}", file=sys.stderr)


def consume_say_commands(
    con: sqlite3.Connection,
    speak: Callable[[str], Any],
    *,
    actor: str = SAY_ACTOR,
    now_ts: str | None = None,
) -> int:
    """Say every pending ``say`` command aloud, once. Returns how many were handled.

    Claim, speak, ack, finish — in that order, so a desk killed mid-sentence
    leaves a claimed command that nobody repeats (a reminder said twice is worse
    than one cut short), and the ack carries what happened for the window's log.
    """
    handled = 0
    for cmd in kill.pending_commands(con, actor=actor, verbs=("say",), now_ts=now_ts):
        if cmd.target_kind == "channel" and cmd.target_id not in (None, "desk"):
            continue  # addressed to some other channel's speaker
        if not kill.claim_command(con, cmd.id, actor, now_ts=now_ts):
            continue
        text = str(cmd.args.get("text") or "").strip()
        if not text:
            result = "nothing to say"
        else:
            try:
                speak(text)
                result = "said"
            except Exception as exc:  # noqa: BLE001 - the ack is where the failure is told
                result = f"failed: {type(exc).__name__}: {exc}"
        kill.ack_command(con, cmd.id, actor, result=result, now_ts=now_ts)
        kill.finish_command(con, cmd.id, now_ts=now_ts)
        handled += 1
    return handled
