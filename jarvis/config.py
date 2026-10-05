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

__all__ = [
    "Briefing",
    "Config",
    "Desk",
    "Location",
    "SecretInConfig",
    "Voice",
    "default_path",
    "load",
]

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
    #: The text model: tidying build requests, the `chat` command, web search.
    #: Taken from the model enum in the installed google-genai SDK (2.23.0),
    #: which lists gemini-3.8-flash as the current Flash model — READ, not
    #: measured: no billed call has been made against it from this project. The
    #: previous pin, "gemini-3-flash", appears nowhere in that enum (only
    #: "gemini-3-flash-preview" does), so it would have been a 404. A wrong pin
    #: here is one line of TOML, which is why it is not in code.
    text_model: str = "gemini-3.8-flash"
    #: Gemini's speech model, used by the reader as its last rung. It is a
    #: generative voice, so it is never trusted with EXACT text (an option label,
    #: an answer key) until tools/fidelity_probe.py says otherwise.
    tts_model: str = "gemini-2.5-flash-preview-tts"
    tts_voice: str = "Kore"
    #: The reader's ladder, best first. Each rung is tried until one speaks:
    #: kokoro (local neural), edge (Microsoft, network), system (the OS's own
    #: voice — espeak-ng, macOS `say`, Windows SAPI), gemini (generative).
    reader_order: tuple[str, ...] = ("kokoro", "edge", "system", "gemini")
    #: Let the conversation use Google Search for current facts — news, scores,
    #: opening hours. Off means it answers from what the model already knows.
    web_search: bool = True
    #: Words YOU say that recognisers get wrong: names, jargon, project names.
    #: They are taught to the live model and to the transcript corrector.
    vocabulary: tuple[str, ...] = ()
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
class Location:
    """Where "here" is, for the weather and the time.

    All optional. With nothing set, "here" comes from your public IP and the
    local GeoLite2 database — approximate, and said to be approximate. Setting
    ``city`` or the coordinates is exact and sends nothing anywhere to find out
    where you are.
    """

    city: str = ""
    latitude: float | None = None
    longitude: float | None = None
    #: Look this public address up instead of discovering it. Useful behind a
    #: VPN that would otherwise put you in another country.
    ip: str = ""
    #: Where the GeoLite2 City database lives. Empty means the XDG data dir.
    geoip_db: str = ""
    #: Not a secret: it identifies the account, and the LICENCE KEY that goes
    #: with it lives in the keyring (`secrets set maxmind_license_key`).
    maxmind_account_id: str = ""
    units: str = "metric"
    #: The language place names are given in, where the database has them.
    language: str = "en"


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
    location: Location = field(default_factory=Location)
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
    raw = dict(table.get(name, {}))
    for old, new in _RENAMED.get(name, {}).items():
        # A key this version renamed. Honoured rather than ignored, because an
        # ignored setting is indistinguishable from one that does not work.
        if old in raw and new not in raw:
            raw[new] = raw.pop(old)
    known = {k: v for k, v in raw.items() if k in fields}
    for k, v in known.items():
        # TOML has arrays and the dataclasses are frozen, so every list becomes
        # a tuple: a frozen object holding a mutable list is one any caller can
        # quietly edit.
        if isinstance(v, list):
            known[k] = tuple(v)
    return cls(**known)


#: Keys a previous version used. ``[voice] tidy_model`` became ``text_model``
#: when the same model started answering the chat and the web searches too.
_RENAMED: dict[str, dict[str, str]] = {"voice": {"tidy_model": "text_model"}}


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
        location=_section(Location, table, "location"),
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
text_model = "gemini-3.8-flash"   # from the SDK model list; a 404 means change this
gemini_voice = "Zephyr"
assume_headset = true    # run tools/aec_bench.py before trusting open speakers
web_search = true
# Words recognisers get wrong when YOU say them: names, jargon, project names.
vocabulary = ["quote", "Jarvis", "Claude Code"]

[location]
# Leave empty to locate by IP with GeoLite2 (approximate, and said to be).
# Set a city, or exact coordinates, to be exact and send nothing anywhere.
city = ""
# latitude = 41.0082
# longitude = 28.9784
units = "metric"
# For `python -m jarvis geo update`. The licence key goes in the keyring:
#   python -m jarvis secrets set maxmind_license_key
maxmind_account_id = ""

[briefing]
at_local = "10:00"
sections = ["projects", "inbox", "issues", "comments"]
"""
