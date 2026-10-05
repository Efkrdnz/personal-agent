"""The window's settings and first-run onboarding, as one tested object.

A person who double-clicked an icon has no terminal, so every fix that used to
be a command — store the Gemini key, fetch the wake model, pick a microphone,
sign in to Claude Code — is a method here that the window's buttons call. The
composition root builds one with the real callables; tests build one with
fakes for every one of them.

THREE PROMISES, each with a test:

* A credential goes from the request to the keyring and NOWHERE else: never
  echoed in a reply, never in an exception message, never in a log line. The
  reply to "store this key" is ``present: true``.
* A setting is written only through :func:`jarvis.config.save_setting`, which
  refuses keys outside ``SETTABLE``, values of the wrong type and values that
  look like credentials. This layer adds the choices only it knows (the voices
  that exist, the wake words with a model).
* Nothing here tells anybody to type a command. A refusal the desk wrote for a
  terminal ("store it with python -m jarvis secrets set ...") reaches the
  window as its headline alone, beside the button that does the fixing.

WHAT IS WRONG RIGHT NOW (``problems``) is read from rows, like everything the
window shows: the desk publishes ``desk.refused`` when it cannot start, and the
supervisor's own status says which processes are held. A refusal counts only
until the desk is running again — a fixed problem must not keep its card.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import Any

from jarvis import config as cfgmod
from jarvis import db, liveness
from jarvis import secrets as secrets_
from jarvis.bus import publish
from jarvis.window.snapshot import plain_sentence

__all__ = [
    "ADDRESSES",
    "MAX_SECRET",
    "READER_VOICES",
    "SECRET_NAMES",
    "SHOWN_SETTINGS",
    "UNITS",
    "Refusal",
    "SetupService",
    "find_problems",
    "latest_refusal",
    "restarts_for",
]

#: The credentials the window may store. Not google_oauth_client: that one is a
#: pasted JSON document, and a password field is the wrong place to paste it.
SECRET_NAMES: tuple[str, ...] = (
    "gemini_api_key",
    "telegram_bot_token",
    "github_token",
    "maxmind_license_key",
)

#: No key or token this tree uses is longer than this, and a request body that
#: is the body of a novel is not a key.
MAX_SECRET = 4096

READER_VOICES: tuple[str, ...] = (
    "en-GB-RyanNeural",
    "en-GB-ThomasNeural",
    "en-GB-SoniaNeural",
    "en-US-GuyNeural",
)
ADDRESSES: tuple[str, ...] = ("sir", "ma'am", "boss")
UNITS: tuple[str, ...] = ("metric", "imperial")

#: Every settable key, in the order a settings screen reads them.
SHOWN_SETTINGS: tuple[str, ...] = (
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
)

#: Which process reads a secret at startup and so must restart to see a new one.
#: The GitHub token and the MaxMind key are read when they are used.
_SECRET_RESTARTS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {"gemini_api_key": ("desk",), "telegram_bot_token": ("telegram",)}
)

_LABELS: Mapping[str, str] = MappingProxyType(
    {
        "gemini_api_key": "The Gemini API key",
        "telegram_bot_token": "The Telegram bot token",
        "github_token": "The GitHub token",
        "maxmind_license_key": "The MaxMind licence key",
        "desk": "The desk",
        "schedule": "The scheduler",
        "telegram": "Telegram",
    }
)

# A refusal whose fix is a button needs no detail, and its own text was written
# for a terminal; these say what is wrong in the window's voice instead.
_ACTION_SENTENCES: Mapping[str, str] = MappingProxyType(
    {
        "secret:gemini_api_key": "I need a Gemini API key before I can listen or speak.",
        "wake": "The wake-word model isn't on this computer yet.",
    }
)

_FALLBACK_LICENCE = (
    "openWakeWord's pretrained models are CC BY-NC-SA 4.0: personal, non-commercial use."
)


def restarts_for(key: str) -> tuple[str, ...]:
    """The processes that read ``key`` at startup, which must restart to use a new value.

    The desk reads its voice, its manner of address and the place its tools call
    "here" once, when it starts; the scheduler reads the time zone it fires
    reminders in. The app.* switches are read when the APP starts, and say so.
    """
    if key.startswith(("voice.", "persona.", "location.")):
        return ("desk",)
    if key == "tz":
        return ("desk", "schedule")
    return ()


# ───────────────────────────── problems, from rows ─────────────────────────────


@dataclass(frozen=True, slots=True)
class Refusal:
    """The desk's last ``desk.refused`` event."""

    ts: str
    sentence: str
    action: str | None


