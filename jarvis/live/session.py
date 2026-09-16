"""One Gemini Live connection: connect, serve, reconnect, and never lose the handle.

WHAT THIS OWNS. The socket, the uplink gate, the resumption handle, the GoAway
reconnect, context-window compression (set in the config, so it is on for every
connection this object ever makes), tool dispatch, and the local drop that
stands in for the interrupt the API does not have.

WHAT THIS DOES NOT OWN. The microphone, the speakers, the mixer, the reader
voice, the database. A source and a sink are CONSTRUCTOR ARGUMENTS, which is
what makes a second session a second object rather than a second global.

THE THREE THINGS THAT ARE EASY TO GET WRONG, AND HOW THEY ARE PREVENTED HERE:

1. A RECONNECT MUST NOT DISCARD THE RESUMPTION HANDLE. In the reference build,
   changing voice forces a reconnect and the reconnect deliberately drops the
   handle, so placing a call in another voice destroys the desk conversation.
   Here the handle is captured from every ``session_resumption_update`` and is
   cleared by exactly one thing: ``close(checkpoint=False)``, which is a caller
   saying "forget this conversation" in as many words.
2. A TOOL MUST NOT DEAFEN THE SESSION. The reference awaits tools inline in the
   receive loop, so a long Claude Code job makes the assistant deaf and mute.
   Here every call becomes its own task and the receive loop keeps running; a
   tool that takes four minutes is indistinguishable from one that takes four
   milliseconds as far as the audio path is concerned.
3. THERE IS NO WAY TO TELL THE MODEL TO STOP. ``AsyncSession`` has no
   ``interrupt()``. So a barge-in is a LOCAL DROP — 100% reliable, costs tokens
   for audio nobody hears — and the synthetic ``activity_start`` is a bonus that
   is armed per profile and never depended on.

THE SEAM THAT MAKES THIS TESTABLE. Nothing here imports ``google.genai`` at
module scope and nothing here calls it directly: a :class:`Connector` hands back
a :class:`LiveTransport`, and :mod:`jarvis.live.fake` provides one that replays a
script. Every reconnect, resumption, GoAway, tool round trip and mid-turn drop
below is therefore exercised in CI with no API key and no socket.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import sqlite3
import threading
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from jarvis.live import BACKOFF_S, DROP_TTL_S, MAX_CONSECUTIVE_FAILURES
from jarvis.live.profiles import SessionProfile, live_connect_config

if TYPE_CHECKING:  # pragma: no cover - typing only
    import numpy as np

__all__ = [
    "ActivityMark",
    "AudioSink",
    "AudioSource",
    "Connector",
    "EventSink",
    "GenaiConnector",
    "GenaiTransport",
    "LiveDown",
    "LiveEvent",
    "LiveSession",
    "LiveTransport",
    "LiveUnavailable",
    "NotConnected",
    "QueuedLiveEvents",
    "QueuedUplink",
    "ServerEvent",
    "SimpleToolRegistry",
    "ToolCall",
    "ToolRegistry",
    "ToolResponse",
    "ToolResult",
    "Usage",
    "record_session_time",
    "record_usage",
    "server_event",
]


class LiveUnavailable(RuntimeError):
    """No credential, or no SDK. Raised at connect, never at import.

    Its own type because "there is no API key on this machine" is an operational
    fact that must not look like a bug, and because CI is exactly that machine.
    """


class LiveDown(RuntimeError):
    """The reconnect budget is exhausted. The conversation is over, say so."""


class NotConnected(RuntimeError):
    """Something was sent to a session that has no socket open."""


class ActivityMark(StrEnum):
    """A client-driven turn boundary, travelling IN the audio stream.

    Not a second channel, because ordering is the whole point: ``activity_start``
    must precede the pre-roll frames of the barge-in that caused it, and two
    queues cannot promise that.
    """

    START = "activity_start"
    END = "activity_end"


@runtime_checkable
class AudioSource(Protocol):
    """Where uplink audio comes from. 16 kHz int16 mono, or a turn boundary.

    ``read`` may be sync or async and may return ``None`` to mean "nothing right
    now"; the same shape as :class:`jarvis.audio.legs.Source`, so the desk leg,
    the phone leg and the synthetic leg all fit without an adapter.
    """

    rate: int
    block: int

    def read(self) -> np.ndarray | bytes | ActivityMark | None: ...


@runtime_checkable
class AudioSink(Protocol):
    """Where downlink audio goes. PCM16 mono at 24 kHz, as bytes.

    Structurally identical to :class:`jarvis.voice.router.PcmSink`, and that is
    deliberate: the Live downlink is one track on the mixer, and it is always the
    ``free`` tier because it paraphrases by construction.
    """

    async def write(self, pcm: bytes, *, tier: str = "free") -> None: ...


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    args: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolResult:
    """What a handler returns, and how the model should be told about it.

    ``scheduling`` is the interesting field. SILENT means "add to context, do not
    trigger generation" — the mechanism that lets the reader voice speak option
    labels while Gemini still KNOWS what was said, so "the second one" resolves.
    INTERRUPT is for the opposite case: Claude Code just asked a question and
    whatever Gemini is saying no longer matters.
    """

    response: Mapping[str, Any]
    scheduling: str = "SILENT"

    def __post_init__(self) -> None:
        if self.scheduling not in ("SILENT", "INTERRUPT", "WHEN_IDLE", "SCHEDULING_UNSPECIFIED"):
            raise ValueError(f"unknown FunctionResponseScheduling {self.scheduling!r}")


@dataclass(frozen=True, slots=True)
class ToolResponse:
    id: str
    name: str
    response: Mapping[str, Any]
    scheduling: str = "SILENT"


@runtime_checkable
class ToolRegistry(Protocol):
    """The tool seam. ``jarvis.tools.ToolRegistry`` will satisfy this as written.

    ``declarations`` is filtered BY PROFILE, which is where the phone's blast
    radius lives: the third-party leg's two tools are the only two that can be
    declared on that connection, so the model has nothing else to call.
    """

    def declarations(self, profile: SessionProfile) -> list[dict[str, Any]]: ...

    async def dispatch(self, call: ToolCall) -> ToolResult: ...


class SimpleToolRegistry:
    """A registry made of a dict. The stand-in until ``jarvis.tools`` lands.

    It is here rather than in the tests because the probe scripts need one too,
    and because a registry small enough to read in one screen is the right thing
    to check the dispatch contract against.
    """

    def __init__(self, handlers: Mapping[str, Callable[..., Any]] | None = None) -> None:
        self._handlers: dict[str, Callable[..., Any]] = dict(handlers or {})
        self._declarations: dict[str, dict[str, Any]] = {}

    def add(
        self,
        name: str,
        handler: Callable[..., Any],
        *,
        description: str = "",
        parameters: dict[str, Any] | None = None,
    ) -> None:
        self._handlers[name] = handler
        self._declarations[name] = {
            "name": name,
            "description": description,
            "parameters": parameters or {"type": "OBJECT", "properties": {}},
        }

    def declarations(self, profile: SessionProfile) -> list[dict[str, Any]]:
        # DEFAULT-DENY: a handler that exists but is not in the profile's tuple is
        # not declared, so the model cannot call it on this leg.
        return [self._declarations[n] for n in profile.tools if n in self._declarations]

    async def dispatch(self, call: ToolCall) -> ToolResult:
        try:
            handler = self._handlers[call.name]
        except KeyError:
            raise KeyError(f"no handler for tool {call.name!r}") from None
        result = handler(**dict(call.args))
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, ToolResult):
            return result
        return ToolResult(response=result if isinstance(result, Mapping) else {"result": result})


@dataclass(frozen=True, slots=True)
class Usage:
    """Token counts, per modality where the server gives them.

    Kept as its own shape rather than passed around as the SDK's object so that
    the ledger, the probe and the tests all read the same three fields whatever
    the SDK renames next.
    """

    total_tokens: int = 0
    prompt_tokens: int = 0
    response_tokens: int = 0
    by_modality: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ServerEvent:
    """One message from the server, normalised.

    The SDK's ``LiveServerMessage`` has nine optional fields of optional fields,
    and every consumer that reads it directly grows its own ``if msg and
    msg.server_content and msg.server_content.model_turn and ...`` ladder. One
    translation function (:func:`server_event`) keeps that in one place, and it
    is what lets the scripted transport produce messages the session cannot tell
    from real ones.
    """

    audio: bytes = b""
    text: str = ""
    input_transcript: str = ""
    output_transcript: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    cancelled_tool_ids: tuple[str, ...] = ()
    resumption_handle: str | None = None
    resumable: bool = False
    go_away_s: float | None = None
    setup_complete: bool = False
    turn_complete: bool = False
    generation_complete: bool = False
    interrupted: bool = False
    usage: Usage | None = None


#: The server stream ended. A distinct OBJECT rather than a flag, because the
#: receive loop already uses ``None`` for "woken by something on this side".
_STREAM_END = ServerEvent()


def _duration_seconds(value: Any) -> float | None:
    """Parse a protobuf duration as the SDK hands it over: usually ``'10s'``."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if text.endswith("s"):
        text = text[:-1]
    try:
        return float(text)
    except ValueError:
        return None


