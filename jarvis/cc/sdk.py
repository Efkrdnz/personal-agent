"""The ONE module in the tree that imports ``claude_agent_sdk``.

Everything else in :mod:`jarvis.cc` imports the names it needs from here. Two
reasons, and the second is the load-bearing one:

*The version is pinned for a reason.* About a third of the payload specifics this
project relies on are undocumented internals of CLI build v2.1.273. When the pin
moves, the diff that matters is the one against this file's import list.

*The pure half of the package must import on a machine with no SDK.* The phone
worker builds a :class:`~jarvis.requests.Presentation` from a stored row and
needs :mod:`jarvis.answers`; it has no business installing a CLI. Keeping the
import in one module makes "which parts need the extra" a fact you can read
rather than a thing you discover in production.
"""

from __future__ import annotations

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    DeferredToolUse,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ToolPermissionContext,
)

__all__ = [
    "AssistantMessage",
    "ClaudeAgentOptions",
    "ClaudeSDKClient",
    "DeferredToolUse",
    "HookMatcher",
    "PermissionResult",
    "PermissionResultAllow",
    "PermissionResultDeny",
    "ResultMessage",
    "TextBlock",
    "ToolPermissionContext",
]

PermissionResult = PermissionResultAllow | PermissionResultDeny
