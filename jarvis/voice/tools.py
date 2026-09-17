"""Where a spoken tool call becomes a database write, and what the user hears back.

Two objects, both small, both here rather than in the composition root because
both are decisions and the composition root is only allowed to hold wiring.

:class:`Transcript` is the user's OWN words. Gemini's function call carries its
summary of what it heard; :mod:`jarvis.spec`'s whole fidelity chain is checked
against the transcript instead, so the summary never becomes the contract. See
:mod:`jarvis.tools.builtin.code_build` for why that distinction is load-bearing.

:class:`LiveTools` adapts :class:`jarvis.tools.registry.Registry` onto the Live
session's tool seam. It does three things worth naming:

* IT OPENS ITS OWN CONNECTION, per call, in the worker thread. A ``sqlite3``
  connection belongs to the thread that made it, and the alternative —
  ``check_same_thread=False`` and a shared handle — is a data race that shows up
  as a corrupted activity log weeks later. One connection per tool call costs
  microseconds against a local WAL file.
* IT INTERSECTS TWO PERMISSION TABLES, and the intersection is deliberate: the
  profile says what this LEG may offer (default-deny, frozen on the third-party
  call) and the tool says which CHANNELS may call it. Either one alone has a
  hole — a new tool defaulting to every channel would be offered on a phone call
  that never listed it; a tool listed in a profile could be called from a
  channel its own table forbids.
* IT DECIDES WHO SPEAKS THE RESULT. With a reader attached the text is read by
  the deterministic voice and the model is told SILENTLY what was said, so "the
  second one" still resolves without Gemini re-wording a promise. With no reader
  the model speaks it, and the response says which happened rather than leaving
  the user in silence wondering whether the tool ran.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from jarvis import requests as rq
from jarvis.live.profiles import SessionProfile
from jarvis.live.session import ToolCall, ToolResult
from jarvis.tools.builtin.answer import OPEN_QUESTION
from jarvis.tools.ctx import ToolCtx
from jarvis.tools.registry import Registry
from jarvis.voice.router import Fidelity, Utterance
from jarvis.voice.script import script

__all__ = ["Transcript", "DeskQuestions", "LiveTools", "TRANSCRIPT_WINDOW_S", "PRESENT_LEASE_S"]

#: How long a desk holds the right to read one question aloud. Long enough to
#: finish speaking it, short enough that a desk killed mid-sentence hands the
#: question back rather than taking it to the grave.
PRESENT_LEASE_S = 90

#: How much of the user's recent speech a build request is allowed to draw on.
#: Three minutes rather than "the current turn" because a request arrives in
#: pieces — "let's build a comment watcher… oh, and no Docker" — and because the
#: two failure modes are not symmetrical. Including a stray sentence is caught by
#: the read-back, where the user hears it and says "drop three". DROPPING a
#: sentence is invisible: nobody misses a requirement they were never read. So
#: the window over-includes on purpose, the same bias :mod:`jarvis.spec` takes
#: with its literal-token net.
TRANSCRIPT_WINDOW_S = 180.0


@dataclass
class Transcript:
    """A rolling window of what the USER said, as the server transcribed it.

    Not what Jarvis said: output transcription goes nowhere near this, because a
    requirement list built partly from the assistant's own suggestions is the
    exact failure R2 forbids, and it would pass every containment check.
    """

    window_s: float = TRANSCRIPT_WINDOW_S
    max_chars: int = 8000
    clock: Callable[[], float] = time.monotonic
    _parts: deque[tuple[float, str]] = field(default_factory=deque, repr=False)

    def heard(self, fragment: str) -> None:
        """One input-transcription fragment. Called from the session's event loop."""
        if not fragment:
            return
        self._parts.append((self.clock(), fragment))
        self._trim()

    def _trim(self) -> None:
        cutoff = self.clock() - self.window_s
        while self._parts and self._parts[0][0] < cutoff:
            self._parts.popleft()
        while self._parts and sum(len(p) for _, p in self._parts) > self.max_chars:
            self._parts.popleft()

    def words(self) -> str:
        """The window as one string, whitespace folded. May be empty."""
        self._trim()
        return " ".join("".join(p for _, p in self._parts).split())

    def clear(self) -> None:
        """Forget the window. Called once a request has consumed it."""
        self._parts.clear()

    def __bool__(self) -> bool:
        return bool(self.words())


