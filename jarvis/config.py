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

THE APP NEVER EDITS config.toml. A person writes that file by hand, with
comments, and a program that round-trips it through a TOML library throws the
comments away. Settings changed in the window go to ``app-settings.toml`` beside
it instead (:func:`save_setting`), which :func:`load` lays OVER config.toml key
by key. The overlay is the app's own file: rewritten whole, atomically, and
holding only the keys in :data:`SETTABLE`, each checked against its field's type
before anything is written — a window is a form, and a form can send anything.
"""

from __future__ import annotations

import contextlib
import math
import os
import re
import tempfile
import time
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

__all__ = [
    "SETTABLE",
    "SETTINGS_FILE",
    "App",
    "Briefing",
    "Config",
    "Desk",
    "Location",
    "Persona",
    "SecretInConfig",
    "Voice",
    "default_path",
    "load",
    "save_setting",
    "settings_path",
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

    def __init__(self, dotted: str, message: str | None = None) -> None:
        super().__init__(
            message
            or (
                f"{dotted!r} looks like a credential, and config.toml is not where credentials "
                f"live (it gets committed to dotfiles and pasted into bug reports). "
                f"Store it with: python -m jarvis secrets set <name>"
            )
        )
        self.dotted = dotted


@dataclass(frozen=True, slots=True)
class Voice:
    """How Jarvis sounds and listens."""

    #: The Gemini Live model. gemini-3.1-flash-live-preview is LEGACY; see CLAUDE.md.
    model: str = "gemini-3.8-live"
    #: The Live session's own voice. Changing it takes effect on the NEXT session,
    #: because a reconnect would discard the resumption handle mid-conversation.
    gemini_voice: str = "Charon"
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
    #: Hand the vocabulary to the Live recogniser as ``custom_vocabulary``. On
    #: by default; see SessionProfile.vocabulary for why it is a switch.
    asr_vocabulary: bool = True
    #: BCP-47 hints for the recogniser, e.g. ["en-US", "tr-TR"]. Empty = auto.
    languages: tuple[str, ...] = ()
    #: Ask the text model to settle a word the evidence leaves split ("coat" or
    #: "quote"?). One short call, only for doubtful words, only ever a vote.
    hearing_arbiter: bool = True
    #: The deterministic reader that speaks load-bearing text. Deliberately a
    #: DIFFERENT voice: "when the other voice speaks, those are somebody else's
    #: exact words" is an audible integrity marker, not a rough edge.
    reader_voice: str = "en-GB-RyanNeural"
    reader_voice_tr: str = "tr-TR-AhmetNeural"
    #: The desk sleeps until it hears this. "" listens all the time, which
    #: is what the desk did before it had a wake word. See jarvis/audio/wake.py.
    wake_word: str = "hey_jarvis"
    #: 0.5 is openWakeWord's own default. Lower hears you from further away
    #: and wakes for the television more often; `python -m jarvis wake test`
    #: prints the score a phrase gets, so this is tuned by measuring.
    wake_threshold: float = 0.5
    #: How long the desk keeps listening after the last thing anyone said.
    wake_window_s: float = 20.0
    #: A soft blip when it wakes, so you know it heard you.
    wake_chime: bool = True
    #: Open speakers are the upgrade AEC buys, not the baseline it must deliver.
    #: tools/aec_bench.py decides this with a pass/fail rule fixed in advance.
    assume_headset: bool = True
    #: How the desk decides a sound is the user speaking. "auto": the Silero model,
    #: or a basic voiced-sound detector if it is missing. "basic": that detector
    #: alone. "energy": loudness alone, as before, when breaths started turns: the
    #: way back if the new rule is ever deaf to someone's voice.
    vad: str = "auto"
    #: None (or "" in TOML, which has no null) means the system's default device.
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
class Persona:
    """How Jarvis addresses you."""

    #: "sir", "ma'am", "boss", or a name. Said naturally, not in every sentence.
    address: str = "sir"
    #: Your own name, if you gave one.
    name: str = ""


@dataclass(frozen=True, slots=True)
class App:
    """The double-click app: what it starts with, and when it starts."""

    start_with_windows: bool = False
    #: Start the Telegram bot alongside the desk, when a bot token is stored.
    start_telegram: bool = True
    open_window_on_start: bool = True


@dataclass(frozen=True, slots=True)
class Config:
    voice: Voice = field(default_factory=Voice)
    desk: Desk = field(default_factory=Desk)
    briefing: Briefing = field(default_factory=Briefing)
    location: Location = field(default_factory=Location)
    persona: Persona = field(default_factory=Persona)
    app: App = field(default_factory=App)
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


def _read_table(p: Path) -> dict[str, Any]:
    """One TOML file as a table, refused if it holds a credential."""
    try:
        # utf-8-sig: Windows editors may prepend a byte-order mark, which TOML
        # rejects as a stray character on line 1.
        text = p.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"{p} is not UTF-8 (PowerShell's `>` writes UTF-16). Re-save it as UTF-8, "
            "or recreate it with `python -m jarvis config init --force`."
        ) from exc
    table = tomllib.loads(text)
    _reject_secrets(table)
    return table


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """``over`` laid on ``base`` section by section, key by key: one changed key
    in the overlay must not wipe the rest of its section in config.toml."""
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {**out[k], **v}
        else:
            out[k] = v
    return out


def load(path: str | Path | None = None) -> Config:
    """Read the config, with the app's settings laid over it, or the defaults.

    Raises :class:`SecretInConfig` if either file holds something that looks
    like a credential, and ``tomllib.TOMLDecodeError`` if config.toml is
    malformed. Both are loud on purpose: a typo that silently reverts you to
    defaults is worse than a refusal, because you find out weeks later when the
    briefing fires an hour early and nobody knows why.
    """
    p = Path(path).expanduser() if path is not None else default_path()
    table = _read_table(p) if p.exists() else {}
    overlay = p.with_name(SETTINGS_FILE)
    if overlay.exists():
        try:
            table = _merge(table, _read_table(overlay))
        except tomllib.TOMLDecodeError as exc:
            # The app writes this file atomically, so a broken one was edited by
            # hand; naming it is the whole of the fix.
            raise ValueError(
                f"{overlay} is not valid TOML ({exc}). It holds only what the Jarvis "
                "window saved; delete it to go back to config.toml's settings."
            ) from exc
    if not table:
        return Config()

    voice = _section(Voice, table, "voice")
    # TOML has no null, so "" is how a file says "the system default device".
    # Passed on as "" it would be a device-name search that matches everything.
    if voice.input_device == "" or voice.output_device == "":
        voice = replace(
            voice,
            input_device=voice.input_device or None,
            output_device=voice.output_device or None,
        )
    base = Config(
        voice=voice,
        desk=_section(Desk, table, "desk"),
        briefing=_section(Briefing, table, "briefing"),
        location=_section(Location, table, "location"),
        persona=_section(Persona, table, "persona"),
        app=_section(App, table, "app"),
    )
    top = {k: v for k, v in table.items() if k in {"tz", "spend_threshold_usd"}}
    return replace(base, **top) if top else base


# ───────────────────────────── the app's settings ─────────────────────────────

#: The overlay's name, beside config.toml.
SETTINGS_FILE = "app-settings.toml"

#: What the window may change. A key is here because a person would look for it
#: in a settings screen; everything else stays a deliberate edit of config.toml.
SETTABLE: frozenset[str] = frozenset(
    {
        "persona.address",
        "persona.name",
        "voice.gemini_voice",
        "voice.reader_voice",
        "voice.input_device",
        "voice.wake_word",
        "voice.wake_threshold",
        "voice.languages",
        "voice.vocabulary",
        "location.city",
        "location.units",
        "tz",
        "app.start_with_windows",
        "app.start_telegram",
        "app.open_window_on_start",
    }
)

_SECTIONS: dict[str, type] = {
    "voice": Voice,
    "desk": Desk,
    "briefing": Briefing,
    "location": Location,
    "persona": Persona,
    "app": App,
}

# A setting is a word or a short phrase. These bound what one form field can
# put in a file every process reads at startup.
_MAX_TEXT = 200
_MAX_ITEMS = 100
_MAX_ITEM = 100

#: Shapes that are credentials whatever field they were typed into. A key
#: pasted into the city box is still a key, and this file is not the keyring.
_SECRET_SHAPES = (
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),  # Google API key
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_\-]{30,}"),  # Telegram bot token
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)
# Any unbroken run this long that mixes letters and digits. No city, voice or
# word anybody says is 32 characters without a space.
_LONG_RUN = re.compile(r"[A-Za-z0-9_\-+/=]{32,}")
_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
_TOML_ESCAPES = {"\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r"}


def settings_path(path: str | Path | None = None) -> Path:
    """Where the app's settings live: ``app-settings.toml`` beside config.toml."""
    p = Path(path).expanduser() if path is not None else default_path()
    return p.with_name(SETTINGS_FILE)