def _usage_from(raw: Any) -> Usage | None:
    if raw is None:
        return None
    by_modality: dict[str, int] = {}
    for attr in ("prompt_tokens_details", "response_tokens_details"):
        for item in getattr(raw, attr, None) or ():
            modality = getattr(item, "modality", None)
            count = getattr(item, "token_count", None) or 0
            if modality is None:
                continue
            side = "in" if attr.startswith("prompt") else "out"
            key = f"{side}:{getattr(modality, 'value', modality)}"
            by_modality[key] = by_modality.get(key, 0) + int(count)
    return Usage(
        total_tokens=int(getattr(raw, "total_token_count", None) or 0),
        prompt_tokens=int(getattr(raw, "prompt_token_count", None) or 0),
        response_tokens=int(getattr(raw, "response_token_count", None) or 0),
        by_modality=by_modality,
    )


def server_event(msg: Any) -> ServerEvent:
    """Translate a ``types.LiveServerMessage`` into a :class:`ServerEvent`.

    A pure function over attributes, so it can be tested against real SDK objects
    with no key and no socket — which is the only honest way to check that this
    layer reads the wire format the SDK actually produces.
    """
    content = getattr(msg, "server_content", None)
    audio = bytearray()
    text_parts: list[str] = []
    if content is not None:
        turn = getattr(content, "model_turn", None)
        for part in (getattr(turn, "parts", None) or ()) if turn is not None else ():
            blob = getattr(part, "inline_data", None)
            if blob is not None and getattr(blob, "data", None):
                audio.extend(blob.data)
            if getattr(part, "text", None):
                text_parts.append(part.text)

    tool_call = getattr(msg, "tool_call", None)
    calls = tuple(
        ToolCall(id=fc.id or "", name=fc.name or "", args=dict(fc.args or {}))
        for fc in (getattr(tool_call, "function_calls", None) or ())
    )
    cancellation = getattr(msg, "tool_call_cancellation", None)
    cancelled = tuple(getattr(cancellation, "ids", None) or ()) if cancellation is not None else ()

    resumption = getattr(msg, "session_resumption_update", None)
    go_away = getattr(msg, "go_away", None)

    def _transcript(name: str) -> str:
        holder = getattr(content, name, None) if content is not None else None
        return str(getattr(holder, "text", "") or "") if holder is not None else ""

    return ServerEvent(
        audio=bytes(audio),
        text="".join(text_parts),
        input_transcript=_transcript("input_transcription"),
        output_transcript=_transcript("output_transcription"),
        tool_calls=calls,
        cancelled_tool_ids=tuple(str(i) for i in cancelled),
        resumption_handle=getattr(resumption, "new_handle", None) if resumption else None,
        resumable=bool(getattr(resumption, "resumable", False)) if resumption else False,
        go_away_s=_duration_seconds(getattr(go_away, "time_left", None)) if go_away else None,
        setup_complete=getattr(msg, "setup_complete", None) is not None,
        turn_complete=bool(getattr(content, "turn_complete", False)) if content else False,
        generation_complete=bool(getattr(content, "generation_complete", False))
        if content
        else False,
        interrupted=bool(getattr(content, "interrupted", False)) if content else False,
        usage=_usage_from(getattr(msg, "usage_metadata", None)),
    )


