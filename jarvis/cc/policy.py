"""Which tools a channel may run, as DATA rather than as branches in the host.

The blast radius of the phone leg is one table in this file. That is deliberate:
"the phone cannot push to main" should be readable by someone who does not know
Python, and changing it should not mean editing the permission host.

DEFAULT-DENY EVERYWHERE EXCEPT THE DESK. A tool is unavailable on a channel
unless that channel opted into it. The desk is the exception because the human is
sitting in front of it and can see what happened; everywhere else the user is
holding a phone in a supermarket and cannot.

``ask`` IS A REAL OUTCOME, not a synonym for deny. It raises a
``tool_permission`` request through the spine and waits on the same machinery a
plan-mode question uses, which is why "Bash at the desk" can be a question the
user answers out loud instead of a policy nobody can change at 2am.

SECRET PATHS ARE NOT A POLICY LINE. They are refused for any channel that sets
``deny_secret_paths``, before the allow list is consulted, because an allow entry
for ``Read`` must not become a way to read ``~/.ssh/id_rsa`` down a phone line.
This is a coarse substring guard on purpose: it is a backstop for a mistake, not
a sandbox — the real containment is ``permissions.blockReadsOutsideWorking
Directories`` and a scoped credential.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal

__all__ = [
    "DEFAULT_POLICIES",
    "SECRET_PATTERNS",
    "ChannelPolicy",
    "Decision",
    "decide",
    "policy_for",
    "secret_hit",
]

Decision = Literal["allow", "ask", "deny"]

#: Substrings that mean "this call is reaching for a credential". Matched against
#: every string in the tool input, so a Bash command that cats a key is caught by
#: the same rule as a Read of it — the reference build's mistake was to filter
#: tool NAMES and let the shell walk straight past.
SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(^|[/\\~])\.ssh([/\\]|$)"),
    re.compile(r"(^|[/\\~])\.aws([/\\]|$)"),
    re.compile(r"(^|[/\\~])\.gnupg([/\\]|$)"),
    re.compile(r"(^|[/\\~])\.claude([/\\]|$)"),
    re.compile(r"(^|[/\\])\.env(\.[\w-]+)?($|[\s\"'])"),
    re.compile(r"id_(rsa|ed25519|ecdsa)"),
    re.compile(r"\.pem($|[\s\"'])"),
)


@dataclass(frozen=True, slots=True)
class ChannelPolicy:
    """One channel's table. Frozen sets, because a policy nobody can mutate at
    runtime is a policy that means the same thing in every process that reads it.
    """

    name: str
    default: Decision
    allow: frozenset[str] = frozenset()
    ask: frozenset[str] = frozenset()
    deny: frozenset[str] = frozenset()
    deny_secret_paths: bool = True

    def decision_for(self, tool_name: str) -> Decision:
        """deny beats ask beats allow beats the default. Most restrictive wins.

        Order matters and is not arbitrary: a tool named in two sets is a typo,
        and the only safe reading of a typo in a permission table is the strict
        one.
        """
        if tool_name in self.deny:
            return "deny"
        if tool_name in self.ask:
            return "ask"
        if tool_name in self.allow:
            return "allow"
        return self.default


#: Tools that only ever read the working tree. Shared by the channels rather than
#: repeated, so adding a read-only tool cannot reach the phone by accident.
_READ_ONLY = frozenset(
    {"Read", "Glob", "Grep", "NotebookRead", "TodoWrite", "Task", "WebFetch", "WebSearch"}
)
_WRITES = frozenset({"Write", "Edit", "NotebookEdit", "MultiEdit"})

DEFAULT_POLICIES: Mapping[str, ChannelPolicy] = MappingProxyType(
    {
        # The human is in the room, watching the screen, and can say "stop".
        # Writes and the shell still pass through a question, because "it ran
        # rm -rf while I was making coffee" is the failure that has no undo.
        "desk": ChannelPolicy(
            name="desk",
            default="allow",
            ask=frozenset({"Bash", "BashOutput", "KillShell"}) | _WRITES,
            deny_secret_paths=False,
        ),
        "telegram": ChannelPolicy(
            name="telegram",
            default="deny",
            allow=_READ_ONLY,
            ask=_WRITES,
        ),
        # The phone reaches a person who cannot see anything. It gets reads and
        # nothing else; a shell there is a blast radius nobody can inspect.
        "phone": ChannelPolicy(
            name="phone",
            default="deny",
            allow=_READ_ONLY - {"WebFetch", "WebSearch", "Task"},
        ),
    }
)

#: An unknown channel is not a desk. Fail closed: a typo in a job row must cost a
#: denied tool call, not a shell.
UNKNOWN_CHANNEL = ChannelPolicy(name="unknown", default="deny")


def policy_for(channel: str, policies: Mapping[str, ChannelPolicy] | None = None) -> ChannelPolicy:
    """The table for this channel, or the fail-closed one if it has none."""
    table = DEFAULT_POLICIES if policies is None else policies
    return table.get(channel, UNKNOWN_CHANNEL)


def _strings(value: Any) -> list[str]:
    """Every string anywhere in a tool input, flattened."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in _strings(v)]
    return []


def secret_hit(tool_input: Any) -> str | None:
    """The first credential-shaped string in this input, or None."""
    for text in _strings(tool_input):
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                return text
    return None


def decide(
    channel: str,
    tool_name: str,
    tool_input: Any = None,
    *,
    policies: Mapping[str, ChannelPolicy] | None = None,
) -> tuple[Decision, str]:
    """``(decision, reason)`` — the reason is spoken and logged, so it is a sentence."""
    policy = policy_for(channel, policies)
    if policy.deny_secret_paths and (hit := secret_hit(tool_input)) is not None:
        return "deny", f"{hit!r} looks like a credential, and {policy.name} may not read those"
    decision = policy.decision_for(tool_name)
    if decision == "deny":
        return decision, f"{tool_name} is not allowed on the {policy.name} channel"
    if decision == "ask":
        return decision, f"{tool_name} needs the user's say-so on the {policy.name} channel"
    return decision, f"{tool_name} is allowed on the {policy.name} channel"
