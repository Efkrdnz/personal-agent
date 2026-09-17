"""What a spoken sentence is allowed to make happen.

One :class:`~jarvis.tools.registry.Registry`, one calling convention, and a
channel column that is data rather than a prompt instruction. The tools
themselves live in :mod:`jarvis.tools.builtin`.

THIS LAYER DOES NOT KNOW HOW IT WAS INVOKED. No module here may import the voice
layer, the audio graph, the Live session, Telegram, or the Claude Code driver —
``tools/check_layers.py`` enforces it. A tool's entire output is a string and
some rows; who speaks the string is decided somewhere else, hours later, possibly
in another process. That is what makes "the phone is one more channel" a
re-wiring rather than a rewrite.
"""

from __future__ import annotations

from jarvis.tools.ctx import ToolCtx
from jarvis.tools.registry import (
    ALL_CHANNELS,
    BadArguments,
    ChannelNotAllowed,
    Registry,
    Tool,
    ToolError,
    UnknownTool,
)

__all__ = [
    "ALL_CHANNELS",
    "BadArguments",
    "ChannelNotAllowed",
    "Registry",
    "Tool",
    "ToolCtx",
    "ToolError",
    "UnknownTool",
]