@runtime_checkable
class LiveTransport(Protocol):
    """One open connection, reduced to what a turn needs.

    ``close`` MUST be idempotent: the serve loop closes in a ``finally`` and
    :meth:`LiveSession.close` closes from the other side, and those two race
    every time the user hangs up while the model is talking.
    """

    async def send_audio(self, data: bytes, *, mime_type: str) -> None: ...

    async def send_activity(self, mark: ActivityMark) -> None: ...

    async def send_text(
        self, text: str, *, role: str = "user", turn_complete: bool = False
    ) -> None: ...

    async def send_tool_responses(self, responses: Sequence[ToolResponse]) -> None: ...

    def receive(self) -> AsyncIterator[ServerEvent]: ...

    async def close(self) -> None: ...


@runtime_checkable
class Connector(Protocol):
    """Opens transports. THE seam: swap this and the session is offline."""

    async def open(
        self,
        profile: SessionProfile,
        *,
        handle: str | None = None,
        declarations: list[dict[str, Any]] | None = None,
    ) -> LiveTransport: ...


class GenaiTransport:
    """The real one: a thin skin over ``google.genai``'s ``AsyncSession``."""

    def __init__(self, session: Any, ctx: Any | None = None) -> None:
        self._session = session
        self._ctx = ctx
        self._closed = False

    async def send_audio(self, data: bytes, *, mime_type: str) -> None:
        from google.genai import types as t

        await self._session.send_realtime_input(audio=t.Blob(data=data, mime_type=mime_type))

    async def send_activity(self, mark: ActivityMark) -> None:
        from google.genai import types as t

        if mark is ActivityMark.START:
            await self._session.send_realtime_input(activity_start=t.ActivityStart())
        else:
            await self._session.send_realtime_input(activity_end=t.ActivityEnd())

    async def send_text(
        self, text: str, *, role: str = "user", turn_complete: bool = False
    ) -> None:
        from google.genai import types as t

        await self._session.send_client_content(
            turns=[t.Content(role=role, parts=[t.Part(text=text)])],
            turn_complete=turn_complete,
        )

    async def send_tool_responses(self, responses: Sequence[ToolResponse]) -> None:
        from google.genai import types as t

        await self._session.send_tool_response(
            function_responses=[
                t.FunctionResponse(
                    id=r.id,
                    name=r.name,
                    response=dict(r.response),
                    scheduling=t.FunctionResponseScheduling(r.scheduling),
                )
                for r in responses
            ]
        )

    async def receive(self) -> AsyncIterator[ServerEvent]:
        async for msg in self._session.receive():
            yield server_event(msg)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._ctx is not None:
            await self._ctx.__aexit__(None, None, None)
        else:  # pragma: no cover - only when a session was handed in directly
            await self._session.close()


@dataclass
class GenaiConnector:
    """Opens real sessions. Takes a credential; never goes looking for one.

    The key comes from the OS keyring at the application layer and is passed in.
    This class does not read the environment, because a process that silently
    picks up ``GEMINI_API_KEY`` from a shell is a process that bills somebody
    without being asked, and because the house rule is that secrets live in the
    keyring and nowhere else.
    """

    api_key: str | None = None
    client: Any | None = None

    async def open(
        self,
        profile: SessionProfile,
        *,
        handle: str | None = None,
        declarations: list[dict[str, Any]] | None = None,
    ) -> LiveTransport:
        client = self.client
        if client is None:
            if not self.api_key:
                raise LiveUnavailable(
                    "no Gemini credential: pass GenaiConnector(api_key=...) from the keyring"
                )
            try:
                from google import genai
            except ImportError as exc:  # pragma: no cover - the extra is installed in CI
                raise LiveUnavailable(
                    "google-genai is not installed: pip install '.[live]'"
                ) from exc
            client = genai.Client(api_key=self.api_key)
        config = live_connect_config(profile, handle=handle, declarations=declarations)
        ctx = client.aio.live.connect(model=profile.model, config=config)
        session = await ctx.__aenter__()
        return GenaiTransport(session, ctx)