@dataclass
class LiveTools:
    """The Live session's ``ToolRegistry``, backed by :mod:`jarvis.tools`."""

    registry: Registry
    #: Opens a connection. Called in the worker thread, once per tool call, and
    #: closed again — see the module docstring.
    open_db: Callable[[], sqlite3.Connection]
    channel: str = "desk"
    actor: str = "desk"
    transcript: Transcript | None = None
    #: Which question this desk has read aloud, if any. See :class:`DeskQuestions`.
    questions: Any | None = None
    #: The deterministic reader. None means nobody but Gemini can speak, and the
    #: response says so rather than the result being lost.
    speak: Callable[[Utterance], Any] | None = None
    #: Extra values every handler should see in ``ctx.extra`` (the spend ceiling,
    #: say). Merged UNDER the transcript, which this object owns.
    extra: dict[str, Any] = field(default_factory=dict)
    calls: int = 0

    # ── the two permission tables ────────────────────────────────────────

    def declarations(self, profile: SessionProfile) -> list[dict[str, Any]]:
        allowed = set(profile.tools)
        return [d for d in self.registry.declarations(self.channel) if d["name"] in allowed]

    def unresolved(self, profile: SessionProfile) -> tuple[str, ...]:
        """Names the profile allows that no tool answers to.

        A profile is written before the tools exist, so this drifts silently: the
        leg offers four tools, the model is told about two, and nothing anywhere
        is an error. ``python -m jarvis doctor`` prints this.
        """
        return tuple(sorted(n for n in profile.tools if n not in self.registry))

    def unreachable(self, profile: SessionProfile) -> tuple[str, ...]:
        """Tools this channel may call that this profile never offers. The other drift."""
        allowed = set(profile.tools)
        return tuple(sorted(n for n in self.registry.names(self.channel) if n not in allowed))

    # ── dispatch ─────────────────────────────────────────────────────────

    async def dispatch(self, call: ToolCall) -> ToolResult:
        self.calls += 1
        text = await asyncio.to_thread(self._run, call.name, dict(call.args))
        spoken = await self._read_aloud(call.name, text)
        return ToolResult(
            response={"said": text, "already_spoken": spoken},
            # SILENT when the reader said it: the model must KNOW the sentence so
            # "start it then" resolves, and must not say it again. WHEN_IDLE when
            # nobody else can speak, so the user is not left in silence.
            scheduling="SILENT" if spoken else "WHEN_IDLE",
        )

    def _run(self, name: str, args: dict[str, Any]) -> str:
        con = self.open_db()
        try:
            return self.registry.dispatch(name, args, self._ctx(con))
        finally:
            con.close()

    def _ctx(self, con: sqlite3.Connection) -> ToolCtx:
        extra = dict(self.extra)
        if self.transcript is not None:
            extra["transcript"] = self.transcript.words()
        if self.questions is not None and self.questions.current:
            # What "the second one" refers to. Read fresh every call: the desk
            # may have read a newer question since the model decided to answer.
            extra[OPEN_QUESTION] = self.questions.current
        return ToolCtx(con=con, channel=self.channel, actor=self.actor, extra=extra)

    async def _read_aloud(self, name: str, text: str) -> bool:
        if self.speak is None or not text.strip():
            return False
        # 'faithful' rather than 'exact': these are Jarvis's own sentences, not
        # somebody else's words — but they carry promises ("nothing is created
        # yet") that a paraphrase can quietly drop, so they go to the reader.
        fidelity: Fidelity = "faithful"
        result = self.speak(Utterance(text=text, fidelity=fidelity, tag=f"tool:{name}"))
        if asyncio.iscoroutine(result):
            await result
        return True