def save_setting(key: str, value: Any, path: str | Path | None = None) -> Config:
    """Change one setting in the overlay, atomically, and return the config as it now loads.

    ``key`` is dotted (``"voice.gemini_voice"``, or ``"tz"``) and must be in
    :data:`SETTABLE`. ``value`` is checked against the field's own type — a
    string for a string, a real bool for a bool, a list of strings for a tuple
    — because it arrives from a web form and a ``"false"`` that becomes a truthy
    string is a switch that cannot be turned off. Raises ``ValueError`` with a
    sentence a person can act on; :class:`SecretInConfig` (a ValueError) when
    the value looks like a credential.
    """
    if not isinstance(key, str) or key not in SETTABLE:
        raise ValueError(f"{key!r} is not a setting the app can change.")
    section, _, name = key.rpartition(".")
    clean = _coerce(key, _annotation(section, name), value)
    _check(key, clean)
    # The same rule load() applies, so a key that looks like a credential can
    # never be written here even if one is added to SETTABLE by mistake.
    _reject_secrets({section: {name: clean}} if section else {name: clean})

    target = settings_path(path)
    table = _read_table(target) if target.exists() else {}
    if section:
        if not isinstance(table.get(section), dict):
            table[section] = {}
        table[section][name] = clean
    else:
        table[name] = clean
    text = _emit(table)
    # The emitter is small and hand-written; reading its output back is what
    # makes "the file always parses" a fact rather than a hope.
    if tomllib.loads(text) != _plain(table):
        raise RuntimeError(f"refusing to write {target}: it would not read back the same")
    _write_atomic(target, text)
    return load(path)


