"""What a tool handler is allowed to see.

A FROZEN dataclass with NO ``player`` and NO ``session``, and both absences are
deliberate. The reference build this project learned from passes its PyQt window
to every action as ``ctx["player"]``, which is why that assistant cannot run
headless, cannot be driven from a phone, and cannot confirm anything without a
HUD. A handler that can reach the UI will reach the UI.

So a handler gets a database connection, who is calling and over which channel,
and a way to say something. It does not get the microphone, the Gemini session,
the speaker, or the window. Anything it wants to tell the user goes out as an
``Utterance`` through the sink, which the desk renders as speech and Telegram
renders as a message — and neither is the handler's business.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

__all__ = ["ToolCtx", "Say", "Fidelity"]

Fidelity = Literal["exact", "faithful", "free"]

#: How a handler says something. text + fidelity; the channel decides the rest.
#: EXACT never passes through a generative model on its way out — see
#: jarvis/voice/router.py, which raises rather than routing it to Gemini's voice.
Say = Callable[[str, Fidelity], None]


def _silent(text: str, fidelity: Fidelity = "free") -> None:
    """The default sink: a tool that says nothing still works."""


@dataclass(frozen=True, slots=True)
class ToolCtx:
    con: sqlite3.Connection
    #: Which channel invoked this: desk | telegram | phone | scheduler | cli.
    #: Handlers use it for POLICY, never for rendering.
    channel: str = "desk"
    #: The actor string that lands in the activity log.
    actor: str = "desk"
    say: Say = _silent
    #: Anything the channel wants to pass down that is not worth a field.
    extra: dict[str, Any] = field(default_factory=dict)