@dataclass(frozen=True, slots=True)
class LiveEvent:
    kind: str
    at: float
    detail: Mapping[str, Any] = field(default_factory=dict)


EventSink = Callable[[LiveEvent], None]


class QueuedLiveEvents:
    """Buffer session events; publish them from something holding a connection.

    Same shape as :class:`jarvis.audio.graph.QueuedEventSink` and for the same
    reason: ``jarvis.bus.publish`` opens a write transaction, and a BEGIN
    IMMEDIATE that blocks on another writer inside the receive loop is a gap in
    the conversation. Bounded, oldest dropped, and the drop is counted — a log
    with a hole it knows about is honest; a stalled session is not.
    """

    def __init__(self, *, limit: int = 2048) -> None:
        self._events: deque[LiveEvent] = deque(maxlen=limit)
        self._dropped = 0
        self._lock = threading.Lock()

    def __call__(self, event: LiveEvent) -> None:
        with self._lock:
            if len(self._events) == self._events.maxlen:
                self._dropped += 1
            self._events.append(event)

    @property
    def dropped(self) -> int:
        return self._dropped

    def pending(self) -> list[LiveEvent]:
        with self._lock:
            return list(self._events)

    def drain(
        self,
        con: sqlite3.Connection,
        actor: str,
        *,
        job_id: str | None = None,
        redactor: Any | None = None,
    ) -> int:
        """Publish everything buffered. Takes an open connection, per house rule 3.

        ``redactor`` is passed straight through to :func:`jarvis.bus.publish`.
        These payloads carry transcripts — what the user said and what Jarvis
        said back — into an append-only log that is kept forever, so the process
        that owns the keyring is expected to hand its :class:`jarvis.bus.Redactor`
        in here. The bus cannot know what a secret looks like; this is the call
        site where somebody does.
        """
        from jarvis.bus import publish

        with self._lock:
            batch = list(self._events)
            self._events.clear()
        for ev in batch:
            publish(
                con,
                f"live.{ev.kind}",
                actor,
                {"at": ev.at, **dict(ev.detail)},
                job_id=job_id,
                redactor=redactor,
            )
        return len(batch)


def record_usage(
    con: sqlite3.Connection,
    usage: Usage,
    *,
    job_id: str | None = None,
    note: str | None = None,
) -> str:
    """Put one usage report in the spend ledger, as tokens and nothing else.

    ``usd_equiv`` stays NULL on purpose: the only Gemini Live price this project
    has is a third-party figure whose primary source was egress-blocked, and a
    ledger that quietly prices an unverified meter is worse than one that says
    "unpriced" out loud — which is exactly what ``jarvis.ledger`` does with it.
    """
    from jarvis import ledger

    return ledger.record(
        con,
        "gemini",
        "tokens",
        float(usage.total_tokens),
        estimated=False,
        job_id=job_id,
        note=note,
    )


def record_session_time(
    con: sqlite3.Connection,
    seconds: float,
    *,
    job_id: str | None = None,
    note: str | None = None,
) -> str:
    """Connected wall time, the other meter. Also unpriced by default."""
    from jarvis import ledger

    return ledger.record(
        con, "gemini", "gemini_sec", float(seconds), estimated=True, job_id=job_id, note=note
    )


class QueuedUplink:
    """The push side of the turn controller, read by the session's pull loop.

    :class:`jarvis.audio.turn.TurnController` runs in the audio thread and calls
    ``activity_start`` / ``send`` / ``activity_end`` synchronously. The session
    lives in the event loop and pulls. This is the one adapter between them, and
    it is deliberately a bounded ring: if the session stalls, the OLDEST audio is
    dropped rather than the newest, because the newest is the word the user is
    saying right now.

    There is NO mute here, by design — the gate lives on the session, and the
    microphone is never gated off at all.
    """

    def __init__(self, *, rate: int = 16_000, block: int = 320, limit: int = 256) -> None:
        self.rate = rate
        self.block = block
        self._q: deque[Any] = deque(maxlen=limit)
        self._lock = threading.Lock()
        self.dropped = 0

    def _put(self, item: Any) -> None:
        with self._lock:
            if len(self._q) == self._q.maxlen:
                self.dropped += 1
            self._q.append(item)

    def activity_start(self) -> None:
        self._put(ActivityMark.START)

    def activity_end(self) -> None:
        self._put(ActivityMark.END)

    def send(self, pcm: Any) -> None:
        self._put(pcm)

    def read(self) -> Any:
        with self._lock:
            return self._q.popleft() if self._q else None

    def __len__(self) -> int:
        with self._lock:
            return len(self._q)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _as_bytes(chunk: Any) -> bytes:
    if isinstance(chunk, (bytes, bytearray, memoryview)):
        return bytes(chunk)
    # numpy is never imported here: an int16 array already knows how to
    # serialise itself, and asking it to keeps this whole package importable on
    # a machine with no audio stack at all.
    return bytes(chunk.tobytes())