def latest_refusal(con: sqlite3.Connection) -> Refusal | None:
    """The newest ``desk.refused`` event, or None. A malformed payload is None too."""
    row = con.execute(
        "SELECT ts, payload FROM events WHERE kind='desk.refused' ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    try:
        body = json.loads(row[1])
    except (TypeError, ValueError):
        return None
    if not isinstance(body, dict):
        return None
    sentence = body.get("sentence")
    action = body.get("action")
    return Refusal(
        ts=str(row[0]),
        sentence=sentence if isinstance(sentence, str) else "",
        action=action if isinstance(action, str) and action else None,
    )


def _refusal_is_current(
    con: sqlite3.Connection, refusal: Refusal, desk: Mapping[str, Any] | None
) -> bool:
    """Is the desk still refusing, or has it run since?

    The supervisor knows best: a held desk is still refusing, and a running one
    is starting with whatever was just fixed. Without a supervisor (the window
    run on its own) the desk's heartbeat row decides: a beat written after the
    refusal means it got past it. The row is read RAW, staleness ignored, because
    "when did the desk last say anything" is exactly the question.
    """
    if desk is not None:
        if desk.get("held"):
            return True
        if desk.get("running"):
            return False
    row = con.execute(
        "SELECT updated_at FROM cursors WHERE name=?", (liveness.key("desk"),)
    ).fetchone()
    return row is None or refusal.ts > str(row[0])


def find_problems(
    con: sqlite3.Connection, processes: Mapping[str, Mapping[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """What is wrong right now, each with the action that fixes it.

    ``processes`` is the supervisor's status when the window runs inside the
    app: ``{name: {"running", "held", "reason", ...}}``. Every sentence is
    command-free; ``action`` is ``secret:<name>``, ``wake``, ``device``,
    ``voice``, ``restart:<process>`` or None.
    """
    procs = processes or {}
    out: list[dict[str, Any]] = []
    refusal = latest_refusal(con)
    desk = procs.get("desk") if isinstance(procs.get("desk"), Mapping) else None
    if refusal is not None and _refusal_is_current(con, refusal, desk):
        action = refusal.action
        sentence = _ACTION_SENTENCES.get(action or "") or plain_sentence(
            refusal.sentence, "The desk couldn't start."
        )
        if action and action.startswith("secret:") and action not in _ACTION_SENTENCES:
            sentence = f"{_LABELS.get(action[7:], action[7:])} is missing."
        out.append({"process": "desk", "sentence": sentence, "action": action})
    for name, proc in procs.items():
        if not isinstance(proc, Mapping) or not proc.get("held"):
            continue
        if any(p["process"] == name for p in out):
            continue  # the refusal already says why, with a better button
        who = _LABELS.get(name, name)
        sentence = plain_sentence(str(proc.get("reason") or ""), f"{who} stopped and is waiting.")
        out.append({"process": name, "sentence": sentence, "action": f"restart:{name}"})
    return out


# ───────────────────────────── the service ─────────────────────────────


class SetupService:
    """Everything the window's onboarding and Settings tab can do.

    Every collaborator is injected: the composition root passes the real
    PortAudio listing, wake-model download, voice sample, supervisor restart,
    start-with-Windows switch and Claude Code sign-in; tests pass fakes. Methods
    raise ``ValueError`` with a sentence for a bad request and ``RuntimeError``
    with a sentence when something they depend on failed — never a message that
    contains a credential, and never one that names a terminal command.
    """

    def __init__(
        self,
        *,
        config_path: str | Path | None,
        db_path: str | Path | None,
        secrets_mod: ModuleType | Any = secrets_,
        list_devices: Callable[[], list[dict[str, Any]]],
        wake_ready: Callable[[str], bool],
        download_wake: Callable[[str], str],
        preview_voice: Callable[[str, str], None] | None,
        restart: Callable[[str], None] | None,
        autostart: Callable[[bool], None] | None,
        claude_login: Callable[[], str] | None,
        control: Any | None = None,
        rescan_devices: Callable[[], None] | None = None,
        run_later: Callable[[Callable[[], None]], None] | None = None,
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._config_path = config_path
        self._db_path = db_path
        self._secrets = secrets_mod
        self._list_devices = list_devices
        self._wake_ready = wake_ready
        self._download_wake = download_wake
        self._preview_voice = preview_voice
        self._restart_fn = restart
        self._autostart = autostart
        self._claude_login = claude_login
        self._control = control
        self._rescan_devices = rescan_devices
        self._run_later = run_later or _daemon_thread
        self._clock = clock
        # Each save is read-modify-write of one file; two clicks at once must
        # not lose one. Downloads and samples are one-at-a-time by nature.
        self._save_lock = threading.Lock()
        self._wake_lock = threading.Lock()
        self._preview_lock = threading.Lock()

    # -- reading ------------------------------------------------------------

    def status(
        self, processes: Mapping[str, Mapping[str, Any]] | None = None, *, rescan: bool = False
    ) -> dict[str, Any]:
        """Everything the onboarding and Settings tab show, in one read.

        ``processes`` is the supervisor's status; without it, the ``control``
        given at construction is asked, if there is one. ``rescan`` looks at
        the audio hardware again first, for a headset plugged in since.
        """
        # Never under a playing sample: re-initialising PortAudio would pull
        # its stream out from under it.
        if (
            rescan
            and self._rescan_devices is not None
            and self._preview_lock.acquire(blocking=False)
        ):
            try:
                _quietly(self._rescan_devices, None)
            finally:
                self._preview_lock.release()
        problems: list[dict[str, Any]] = []
        try:
            cfg = cfgmod.load(self._config_path)
        except (ValueError, OSError) as exc:
            # A broken config.toml must not blank the settings screen that
            # could explain it: show the defaults and say what is wrong.
            cfg = cfgmod.Config()
            problems.append(
                {
                    "process": "config",
                    "sentence": plain_sentence(str(exc), "The settings file could not be read."),
                    "action": None,
                }
            )
        present = {name: self._present(name) for name in SECRET_NAMES}
        devices, devices_why = self._devices(cfg)
        if processes is None and self._control is not None:
            processes = _quietly(self._control.status, None)
        problems.extend(self._problems(processes))
        word = cfg.voice.wake_word
        return {
            "first_run": not present["gemini_api_key"]
            and not cfgmod.settings_path(self._config_path).exists(),
            "secrets": present,
            "wake": {
                "word": word,
                "phrase": _wake_phrase(word),
                "ready": True
                if not word
                else bool(_quietly(lambda: self._wake_ready(word), False)),
                "licence": _wake_licence(),
            },
            "phone": {"paired": self._phone_paired()},
            "devices": devices,
            "devices_why": devices_why,
            "settings": _settings(cfg),
            "choices": self._choices(cfg),
            "can": {
                "preview": self._preview_voice is not None,
                "restart": self._restart_fn is not None,
                "autostart": self._autostart is not None,
                "claude": self._claude_login is not None,
            },
            "problems": problems,
        }

    def _present(self, name: str) -> bool:
        # Presence only: the value is looked up and dropped in one expression.
        return bool(_quietly(lambda: self._secrets.get(name), None))

    def _devices(self, cfg: cfgmod.Config) -> tuple[list[dict[str, Any]], str]:
        try:
            found = self._list_devices()
        except Exception as exc:  # noqa: BLE001 - no PortAudio is a sentence, not a 500
            return [], plain_sentence(str(exc), "I couldn't list the microphones.")
        chosen = cfg.voice.input_device or ""
        out = []
        for d in found:
            label = str(d.get("label", "")) if isinstance(d, Mapping) else ""
            if label:
                out.append({"label": label, "selected": label == chosen})
        return out, ""

    def _choices(self, cfg: cfgmod.Config) -> dict[str, list[str]]:
        def with_current(options: tuple[str, ...], current: str) -> list[str]:
            # A value set by hand in config.toml stays selectable, rather than
            # the picker silently showing a different one.
            return list(options) if not current or current in options else [current, *options]

        return {
            "voice.gemini_voice": with_current(_gemini_voices(), cfg.voice.gemini_voice),
            "voice.reader_voice": with_current(READER_VOICES, cfg.voice.reader_voice),
            "persona.address": with_current(ADDRESSES, cfg.persona.address),
            "location.units": list(UNITS),
            "voice.wake_word": [*_wake_words(), ""],
        }

    def _problems(self, processes: Mapping[str, Mapping[str, Any]] | None) -> list[dict[str, Any]]:
        try:
            con = db.connect(self._db_path)
        except Exception as exc:  # noqa: BLE001 - the settings screen still renders
            why = plain_sentence(str(exc), "The database could not be opened.")
            return [{"process": "database", "sentence": why, "action": None}]
        try:
            return find_problems(con, processes)
        except sqlite3.Error as exc:
            why = f"I couldn't read what went wrong ({type(exc).__name__})."
            return [{"process": "database", "sentence": why, "action": None}]
        finally:
            con.close()

    # -- writing ------------------------------------------------------------

    def set_secret(self, name: str, value: str, *, restart: bool = True) -> dict[str, Any]:
        """Store one credential in the keyring. The reply says it is there, never what it is."""
        if name not in SECRET_NAMES:
            raise ValueError(
                f"I don't store {name!r} from here; I store {', '.join(SECRET_NAMES)}."
            )
        check_secret_value(value)
        try:
            self._secrets.store(name, value)
        except Exception as exc:  # noqa: BLE001 - its message may quote the value
            # `from None`: a chained exception is printed with any traceback,
            # and a keyring backend's message is free to include what it was given.
            raise RuntimeError(self._keyring_trouble(exc, value)) from None
        return {"ok": True, "present": True, "restarted": self._restart(name, restart)}

    def _keyring_trouble(self, exc: BaseException, value: str) -> str:
        why = ""
        check = getattr(self._secrets, "keyring_available", None)
        if callable(check):
            ok, said = _quietly(check, (True, ""))
            if not ok:
                why = plain_sentence(str(said))
        sentence = "I couldn't put it in the system keyring" + (
            f": {why}" if why else f" ({type(exc).__name__})."
        )
        return sentence.replace(value, "[hidden]") if value else sentence

    def set_setting(self, key: str, value: Any, *, restart: bool = True) -> dict[str, Any]:
        """Save one setting, and restart what reads it. ``ValueError`` names what was wrong."""
        if key == "voice.gemini_voice":
            voices = _gemini_voices()
            if not isinstance(value, str) or value not in voices:
                raise ValueError(f"{value!r} is not one of the Gemini voices.")
        if key == "voice.wake_word" and value not in ("", *_wake_words()):
            raise ValueError(f"There is no wake-word model called {value!r}.")
        with self._save_lock:
            if key == "app.start_with_windows":
                self._switch_autostart(value)
            else:
                cfgmod.save_setting(key, value, self._config_path)
        note = ""
        if key in ("app.start_telegram", "app.open_window_on_start"):
            note = "That takes effect the next time Jarvis starts."
        return {"ok": True, "restarted": self._restart_key(key, restart), "note": note}

    def _switch_autostart(self, value: Any) -> None:
        if not isinstance(value, bool):
            raise ValueError("app.start_with_windows is a switch: true or false.")
        if self._autostart is None:
            if value:
                raise RuntimeError("Starting with Windows isn't available on this computer.")
            cfgmod.save_setting("app.start_with_windows", value, self._config_path)
            return
        before = cfgmod.load(self._config_path).app.start_with_windows
        cfgmod.save_setting("app.start_with_windows", value, self._config_path)
        try:
            self._autostart(value)
        except Exception as exc:  # noqa: BLE001 - the switch must say what it really is
            cfgmod.save_setting("app.start_with_windows", before, self._config_path)
            why = plain_sentence(str(exc))
            raise RuntimeError(
                "I couldn't change whether I start with Windows" + (f": {why}" if why else ".")
            ) from exc

    def download_wake(self, *, restart: bool = True) -> dict[str, Any]:
        """Fetch the wake-word model if it is missing, then restart the desk to load it."""
        word = cfgmod.load(self._config_path).voice.wake_word
        licence = _wake_licence()
        if not word:
            return {
                "ok": True,
                "ready": True,
                "message": "There is no wake word set, so the desk listens all the time.",
                "licence": licence,
                "restarted": [],
            }
        with self._wake_lock:
            if _quietly(lambda: self._wake_ready(word), False):
                said = "The wake-word model is already here."
                # Already here, but a desk that started before it landed is
                # still held waiting for it; the button is how it is woken.
                restarted: list[str] = (
                    self._restart_names(("desk",), restart) if self._desk_held() else []
                )
            else:
                try:
                    said = self._download_wake(word) or "The wake-word model is in place."
                except Exception as exc:  # noqa: BLE001 - a network failure is a sentence
                    why = plain_sentence(str(exc))
                    raise RuntimeError(
                        "I couldn't fetch the wake-word model" + (f": {why}" if why else ".")
                    ) from exc
                restarted = self._restart_names(("desk",), restart)
        return {
            "ok": True,
            "ready": True,
            "message": plain_sentence(said, "The wake-word model is in place."),
            "licence": licence,
            "restarted": restarted,
        }

    def preview(self, voice: str) -> dict[str, Any]:
        """Play a sample of a Gemini voice, on a thread of its own. Returns at once."""
        if not isinstance(voice, str) or voice not in _gemini_voices():
            raise ValueError(f"{voice!r} is not one of the Gemini voices.")
        play = self._preview_voice
        if play is None:
            raise RuntimeError("Voice samples aren't available on this computer.")
        if not self._preview_lock.acquire(blocking=False):
            return {"ok": True, "message": "One moment; the last sample is still playing."}
        sentence = self.sample_line(voice)

        def run() -> None:
            try:
                play(voice, sentence)
            except Exception as exc:  # noqa: BLE001 - nobody waits on this; the feed hears it
                why = plain_sentence(str(exc))
                self._note(f"I couldn't play a sample of {voice}" + (f": {why}" if why else "."))
            finally:
                self._preview_lock.release()

        try:
            self._run_later(run)
        except Exception:
            self._preview_lock.release()
            raise
        return {"ok": True, "message": f"Playing {voice}."}

    def sample_line(self, voice: str) -> str:
        """What a voice sample says: Jarvis's manner, the user's address, the voice's name."""
        try:
            address = cfgmod.load(self._config_path).persona.address or "sir"
        except (ValueError, OSError):
            address = "sir"
        hour = self._clock().hour
        greeting = (
            "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"
        )
        return f"{greeting}, {address}. This is {voice}. Shall I carry on in this voice?"

    def pair_phone(self) -> dict[str, Any]:
        """A one-time code that binds the user's Telegram chat to the bot, shown once.

        Without a bound chat the bot drops every message, and the only other
        way to get a code is ``python -m jarvis.telegram --bind`` in a terminal,
        which a windowed app does not have. The code is never stored readably
        (:mod:`jarvis.telegram.identity`) and a new one replaces the last.
        """
        from jarvis.telegram import identity

        if not self._present("telegram_bot_token"):
            raise RuntimeError("Store the Telegram bot token first; the code is for that bot.")
        con = db.connect(self._db_path)
        try:
            code = identity.offer_code(con, by="window")
        finally:
            con.close()
        minutes = identity.DEFAULT_TTL_S // 60
        # The bot reads the offer from the database, but only while it runs.
        self._restart_names(("telegram",), not self._running("telegram"))
        return {
            "ok": True,
            "code": code,
            "minutes": minutes,
            "message": f"Send this code to your bot within {minutes} minutes.",
        }

    def unpair_phone(self) -> dict[str, Any]:
        """Forget the paired chat, so a new phone can be paired."""
        from jarvis.telegram import identity

        con = db.connect(self._db_path)
        try:
            existed = identity.unbind(con, by="window")
        finally:
            con.close()
        said = "Your phone is no longer paired." if existed else "No phone was paired."
        return {"ok": True, "message": said}

    def _phone_paired(self) -> bool:
        from jarvis.telegram import identity

        def paired() -> bool:
            con = db.connect(self._db_path)
            try:
                return identity.bound_chat(con) is not None
            finally:
                con.close()

        return bool(_quietly(paired, False))

    def _running(self, name: str) -> bool:
        if self._control is None:
            return True  # unknown: assume so rather than restart something that works
        processes = _quietly(self._control.status, None)
        proc = processes.get(name) if isinstance(processes, Mapping) else None
        return bool(isinstance(proc, Mapping) and proc.get("running"))

    def sign_in_claude(self) -> dict[str, Any]:
        """Open Claude Code's own sign-in. Jarvis never sees the account's credentials."""
        if self._claude_login is None:
            raise RuntimeError("Signing in to Claude Code isn't available from here.")
        try:
            said = self._claude_login()
        except Exception as exc:  # noqa: BLE001 - a missing CLI is a sentence
            why = plain_sentence(str(exc))
            raise RuntimeError(
                "I couldn't open the Claude Code sign-in" + (f": {why}" if why else ".")
            ) from exc
        return {"ok": True, "message": plain_sentence(said, "The Claude Code sign-in is open.")}

    # -- plumbing ------------------------------------------------------------

    def _restart_key(self, key: str, enabled: bool) -> list[str]:
        return self._restart_names(restarts_for(key), enabled)

    def _restart(self, secret: str, enabled: bool) -> list[str]:
        return self._restart_names(_SECRET_RESTARTS.get(secret, ()), enabled)

    def _desk_held(self) -> bool:
        """Whether the supervisor is holding the desk after a refusal. False when unknown."""
        if self._control is None:
            return False
        processes = _quietly(self._control.status, None)
        desk = processes.get("desk") if isinstance(processes, Mapping) else None
        return bool(isinstance(desk, Mapping) and desk.get("held"))

    def _restart_names(self, names: tuple[str, ...], enabled: bool) -> list[str]:
        """Restart each that exists; report the ones that did. Never raises.

        ``enabled`` is false while onboarding is still collecting answers: the
        desk restarts once at the end rather than once per step.
        """
        if not enabled or self._restart_fn is None:
            return []
        done = []
        for name in names:
            try:
                self._restart_fn(name)
            except Exception:  # noqa: BLE001 - a process the app does not run is not an error
                continue
            done.append(name)
        return done

    def _note(self, sentence: str) -> None:
        """A failure nobody is waiting on a response for goes to the feed."""
        try:
            con = db.connect(self._db_path)
        except Exception:  # noqa: BLE001
            return
        try:
            publish(con, "window.error", "window", {"text": sentence})
        except Exception:  # noqa: BLE001 - the feed was the only place left to say it
            pass
        finally:
            con.close()


def check_secret_value(value: Any) -> None:
    """A key is one unbroken run of printable characters. The message never quotes it."""
    if not isinstance(value, str) or not value:
        raise ValueError("The key is empty.")
    if len(value.encode("utf-8")) > MAX_SECRET:
        raise ValueError(f"That is longer than any key (over {MAX_SECRET // 1024} KiB).")
    if any(ch.isspace() for ch in value):
        raise ValueError(
            "A key has no spaces or line breaks in it; check that only the key was pasted."
        )
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise ValueError("That has control characters in it; paste only the key.")


def _settings(cfg: cfgmod.Config) -> dict[str, Any]:
    """Every settable value as JSON: tuples as lists, "no device" as ""."""
    out: dict[str, Any] = {}
    for key in SHOWN_SETTINGS:
        section, _, name = key.rpartition(".")
        value = getattr(getattr(cfg, section) if section else cfg, name)
        if value is None:
            value = ""
        elif isinstance(value, tuple):
            value = list(value)
        out[key] = value
    return out


def _gemini_voices() -> tuple[str, ...]:
    try:
        from jarvis.live.persona import GEMINI_VOICES
    except ImportError:
        # The live extra is missing; the default voice still works with the
        # Gemini key, so it is the one choice offered.
        return (cfgmod.Voice().gemini_voice,)
    return tuple(GEMINI_VOICES)


def _wake_words() -> tuple[str, ...]:
    try:
        from jarvis.audio.wake import PHRASES  # numpy: the voice extra
    except ImportError:
        return ("hey_jarvis",)
    return tuple(PHRASES)


def _wake_phrase(word: str) -> str:
    if not word:
        return ""
    try:
        from jarvis.audio.wake import PHRASES
    except ImportError:
        return word.replace("_", " ")
    found = PHRASES.get(word)
    return found[1] if found else word.replace("_", " ")


def _wake_licence() -> str:
    try:
        from jarvis.audio.wake import LICENCE
    except ImportError:
        return _FALLBACK_LICENCE
    # The licence says where the models are kept in the REPOSITORY's terms;
    # the first sentence is the part a person deciding to download needs.
    return plain_sentence(LICENCE.split(". ")[0], _FALLBACK_LICENCE)


def _quietly(fn: Callable[[], Any], fallback: Any) -> Any:
    try:
        return fn()
    except Exception:  # noqa: BLE001 - a probe that fails answers "no"
        return fallback


def _daemon_thread(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, name="setup-preview", daemon=True).start()
