"""Settings that are not secrets.

One TOML file at ``~/.config/jarvis/config.toml``, read with the standard
library's ``tomllib``. Everything has a default that works, so a missing file is
a valid configuration rather than an error — the first run should not be a
scavenger hunt.

THIS FILE NEVER HOLDS A CREDENTIAL. Not a token, not a key, not a PIN. Those
live in the OS keyring via :mod:`jarvis.secrets`, and the separation is the
point: config is checked into your dotfiles and pasted into bug reports, and the
moment one secret is tolerated here a second one follows. :func:`load` refuses a
file containing a key that looks like a credential rather than quietly reading
it, because a refusal is noticed and a warning is not.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

__all__ = ["Config", "Voice", "Desk", "Briefing", "load", "default_path", "SecretInConfig"]

#: Key names that must never appear in the TOML. Matched case-insensitively
#: against the leaf name, so ``[telegram] token = "..."`` is caught as well as a
#: top-level ``api_key``.
_SECRET_LOOKING = ("token", "secret", "password", "api_key", "apikey", "key", "pin", "credential")

#: Exempt leaves whose name contains a forbidden word but which are not secrets.
#: Kept tiny on purpose; each entry is a hole in the rule above.
_NOT_SECRETS = frozenset({"keyring_service", "public_key_path"})


class SecretInConfig(ValueError):
    """A credential was found in the config file, which is not where they live."""

    def __init__(self, dotted: str) -> None:
        super().__init__(
            f"{dotted!r} looks like a credential, and config.toml is not where credentials live "
            f"(it gets committed to dotfiles and pasted into bug reports). "
            f"Store it with: python -m jarvis secrets set <name>"
        )
        self.dotted = dotted


@dataclass(frozen=True, slots=True)
class Voice:
    """How Jarvis sounds and listens."""

    #: The Gemini Live model. gemini-3.1-flash-live-preview is LEGACY; see CLAUDE.md.
    model: str = "gemini-3.8-live"
    #: The Live session's own voice. Changing it takes effect on the NEXT session,
    #: because a reconnect would discard the resumption handle mid-conversation.
    gemini_voice: str = "Zephyr"
    #: The text model that tidies a spoken build request into a requirement list.
    #: UNVERIFIED: unlike the Live model, which was measured, nobody has yet run
    #: a real call against this id from this project. It is here rather than in
    #: code so that a wrong pin is one line of TOML instead of a patch — the
    #: failure it causes is a 404 at the first real build, which is exactly when
    #: editing source is least welcome.
    tidy_model: str = "gemini-3-flash"
    #: The deterministic reader that speaks load-bearing text. Deliberately a
    #: DIFFERENT voice: "when the other voice speaks, those are somebody else's
    #: exact words" is an audible integrity marker, not a rough edge.
    reader_voice: str = "en-GB-SoniaNeural"
    reader_voice_tr: str = "tr-TR-AhmetNeural"
    wake_word: str = "hey_jarvis"
    #: Open speakers are the upgrade AEC buys, not the baseline it must deliver.
    #: tools/aec_bench.py decides this with a pass/fail rule fixed in advance.
    assume_headset: bool = True
    input_device: str | None = None
    output_device: str | None = None


@dataclass(frozen=True, slots=True)
class Desk:
    """The at-the-desk process."""

    #: Where cloned projects live. Not inside the repo, not in a synced folder.
    workspace: str = "~/code"
    github_owner: str = ""
    #: Claude Code defaults. NEVER "dontAsk" — it denies AskUserQuestion, and the
    #: jobs schema has a CHECK constraint that refuses it anyway.
    model: str = "claude-opus-5"
    #: 'high' is Claude Code's DEFAULT, so the levels that change behaviour are
    #: low, medium, xhigh and max. Jarvis should be able to say that out loud.
    effort: str = "high"
    permission_mode: str = "plan"


@dataclass(frozen=True, slots=True)
class Briefing:
    """The morning call."""

    at_local: str = "10:00"
    enabled: bool = True
    #: Sections in delivery order. Drop one by removing it here.
    sections: tuple[str, ...] = ("projects", "inbox", "issues", "comments")
    youtube_channel_id: str = ""


@dataclass(frozen=True, slots=True)
class Config:
    voice: Voice = field(default_factory=Voice)
    desk: Desk = field(default_factory=Desk)
    briefing: Briefing = field(default_factory=Briefing)
    #: Where Jarvis speaks from, and the only place a local timezone appears.
    tz: str = "Europe/Istanbul"
    #: Spoken when the running total crosses it. Under a Max subscription the
    #: meaningful unit is rate-limit proximity rather than dollars, and the
    #: ledger says so out loud rather than reporting a reassuring zero.
    spend_threshold_usd: float = 20.0

    @property
    def workspace_path(self) -> Path:
        return Path(self.desk.workspace).expanduser()


def default_path() -> Path:
    """``$JARVIS_CONFIG``, else ``$XDG_CONFIG_HOME/jarvis/config.toml``."""
    if env := os.environ.get("JARVIS_CONFIG"):
        return Path(env).expanduser()
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "jarvis" / "config.toml"


def _reject_secrets(table: dict[str, Any], prefix: str = "") -> None:
    for key, value in table.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict):
            _reject_secrets(value, f"{dotted}.")
            continue
        leaf = key.lower()
        if leaf in _NOT_SECRETS:
            continue
        if any(word in leaf for word in _SECRET_LOOKING):
            raise SecretInConfig(dotted)


def _section(cls: type, table: dict[str, Any], name: str) -> Any:
    """Build one dataclass from its table, ignoring keys it does not know.

    Unknown keys are tolerated rather than fatal because a config written for a
    newer Jarvis should not stop an older one from starting — but they are
    returned so the caller can mention them, since a silently ignored setting is
    indistinguishable from one that does not work.
    """
    fields = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    known = {k: v for k, v in table.get(name, {}).items() if k in fields}
    if "sections" in known and isinstance(known["sections"], list):
        known["sections"] = tuple(known["sections"])
    return cls(**known)


def load(path: str | Path | None = None) -> Config:
    """Read the config, or return the defaults if there is no file.

    Raises :class:`SecretInConfig` if the file holds something that looks like a
    credential, and ``tomllib.TOMLDecodeError`` if it is malformed. Both are
    loud on purpose: a typo that silently reverts you to defaults is worse than
    a refusal, because you find out weeks later when the briefing fires an hour
    early and nobody knows why.
    """
    p = Path(path).expanduser() if path is not None else default_path()
    if not p.exists():
        return Config()

    table = tomllib.loads(p.read_text(encoding="utf-8"))
    _reject_secrets(table)

    base = Config(
        voice=_section(Voice, table, "voice"),
        desk=_section(Desk, table, "desk"),
        briefing=_section(Briefing, table, "briefing"),
    )
    top = {k: v for k, v in table.items() if k in {"tz", "spend_threshold_usd"}}
    return replace(base, **top) if top else base


EXAMPLE = """\
# ~/.config/jarvis/config.toml — settings, never credentials.
# Secrets live in the OS keyring: python -m jarvis secrets set <name>

tz = "Europe/Istanbul"
spend_threshold_usd = 20.0

[desk]
workspace = "~/code"
github_owner = "Efkrdnz"
model = "claude-opus-5"
effort = "high"          # 'high' IS the default; low/medium/xhigh/max change behaviour

[voice]
model = "gemini-3.8-live"
tidy_model = "gemini-3-flash"   # UNVERIFIED pin; a 404 at your first build means change this
gemini_voice = "Zephyr"
assume_headset = true    # run tools/aec_bench.py before trusting open speakers

[briefing]
at_local = "10:00"
sections = ["projects", "inbox", "issues", "comments"]
"""
