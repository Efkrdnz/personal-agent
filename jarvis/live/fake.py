"""A Gemini Live connection made of a list. No key, no socket, no clock.

THIS IS NOT A TEST FIXTURE THAT ESCAPED INTO THE PACKAGE. There is no API key on
the machine this project is developed on and there will never be one in CI, so
without a scripted transport the reconnect logic, the resumption-handle replay,
the GoAway path and the tool round trip would all ship untested — which is to
say, would ship broken, because every one of them is a path that only runs when
something has already gone wrong.

The script is a list of :class:`~jarvis.live.session.ServerEvent` objects and
three directives:

``Drop``    the socket dies mid-turn. The handle must survive this.
``Hangup``  the server closes cleanly. A reconnect follows.
``Pause``   wait on an event the test owns, so assertions can land mid-stream.

When a script runs out, the connection simply goes quiet and stays open — which
is what a real one does between turns — until someone closes it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from jarvis.live.profiles import SessionProfile
from jarvis.live.session import (
    ActivityMark,
    ServerEvent,
    ToolCall,
    ToolResponse,
    Usage,
)

__all__ = [
    "BytesSink",
    "Drop",
    "FrameSource",
    "Hangup",
    "Pause",
    "ScriptExhausted",
    "ScriptedConnector",
    "ScriptedTransport",
    "audio",
    "go_away",
    "handle",
    "text",
    "tool_call",
    "turn_complete",
    "usage",
]


class ScriptExhausted(RuntimeError):
    """The connector was asked for one more connection than the test wrote."""


@dataclass(frozen=True)
class Drop:
    """The connection dies mid-turn, the way a real one does: without warning."""

    error: str = "connection reset by peer"


@dataclass(frozen=True)
class Hangup:
    """The server closes the stream cleanly."""


@dataclass
class Pause:
    """Block the stream until the test says go."""

    event: asyncio.Event = field(default_factory=asyncio.Event)


Step = ServerEvent | Drop | Hangup | Pause


def audio(payload: bytes | int = 960) -> ServerEvent:
    """One downlink chunk. An int means "this many bytes of silence"."""
    data = bytes(payload) if isinstance(payload, int) else payload
    return ServerEvent(audio=data)


def text(value: str) -> ServerEvent:
    return ServerEvent(text=value)


def handle(value: str, *, resumable: bool = True) -> ServerEvent:
    return ServerEvent(resumption_handle=value, resumable=resumable)


def go_away(seconds: float = 10.0) -> ServerEvent:
    return ServerEvent(go_away_s=seconds)


def tool_call(call_id: str, name: str, **args: Any) -> ServerEvent:
    return ServerEvent(tool_calls=(ToolCall(id=call_id, name=name, args=args),))


def turn_complete() -> ServerEvent:
    return ServerEvent(turn_complete=True)


def usage(
    total: int = 100, *, prompt: int = 60, response: int = 40, **modality: int
) -> ServerEvent:
    return ServerEvent(
        usage=Usage(
            total_tokens=total,
            prompt_tokens=prompt,
            response_tokens=response,
            by_modality=dict(modality),
        )
    )


class ScriptedTransport:
    """One connection's worth of script, plus everything the session sent back."""

    def __init__(self, script: Sequence[Step] = ()) -> None:
        self._script: list[Step] = list(script)
        self.sent_audio: list[bytes] = []
        self.sent_mime: list[str] = []
        self.marks: list[ActivityMark] = []
        self.sent_text: list[tuple[str, str, bool]] = []
        self.tool_responses: list[ToolResponse] = []
        self.closed = False
        self.script_done = asyncio.Event()
        self._closing = asyncio.Event()

    async def send_audio(self, data: bytes, *, mime_type: str) -> None:
        self.sent_audio.append(bytes(data))
        self.sent_mime.append(mime_type)

    async def send_activity(self, mark: ActivityMark) -> None:
        self.marks.append(mark)

    async def send_text(
        self, text: str, *, role: str = "user", turn_complete: bool = False
    ) -> None:
        self.sent_text.append((text, role, turn_complete))

    async def send_tool_responses(self, responses: Sequence[ToolResponse]) -> None:
        self.tool_responses.extend(responses)

    async def receive(self) -> AsyncIterator[ServerEvent]:
        for step in self._script:
            # Yield the loop between steps so the uplink pump and any tool task
            # actually run; this is scheduling, not waiting, and costs no time.
            await asyncio.sleep(0)
            if isinstance(step, Drop):
                self.script_done.set()
                raise ConnectionResetError(step.error)
            if isinstance(step, Hangup):
                self.script_done.set()
                return
            if isinstance(step, Pause):
                # Either the test says go, or the session hangs up while we are
                # paused — a scripted transport that could only be unblocked by
                # the test would turn a forgotten `event.set()` into a hung
                # suite instead of a failed assertion.
                if await self._race(step.event) is False:
                    return
                continue
            yield step
        self.script_done.set()
        # Out of script: a quiet, open connection. Real ones spend most of their
        # life here, and a session that treated silence as a disconnect would
        # reconnect every time the user stopped talking.
        await self._closing.wait()

    async def _race(self, event: asyncio.Event) -> bool:
        """True when ``event`` fired, False when the connection closed first."""
        waiters = [
            asyncio.ensure_future(event.wait()),
            asyncio.ensure_future(self._closing.wait()),
        ]
        done, pending = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            task.exception()
        return event.is_set()

    async def close(self) -> None:
        self.closed = True
        self._closing.set()
        self.script_done.set()

    @property
    def audio_bytes(self) -> int:
        return sum(len(c) for c in self.sent_audio)


