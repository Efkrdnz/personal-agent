"""One registry, and the rule that long tools never block the conversation.

THE ONE MECHANISM. The reference build has two near-identical loaders — actions
and plugins — with subtly different calling conventions: one passes parameters
positionally and the other by keyword, one injects ``speak`` and the other does
not. That duality produces a whole class of "why didn't my tool get speak" bugs
and buys nothing, so there is one :class:`Tool` here and one way to call it.

THE RULE THAT MATTERS. In the reference, tools are awaited INLINE inside the
single ``session.receive()`` loop, so a tool that takes four minutes makes the
assistant deaf and mute for four minutes — and driving Claude Code is a
long-running, streaming, interactive job, not a fast call. So a tool declares
whether it is ``long_running``. A long tool returns its ACKNOWLEDGEMENT
immediately ("Starting that now") and does its work against the database, where
some other process picks it up. Nothing here ever holds the conversation open.

CHANNELS ARE A CAPABILITY, NOT A HINT. Each tool names the channels allowed to
call it, so "the phone may not push to main" is a data table rather than a
prompt instruction — and a model that decides to try anyway is refused by code.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from jarvis.bus import publish
from jarvis.tools.ctx import ToolCtx

__all__ = [
    "Tool",
    "Registry",
    "ToolError",
    "UnknownTool",
    "ChannelNotAllowed",
    "BadArguments",
    "ALL_CHANNELS",
]

ALL_CHANNELS: tuple[str, ...] = ("desk", "telegram", "phone", "scheduler", "cli")


class ToolError(RuntimeError):
    """A tool refused. The message is spoken, so it is written to be heard."""


class UnknownTool(ToolError):
    pass


class ChannelNotAllowed(ToolError):
    def __init__(self, tool: str, channel: str) -> None:
        super().__init__(f"I can't do that from here — {tool} isn't available over {channel}.")
        self.tool = tool
        self.channel = channel


class BadArguments(ToolError):
    """The model called a real tool with arguments it does not have.

    Named rather than dropped. Silently discarding an argument the model invented
    is how "I set it to private" becomes true in the transcript and false on the
    disk; and a bare ``TypeError`` from the handler reaches the user as a
    traceback fragment it cannot act on. A sentence naming the argument is
    something the model can correct on its next turn.
    """

    def __init__(self, tool: str, detail: str) -> None:
        super().__init__(f"I couldn't run {tool}: {detail}")
        self.tool = tool


@dataclass(frozen=True, slots=True)
class Tool:
    """A capability, described so a model can choose it and a channel can gate it."""

    name: str
    #: What Gemini reads to decide whether to call this. Be explicit about the
    #: trigger phrasing, and if it could be confused with another tool, say which
    #: one to use instead — the model reads this and nothing else.
    description: str
    handler: Callable[..., str]
    #: A Gemini function-declaration schema. Empty means no arguments.
    parameters: Mapping[str, Any] = field(
        default_factory=lambda: {"type": "OBJECT", "properties": {}}
    )
    #: Which channels may invoke it. Default: every channel.
    channels: tuple[str, ...] = ALL_CHANNELS
    #: True when the work outlives the utterance. The handler must return fast
    #: with something worth hearing and leave a row behind for a real worker.
    long_running: bool = False

    def declaration(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": dict(self.parameters),
        }


class Registry:
    """The tools this process offers, filtered by who is asking."""

    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.add(tool)

    def add(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool {tool.name!r}")
        if not tool.channels:
            # A tool nobody may call is dead code wearing the costume of a
            # capability: it shows up in the table, it is never offered anywhere,
            # and the reason is invisible at the call site that wonders why.
            raise ValueError(f"{tool.name}: no channels, so nothing could ever call it")
        unknown = set(tool.channels) - set(ALL_CHANNELS)
        if unknown:
            raise ValueError(f"{tool.name}: unknown channels {sorted(unknown)}")
        self._tools[tool.name] = tool

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def names(self, channel: str | None = None) -> tuple[str, ...]:
        return tuple(sorted(t.name for t in self.for_channel(channel)))

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError:
            raise UnknownTool(f"I don't have a tool called {name}.") from None

    def for_channel(self, channel: str | None) -> tuple[Tool, ...]:
        if channel is None:
            return tuple(self._tools.values())
        return tuple(t for t in self._tools.values() if channel in t.channels)

    def declarations(self, channel: str | None = None) -> list[dict[str, Any]]:
        """The function declarations to hand a Live session for this channel.

        Filtering here rather than refusing later is the point: a tool the phone
        may not call is never offered to the phone's session, so the model is not
        put in the position of proposing something that will be denied.
        """
        return [t.declaration() for t in self.for_channel(channel)]

    def dispatch(self, name: str, arguments: Mapping[str, Any], ctx: ToolCtx) -> str:
        """Run a tool and return what to say. Never raises out of here.

        A crashing tool must not take the conversation with it — the assistant
        says the tool failed and carries on, and the traceback goes to the
        activity log where it can be read later.
        """
        try:
            tool = self.get(name)
            if ctx.channel not in tool.channels:
                raise ChannelNotAllowed(name, ctx.channel)
            publish(
                ctx.con,
                "tool.used",
                ctx.actor,
                {"tool": name, "channel": ctx.channel, "long_running": tool.long_running},
            )
            return _call(name, tool.handler, arguments, ctx) or "Done."
        except ToolError as e:
            # A refusal is a sentence the user should hear, not an incident.
            publish(ctx.con, "tool.denied", ctx.actor, {"tool": name, "why": str(e)})
            return str(e)
        except Exception as e:  # noqa: BLE001 — a bad tool must not end the conversation
            publish(
                ctx.con,
                "tool.denied",
                ctx.actor,
                {"tool": name, "error": f"{type(e).__name__}: {e}"},
            )
            return f"Sorry — {name} failed: {e}"


def _call(
    name: str, handler: Callable[..., str], arguments: Mapping[str, Any], ctx: ToolCtx
) -> str:
    """Pass ``ctx`` only if the handler asks for it, and arguments by keyword.

    Signature introspection rather than a fixed calling convention, so a
    zero-argument tool stays a zero-argument function and nobody writes
    ``def handler(params, ctx=None)`` out of habit.

    The argument check is here rather than left to Python because the caller is a
    language model: it will occasionally invent a plausible-sounding option, and
    the difference between "I don't have a colour option" and a ``TypeError``
    reaching the user as speech is the difference between a correctable turn and
    a dead end.
    """
    sig = inspect.signature(handler)
    params = sig.parameters
    accepts_any = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    takes_ctx = "ctx" in params or accepts_any

    kwargs = dict(arguments)
    kwargs.pop("ctx", None)  # ctx is ours to supply; a model-supplied one is not ctx
    if not accepts_any:
        strangers = sorted(set(kwargs) - set(params))
        if strangers:
            raise BadArguments(name, f"I don't have {_and(strangers)} to set on it.")
    if takes_ctx:
        kwargs["ctx"] = ctx

    missing = sorted(
        p.name
        for p in params.values()
        if p.default is inspect.Parameter.empty
        and p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        and p.name not in kwargs
    )
    if missing:
        raise BadArguments(name, f"I still need {_and(missing)}.")
    return handler(**kwargs)


def _and(items: list[str]) -> str:
    """``['a', 'b', 'c']`` -> ``"a, b and c"``. Spoken, so no Oxford comma."""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + f" and {items[-1]}"