def _annotation(section: str, name: str) -> str:
    """The field's declared type, as written (annotations are strings here)."""
    if not section:
        f = Config.__dataclass_fields__[name]  # type: ignore[attr-defined]
    else:
        f = _SECTIONS[section].__dataclass_fields__[name]  # type: ignore[attr-defined]
    return str(f.type)


def _coerce(key: str, ann: str, value: Any) -> Any:
    if ann == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{key} is a switch: true or false.")
        return value
    if ann == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{key} is a whole number.")
        return value
    if ann == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{key} is a number.")
        if not math.isfinite(value):
            raise ValueError(f"{key} must be an ordinary number.")
        return float(value)
    if ann in ("str", "str | None"):
        if value is None and ann == "str | None":
            return ""  # TOML has no null; load() reads "" back as None
        if not isinstance(value, str):
            raise ValueError(f"{key} is text.")
        return _text(key, value, _MAX_TEXT)
    if ann == "tuple[str, ...]":
        if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
            raise ValueError(f"{key} is a list of words.")
        if len(value) > _MAX_ITEMS:
            raise ValueError(f"{key} holds at most {_MAX_ITEMS} entries.")
        items = (_text(key, v, _MAX_ITEM) for v in value)
        return list(dict.fromkeys(i for i in items if i))
    raise ValueError(f"{key} cannot be changed from the app.")


def _text(key: str, value: str, limit: int) -> str:
    v = value.strip()
    if len(v) > limit:
        raise ValueError(f"{key} is too long (at most {limit} characters).")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in v):
        raise ValueError(f"{key} cannot hold line breaks or control characters.")
    try:
        v.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{key} holds characters that cannot be saved.") from exc
    return v


def _looks_secret(text: str) -> bool:
    if any(p.search(text) for p in _SECRET_SHAPES):
        return True
    return any(
        any(c.isdigit() for c in m.group()) and any(c.isalpha() for c in m.group())
        for m in _LONG_RUN.finditer(text)
    )


def _check(key: str, value: Any) -> None:
    """The rules a type cannot say. Each refusal is a sentence for the person who typed it."""
    texts = value if isinstance(value, list) else [value] if isinstance(value, str) else []
    if any(_looks_secret(t) for t in texts):
        raise SecretInConfig(
            key,
            f"That looks like a key or a token, and {key} is not where credentials live. "
            "Put it in the keys section instead, which stores it in your system keyring.",
        )
    if key in ("persona.address", "voice.gemini_voice", "voice.reader_voice", "tz") and not value:
        raise ValueError(f"{key} cannot be empty.")
    if key == "location.units" and value not in ("metric", "imperial"):
        raise ValueError("Units are either metric or imperial.")
    if key == "voice.wake_threshold" and not 0.0 < value < 1.0:
        raise ValueError("The wake threshold is between 0 and 1; 0.5 is the usual.")
    if key == "voice.wake_word" and not re.fullmatch(r"[a-z0-9_]{0,40}", value):
        raise ValueError('The wake word is a model name like "hey_jarvis", or empty.')
    if key == "tz":
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                f"{value!r} is not a time zone I know. Use a name like Europe/Istanbul."
            ) from exc