@dataclass
class ScriptedConnector:
    """Hands out one transport per script, and remembers what it was asked for.

    ``handles`` is the assertion that matters most in this file: it is the list
    of resumption handles the session presented, in order, so "a reconnect
    replays the handle" and "a mid-turn drop does not lose it" are one-line
    tests rather than arguments.
    """

    scripts: list[Sequence[Step]] = field(default_factory=list)
    fail_times: int = 0
    handles: list[str | None] = field(default_factory=list)
    voices: list[str] = field(default_factory=list)
    declarations: list[list[dict[str, Any]] | None] = field(default_factory=list)
    opened: list[ScriptedTransport] = field(default_factory=list)
    repeat_last: bool = False

    async def open(
        self,
        profile: SessionProfile,
        *,
        handle: str | None = None,
        declarations: list[dict[str, Any]] | None = None,
    ) -> ScriptedTransport:
        if self.fail_times > 0:
            self.fail_times -= 1
            self.handles.append(handle)
            raise ConnectionRefusedError("scripted connect failure")
        if self.scripts:
            script = (
                self.scripts[0]
                if (self.repeat_last and len(self.scripts) == 1)
                else self.scripts.pop(0)
            )
        elif self.repeat_last:
            script = ()
        else:
            raise ScriptExhausted("the session connected more times than the script allows")
        self.handles.append(handle)
        self.voices.append(profile.voice)
        self.declarations.append(declarations)
        transport = ScriptedTransport(script)
        self.opened.append(transport)
        return transport

    @property
    def last(self) -> ScriptedTransport:
        return self.opened[-1]


class BytesSink:
    """Everything the session played, kept in memory."""

    def __init__(self) -> None:
        self.chunks: list[bytes] = []
        self.tiers: list[str] = []

    async def write(self, pcm: bytes, *, tier: str = "free") -> None:
        self.chunks.append(bytes(pcm))
        self.tiers.append(tier)

    @property
    def nbytes(self) -> int:
        return sum(len(c) for c in self.chunks)

    def audio(self) -> bytes:
        return b"".join(self.chunks)


class FrameSource:
    """A list of frames and turn marks, then silence.

    Returning ``None`` forever rather than ending is deliberate: a source that
    stops existing is not a thing the audio graph can do, and a session that
    treated exhaustion as a disconnect would be testing something no leg does.
    """

    def __init__(
        self,
        items: Sequence[Any] = (),
        *,
        rate: int = 16_000,
        block: int = 320,
    ) -> None:
        self.rate = rate
        self.block = block
        self._items = list(items)
        self.reads = 0

    def read(self) -> Any:
        self.reads += 1
        return self._items.pop(0) if self._items else None

    def feed(self, item: Any) -> None:
        self._items.append(item)

    def __len__(self) -> int:
        return len(self._items)
