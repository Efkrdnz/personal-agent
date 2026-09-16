"""The per-channel permission table, tested at its edges rather than its middle.

Every test here is a case where being wrong is expensive and silent: a channel
nobody configured, a tool named in two sets, a credential read through a tool
that was legitimately allowed.
"""

from __future__ import annotations

import pytest

from jarvis.cc import policy


def test_an_unknown_channel_gets_nothing() -> None:
    # A typo in a job row must cost a denied tool call, not a shell. Falling back
    # to the desk's table would make 'dekstop' a privilege escalation.
    decision, reason = policy.decide("dekstop", "Bash", {"command": "ls"})
    assert decision == "deny"
    assert "unknown" in reason


def test_the_phone_denies_by_default_and_the_desk_does_not() -> None:
    assert policy.decide("phone", "Bash", {"command": "ls"})[0] == "deny"
    assert policy.decide("phone", "Write", {"file_path": "a.py"})[0] == "deny"
    assert policy.decide("phone", "Read", {"file_path": "a.py"})[0] == "allow"
    assert policy.decide("desk", "Read", {"file_path": "a.py"})[0] == "allow"


def test_the_desk_asks_rather_than_assumes_for_the_shell_and_for_writes() -> None:
    # 'ask' is a real outcome: it raises a request through the spine and the user
    # answers out loud. Collapsing it to allow is how "it ran rm -rf while I was
    # making coffee" happens.
    assert policy.decide("desk", "Bash", {"command": "rm -rf build"})[0] == "ask"
    assert policy.decide("desk", "Write", {"file_path": "a.py"})[0] == "ask"


def test_a_tool_in_two_sets_reads_as_the_strict_one() -> None:
    table = {
        "odd": policy.ChannelPolicy(
            name="odd",
            default="allow",
            allow=frozenset({"Bash"}),
            ask=frozenset({"Bash"}),
            deny=frozenset({"Bash"}),
        )
    }
    assert policy.decide("odd", "Bash", {}, policies=table)[0] == "deny"


@pytest.mark.parametrize(
    "tool_input",
    [
        {"file_path": "/home/user/.ssh/id_rsa"},
        {"file_path": "~/.claude/settings.json"},
        {"command": "cat ~/.ssh/id_ed25519"},
        {"pattern": "**/.env"},
        {"paths": ["src/main.py", "/etc/ssl/private/site.pem"]},
        {"nested": {"deep": {"path": "/home/user/.aws/credentials"}}},
    ],
)
def test_a_credential_shaped_string_is_denied_even_through_an_allowed_tool(
    tool_input: dict,
) -> None:
    # The guard is on the INPUT, not on the tool name: the reference build
    # filtered names and let the shell walk straight past.
    decision, reason = policy.decide("phone", "Read", tool_input)
    assert decision == "deny"
    assert "credential" in reason


def test_the_desk_is_exempt_from_the_credential_guard_on_purpose() -> None:
    # The human is in the room and can see the screen; refusing them their own
    # ~/.ssh would be theatre, and the real containment is a scoped credential.
    assert policy.decide("desk", "Read", {"file_path": "~/.ssh/config"})[0] == "allow"


def test_an_ordinary_path_that_merely_mentions_env_is_not_a_credential() -> None:
    # False positives cost a denied build step, so the pattern is anchored: a
    # file called environment.py must not trip it.
    assert policy.decide("phone", "Read", {"file_path": "src/environment.py"})[0] == "allow"
    assert policy.secret_hit({"file_path": "docs/env-setup.md"}) is None


def test_the_table_cannot_be_edited_at_runtime() -> None:
    # A policy one process can mutate is a policy that means something different
    # in the next process, which reads the same table from the same file.
    with pytest.raises(TypeError):
        policy.DEFAULT_POLICIES["phone"] = policy.DEFAULT_POLICIES["desk"]  # type: ignore[index]