class LiveSession:
    """One connection, one profile, one conversation. Instantiate as many as the lease allows."""

    def __init__(
        self,
        profile: SessionProfile,
        source: AudioSource | None,
        sink: AudioSink | None,
        *,
        connector: Connector,
        tools: ToolRegistry | None = None,
        grant: Any | None = None,
        on_event: EventSink | None = None,
        handle: str | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        idle_s: float | None = None,
        backoff: Sequence[float] = BACKOFF_S,
        max_failures: int = MAX_CONSECUTIVE_FAILURES,
        reconnect: bool = True,
        drop_ttl_s: float = DROP_TTL_S,
    ) -> None:
        self.profile = profile
        self._source = source
        self._sink = sink
        self._connector = connector
        self._tools = tools
        self._grant = grant
        self._on_event = on_event
        self._handle = handle
        self._clock = clock
        self._sleep = sleep
        self._backoff = tuple(backoff) or (0.0,)
        self._max_failures = max_failures
        self._reconnect = reconnect
        self._drop_ttl_s = drop_ttl_s
        # A source that never blocks would spin the loop; a source that blocks
        # needs no sleep at all. Both exist (the synthetic leg and the queued
        # uplink), so the idle wait is derived from the block size and can be
        # set to zero by a test that drives the clock itself.
        if idle_s is not None:
            self._idle_s = idle_s
        elif source is not None and getattr(source, "rate", 0):
            self._idle_s = source.block / source.rate
        else:
            self._idle_s = 0.02

        self._transport: LiveTransport | None = None
        self._closed = asyncio.Event()
        self._reconnect_now = asyncio.Event()
        # Set by anything that wants the receive loop to stop waiting on a
        # server that may say nothing for minutes.
        self._wake = asyncio.Event()
        self._reconnect_reason = ""
        self._uplink_gated = False
        self._gate_reason = ""
        self._dropping = False
        self._drop_at = 0.0
        self._goaway = False
        self._pending: dict[str, asyncio.Task[None]] = {}
        self._started_at = 0.0

        self.connects = 0
        self.resumes = 0
        self.reconnects = 0
        self.go_aways = 0
        self.frames_sent = 0
        self.frames_gated = 0
        self.audio_bytes_out = 0
        self.audio_bytes_dropped = 0
        self.tool_calls = 0
        self.uplink_errors = 0
        self.connected_s = 0.0
        self.last_usage: Usage | None = None

    # ── state a caller can read ──────────────────────────────────────────

    @property
    def resume_handle(self) -> str | None:
        """The handle a reconnect would replay. Never logged, never in a payload."""
        return self._handle

    @property
    def connected(self) -> bool:
        return self._transport is not None

    @property
    def uplink_gated(self) -> bool:
        return self._uplink_gated

    @property
    def dropping(self) -> bool:
        return self._dropping

    # ── the loop ─────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Connect, serve, reconnect until closed or out of budget."""
        watcher: asyncio.Task[None] | None = None
        if self._grant is not None and hasattr(self._grant, "revoked"):
            watcher = asyncio.create_task(self._watch_revocation())
        failures = 0
        try:
            while not self._closed.is_set():
                try:
                    transport = await self._connector.open(
                        self.profile,
                        handle=self._handle,
                        declarations=self._declarations(),
                    )
                except LiveUnavailable:
                    # No credential is not a transient fault and retrying it is a
                    # busy loop that says nothing useful. Let it out.
                    raise
                except Exception as exc:
                    failures += 1
                    self._emit(
                        "connect_failed",
                        {
                            "error": type(exc).__name__,
                            "attempt": failures,
                            "resumed": self._handle is not None,
                        },
                    )
                    if failures >= self._max_failures:
                        raise LiveDown(
                            f"{failures} consecutive connect failures "
                            f"for profile {self.profile.name!r}"
                        ) from exc
                    await self._sleep(self._backoff_for(failures))
                    continue

                self._transport = transport
                self.connects += 1
                resumed = self._handle is not None
                if resumed:
                    self.resumes += 1
                self._started_at = self._clock()
                self._emit(
                    "connected",
                    {
                        "profile": self.profile.name,
                        "model": self.profile.model,
                        "voice": self.profile.voice,
                        "resumed": resumed,
                        "connects": self.connects,
                    },
                )
                reason, saw_events = await self._serve(transport)
                self.connected_s += max(0.0, self._clock() - self._started_at)
                self._transport = None
                self._emit(
                    "disconnected",
                    {"reason": reason, "handle_present": self._handle is not None},
                )
                if self._closed.is_set() or not self._reconnect or reason == "closed":
                    break
                self.reconnects += 1
                if reason == "go_away":
                    # The server told us in advance, the handle is current, and
                    # the user is mid-conversation: reconnect at once.
                    failures = 0
                    continue
                # A connection that produced nothing before dying is a failing
                # connection, not a long conversation that ended; back off. So is
                # one whose UPLINK died — a chatty downlink is no evidence that
                # this session can still be heard, and without this a source that
                # raises on every frame reconnects forever.
                failures = 0 if (saw_events and reason != "uplink_error") else failures + 1
                if failures >= self._max_failures:
                    raise LiveDown(
                        f"{failures} consecutive empty connections "
                        f"for profile {self.profile.name!r}"
                    )
                await self._sleep(self._backoff_for(max(1, failures)))
        finally:
            if watcher is not None:
                watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watcher
            await self._cancel_pending()

    def _backoff_for(self, attempt: int) -> float:
        return self._backoff[min(attempt, len(self._backoff)) - 1]

    def _declarations(self) -> list[dict[str, Any]] | None:
        if self._tools is None:
            return None
        return self._tools.declarations(self.profile) or None

    async def _watch_revocation(self) -> None:
        """A revoked lease checkpoints and closes. That IS the single-flight rule."""
        await self._grant.revoked.wait()
        reason = getattr(self._grant, "revoke_reason", "") or "lease revoked"
        self._emit("revoked", {"holder": getattr(self._grant, "holder", ""), "reason": reason})
        await self.close(checkpoint=True)

    async def _serve(self, transport: LiveTransport) -> tuple[str, bool]:
        # A request made while there was no socket is satisfied by THIS one: the
        # profile already carries the new voice, so opening is the reconnect.
        self._reconnect_now.clear()
        self._wake.clear()
        pump = asyncio.create_task(self._pump(transport))
        stream = transport.receive()
        saw_events = False
        reason = "stream_closed"
        try:
            while True:
                if self._closed.is_set():
                    reason = "closed"
                    break
                if self._reconnect_now.is_set():
                    self._reconnect_now.clear()
                    reason = self._reconnect_reason or "reconnect"
                    break
                event = await self._next_event(stream)
                if event is None:
                    # WOKEN, NOT SERVED. A voice change, a close or a dead uplink
                    # must not wait for the model's next word: a session between
                    # turns is silent, and "reconnect after the server says
                    # something" is indistinguishable from "never" at the desk.
                    continue
                if event is _STREAM_END:
                    reason = "go_away" if self._goaway else "stream_closed"
                    break
                saw_events = True
                await self._on_server_event(event, transport)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A drop mid-turn. The handle survives this: that is the whole point
            # of capturing it on arrival rather than at a clean shutdown.
            reason = "error"
            self._emit("stream_error", {"error": type(exc).__name__, "detail": str(exc)[:200]})
        finally:
            self._goaway = False
            pump.cancel()
            # A pump that died of its own accord already said so and asked for a
            # reconnect; re-raising it here instead would end the conversation.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pump
            with contextlib.suppress(Exception):
                await transport.close()
        return reason, saw_events

    async def _next_event(self, stream: AsyncIterator[ServerEvent]) -> ServerEvent | None:
        """The next server message, or ``None`` when something else wants the loop.

        ``_wake`` is the other half of the receive loop: without it the only
        thing that can end a connection is the server, and every reconnect this
        object decides on for itself would wait for a model that has stopped
        talking.
        """
        nxt = asyncio.ensure_future(anext(stream))
        woken = asyncio.ensure_future(self._wake.wait())
        try:
            await asyncio.wait({nxt, woken}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            woken.cancel()
        if not nxt.done():
            nxt.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await nxt
            self._wake.clear()
            return None
        try:
            return nxt.result()
        except StopAsyncIteration:
            return _STREAM_END

    async def _pump(self, transport: LiveTransport) -> None:
        """Source -> socket. Gated on activity, never muted."""
        if self._source is None:
            await self._closed.wait()
            return
        try:
            while True:
                item = await _maybe_await(self._source.read())
                if item is None:
                    await self._sleep(self._idle_s)
                    continue
                if isinstance(item, ActivityMark):
                    if self._uplink_gated:
                        continue
                    await transport.send_activity(item)
                    self._emit("activity", {"mark": item.value})
                    continue
                if self._uplink_gated:
                    # READ AND DISCARD. The microphone is never gated off; the
                    # gate decides only what Gemini is TOLD about — which is what
                    # keeps the wake word, the kill phrase and the spotter alive
                    # while the reader voice is speaking.
                    self.frames_gated += 1
                    continue
                data = _as_bytes(item)
                await transport.send_audio(data, mime_type=self.profile.input_mime)
                self.frames_sent += 1
                self.audio_bytes_out += len(data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # THE SEND SIDE OFTEN NOTICES A DEAD SOCKET FIRST — a write fails
            # while receive() sits there waiting for a message that will never
            # come. A pump that just ended would leave a session that looks
            # connected, hears nothing and never says why, so this is loud and
            # it asks for the same reconnect a receive-side death would.
            self.uplink_errors += 1
            self._emit("uplink_error", {"error": type(exc).__name__, "detail": str(exc)[:200]})
            self._request_reconnect("uplink_error")

    async def _on_server_event(self, event: ServerEvent, transport: LiveTransport) -> None:
        if event.setup_complete:
            self._emit("setup_complete", {"profile": self.profile.name})

        if event.resumption_handle and event.resumable:
            # Captured on arrival. Never cleared by a reconnect, a GoAway or a
            # voice change — only by close(checkpoint=False).
            self._handle = event.resumption_handle
            self._emit("handle", {"present": True, "resumable": True})

        if event.go_away_s is not None:
            self._goaway = True
            self.go_aways += 1
            self._emit("go_away", {"time_left_s": event.go_away_s})

        if event.interrupted:
            self._release_drop("server_interrupt")
            self._emit("interrupted", {})

        if event.audio:
            if self._should_play():
                if self._sink is not None:
                    await _maybe_await(self._sink.write(event.audio, tier="free"))
            else:
                self.audio_bytes_dropped += len(event.audio)

        if event.text:
            self._emit("text", {"chars": len(event.text)})
        if event.output_transcript:
            # The self-speech veto reads this: a detector hit on a phrase Jarvis
            # just said aloud must be discarded, or reading a GitHub issue
            # containing the kill phrase halts the system.
            self._emit("output_transcript", {"text": event.output_transcript})
        if event.input_transcript:
            self._emit("input_transcript", {"text": event.input_transcript})

        if event.cancelled_tool_ids:
            await self._cancel_tools(event.cancelled_tool_ids)

        for call in event.tool_calls:
            self.tool_calls += 1
            self._start_tool(call, transport)

        if event.usage is not None:
            self.last_usage = event.usage
            self._emit(
                "usage",
                {
                    "total_tokens": event.usage.total_tokens,
                    "prompt_tokens": event.usage.prompt_tokens,
                    "response_tokens": event.usage.response_tokens,
                    **{f"modality.{k}": v for k, v in event.usage.by_modality.items()},
                },
            )

        if event.turn_complete or event.generation_complete:
            self._release_drop("turn_complete")
            self._emit(
                "turn_complete" if event.turn_complete else "generation_complete",
                {"goaway_pending": self._goaway},
            )
            if self._goaway and event.turn_complete:
                # Reconnect at a turn boundary: the least disruptive moment, and
                # it means we do not depend on the server actually closing the
                # socket after its warning.
                self._request_reconnect("go_away")

    # ── turn control ─────────────────────────────────────────────────────

    async def activity_start(self) -> None:
        await self._require_transport().send_activity(ActivityMark.START)

    async def activity_end(self) -> None:
        await self._require_transport().send_activity(ActivityMark.END)

    def gate_uplink(self, reason: str = "") -> None:
        """Stop telling Gemini what the microphone hears. It keeps hearing."""
        self._uplink_gated = True
        self._gate_reason = reason
        self._emit("uplink_gated", {"reason": reason})

    def ungate_uplink(self) -> None:
        self._uplink_gated = False
        self._emit("uplink_ungated", {"reason": self._gate_reason})
        self._gate_reason = ""

    async def drop_output(self, reason: str = "barge_in") -> None:
        """The barge-in. Local, reliable, and the only lever that actually exists.

        There is no ``interrupt()`` on the Live session, so downlink audio is
        discarded HERE until the next turn boundary. The synthetic
        ``activity_start`` is sent as well only when the profile arms it, and
        nothing above this line depends on the server honouring it.
        """
        self._dropping = True
        self._drop_at = self._clock()
        self._emit("drop", {"reason": reason, "synthetic": self.profile.synthetic_barge_in})
        if self.profile.synthetic_barge_in and self._transport is not None:
            with contextlib.suppress(Exception):
                await self._transport.send_activity(ActivityMark.START)

    def _release_drop(self, why: str) -> None:
        if self._dropping:
            self._dropping = False
            self._emit("drop_released", {"why": why})

    def _should_play(self) -> bool:
        if not self._dropping:
            return True
        if self._clock() - self._drop_at >= self._drop_ttl_s:
            # A boundary that never came. Without this expiry one missed
            # turn_complete would leave the assistant mute for the rest of the
            # conversation, which is a far worse bug than a stale half second.
            self._release_drop("ttl")
            return True
        return False

    # ── sending ──────────────────────────────────────────────────────────

    def _require_transport(self) -> LiveTransport:
        if self._transport is None:
            raise NotConnected(f"session {self.profile.name!r} has no open connection")
        return self._transport

    async def prefill(self, text: str, *, role: str = "user", turn_complete: bool = False) -> None:
        """Put text in the model's context without streaming audio at it.

        ``send_client_content`` is a CONTEXT PREFILL mechanism whose own docstring
        warns that interleaving it with ``send_realtime_input`` "can lead to
        unexpected results", so this refuses to run while the uplink is open.
        """
        if not self._uplink_gated and not turn_complete:
            raise RuntimeError(
                "prefill while the uplink is open interleaves client content with realtime "
                "input; gate the uplink first"
            )
        await self._require_transport().send_text(text, role=role, turn_complete=turn_complete)
        self._emit("prefill", {"chars": len(text), "role": role, "turn_complete": turn_complete})

    async def say(self, utt: Any) -> None:
        """Speak FREE-tier text in the Live voice. Exact text is refused.

        This is :class:`jarvis.voice.router.LiveSink`. The import is inside the
        call so ``jarvis.live`` does not depend on the voice package to load —
        and so the exception the router raises and the exception this raises are
        the same type, which matters because they are the same bug.
        """
        from jarvis.voice.router import FidelityViolation

        if getattr(utt, "fidelity", "free") == "exact":
            raise FidelityViolation(
                f"exact-tier text {getattr(utt, 'text', '')!r} was handed to the Live voice, "
                "which paraphrases everything it says"
            )
        await self._require_transport().send_text(
            getattr(utt, "text", str(utt)), role="user", turn_complete=True
        )

    async def send_tool_response(
        self,
        call_id: str,
        result: Mapping[str, Any],
        *,
        name: str = "",
        scheduling: str = "SILENT",
    ) -> None:
        await self._require_transport().send_tool_responses(
            [ToolResponse(id=call_id, name=name, response=dict(result), scheduling=scheduling)]
        )
        self._emit("tool_response", {"id": call_id, "name": name, "scheduling": scheduling})

    async def announce_options(
        self,
        call: ToolCall,
        options: Sequence[Mapping[str, Any]] | Sequence[str],
        *,
        question: str = "",
    ) -> None:
        """Tell Gemini what the READER just read aloud, without asking for a turn.

        SILENT means "add the result to the conversation context, do not
        interrupt or trigger generation" — the mechanism that lets the reader
        speak the labels while Gemini still knows them, so "the second one"
        resolves against something. Whether 3.8 honours it is UNVERIFIED, so
        until a profile says it is verified the documented fallback goes out
        first: the option table as a client-content prefill, BEFORE the uplink is
        ungated. Both paths are here; ``tools/probe_live.py`` decides which one
        is doing the work.
        """
        if not self._uplink_gated:
            raise RuntimeError(
                "announce_options must run while the uplink is gated: the reader is speaking "
                "and Gemini must not hear it as the user"
            )
        # THE NUMBERING IS GENERATED HERE AND NOWHERE ELSE. It is what the reader
        # spoke and what the answer path resolves an index against, so a caller
        # carrying its own "n" does not get to disagree with the spoken ordinal.
        table = [{"label": o} if isinstance(o, str) else dict(o) for o in options]
        for i, row in enumerate(table, start=1):
            row["n"] = i
        if not self.profile.silent_scheduling_verified:
            lines = [f"{row['n']}. {row.get('label', '')}" for row in table]
            prefix = f"{question}\n" if question else ""
            await self.prefill(
                "[read aloud verbatim to the user by the reader voice; do not repeat]\n"
                + prefix
                + "\n".join(lines),
                role="user",
            )
        await self.send_tool_response(
            call.id,
            {
                "already_read_aloud": True,
                "do_not_repeat": True,
                "options": table,
                **({"question": question} if question else {}),
            },
            name=call.name,
            scheduling="SILENT",
        )

    async def change_voice(self, voice: str) -> None:
        """Change the conversational voice, keeping the conversation.

        Voice is set at connect time and the API has no way to change it on a
        live socket, so this reconnects — WITH the resumption handle. The
        reference build does the same reconnect and throws the handle away,
        which is why placing a call there destroys the desk conversation.
        """
        if voice == self.profile.voice:
            return
        self.profile = self.profile.with_voice(voice)
        self._emit("voice_change", {"voice": voice, "handle_present": self._handle is not None})
        self._request_reconnect("voice_change")

    def _request_reconnect(self, reason: str) -> None:
        self._reconnect_reason = reason
        self._reconnect_now.set()
        self._wake.set()

    # ── tools ────────────────────────────────────────────────────────────

    def _start_tool(self, call: ToolCall, transport: LiveTransport) -> None:
        if self._tools is None:
            self._emit("tool_unroutable", {"id": call.id, "name": call.name})
            return
        if not self.profile.allows(call.name):
            # DEFAULT-DENY, checked again at dispatch: the declarations were
            # already filtered, so a call for an undeclared tool means either a
            # stale session or a model improvising, and neither should run.
            self._emit("tool_denied", {"id": call.id, "name": call.name})
            task = asyncio.create_task(
                self._respond(
                    transport, call, ToolResult({"error": "tool_not_available"}, "WHEN_IDLE")
                )
            )
            self._pending[call.id] = task
            task.add_done_callback(lambda t, cid=call.id: self._pending.pop(cid, None))
            return
        task = asyncio.create_task(self._run_tool(call, transport))
        self._pending[call.id] = task
        task.add_done_callback(lambda t, cid=call.id: self._pending.pop(cid, None))

    async def _run_tool(self, call: ToolCall, transport: LiveTransport) -> None:
        self._emit("tool_call", {"id": call.id, "name": call.name})
        assert self._tools is not None
        try:
            result = await self._tools.dispatch(call)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # WHEN_IDLE, not INTERRUPT: the model should learn that the tool
            # failed, but not by cutting the user off mid-sentence to say so.
            result = ToolResult(
                {"error": type(exc).__name__, "detail": str(exc)[:200]}, "WHEN_IDLE"
            )
            self._emit(
                "tool_failed", {"id": call.id, "name": call.name, "error": type(exc).__name__}
            )
        await self._respond(transport, call, result)

    async def _respond(self, transport: LiveTransport, call: ToolCall, result: ToolResult) -> None:
        with contextlib.suppress(Exception):
            await transport.send_tool_responses(
                [
                    ToolResponse(
                        id=call.id,
                        name=call.name,
                        response=dict(result.response),
                        scheduling=result.scheduling,
                    )
                ]
            )
        self._emit(
            "tool_result", {"id": call.id, "name": call.name, "scheduling": result.scheduling}
        )

    async def _cancel_tools(self, ids: Sequence[str]) -> None:
        for call_id in ids:
            task = self._pending.pop(call_id, None)
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                self._emit("tool_cancelled", {"id": call_id})

    async def _cancel_pending(self) -> None:
        for call_id in list(self._pending):
            task = self._pending.pop(call_id, None)
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    # ── shutdown ─────────────────────────────────────────────────────────

    async def close(self, *, checkpoint: bool = True) -> None:
        """Close the socket. KEEP the handle unless told in as many words not to.

        ``checkpoint=True`` is what makes the lease's hand-off lossless: the desk
        session closes, the call happens, and reopening replays the handle into
        the same conversation. ``checkpoint=False`` is the only thing in this
        file that forgets a conversation, and it says so.
        """
        self._closed.set()
        self._wake.set()
        if not checkpoint:
            self._handle = None
        self._emit("closed", {"checkpoint": checkpoint, "handle_present": self._handle is not None})
        transport, self._transport = self._transport, None
        if transport is not None:
            with contextlib.suppress(Exception):
                await transport.close()
        await self._cancel_pending()

    # ── events ───────────────────────────────────────────────────────────

    def _emit(self, kind: str, detail: Mapping[str, Any]) -> None:
        if self._on_event is None:
            return
        self._on_event(LiveEvent(kind=kind, at=self._clock(), detail=dict(detail)))