def _plain(table: dict[str, Any]) -> dict[str, Any]:
    """The table as tomllib would hand it back: tuples are lists."""
    return {
        k: _plain(v) if isinstance(v, dict) else list(v) if isinstance(v, tuple) else v
        for k, v in table.items()
    }


def _emit(table: dict[str, Any]) -> str:
    """TOML for a table of scalars and one level of sections. Stdlib has no writer."""
    lines = [
        "# Written by the Jarvis window. It overrides config.toml key by key.",
        "# Edit config.toml by hand instead: this file is rewritten whole on every change.",
        "",
    ]
    for k, v in table.items():
        if not isinstance(v, dict):
            lines.append(f"{_toml_key(k)} = {_toml_value(v, k)}")
    for k, v in table.items():
        if isinstance(v, dict):
            lines.append("")
            lines.append(f"[{_toml_key(k)}]")
            for kk, vv in v.items():
                if isinstance(vv, dict):
                    raise ValueError(f"{SETTINGS_FILE} holds a nested table at {k}.{kk}.")
                lines.append(f"{_toml_key(kk)} = {_toml_value(vv, f'{k}.{kk}')}")
    return "\n".join(lines) + "\n"


def _toml_key(k: str) -> str:
    return k if _BARE_KEY.fullmatch(k) else _toml_str(k)


def _toml_str(s: str) -> str:
    out = ['"']
    for ch in s:
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch in _TOML_ESCAPES:
            out.append(_TOML_ESCAPES[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _toml_value(v: Any, where: str) -> str:
    if isinstance(v, bool):  # before int: a bool IS an int in Python
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if not math.isfinite(v):
            raise ValueError(f"{where} is not a finite number.")
        return repr(v)
    if isinstance(v, str):
        return _toml_str(v)
    if isinstance(v, (list, tuple)):
        if any(isinstance(x, (list, tuple, dict)) for x in v):
            raise ValueError(f"{SETTINGS_FILE} holds a nested array at {where}.")
        return "[" + ", ".join(_toml_value(x, where) for x in v) + "]"
    raise ValueError(f"{SETTINGS_FILE} holds a value at {where} the app cannot rewrite.")


def _write_atomic(target: Path, text: str) -> None:
    """Temp file, fsync, rename: a reader sees the old file or the new one, never half."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".app-settings.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        # Windows refuses to replace a file another process has open this very
        # instant (a desk reading its config at startup). That passes in
        # milliseconds, so a few short retries beat failing the user's click.
        for attempt in range(5):
            try:
                os.replace(tmp, target)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


EXAMPLE = """\
# ~/.config/jarvis/config.toml — settings, never credentials.
# Secrets live in the OS keyring: python -m jarvis secrets set <name>
# Settings changed in the Jarvis window are saved beside this file, in
# app-settings.toml, and override what is here key by key.

tz = "Europe/Istanbul"
spend_threshold_usd = 20.0

[persona]
address = "sir"          # how Jarvis addresses you: "sir", "ma'am", "boss", or a name
name = ""

[desk]
workspace = "~/code"
github_owner = "Efkrdnz"
model = "claude-opus-5"
effort = "high"          # 'high' IS the default; low/medium/xhigh/max change behaviour

[voice]
model = "gemini-3.8-live"
text_model = "gemini-3.8-flash"   # from the SDK model list; a 404 means change this
gemini_voice = "Charon"
reader_voice = "en-GB-RyanNeural"
assume_headset = true    # run tools/aec_bench.py before trusting open speakers
# How a sound counts as you speaking: "auto" (Silero), "basic", or "energy"
# (loudness only, the old way: breaths start turns).
vad = "auto"
web_search = true
# Words recognisers get wrong when YOU say them: names, jargon, project names.
vocabulary = ["quote", "Jarvis", "Claude Code"]
asr_vocabulary = true
# languages = ["en-US", "tr-TR"]
hearing_arbiter = true
wake_word = "hey_jarvis"  # "" = always listening; models: python -m jarvis wake download
wake_threshold = 0.5
wake_window_s = 20.0
wake_chime = true

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

[app]
start_with_windows = false
start_telegram = true    # when a Telegram bot token is stored
open_window_on_start = true
"""