@dataclass
class DeskQuestions:
    """The desk's half of the request lifecycle: read one aloud, remember which.

    The scheduler writes a ``desk`` rung for every question (it is the first and
    immediate one, because a user at their desk should not wait ninety seconds
    for Telegram). Until this existed nothing ever claimed those rungs — the only
    caller of :func:`jarvis.requests.due_deliveries` in the tree was the Telegram
    channel — so the desk never learned a question existed and the whole "plan
    mode as a spoken conversation" requirement had no way to start.

    THE CLAIM IS WHY THIS IS NOT A LOOP OVER OPEN REQUESTS. Two desk processes
    (an old one that has not noticed it was replaced) must not both read the same
    question aloud, and a desk killed mid-sentence must hand it back. Both are
    what the lease on the delivery row is for.
    """

    open_db: Callable[[], sqlite3.Connection]
    speak: Callable[[Utterance], Any] | None = None
    channel: str = "desk"
    actor: str = "desk"
    #: The request this desk most recently read. What "the second one" refers to,
    #: and the only thing :mod:`jarvis.tools.builtin.answer` will settle.
    current: str | None = None
    presented: int = 0

    def poll(self, now_ts: str | None = None) -> list[str]:
        """Claim every due desk rung and read it. Returns the ids actually read."""
        con = self.open_db()
        try:
            return self._present(con, now_ts)
        finally:
            con.close()

    def _present(self, con: sqlite3.Connection, now_ts: str | None) -> list[str]:
        out: list[str] = []
        for delivery in rq.due_deliveries(con, now_ts):
            if delivery.channel_kind != self.channel:
                continue
            if not rq.claim_delivery(con, delivery.id, self.actor, lease_s=PRESENT_LEASE_S):
                continue
            req = rq.get_request(con, delivery.request_id)
            if req is None or req.state != "pending":
                # Settled between the sweep and the claim. Marking it presented
                # would be a lie; leaving the claim to lapse costs one lease.
                continue
            self._say(req)
            rq.mark_presented(con, delivery.id, self.actor)
            self.current = req.id
            self.presented += 1
            out.append(req.id)
            # ONE PER POLL. Reading two questions back to back gives the user no
            # gap to answer the first, and `current` can only point at one.
            break
        return out

    def _say(self, req: rq.Request) -> None:
        if self.speak is None:
            return
        for utterance in self.utterances(req):
            self.speak(utterance)

    @staticmethod
    def utterances(req: rq.Request) -> list[Utterance]:
        """The clips, at the tier each one has to be spoken at.

        The intro and the options are EXACT for a plan question — they are
        somebody else's words and an answer key — so they carry the earcon and
        the router refuses to hand them to the conversational voice. The framing
        is free prose and is pinned to the reader anyway, because switching
        voices mid-question is what makes the seam jarring instead of legible.
        """
        pres = req.presentation
        tier: Fidelity = "exact" if pres.get("verbatim") else "faithful"
        out = [
            Utterance(
                text=str(pres["intro"]),
                fidelity=tier,
                track="verbatim",
                earcon="open",
                tag=f"req:{req.id}",
                request_id=req.id,
            )
        ]
        out += [
            Utterance(
                text=f"{item['index']}. {item['label']}",
                fidelity=tier,
                track="verbatim",
                index=int(item["index"]),
                label=str(item["label"]),
                tag=f"req:{req.id}",
                request_id=req.id,
            )
            for item in pres["items"]
        ]
        if prompt := str(pres.get("free_text_prompt") or ""):
            out.append(
                Utterance(
                    text=prompt,
                    fidelity="free",
                    track="verbatim",
                    earcon="close",
                    tag=f"req:{req.id}",
                    request_id=req.id,
                )
            )
        elif out:
            out[-1] = replace(out[-1], earcon="close")
        return out

    def spoken_script(self, req: rq.Request) -> list[str]:
        """The same numbering a channel with no reader would show. Used by tests."""
        try:
            return [line.text for line in script(req.payload)]
        except Exception:  # noqa: BLE001 - only a plan_question has a questions array
            return [u.text for u in self.utterances(req)]
