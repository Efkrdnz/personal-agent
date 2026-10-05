"""``python -m jarvis`` — the composition root, and the only file allowed to be one.

Everything else in this tree points inwards; this file points at everything. That
is its job and it is the reason ``tools/check_layers.py`` exempts exactly one
path. The rule it still obeys, which no checker can enforce: THERE ARE NO
DECISIONS HERE. Anything below that is worth a unit test lives in a layer that
has one — the tool surface in :mod:`jarvis.tools`, the transcript window and the
tool adapter in :mod:`jarvis.voice.tools`, every sentence in the module that owns
the data behind it.

The commands, in the order somebody new to the machine needs them:

    python -m jarvis                the app: the window, the desk and the rest, no terminal
    python -m jarvis doctor         what is missing, and the command that fixes it
    python -m jarvis secrets set X  put a credential in the OS keyring
    python -m jarvis config init    write a config file with the defaults in it
    python -m jarvis status         what is running, as text
    python -m jarvis tools          the tool surface, per channel
    python -m jarvis run "..."      start a build and drive Claude Code
    python -m jarvis build          carry every spoken build request forward a step
    python -m jarvis pending        the questions waiting on you, numbered
    python -m jarvis answer 1 2     answer one, by the numbers you were read
    python -m jarvis desk           listen, talk, and drive Claude Code
    python -m jarvis chat           the same assistant by text, over the Gemini API
    python -m jarvis window         the HUD: status, the conversation and every tool
    python -m jarvis say "..."      speak with the built-in reader voice
    python -m jarvis weather        the weather here, or somewhere named
    python -m jarvis geo update     download GeoLite2, for "where am I"
    python -m jarvis remind         the reminders the scheduler will say
    python -m jarvis hearing list   the words Jarvis corrects when it mishears you
    python -m jarvis wake download  fetch the "hey Jarvis" model; `wake test` to measure
    python -m jarvis app --selftest check a built app from inside it, as JSON

With no command at all it runs ``app``, which is what double-clicking Jarvis.exe
does: :mod:`jarvis.app` starts the other processes and keeps them running.

``doctor`` is the important one. A voice assistant that fails at startup fails
with no screen and no log the user will find, so the whole of "why won't it
start" is one command that names the gap AND the command that closes it.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jarvis import answers as ans
from jarvis import config as cfgmod
from jarvis import db, jobs, kill, ledger, presence, reconcile, secrets
from jarvis import requests as rq

__all__ = ["build_parser", "main"]

OK = "ok     "
WARN = "warn   "
BAD = "MISSING"

#: Extras, what breaks without them, and the command that installs them. Kept as
#: data so ``doctor`` and the ``desk`` refusal path cannot disagree about which
#: package is needed for what.
EXTRAS: tuple[tuple[str, str, str, bool], ...] = (
    ("keyring", "secrets", "credentials fall back to environment variables", False),
    ("claude_agent_sdk", "cc", "Claude Code cannot be driven at all", True),
    ("google.genai", "live", "there is no voice: Gemini Live is the conversation", True),
    ("numpy", "voice", "no audio graph, so no microphone", True),
    ("sounddevice", "voice", "no sound card access", True),
    ("soxr", "voice", "no resampling between 48 kHz and 16 kHz", True),
    ("pywebrtc_audio", "aec", "open speakers cannot barge in; a headset still works", False),
    # One rung of the reader's ladder, not a requirement: the OS's own voice
    # (espeak-ng / say / SAPI) reads exact text with nothing installed. What
    # the desk requires is SOME deterministic rung, and doctor checks that
    # directly in its "reader voice" section.
    ("edge_tts", "tts", "one fewer reader voice (Microsoft's, over the network)", False),
    # Not required in the table: with voice.wake_word = "" the desk listens
    # all the time and needs no model. When a wake word IS configured the desk
    # refuses to start without it, and doctor's "wake word" section says why.
    ("onnxruntime", "wake", "no wake word — the desk can only listen all the time", False),
)


def _installed(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


@dataclass
class Report:
    """Lines to print, and whether anything blocking was found."""

    lines: list[str]
    blocking: int = 0

    def add(self, mark: str, text: str) -> None:
        self.lines.append(f"{mark}  {text}")
        if mark == BAD:
            self.blocking += 1

    def section(self, title: str) -> None:
        self.lines.append("")
        self.lines.append(f"── {title} " + "─" * max(0, 60 - len(title)))


# ───────────────────────────── doctor ─────────────────────────────


def _check_runtime(r: Report) -> None:
    r.section("runtime")
    v = sys.version_info
    mark = OK if v >= (3, 11) else BAD
    r.add(mark, f"python {v.major}.{v.minor}.{v.micro}  (3.11+ required)")
    sqlite_ok = tuple(int(p) for p in sqlite3.sqlite_version.split(".")) >= db.MIN_SQLITE
    r.add(
        OK if sqlite_ok else BAD,
        f"sqlite {sqlite3.sqlite_version}  "
        f"({'.'.join(map(str, db.MIN_SQLITE))}+ for UPDATE...RETURNING)",
    )


def _check_database(r: Report, path: str | None) -> None:
    from jarvis.bus import verify_chain

    r.section("database")
    target = Path(path) if path else db.default_path()
    r.add(OK, f"{target}")
    try:
        con = db.open_db(target)
    except Exception as exc:  # noqa: BLE001 - the failure IS the finding
        r.add(BAD, f"cannot open it: {type(exc).__name__}: {exc}")
        return
    try:
        version = con.execute("PRAGMA user_version").fetchone()[0]
        jobs_open = con.execute(
            "SELECT COUNT(*) c FROM jobs WHERE state NOT IN ('done','failed','killed','orphaned')"
        ).fetchone()["c"]
        broken = verify_chain(con)
        r.add(OK, f"schema version {version}, {jobs_open} job(s) not finished")
        # The hash chain is the honesty claim in R5. A break means somebody
        # edited the log, which is worth a loud line rather than a debug flag.
        r.add(
            OK if broken is None else BAD,
            "activity log chain verifies"
            if broken is None
            else f"ACTIVITY LOG CHAIN IS BROKEN at {broken}",
        )
    finally:
        con.close()


def _check_config(r: Report, path: str | None) -> cfgmod.Config | None:
    r.section("config")
    target = Path(path).expanduser() if path else cfgmod.default_path()
    try:
        cfg = cfgmod.load(target)
    except cfgmod.SecretInConfig as exc:
        r.add(BAD, f"{target}: {exc}")
        return None
    except Exception as exc:  # noqa: BLE001
        r.add(BAD, f"{target} will not parse: {type(exc).__name__}: {exc}")
        return None
    if target.exists():
        r.add(OK, f"{target}")
    else:
        r.add(WARN, f"{target} does not exist — using defaults ('python -m jarvis config init')")
    try:
        import zoneinfo

        zoneinfo.ZoneInfo(cfg.tz)
        r.add(OK, f"timezone {cfg.tz}, workspace {cfg.workspace_path}")
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        r.add(
            BAD,
            f"no timezone data for {cfg.tz!r} — on Windows: uv pip install tzdata "
            "(the scheduler and every spoken time need it)",
        )
    r.add(OK, f"claude {cfg.desk.model}, effort {cfg.desk.effort}, mode {cfg.desk.permission_mode}")
    r.add(OK, f"gemini {cfg.voice.model}, voice {cfg.voice.gemini_voice}")
    r.add(
        WARN,
        f"text model {cfg.voice.text_model} — from the SDK's model list, not yet called "
        f"from here; a 404 means change voice.text_model in config.toml",
    )
    if not cfg.desk.github_owner:
        r.add(
            WARN,
            "desk.github_owner is empty — a build cannot create a repository without "
            "knowing whose account it goes under",
        )
    return cfg


def _check_secrets(r: Report) -> None:
    r.section("credentials")
    ok, why = secrets.keyring_available()
    r.add(OK if ok else WARN, why)
    for found in secrets.probe():
        mark = OK if found.ok else (BAD if found.secret.required else WARN)
        r.add(mark, found.detail())
    if any(p.blocking for p in secrets.probe()):
        r.lines.append("")
        r.lines.append("         to fix:  python -m jarvis secrets set gemini_api_key")


def _check_packages(r: Report) -> None:
    r.section("packages")
    for module, extra, breaks, required in EXTRAS:
        if _installed(module):
            r.add(OK, f"{module}")
        else:
            r.add(
                BAD if required else WARN,
                f'{module} — without it, {breaks}  (pip install -e ".[{extra}]")',
            )


def claude_cli_path() -> str | None:
    """The CLI the DRIVER would actually run, which is usually not the one on PATH.

    ``claude-agent-sdk`` ships its own pinned CLI and prefers it; ``shutil.which``
    is only its fallback. Checking PATH alone gets this wrong in BOTH directions:
    it reports "missing" on a machine where ``pip install -e ".[cc]"`` is all that
    is needed, and it reports the version of a binary the driver will never
    execute. CLAUDE.md pins the measured facts to one CLI build, so which build
    runs is not a detail.
    """
    try:
        from claude_agent_sdk._internal.transport.subprocess_cli import (
            SubprocessCLITransport,
        )

        bundled = SubprocessCLITransport.__new__(SubprocessCLITransport)._find_bundled_cli()
        if bundled:
            return str(bundled)
    except Exception:  # noqa: BLE001 - a private path; PATH is the documented fallback
        pass
    return shutil.which("claude")


def _check_claude_cli(r: Report) -> None:
    r.section("claude code")
    exe = claude_cli_path()
    if exe is None:
        r.add(BAD, 'no `claude` CLI — the SDK bundles one; pip install -e ".[cc]"')
        return
    bundled = "bundled with the SDK" if "claude_agent_sdk" in exe else "from PATH"
    try:
        out = subprocess.run(  # noqa: S603 - a fixed argv, no shell
            [exe, "--version"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            # CREATE_NO_WINDOW: invisible from a terminal, and from the windowed
            # exe, which has no console to lend, claude.exe would flash its own.
            creationflags=0x08000000 if sys.platform == "win32" else 0,
        )
        version = (out.stdout or out.stderr).strip().splitlines()[:1]
        r.add(OK, f"{version[0] if version else '(version unknown)'}  ({bundled})")
        r.add(OK, f"  {exe}")
    except (OSError, subprocess.SubprocessError) as exc:
        r.add(WARN, f"{exe} would not report a version: {type(exc).__name__}")


def _check_audio(r: Report, cfg: cfgmod.Config | None) -> tuple[bool, str]:
    """Run the EXACT selection ``_build_desk`` runs, and report what it says.

    An earlier version of this listed devices and marked a failure only when some
    were enumerable and none were duplex. That check passes on most of the ways
    the desk actually refuses — no PortAudio at all, a host with no default
    device, a laptop whose default input and default output are two different
    devices (``ClockSplit``), a configured ``input_device`` that matches two
    devices or none. So ``doctor`` could end with "Everything required is
    present" on a machine where ``desk`` exits 2 one second later, which is the
    worst available bug in a command whose entire job is to say "you are ready".

    The fix is not a longer list of conditions to keep in sync — it is calling
    the same function. Returns (ready, why) for the readiness summary.
    """
    r.section("audio")
    if not _installed("sounddevice"):
        why = 'sounddevice is not installed (pip install -e ".[voice]")'
        r.add(BAD, why)
        return False, why
    from jarvis.audio import DEV_RATE
    from jarvis.audio.devices import (
        DeviceError,
        PortAudioProbe,
        select_duplex_device,
        usable_devices,
    )

    probe = PortAudioProbe()
    try:
        found = list(probe.devices())
        usable = usable_devices(found)
        r.add(OK, f"{len(found)} endpoint(s); {len(usable)} device(s) can both listen and speak")
        for label in usable[:6]:
            r.add(OK, f"  {label}")
    except DeviceError as exc:
        r.add(BAD, str(exc))
        return False, str(exc)

    name = cfg.voice.input_device if cfg is not None else None
    try:
        selection = select_duplex_device(probe, name=name, samplerate=DEV_RATE)
    except DeviceError as exc:
        r.add(BAD, f"{type(exc).__name__}: {exc}")
        return False, str(exc)
    r.add(OK, f"desk would use: {selection.describe()}")
    return True, ""


def _check_reader(r: Report, cfg: cfgmod.Config | None) -> None:
    """Blocking only when NO rung can read exact text — the desk refuses then too."""
    r.section("reader voice")
    if cfg is None:
        return
    ladder = reader_engines(cfg)
    for e in ladder:
        trust = (
            "reads exact text" if (e.deterministic or e.verified) else "everything but exact text"
        )
        r.add(OK, f"{e.name:8} {trust}")
    if not any(e.deterministic or e.verified for e in ladder):
        r.add(
            BAD,
            "nothing can read an option word for word — `sudo apt install espeak-ng` "
            "(macOS/Windows have a voice built in)",
        )


def _wake_ready(phrase: str) -> bool:
    from jarvis.audio.wake import PHRASES, default_model_dir, missing

    return (
        phrase in PHRASES and _installed("onnxruntime") and not missing(phrase, default_model_dir())
    )


def _check_wake(r: Report, cfg: cfgmod.Config | None) -> None:
    """Blocking only for the desk, and only when a wake word is configured."""
    from jarvis.audio.wake import PHRASES, default_model_dir, missing

    r.section("wake word")
    if cfg is None:
        return
    if not cfg.voice.wake_word:
        r.add(WARN, 'voice.wake_word = "": the desk listens all the time')
        return
    if cfg.voice.wake_word not in PHRASES:
        r.add(BAD, f"no wake model called {cfg.voice.wake_word!r}; have {', '.join(PHRASES)}")
        return
    if not _installed("onnxruntime"):
        r.add(BAD, 'onnxruntime is not installed: pip install -e ".[wake]"')
        return
    where = default_model_dir()
    gone = missing(cfg.voice.wake_word, where)
    if gone:
        r.add(BAD, f"wake model missing in {where}: python -m jarvis wake download")
        return
    phrase = PHRASES[cfg.voice.wake_word][1]
    r.add(
        OK,
        f"'{phrase}' wakes the desk (threshold {cfg.voice.wake_threshold}, "
        f"{cfg.voice.wake_window_s:.0f}s window); `python -m jarvis wake test` to measure",
    )
    from jarvis.audio.wake import LICENCE

    r.add(WARN, LICENCE)


def _check_hearing(r: Report, cfg: cfgmod.Config | None, db_path: str | None) -> None:
    """Never blocking: an empty lexicon still transcribes, it just mishears more."""
    from jarvis import hearing

    r.section("hearing")
    if cfg is None:
        return
    try:
        con = db.open_db(db_path)
    except Exception as exc:  # noqa: BLE001 - the database section already said why
        r.add(WARN, f"could not read the lexicon: {exc}")
        return
    try:
        lex = hearing.lexicon(con, extra_terms=cfg.voice.vocabulary)
    finally:
        con.close()
    fixes = [e for e in lex.entries if e.heard_as]
    r.add(
        OK,
        f"{len(fixes)} word(s) corrected from context ({', '.join(e.term for e in fixes)}); "
        "`python -m jarvis hearing list` shows them",
    )
    if cfg.voice.asr_vocabulary:
        r.add(
            OK,
            f"recogniser biased toward {len(hearing.vocabulary(lex))} phrases — UNVERIFIED on "
            "the Live server; set voice.asr_vocabulary = false if the desk cannot connect",
        )
    r.add(
        OK if cfg.voice.hearing_arbiter else WARN,
        "doubtful words go to the text model for a vote"
        if cfg.voice.hearing_arbiter
        else "voice.hearing_arbiter = false: doubtful words are left as heard",
    )


def _check_location(r: Report, cfg: cfgmod.Config | None) -> None:
    """Never blocking: weather for a NAMED place works with none of this."""
    import datetime as _dt

    r.section("location")
    if cfg is None:
        return
    loc = cfg.location
    if loc.latitude is not None and loc.longitude is not None:
        r.add(OK, f"configured coordinates {loc.latitude}, {loc.longitude} — exact, no lookup")
        return
    if loc.city:
        r.add(OK, f"configured city {loc.city!r} — exact, geocoded once per question")
        return
    path = locator_for(cfg).db_path
    if not _installed("maxminddb"):
        r.add(
            WARN, 'no city or coordinates set, and maxminddb is missing (pip install -e ".[geo]")'
        )
        return
    if not path.exists():
        r.add(
            WARN,
            f"no GeoLite2 database at {path} — `python -m jarvis geo update`, or set "
            "[location] city in config.toml",
        )
        return
    age = _dt.datetime.now() - _dt.datetime.fromtimestamp(path.stat().st_mtime)
    stale = age.days > 30
    r.add(
        WARN if stale else OK,
        f"GeoLite2 at {path}, {age.days} day(s) old"
        + (" — refresh it: `python -m jarvis geo update`" if stale else ""),
    )


def _check_tool_surface(r: Report) -> None:
    from jarvis.live.profiles import DESK, PHONE_USER
    from jarvis.tools.default import registry
    from jarvis.voice.tools import LiveTools

    r.section("tool surface")
    reg = registry()
    for profile, channel in ((DESK, "desk"), (PHONE_USER, "phone")):
        lt = LiveTools(registry=reg, open_db=db.open_db, channel=channel)
        offered = [d["name"] for d in lt.declarations(profile)]
        r.add(OK, f"{channel}: {', '.join(offered) or 'nothing'}")
        # Both directions of the drift, because neither is an error anywhere.
        if missing := lt.unresolved(profile):
            r.add(
                WARN, f"  {channel} profile names tools that do not exist yet: {', '.join(missing)}"
            )
        if unreachable := lt.unreachable(profile):
            r.add(WARN, f"  built but never offered on {channel}: {', '.join(unreachable)}")


def _readiness(r: Report, cfg: cfgmod.Config | None, audio_ok: bool, audio_why: str) -> None:
    """Per-command readiness, because "ready" is not one fact.

    The earlier version printed a single "Everything required is present", which
    was wrong in both directions: it failed a headless box that only wanted the
    Telegram bot, and — worse — it passed machines where ``desk`` exits two
    seconds later, because the audio check was a warning. A verdict per entry
    point is the honest shape, and it is also the answer to the question people
    actually arrive with, which is "what can I run".

    The processes are listed here because nothing else lists them. ``python -m
    jarvis`` does not own ``jarvis.cc``, ``jarvis.telegram`` or
    ``jarvis.schedule`` — they are separate daemons — and a front door that does
    not mention the other three doors is not a front door.
    """
    r.section("what you can run")
    have = {s.secret.name: s.ok for s in secrets.probe()}
    missing_pkgs = [m for m, _, _, required in EXTRAS if required and not _installed(m)]

    def verdict(ok: bool, cmd: str, why: str) -> None:
        r.add(OK if ok else BAD, f"{cmd:<28} {'' if ok else '— ' + why}")

    verdict(True, "python -m jarvis status", "")
    verdict(True, "python -m jarvis tools", "")

    desk_why = ""
    if missing_pkgs:
        desk_why = f"missing packages: {', '.join(missing_pkgs)}"
    elif not have.get("gemini_api_key"):
        desk_why = "no gemini_api_key"
    elif not audio_ok:
        desk_why = audio_why
    elif cfg is not None and cfg.voice.wake_word and not _wake_ready(cfg.voice.wake_word):
        desk_why = 'no wake model (python -m jarvis wake download), or set voice.wake_word = ""'
    verdict(not desk_why, "python -m jarvis desk", desk_why)

    cc_why = "" if claude_cli_path() else 'no claude CLI (pip install -e ".[cc]")'
    verdict(not cc_why, "python -m jarvis.cc", cc_why)
    verdict(not cc_why, "python -m jarvis run", cc_why)
    chat_why = "" if have.get("gemini_api_key") else "no gemini_api_key"
    if not chat_why and not _installed("google.genai"):
        chat_why = 'google-genai is not installed (pip install -e ".[live]")'
    verdict(not chat_why, "python -m jarvis chat", chat_why)
    r.add(OK, f"{'python -m jarvis window':<28} — the HUD; chat in it needs the same key")
    r.add(OK, f"{'python -m jarvis app':<28} — all of it, in a window; what Jarvis.exe runs")
    build_why = "" if have.get("gemini_api_key") else "no gemini_api_key (it tidies your words)"
    r.add(
        OK if not build_why else WARN,
        f"{'python -m jarvis build':<28} {'' if not build_why else '— ' + build_why}",
    )
    r.add(OK, f"{'python -m jarvis pending/answer':<28} — see and settle open questions")

    tg_why = "" if have.get("telegram_bot_token") else "no telegram_bot_token (feature off)"
    tail = "" if not tg_why else f"— {tg_why}"
    r.add(OK if not tg_why else WARN, f"{'python -m jarvis.telegram':<28} {tail}")
    r.add(
        OK,
        f"{'python -m jarvis.schedule':<28} — the 10am gate, reminders, and routing questions",
    )
    del cfg


def cmd_doctor(args: argparse.Namespace) -> int:
    r = Report(lines=[])
    _check_runtime(r)
    cfg = _check_config(r, args.config)
    _check_secrets(r)
    _check_packages(r)
    _check_database(r, args.db)
    _check_claude_cli(r)
    audio_ok, audio_why = _check_audio(r, cfg)
    _check_location(r, cfg)
    _check_wake(r, cfg)
    _check_hearing(r, cfg, args.db)
    _check_reader(r, cfg)
    _check_tool_surface(r)
    _readiness(r, cfg, audio_ok, audio_why)

    print("\n".join(r.lines).strip())
    print()
    if r.blocking:
        print(f"{r.blocking} blocking problem(s) above. Each MISSING line names its own fix.")
        return 1
    print("Nothing blocking. The 'what you can run' list above is what will actually start.")
    return 0


# ───────────────────────────── secrets ─────────────────────────────


def cmd_secrets(args: argparse.Namespace) -> int:
    if args.action == "list":
        ok, why = secrets.keyring_available()
        print(f"{OK if ok else WARN}  {why}")
        for found in secrets.probe():
            print(found.line())
        return 1 if any(p.blocking for p in secrets.probe()) else 0

    if args.action == "forget":
        print(f"removed {args.name}" if secrets.forget(args.name) else f"{args.name} was not set")
        return 0

    # set. The value is read from a prompt or from stdin, NEVER from argv: a
    # credential passed as an argument is in the shell history and in `ps`.
    import getpass

    value = (
        sys.stdin.readline().strip()
        if not sys.stdin.isatty()
        else getpass.getpass(f"{args.name} (input hidden): ")
    )
    if not value:
        print("nothing entered; nothing stored", file=sys.stderr)
        return 1
    try:
        secrets.store(args.name, value)
    except ImportError:
        print(
            'the keyring package is not installed (pip install -e ".[secrets]").\n'
            f"Until then, export {secrets.SECRETS[0].env}=... in the shell that runs Jarvis.",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"could not store it: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"stored {args.name} in the {secrets.KEYRING_SERVICE} keyring")
    return 0


# ───────────────────────────── config ─────────────────────────────


def cmd_config(args: argparse.Namespace) -> int:
    path = Path(args.config).expanduser() if args.config else cfgmod.default_path()
    if args.action == "path":
        print(path)
        return 0
    if args.action == "init":
        if path.exists() and not args.force:
            print(f"{path} already exists; --force to overwrite", file=sys.stderr)
            return 1
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(cfgmod.EXAMPLE, encoding="utf-8")
        print(f"wrote {path}")
        return 0
    cfg = cfgmod.load(path)
    print(f"# {path}{'' if path.exists() else '  (does not exist — these are the defaults)'}")
    for section in ("voice", "desk", "briefing"):
        print(f"\n[{section}]")
        block = getattr(cfg, section)
        for field_name in block.__dataclass_fields__:
            print(f"{field_name} = {getattr(block, field_name)!r}")
    print(f"\ntz = {cfg.tz!r}\nspend_threshold_usd = {cfg.spend_threshold_usd!r}")
    return 0


# ───────────────────────────── status ─────────────────────────────


def cmd_status(args: argparse.Namespace) -> int:
    con = db.open_db(args.db)
    try:
        cfg = cfgmod.load(args.config)
        reconcile.reconcile(con, actor="cli")
        st = reconcile.project_status(con)
        for line in st.lines:
            print(line)
        print()
        print(
            ledger.spoken_status(
                ledger.status(con, "today", config=ledger.LedgerConfig(cfg.spend_threshold_usd))
            )
        )
        verdict = presence.evaluate_presence(con)
        print(f"presence: {verdict.state} — {verdict.reason}")
    finally:
        con.close()
    return 0


def locator_for(cfg: cfgmod.Config) -> Any:
    """The process's Locator, from ``[location]``. Wiring only."""
    from jarvis.geo import Locator, default_db_path

    loc = cfg.location
    return Locator(
        city=loc.city,
        latitude=loc.latitude,
        longitude=loc.longitude,
        ip=loc.ip,
        db_path=Path(loc.geoip_db).expanduser() if loc.geoip_db else default_db_path(),
        language=loc.language,
    )


def tool_extra(cfg: cfgmod.Config, api_key: str | None = None) -> dict[str, Any]:
    """What every channel's tools get in ``ctx.extra``. One place, so they cannot differ."""
    extra: dict[str, Any] = {
        "spend_threshold_usd": cfg.spend_threshold_usd,
        "locator": locator_for(cfg),
        "units": cfg.location.units,
        "tz": cfg.tz,
    }
    if api_key and cfg.voice.web_search and _installed("google.genai"):
        from jarvis.live.text import GeminiSearch

        extra["search"] = GeminiSearch(api_key=api_key, model=cfg.voice.text_model)
    return extra


def hearing_for(cfg: cfgmod.Config, api_key: str | None = None) -> Any:
    """The transcript corrector every voice channel hands its tools.

    The lexicon is re-read on every call rather than captured here: a word the
    user teaches one sentence ago has to be in force for this one, and it was
    written by a tool call on another connection.
    """
    from jarvis import hearing

    arbiter = None
    if api_key and cfg.voice.hearing_arbiter and _installed("google.genai"):
        from jarvis.live.text import GeminiArbiter, GeminiText

        arbiter = GeminiArbiter(GeminiText(api_key=api_key, model=cfg.voice.text_model))
    terms = cfg.voice.vocabulary

    def hear(con: sqlite3.Connection, text: str) -> Any:
        return hearing.correct(text, hearing.lexicon(con, extra_terms=terms), arbiter=arbiter)

    return hear


def remembered(con: sqlite3.Connection) -> str:
    """The user's notes, as a paragraph any model's instructions can carry."""
    from jarvis import memory

    facts = [n.text for n in memory.notes(con)]
    if not facts:
        return ""
    return "What the user has asked you to remember (their words, newest first):\n" + "\n".join(
        f"- {f}" for f in facts
    )


def heard_profile(cfg: cfgmod.Config, con: sqlite3.Connection, prof: Any) -> Any:
    """A Live profile that knows this user: their mishearings, and their notes.

    Two different listeners for the mishearings: the recogniser gets
    ``custom_vocabulary`` so it writes "quote" in the first place, and the
    conversational model, which hears the AUDIO rather than any transcript,
    gets the same list in words.
    """
    from dataclasses import replace

    from jarvis import hearing
    from jarvis.live import persona as manner

    lex = hearing.lexicon(con, extra_terms=cfg.voice.vocabulary)
    told = hearing.instruction(lex)
    # How Jarvis addresses the user is a setting, so the two profiles that talk
    # to the user are rebuilt with it; any other profile keeps its own words.
    addressed = {"desk": manner.desk_instruction, "phone_user": manner.phone_instruction}
    build = addressed.get(prof.name)
    base = build(cfg.persona.address, cfg.persona.name) if build else prof.system_instruction
    return replace(
        prof,
        model=cfg.voice.model,
        voice=cfg.voice.gemini_voice,
        system_instruction="\n\n".join(x for x in (base, told, remembered(con)) if x),
        vocabulary=hearing.vocabulary(lex) if cfg.voice.asr_vocabulary else (),
        language_codes=tuple(cfg.voice.languages),
    )


def cmd_tools(args: argparse.Namespace) -> int:
    from jarvis.tools.default import registry
    from jarvis.tools.registry import ALL_CHANNELS

    reg = registry()
    for channel in ALL_CHANNELS:
        print(f"\n{channel}:")
        for tool in sorted(reg.for_channel(channel), key=lambda t: t.name):
            tag = "  (long-running)" if tool.long_running else ""
            print(f"  {tool.name}{tag}")
            if args.verbose:
                print(f"      {tool.description}")
    return 0


# ───────────────────────────── run, pending, answer ─────────────────────────────

#: How often `run` looks for a question its child has raised. The child blocks
#: inside `can_use_tool` until the row is answered, so this is the latency
#: between Claude asking and the terminal saying so — not a timeout on anything.
RUN_POLL_S = 0.5

#: How often the desk looks for a question routed to it. Two seconds because the
#: rung is written by another process on its own tick, and a question the user is
#: waiting on should not sit unread for longer than they would tolerate silence.
DESK_POLL_S = 2.0


def _spawn_runner(
    job_id: str, db_path: str | None, *, resume: bool = False, prompt: str | None = None
):
    """`python -m jarvis.cc` as its own OS process, exactly as the docstring says.

    In-process would be simpler and wrong: the driver is designed to be killed,
    resumed and reparented, and `jarvis/cc/__main__.py` documents exit codes for
    a supervisor to read. A build must outlive the terminal that started it.

    ``env`` is the whole environment plus JARVIS_DB. A bare dict would strip
    PATH, HOME and XDG_*, and the CLI the SDK bundles would not start.
    """
    argv = [sys.executable, "-m", "jarvis.cc", "--job-id", job_id, "--channel", "cli"]
    if db_path:
        argv += ["--db", db_path]
    if resume:
        argv += ["--resume"]
    elif prompt is not None:
        argv += ["--prompt", prompt]
    env = {**os.environ}
    if db_path:
        env["JARVIS_DB"] = db_path
    # INHERIT the terminal rather than piping. A PIPE nobody reads is two bugs:
    # the child's refusal — exit 2 is "a settings file would auto-close a pending
    # question", which is a thing the user must see — is discarded, and a chatty
    # child eventually BLOCKS forever on a full pipe buffer while the parent
    # polls for questions that can never come.
    return subprocess.Popen(argv, env=env)


def resume_command(job: jobs.Job | None) -> str:
    """The command that picks THIS job up. One place, because getting it wrong hurt.

    ``jarvis answer`` used to print ``run --resume`` for every parked job. For a
    ``repo_setup`` row that handed the build request to the Claude Code driver,
    which failed it terminally — so the sentence printed after a user approved
    their build was the command that destroyed it.
    """
    if job is not None and job.kind == "repo_setup":
        return "python -m jarvis build"
    return "python -m jarvis run --resume"


def cmd_run(args: argparse.Namespace) -> int:
    """Create the `claude_code` job and drive it. The front door the driver lacked.

    Until this existed the only caller of `create_job(kind='claude_code')`
    outside tests was a spike script, so "drive Claude Code" meant "write Python".
    """
    if not _installed("claude_agent_sdk") or claude_cli_path() is None:
        print(
            'Claude Code is not installed here: pip install -e ".[cc]"\n'
            "Run `python -m jarvis doctor` for the whole picture.",
            file=sys.stderr,
        )
        return 2

    cfg = cfgmod.load(args.config)
    cwd = Path(args.into).expanduser().resolve() if args.into else Path.cwd()
    if not cwd.is_dir():
        print(f"{cwd} is not a directory", file=sys.stderr)
        return 2

    con = db.open_db(args.db)
    try:
        if args.resume:
            return _resume(con, args)
        job = jobs.create_job(
            con,
            kind="claude_code",
            title=args.title or " ".join(args.prompt.split())[:60],
            created_by="cli",
            actor="cli",
            cwd=str(cwd),
            model=args.model or cfg.desk.model,
            effort=args.effort or cfg.desk.effort,
            permission_mode=args.permission_mode or cfg.desk.permission_mode,
            prompt_text=args.prompt,
            # Without this the job is born at epoch 0 and `assert_epoch` refuses
            # to start it on any machine where the kill switch has EVER fired.
            kill_epoch=kill.current_epoch(con),
        )
        print(f"job {job.id}  in {cwd}  ({job.model}, {job.effort}, {job.permission_mode})")
        try:
            child = _spawn_runner(job.id, args.db, prompt=args.prompt)
        except OSError as exc:
            # Otherwise the row sits `queued` forever: reconcile only scans
            # ACTIVE_STATES, so nothing in the system would ever look at it again.
            jobs.set_state(con, job.id, "failed", actor="cli", stop_reason=f"spawn failed: {exc}")
            print(f"could not start the runner: {exc}", file=sys.stderr)
            return 2
        return _watch(con, job.id, child)
    finally:
        con.close()


def _resume(con: sqlite3.Connection, args: argparse.Namespace) -> int:
    """Pick up every job parked on an answer that has since arrived.

    This is the ONE spawner in the tree. ``reconcile`` deliberately refuses to
    claim a resume when no spawner was passed — claiming without one would spend
    a resume attempt, move the job to a state nothing scans, and record that the
    build resumed when no runner exists. Every other process therefore gets
    ``resumable`` and this one gets ``resumed``.
    """
    children: dict[str, Any] = {}

    def spawn(job: jobs.Job) -> None:
        children[job.id] = _spawn_runner(job.id, args.db, resume=True)

    report = reconcile.reconcile(con, actor="cli", spawn=spawn)
    if not children:
        waiting = report.get("awaiting_answer") or []
        if waiting:
            print(
                f"{len(waiting)} job(s) are still waiting on an answer — `python -m jarvis pending`"
            )
        elif report.get("needs_human"):
            print(f"{len(report['needs_human'])} job(s) need a human: out of resume attempts.")
        else:
            print("nothing to resume.")
        # Guarded on `children`, not on report['resumed']: a spawner that threw
        # leaves the id in `resumed` and `spawn_errors` both, and waiting on an
        # empty sequence below would be a bare exception instead of a sentence.
        for err in report.get("spawn_errors") or ():
            print(f"could not start {err['job_id']}: {err['error']}", file=sys.stderr)
        return 1 if report.get("spawn_errors") else 0

    worst = 0
    for job_id, child in children.items():
        worst = max(worst, _watch(con, job_id, child))
    return worst


def _watch(con: sqlite3.Connection, job_id: str, child) -> int:
    """Print questions as they are raised, until the child exits."""

    announced: set[str] = set()

    def sweep() -> None:
        for req in rq.open_requests(con, job_id):
            if req.id in announced:
                continue
            announced.add(req.id)
            print()
            print(_render_request(req, len(announced)))

    while child.poll() is None:
        sweep()
        time.sleep(RUN_POLL_S)
    # ONE MORE SWEEP after the child is gone. A question raised between the last
    # poll and the exit is not a corner case — it is the entire defer path, where
    # the row is written and the process leaves immediately.
    sweep()

    job = jobs.get(con, job_id)
    if job is not None and job.state == "queued" and child.returncode != 0:
        # The child refused before it transitioned anything — a settings file it
        # would not run under, a forbidden permission mode, no auth. `queued` is
        # scanned by NOTHING, so the row would be invisible to every process in
        # the system forever. `parked` is re-queueable by hand.
        jobs.set_state(
            con, job_id, "parked", actor="cli", stop_reason=f"runner exited {child.returncode}"
        )
        job = jobs.get(con, job_id)
    state = job.state if job else "gone"
    print(f"\nrunner exited {child.returncode}; job is {state}")
    if state in ("deferred", "blocked"):
        print(f"parked on a question. Answer it, then:  {resume_command(job)}")
    elif job and job.result_summary:
        print(job.result_summary.strip()[:500])
    return 0 if child.returncode == 0 else 1


def _render_request(req: rq.Request, n: int) -> str:
    """One open question as numbered lines. The SAME numbering every channel uses.

    Built from ``presentation``, not from the raw payload: every request kind has
    a presentation and only ``plan_question`` has an AskUserQuestion payload, so
    rendering from the payload would silently show nothing for an exit-plan or a
    permission question — which are most of them.

    THE REQUEST ID IS PRINTED, not just the position. A position is resolved
    after the user has typed it, and the list shifts whenever anything else is
    answered — so between reading and typing, `answer 1 2` can approve a
    DIFFERENT question. With two builds running that is an `Allow` landing on a
    tool permission the user never read.
    """
    pres = req.presentation
    lines = [f"[{n}] {req.short_label}  ({req.kind})  {req.id}", f"    {pres['intro']}"]
    lines += [f"    {item['index']}. {item['label']}" for item in pres["items"]]
    lines.append(f"    answer with:  python -m jarvis answer {req.id} <number>")
    if pres.get("allows_free_text"):
        lines.append(f'    or:           python -m jarvis answer {req.id} --text "your own words"')
    return "\n".join(lines)


def cmd_pending(args: argparse.Namespace) -> int:
    con = db.open_db(args.db)
    try:
        open_ = rq.open_requests(con)
        if not open_:
            print("nothing is waiting on you.")
            return 0
        for n, req in enumerate(open_, start=1):
            print(_render_request(req, n))
            print()
    finally:
        con.close()
    return 0


def _resolve(open_: list[rq.Request], which: str) -> rq.Request | None:
    """A request id, or a position from the last `pending`. Ids are preferred.

    The positional form is kept because it is what a person types, but it is
    resolved against the CURRENT list — so if something was answered on another
    channel in between, it refuses rather than silently settling its neighbour.
    """
    for req in open_:
        if req.id == which:
            return req
    if which.isdigit() and 1 <= int(which) <= len(open_):
        return open_[int(which) - 1]
    return None


def cmd_answer(args: argparse.Namespace) -> int:
    """Answer the nth open question by the option numbers you were read.

    The numbers are the ones every channel shows, and they run 1..N across the
    WHOLE batch rather than restarting per question — so a three-question payload
    is answered `1 4 7`. That is the frozen array's own numbering; nothing here
    renumbers anything, and the model (or the human) may never name a label.
    """
    con = db.open_db(args.db)
    try:
        open_ = rq.open_requests(con)
        req = _resolve(open_, args.which)
        if req is None:
            count = len(open_)
            print(
                f"no open question {args.which!r}. There "
                f"{'is' if count == 1 else 'are'} {count} — run `python -m jarvis pending`",
                file=sys.stderr,
            )
            return 1
        try:
            answer = ans.build_answer(req, picks=tuple(args.picks), free_text=args.text)
        except Exception as exc:  # noqa: BLE001 - every shape error is the user's to read
            print(f"{exc}", file=sys.stderr)
            return 1

        # 'hud', not a new 'cli' mode. ANSWER_MODES is the vocabulary the whole
        # system reads back ("you approved that by voice"), and a typed answer at
        # a screen is exactly what 'hud' already means. Inventing a sixth word
        # for the same fact would make two of them mean the same thing.
        won = rq.answer_request(con, req.id, answer, answered_by="cli", answer_mode="hud")
        if not won:
            fresh = rq.get_request(con, req.id)
            print(f"too late — it was already answered ({fresh.state if fresh else 'gone'})")
            return 0
        print(f"answered: {req.short_label}")
        job = jobs.get(con, req.job_id) if req.job_id else None
        if job and job.state == "deferred":
            # DEFERRED only. `blocked` means a runner IS sitting on this row and
            # polling — it picks the answer up by itself within a second, and
            # telling the user to resume it would be an instruction to do
            # nothing, printed at the moment they most want to believe one.
            print(f"that job is parked — pick it up with:  {resume_command(job)}")
    finally:
        con.close()
    return 0


# ───────────────────────────── say ─────────────────────────────


def _play(pcm: bytes, rate: int) -> str | None:
    """Play PCM16 mono on the default device. None, or the sentence saying why not."""
    try:
        import numpy as np
        import sounddevice as sd

        sd.play(np.frombuffer(pcm, dtype="<i2"), rate)
        sd.wait()
    except Exception as exc:  # noqa: BLE001 - no PortAudio, no device: say where to look instead
        return f"couldn't play it ({exc})"
    return None


def cmd_say(args: argparse.Namespace) -> int:
    """Speak a sentence with the reader's ladder — to the speakers, or to a WAV file."""
    import asyncio
    import wave

    from jarvis.voice.engines import RATE
    from jarvis.voice.verbatim import NoVerbatimEngine, VerbatimSpeaker

    cfg = cfgmod.load(args.config)
    ladder = [e for e in reader_engines(cfg) if not args.engine or e.name == args.engine]
    if not ladder:
        print(
            f"no reader voice{f' called {args.engine!r}' if args.engine else ''} here",
            file=sys.stderr,
        )
        return 2
    text = " ".join(args.text)
    try:
        pcm = asyncio.run(
            VerbatimSpeaker(engines=tuple(ladder)).pcm_for(text, args.lang, exact=args.exact)
        )
    except NoVerbatimEngine as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.out:
        with wave.open(args.out, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(pcm)
        print(f"wrote {args.out} ({len(pcm) / (RATE * 2):.1f}s)")
        return 0
    if why := _play(pcm, RATE):
        print(f"{why}; use --out speech.wav", file=sys.stderr)
        return 1
    return 0


# ───────────────────────────── chat ─────────────────────────────


def cmd_chat(args: argparse.Namespace) -> int:
    """Talk to Jarvis by text over the Gemini API, with every tool the desk has."""
    import asyncio

    from jarvis.live.chat import GeminiChat, persona
    from jarvis.live.text import TextCallFailed
    from jarvis.tools.ctx import ToolCtx
    from jarvis.tools.default import registry

    cfg = cfgmod.load(args.config)
    try:
        key = secrets.require("gemini_api_key")
    except secrets.MissingSecret as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if not _installed("google.genai"):
        print('google-genai is not installed: pip install -e ".[live]"', file=sys.stderr)
        return 2

    con = db.open_db(args.db)
    reg = registry()
    ctx = ToolCtx(con=con, channel="cli", actor="cli", extra=tool_extra(cfg, key))
    chat = GeminiChat(
        api_key=key,
        model=cfg.voice.text_model,
        declarations=reg.declarations("cli"),
        dispatch=lambda name, a: reg.dispatch(name, a, ctx),
        system_instruction=persona(
            extra=remembered(con), address=cfg.persona.address, name=cfg.persona.name
        ),
    )
    speaker = None
    if args.speak:
        from jarvis.voice.verbatim import VerbatimSpeaker

        ladder = reader_engines(cfg)
        speaker = VerbatimSpeaker(engines=tuple(ladder)) if ladder else None
        if speaker is None:
            print(f"{WARN}  no voice to speak with; `sudo apt install espeak-ng`", file=sys.stderr)

    def turn(text: str) -> int:
        try:
            out = chat.send(text)
        except TextCallFailed as exc:
            print(f"[gemini] {exc}", file=sys.stderr)
            return 1
        for name, said in out.tools:
            print(f"  [{name}] {said}")
        print(out.text)
        if speaker is not None and out.text:
            from jarvis.voice.engines import RATE

            try:
                pcm = asyncio.run(speaker.pcm_for(out.text, args.lang, exact=False))
            except Exception as exc:  # noqa: BLE001 - speech is a bonus on a text channel
                print(f"[voice] {exc}", file=sys.stderr)
            else:
                if why := _play(pcm, RATE):
                    print(f"[voice] {why}", file=sys.stderr)
        return 0

    try:
        if args.message:
            return turn(" ".join(args.message))
        print("Jarvis by text. /reset forgets the conversation, /quit or ctrl-d leaves.")
        while True:
            try:
                line = input("you> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if not line:
                continue
            if line in ("/quit", "/exit"):
                return 0
            if line == "/reset":
                chat.reset()
                print("(forgotten)")
                continue
            turn(line)
    finally:
        con.close()


# ───────────────────────────── geo and weather ─────────────────────────────


def cmd_geo(args: argparse.Namespace) -> int:
    """`geo update` downloads GeoLite2; `geo where` says where Jarvis thinks you are."""
    from jarvis.geo import GeoUnavailable
    from jarvis.geo.geolite import update
    from jarvis.tools.builtin import world

    cfg = cfgmod.load(args.config)
    loc = locator_for(cfg)
    if args.action == "update":
        try:
            path = update(
                account_id=cfg.location.maxmind_account_id,
                license_key=secrets.get("maxmind_license_key") or "",
                dest=loc.db_path,
            )
        except GeoUnavailable as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"installed {path} ({path.stat().st_size // 1024} KB)")
        return 0

    con = db.open_db(args.db)
    try:
        ctx = _cli_ctx(con, cfg)
        print(world.where_am_i(ctx))
    except Exception as exc:  # noqa: BLE001 - every geo failure is a sentence
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        con.close()
    return 0


def _cli_ctx(con: sqlite3.Connection, cfg: cfgmod.Config) -> Any:
    from jarvis.tools.ctx import ToolCtx

    return ToolCtx(con=con, channel="cli", actor="cli", extra=tool_extra(cfg))


def cmd_weather(args: argparse.Namespace) -> int:
    from jarvis.tools.default import registry

    cfg = cfgmod.load(args.config)
    con = db.open_db(args.db)
    try:
        print(
            registry().dispatch(
                "weather", {"place": " ".join(args.place), "when": args.when}, _cli_ctx(con, cfg)
            )
        )
    finally:
        con.close()
    return 0


# ───────────────────────────── wake word ─────────────────────────────


def cmd_wake(args: argparse.Namespace) -> int:
    """`wake download` fetches the models; `wake test` says what score a phrase gets."""
    from jarvis.audio import wake

    cfg = cfgmod.load(args.config)
    phrase = cfg.voice.wake_word or "hey_jarvis"
    where = wake.default_model_dir()
    if args.action == "download":
        try:
            got = wake.download(phrase, where)
        except (wake.ModelsMissing, OSError) as exc:
            print(f"couldn't download the wake model: {exc}", file=sys.stderr)
            return 1
        print(f"{OK}  {', '.join(got) if got else 'already present'} in {where}")
        print(f"{WARN}  {wake.LICENCE}")
        return 0

    try:
        model = wake.OnnxWakeWord(phrase, where)
    except wake.ModelsMissing as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.wav:
        import wave

        from jarvis.voice.engines import wav_to_pcm

        try:
            pcm, rate = wav_to_pcm(Path(args.wav).read_bytes())
        except (OSError, ValueError, wave.Error) as exc:
            print(f"couldn't read {args.wav}: {exc}", file=sys.stderr)
            return 2
        label = args.wav
    else:
        from jarvis.voice.engines import EngineFailed, EngineUnavailable, SystemEngine

        text = " ".join(args.words) or wake.PHRASES[phrase][1]
        try:
            pcm, rate = SystemEngine().synth(text, "en"), 24_000
        except (EngineUnavailable, EngineFailed) as exc:
            print(f"no OS voice to say it with ({exc}); pass --wav", file=sys.stderr)
            return 2
        label = f"'{text}' (spoken by the OS voice)"
    best = _wake_score(model, pcm, rate)
    verdict = "WOULD wake" if best >= cfg.voice.wake_threshold else "would NOT wake"
    print(f"{label}: best score {best:.3f} — {verdict} at threshold {cfg.voice.wake_threshold}")
    return 0


def _wake_score(model: Any, pcm: bytes, rate: int) -> float:
    """The best score a recording gets, padded with a second of silence each side."""
    import numpy as np

    from jarvis.audio.wake import CHUNK

    audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    if rate != 16_000 and audio.size:
        n = int(audio.size * 16_000 / rate)
        audio = np.interp(np.linspace(0, audio.size - 1, n), np.arange(audio.size), audio)
    pad = np.zeros(16_000, dtype=np.int16)
    clip = np.concatenate((pad, audio.astype(np.int16), pad))
    model.reset()
    return max(
        (model.score(clip[i : i + CHUNK]) for i in range(0, clip.size - CHUNK + 1, CHUNK)),
        default=0.0,
    )


# ───────────────────────────── the window ─────────────────────────────


def build_window_services(
    cfg: cfgmod.Config,
    db_path: str | None,
    *,
    control: Any | None = None,
    setup: Any | None = None,
    late_key: bool = False,
    reload: Any | None = None,
) -> Any:
    """Everything the window server can do, for ``window`` and ``app`` alike. Wiring only.

    ``late_key`` is the app's: its first-run screen stores the Gemini key AFTER
    the window is up, and its Settings change the key, the city and the form
    of address while the window stays up. So the app's chat and tool context
    are re-read from ``reload()`` (the config as it is now) and the keyring
    on every use, and rebuilt when either changed; built once, they would
    answer with what was true at launch until the user restarted.
    """
    from jarvis.tools.default import registry
    from jarvis.window.server import Services

    key = secrets.get("gemini_api_key")
    con = db.open_db(db_path)  # migrate once, before any request needs the tables
    try:
        notes_paragraph = remembered(con)
    finally:
        con.close()

    reg = registry()
    extra = tool_extra(cfg, key)
    redactor = desk_redactor()
    chat, chat_why = _window_chat(cfg, key, db_path, reg, extra, notes_paragraph)
    tools_extra: Any = extra
    if late_key and _installed("google.genai"):
        now = _as_of_now(reload or (lambda: cfg))
        chat, chat_why = _late_chat(now, db_path, reg, notes_paragraph), ""
        tools_extra = _ExtraNow(now)
    return Services(
        open_db=lambda: db.connect(db_path),
        registry=reg,
        extra=tools_extra,
        chat=chat,
        chat_why=chat_why,
        speak=_window_speaker(cfg, db_path, redactor=redactor),
        wake_word=_wake_phrase(cfg),
        wake_threshold=cfg.voice.wake_threshold,
        spend_threshold_usd=cfg.spend_threshold_usd,
        tz=cfg.tz,
        redactor=redactor,
        setup=setup,
        control=control,
    )


def cmd_window(args: argparse.Namespace) -> int:
    """The HUD: a local page in an app window, reading the rows every process writes."""
    from jarvis.window.launch import open_window
    from jarvis.window.server import make_server

    services = build_window_services(cfgmod.load(args.config), args.db)
    try:
        server = make_server(services, port=args.port)
    except OSError as exc:
        print(
            f"jarvis window: port {args.port} is not free ({exc.strerror or exc}); "
            "leave out --port to take any free one",
            file=sys.stderr,
        )
        return 1
    # The token is printed only when the user must paste it themselves: a
    # terminal gets screenshotted, and the token is the whole of the access check.
    print(f"jarvis window on http://127.0.0.1:{server.port}/  (ctrl-c to stop)")
    if args.no_open:
        print(f"open this in a browser: {server.url}")
    else:
        how = open_window(server.url)
        print(f"opened it ({how}). If nothing appeared, run again with --no-open.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        server.shutdown()
    return 0


def _wake_phrase(cfg: cfgmod.Config) -> str:
    """The wake word as it is said, for the HUD to print. "" when there is none."""
    if not cfg.voice.wake_word:
        return ""
    try:
        from jarvis.audio.wake import PHRASES  # imports numpy: the voice extra
    except ImportError:
        return cfg.voice.wake_word.replace("_", " ")
    found = PHRASES.get(cfg.voice.wake_word)
    return found[1] if found else cfg.voice.wake_word.replace("_", " ")


def _window_chat(
    cfg: cfgmod.Config,
    key: str | None,
    db_path: str | None,
    reg: Any,
    extra: dict[str, Any],
    notes_paragraph: str,
) -> tuple[Any, str]:
    """``(chat, why_not)``: the same assistant as `python -m jarvis chat`, for the window."""
    if not key:
        return None, "Chat needs the Gemini key: python -m jarvis secrets set gemini_api_key"
    if not _installed("google.genai"):
        return None, 'Chat needs google-genai: uv pip install -e ".[live]"'
    from jarvis.live.chat import GeminiChat, persona
    from jarvis.live.text import TextCallFailed
    from jarvis.tools.ctx import ToolCtx

    def dispatch(name: str, args_: dict[str, Any]) -> str:
        # Its own connection per call: the chat runs on whichever server thread
        # took the request, and a connection belongs to the thread that made it.
        con = db.connect(db_path)
        try:
            ctx = ToolCtx(con=con, channel="cli", actor="window", extra=extra)
            return reg.dispatch(name, args_, ctx)
        finally:
            con.close()

    chat = GeminiChat(
        api_key=key,
        model=cfg.voice.text_model,
        declarations=reg.declarations("cli"),
        dispatch=dispatch,
        system_instruction=persona(
            extra=notes_paragraph, address=cfg.persona.address, name=cfg.persona.name
        ),
    )

    def send(text: str) -> tuple[str, tuple[tuple[str, str], ...]]:
        try:
            turn = chat.send(text)
        except TextCallFailed as exc:
            raise RuntimeError(f"Gemini didn't answer: {exc}") from exc
        return turn.text, turn.tools

    return send, ""


def _as_of_now(reload: Any) -> Any:
    """``now() -> (cfg, key, extra)``, rebuilt only when the config or the key changed."""
    import hashlib

    last: dict[str, Any] = {}

    def now() -> tuple[cfgmod.Config, str | None, dict[str, Any]]:
        cfg = reload()
        key = secrets.get("gemini_api_key")
        # A digest, not the key: this dict outlives the call.
        sig = (cfg, hashlib.sha256((key or "").encode("utf-8")).hexdigest())
        if last.get("sig") != sig:
            last.update(sig=sig, extra=tool_extra(cfg, key))
        return cfg, key, last["extra"]

    return now


class _ExtraNow(Mapping[str, Any]):
    """The tools' ``ctx.extra`` as of the current settings and key. A view, read per call."""

    def __init__(self, now: Any) -> None:
        self._now = now

    def _current(self) -> dict[str, Any]:
        return self._now()[2]

    def __getitem__(self, key: str) -> Any:
        return self._current()[key]

    def __iter__(self) -> Any:
        return iter(self._current())

    def __len__(self) -> int:
        return len(self._current())


def _late_chat(now: Any, db_path: str | None, reg: Any, notes_paragraph: str) -> Any:
    """The app window's chat, rebuilt whenever the key or the settings it was built from change.

    A rebuild starts a new conversation; it happens only when the user has
    just changed what Jarvis is (its key, its city, how it addresses them).
    """
    built: dict[str, Any] = {}

    def send(text: str) -> tuple[str, tuple[tuple[str, str], ...]]:
        # The server serialises chat calls, so rebuilding here cannot race.
        cfg, key, extra = now()
        if built.get("from") is not extra or "chat" not in built:
            chat, _ = _window_chat(cfg, key, db_path, reg, extra, notes_paragraph)
            if chat is None:
                raise RuntimeError("I need the Gemini key before I can chat; it goes in Settings.")
            built.update({"from": extra, "chat": chat})
        return built["chat"](text)

    return send


def _window_speaker(cfg: cfgmod.Config, db_path: str | None, *, redactor: Any = None) -> Any:
    """``speak(text) -> "desk" | "local"`` for the window. Wiring only.

    Through the desk when it is running — it owns the speaker, and a second
    output stream is echo its canceller never saw — and through the reader
    voice right here when it is not.
    """
    import asyncio

    from jarvis import kill, liveness
    from jarvis.voice.engines import RATE
    from jarvis.voice.verbatim import VerbatimSpeaker

    ladder = tuple(reader_engines(cfg))
    language = cfg.location.language or "en"

    def speak(text: str) -> str:
        con = db.connect(db_path)
        try:
            beat = liveness.read(con, "desk")
            if beat is not None and beat.state != liveness.OFFLINE:
                # A row outlives the sentence, so a secret is kept out of it
                # the way the event log keeps one out.
                said = redactor.apply(text)[0] if redactor is not None else text
                kill.issue_command(
                    con,
                    verb="say",
                    target_kind="channel",
                    target_id="desk",
                    args={"text": said},
                    issued_by="window",
                    # The default, not a few seconds: a desk mid-reply reads
                    # its commands only after it stops talking.
                    ttl_s=kill.DEFAULT_TTL_S,
                )
                return "desk"
        finally:
            con.close()
        if not ladder:
            raise RuntimeError(
                "there is no voice on this machine (Linux: sudo apt install espeak-ng)"
            )
        pcm = asyncio.run(VerbatimSpeaker(engines=ladder).pcm_for(text, language, exact=False))
        if why := _play(pcm, RATE):
            raise RuntimeError(why)
        return "local"

    return speak


# ───────────────────────────── reminders ─────────────────────────────


def cmd_remind(args: argparse.Namespace) -> int:
    """The reminders the scheduler will say, and a way to take one back."""
    from jarvis.tools.default import registry

    cfg = cfgmod.load(args.config)
    con = db.open_db(args.db)
    try:
        ctx = _cli_ctx(con, cfg)
        if args.action == "cancel":
            print(registry().dispatch("cancel_reminder", {"about": " ".join(args.words)}, ctx))
        else:
            print(registry().dispatch("list_reminders", {}, ctx))
    finally:
        con.close()
    return 0


# ───────────────────────────── hearing ─────────────────────────────


def cmd_hearing(args: argparse.Namespace) -> int:
    """See, teach and test the words Jarvis corrects. The 2am view of the lexicon."""
    from jarvis import hearing

    cfg = cfgmod.load(args.config)
    con = db.open_db(args.db)
    try:
        if args.action == "list":
            lex = hearing.lexicon(con, extra_terms=cfg.voice.vocabulary)
            for e in lex.entries:
                origin = "taught" if e.taught else ("shipped" if e in hearing.SEED else "config")
                print(f"{e.spelling or e.term}  ({origin})")
                if e.heard_as:
                    print(f"    heard as: {', '.join(e.heard_as)}")
                learned = lex.learned.get(e.term, ())
                if learned:
                    print(f"    learned cues: {', '.join(learned)}")
                for (h, m), n in sorted(lex.prior.items()):
                    if m == e.term:
                        print(f"    history {h} -> {m}: {n:+.1f}")
            return 0
        if args.action == "test":
            sentence = " ".join(args.words)
            if not sentence:
                print("give a sentence: python -m jarvis hearing test get me a stock coat")
                return 2
            out = hearing_for(cfg)(con, sentence)
            print(out.text)
            for f in out.fixes:
                verdict = "changed" if f.applied else "left"
                print(f"  {verdict} {f.heard!r} -> {f.meant!r}  score {f.score:+.1f}  ({f.by})")
                if f.why:
                    print(f"      because: {', '.join(f.why)}")
            return 0
        if args.action == "teach":
            if len(args.words) != 2:
                print("usage: python -m jarvis hearing teach <heard> <meant>  (teach coat quote)")
                return 2
            entry = hearing.teach(con, args.words[0], args.words[1], actor="cli")
            print(f"{OK}  {', '.join(entry.heard_as)} -> {entry.term}")
            return 0
        if args.action == "forget":
            term = " ".join(args.words)
            if hearing.forget(con, term):
                print(f"{OK}  forgot what you taught about {term!r}")
            else:
                print(f"nothing taught about {term!r}")
            return 0
    finally:
        con.close()
    return 2


# ───────────────────────────── build ─────────────────────────────


def _builder(args: argparse.Namespace, cfg: cfgmod.Config):
    """Assemble the runner's Deps from this install's credentials. Wiring only.

    Every decision here belongs to somebody else: which model tidies is
    :mod:`jarvis.live.text`, what the matrix says is
    :mod:`jarvis.github.scopes`, and what to do with either is
    :mod:`jarvis.project.runner`. This function's whole job is that the runner
    never has to go looking for a credential.
    """
    from jarvis import spec
    from jarvis.github import scopes
    from jarvis.github.transport import HttpTransport
    from jarvis.live.text import GeminiText
    from jarvis.project.runner import Deps
    from jarvis.project.workspace import SubprocessGit

    token = secrets.get("github_token")
    transport = HttpTransport(token=token) if token else None
    if transport is not None:
        caps = scopes.capabilities(transport)
    else:
        # 'no' rather than 'unknown': with no credential at all there is nothing
        # to be uncertain about, and `unknown` would let the runner try and fail
        # halfway through instead of saying so before it starts.
        caps = scopes.Capabilities(
            token_kind="unknown",
            create="no",
            source="no github_token is set",
            notes=("no GitHub credential; builds run locally",),
        )
    return Deps(
        model_call=GeminiText(
            api_key=secrets.require("gemini_api_key"),
            model=cfg.voice.text_model,
            response_schema=spec.RESPONSE_SCHEMA,
        ),
        git=SubprocessGit(),
        git_token=token,
        capabilities=caps,
        owner=cfg.desk.github_owner,
        workspace_root=cfg.workspace_path,
        transport=transport,
        actor="builder",
    )


def cmd_build(args: argparse.Namespace) -> int:
    """Carry every spoken build request forward one step, and say where each got to.

    One step, not a loop to completion: the interesting states are the ones where
    it is WAITING on a human, and a command that blocked until the whole build
    finished would hide the question it is waiting on.
    """
    from jarvis.project import runner

    con = db.open_db(args.db)
    try:
        cfg = cfgmod.load(args.config)
        pending_jobs = [
            j
            for state in ("queued", "deferred", "blocked", "parked")
            for j in jobs.list_by_state(con, state)
            if j.kind == runner.BUILD_JOB_KIND
        ]
        if not pending_jobs:
            print("no build requests. Say one to the desk, or `python -m jarvis run` directly.")
            return 0
        try:
            deps = _builder(args, cfg)
        except secrets.MissingSecret as exc:
            print(str(exc), file=sys.stderr)
            return 2

        for job in pending_jobs:
            if job.state == "parked":
                # A parked build stopped for a reason that was said out loud.
                # Re-queue it explicitly rather than retrying on every sweep.
                if not args.retry:
                    print(f"{job.id}  parked: {job.stop_reason}  (--retry to try again)")
                    continue
                jobs.set_state(con, job.id, "queued", actor="cli")
            step = runner.advance(con, job.id, deps)
            print(f"\n{job.id}  {step.action}")
            print(step.spoken)
            if step.request_id:
                print("\n  answer with:  python -m jarvis pending  /  python -m jarvis answer")
            if step.child_job_id:
                # THE BUILD STARTS HERE. The child is born `queued`, and nothing
                # in the tree picks a queued job up: `reconcile` scans only
                # ACTIVE_STATES plus deferred/orphaned, so without this the
                # approved build sits invisible forever while `status` cheerfully
                # reports the request "finished".
                child = jobs.get(con, step.child_job_id)
                print(f"  starting the build in {step.cwd}")
                try:
                    proc = _spawn_runner(
                        step.child_job_id, args.db, prompt=child.prompt_text if child else None
                    )
                except OSError as exc:
                    jobs.set_state(
                        con,
                        step.child_job_id,
                        "failed",
                        actor="cli",
                        stop_reason=f"spawn failed: {exc}",
                    )
                    print(f"  could not start the runner: {exc}", file=sys.stderr)
                    return 2
                return _watch(con, step.child_job_id, proc)
    finally:
        con.close()
    return 0


# ───────────────────────────── desk ─────────────────────────────


class StartupRefused(RuntimeError):
    """The desk cannot start, and the message says what to run.

    ``action`` is what the HUD's button for it does — ``"secret:<name>"``,
    ``"wake"``, ``"device"``, ``"voice"`` — or None when no button can fix it.
    It travels in the ``desk.refused`` event, because in the app nobody reads
    the message off a terminal.
    """

    def __init__(self, message: str, *, action: str | None = None) -> None:
        super().__init__(message)
        self.action = action


@dataclass
class Desk:
    """Everything one desk is made of, so the wiring has a name rather than a tuple.

    ``reader`` is here and not only inside :class:`DeskQuestions` because the
    bridge that lets a worker thread speak can only be built once there is a
    running loop, which is after this is assembled.
    """

    leg: Any
    graph: Any
    session: Any
    questions: Any
    reader: Any | None
    #: The wake-word thread, or None when the desk listens all the time.
    wake: Any | None = None
    #: Puts the desk's state and events into the database for the window.
    publisher: Any | None = None


def reader_engines(cfg: cfgmod.Config) -> list[Any]:
    """The reader's ladder, in ``voice.reader_order``, keeping only rungs that can run.

    Wiring only: which engine may read EXACT text is decided by each engine's
    own ``deterministic``/``verified`` flags and enforced by VerbatimSpeaker,
    not here.
    """
    from jarvis.voice import engines as eng

    built: list[Any] = []
    for rung in cfg.voice.reader_order:
        if rung == "kokoro" and _installed("kokoro"):
            built.append(eng.KokoroEngine())
        elif rung == "edge" and _installed("edge_tts"):
            built.append(
                eng.EdgeEngine(
                    voices={"en": cfg.voice.reader_voice, "tr": cfg.voice.reader_voice_tr}
                )
            )
        elif rung == "system" and eng._which_system_tts() is not None:
            built.append(eng.SystemEngine())
        elif rung == "gemini" and _installed("google.genai"):
            key = secrets.get("gemini_api_key")
            if key:
                built.append(
                    eng.GeminiTtsEngine(
                        api_key=key, model=cfg.voice.tts_model, voice=cfg.voice.tts_voice
                    )
                )
    return built


def _desk_reader(cfg: cfgmod.Config, mixer: Any) -> Any:
    """The reader voice. Refuses to start without a rung that can read EXACT text.

    A desk with no deterministic reader can hold a conversation but can never
    present a question — its options are an answer key, and only a voice with no
    language model in the path may say them. It would find that out at the worst
    moment, with a build parked and the user waiting, so it is said at startup.
    """
    from jarvis.audio.mixer import Prio
    from jarvis.voice.cache import PcmCache
    from jarvis.voice.router import TrackSink
    from jarvis.voice.verbatim import VerbatimSpeaker

    ladder = reader_engines(cfg)
    if not any(e.deterministic or e.verified for e in ladder):
        raise StartupRefused(
            "no reader voice that can read a question word for word. The simplest is your "
            "system's own: `sudo apt install espeak-ng` on Linux (macOS and Windows have one "
            'built in). Or `pip install -e ".[tts]"` for Microsoft\'s voices.',
            action="voice",
        )
    print(f"{OK}  reader voice: {' -> '.join(e.name for e in ladder)}")
    track = mixer.track("verbatim", Prio.VERBATIM, content_rate=24_000)
    return VerbatimSpeaker(engines=tuple(ladder), cache=PcmCache(), sink=TrackSink(track))


def _build_desk(args: argparse.Namespace) -> Desk:
    """Wire the desk. Raises :class:`StartupRefused` with a sentence, never a traceback.

    Read top to bottom: this is the whole architecture in one function, which is
    the point of having exactly one place allowed to know every layer.
    """
    missing = [m for m, _, _, required in EXTRAS if required and not _installed(m)]
    if missing:
        raise StartupRefused(
            f"missing packages: {', '.join(missing)}. Run `python -m jarvis doctor` for the "
            'install commands, or `pip install -e ".[cc,voice,live,tts]"`.',
            # No button installs a package; in the app this is a broken build.
            action=None,
        )

    from jarvis.audio import BLOCK, DEV_RATE, MIC_RATE
    from jarvis.audio.devices import DeviceError
    from jarvis.audio.dsp import AecUnavailable, EnergyVad
    from jarvis.audio.graph import AudioEvent, AudioGraph, QueuedEventSink
    from jarvis.audio.legs import DeskLeg
    from jarvis.audio.micbus import MicBus
    from jarvis.audio.mixer import PlaybackMixer, Prio
    from jarvis.audio.turn import TurnController
    from jarvis.live.profiles import DESK, SessionProfile
    from jarvis.live.session import GenaiConnector, LiveSession, QueuedLiveEvents, QueuedUplink
    from jarvis.tools.default import registry
    from jarvis.voice.desk import DeskPublisher, desk_state
    from jarvis.voice.router import TrackSink
    from jarvis.voice.tools import DeskQuestions, LiveTools, Transcript

    cfg = cfgmod.load(args.config)
    key = secrets.require("gemini_api_key")  # raises MissingSecret with instructions

    con = db.open_db(args.db)
    try:
        reconcile.reconcile(con, actor="desk")
        profile: SessionProfile = heard_profile(cfg, con, DESK)
    finally:
        con.close()

    # ── the sound path ───────────────────────────────────────────────────
    mixer = PlaybackMixer(rate=DEV_RATE)
    micbus = MicBus(rate=MIC_RATE)
    uplink = QueuedUplink(rate=MIC_RATE, block=BLOCK * MIC_RATE // DEV_RATE)
    leg = DeskLeg(device_name=cfg.voice.input_device, require_aec=not cfg.voice.assume_headset)
    # Resolved HERE rather than at open(): `DeskLeg.select` exists so that a
    # startup check can name the device — or refuse — before PortAudio has
    # grabbed anything, and a voice assistant's first failure should be a
    # sentence rather than a traceback nobody is sitting in front of.
    try:
        selection = leg.select()
    except DeviceError as exc:
        raise StartupRefused(str(exc), action="device") from exc
    print(f"{OK}  microphone and speaker: {selection.describe()}")
    # The audio graph's events — wake, sleep, turns, barge-ins — and the turn
    # controller's own, into ONE queue the publisher drains. The controller had
    # no sink at all before, so "awake" happened and nothing anywhere knew.
    audio_events = QueuedEventSink()
    turn = TurnController(
        mixer=mixer,
        vad=EnergyVad(),
        uplink=uplink,
        on_event=lambda e: audio_events(AudioEvent(kind=e.kind, at=e.at, detail=dict(e.detail))),
        wake_window_s=cfg.voice.wake_window_s if cfg.voice.wake_word else None,
    )
    try:
        aec = leg.make_aec()
    except AecUnavailable as exc:
        # Reached only when the config says assume_headset = false, which is the
        # user asking for open speakers. That is a refusal with a fix, not a
        # traceback: AecUnavailable is a plain RuntimeError and nothing above
        # here would have caught it.
        raise StartupRefused(
            f"{exc}\nYou have voice.assume_headset = false, which means open speakers and "
            'therefore a real echo canceller. Either `pip install -e ".[aec]"`, or set '
            "assume_headset = true and use a headset.",
            action="device",
        ) from exc
    graph = AudioGraph(
        mixer=mixer,
        micbus=micbus,
        turn=turn,
        aec=aec,
        device_rate=leg.device_rate,
        block=leg.block,
        on_event=audio_events,
    )
    if leg.aec_degraded:
        print(f"{WARN}  no echo canceller — use a headset, or open speakers will hear themselves")

    # ── the voices ───────────────────────────────────────────────────────
    reader = _desk_reader(cfg, mixer)
    live_track = mixer.track("live", Prio.LIVE, content_rate=24_000)
    wake = _desk_wake(cfg, graph, turn, mixer, args.db)

    # ── what a sentence is allowed to do ─────────────────────────────────
    transcript = Transcript()
    # `speak` is filled in by cmd_desk once there is a running loop to bridge to.
    # See DeskQuestions.speak: a coroutine handed over here would be created,
    # never awaited, and the question marked presented having been said to nobody.
    questions = DeskQuestions(open_db=lambda: db.open_db(args.db))
    tools = LiveTools(
        registry=registry(),
        # A fresh connection per tool call, opened in the worker thread that
        # runs it. See jarvis/voice/tools.py.
        open_db=lambda: db.open_db(args.db),
        channel="desk",
        actor="desk",
        transcript=transcript,
        questions=questions,
        speak=(lambda utt: reader.speak(utt)) if reader is not None else None,
        extra=tool_extra(cfg, key),
        hearing=hearing_for(cfg, key),
    )
    if profile.vocabulary:
        print(f"{OK}  listening for: {', '.join(profile.vocabulary[-6:])}")

    live_events = QueuedLiveEvents()
    printer = _desk_event_printer(transcript)

    def on_live_event(event: Any) -> None:
        printer(event)
        live_events(event)

    session = LiveSession(
        profile,
        uplink,
        TrackSink(live_track),
        connector=GenaiConnector(api_key=key),
        tools=tools,
        on_event=on_live_event,
    )
    # Transcripts go into a log that is kept forever, so every configured
    # secret's value is scrubbed on the way in. Built here because this is the
    # one place allowed to read the keyring and know the bus.
    redactor = desk_redactor()
    publisher = DeskPublisher(
        open_db=lambda: db.open_db(args.db),
        state=lambda: desk_state(turn, mixer),
        drains=(
            lambda con: live_events.drain(con, "desk", redactor=redactor),
            lambda con: audio_events.drain(con, "desk"),
        ),
    )
    return Desk(
        leg=leg,
        graph=graph,
        session=session,
        questions=questions,
        reader=reader,
        wake=wake,
        publisher=publisher,
    )


def desk_redactor() -> Any:
    """A bus Redactor holding every configured secret's value. Wiring only."""
    from jarvis.bus import Redactor

    values = []
    for s in secrets.SECRETS:
        with suppress(Exception):
            if value := secrets.get(s.name):
                values.append(value)
    return Redactor.of(values)


def _desk_wake(
    cfg: cfgmod.Config, graph: Any, turn: Any, mixer: Any, db_path: str | None
) -> Any | None:
    """The wake-word thread for this desk, or None when it listens all the time.

    A configured wake word whose model cannot load is a REFUSAL, not a quiet
    fallback to always-listening: the user asked for a desk that sends nothing
    until it hears its name, and silently sending everything instead is the one
    failure here that is a privacy failure rather than an inconvenience.
    """
    if not cfg.voice.wake_word:
        print(f'{WARN}  no wake word (voice.wake_word = ""): listening all the time')
        return None
    from jarvis.audio.mixer import Prio
    from jarvis.audio.wake import (
        PHRASES,
        ModelsMissing,
        OnnxWakeWord,
        WakeDetector,
        WakeWatch,
        chime,
    )

    if cfg.voice.wake_word not in PHRASES:
        raise StartupRefused(
            f"no wake model called {cfg.voice.wake_word!r}; have {', '.join(sorted(PHRASES))}. "
            'Set voice.wake_word to one of those, or "" to listen all the time.',
            action="voice",
        )
    try:
        model = OnnxWakeWord(cfg.voice.wake_word)
    except ModelsMissing as exc:
        raise StartupRefused(
            f'{exc}\nOr set voice.wake_word = "" in config.toml to listen all the time.',
            action="wake",
        ) from exc
    phrase = PHRASES[cfg.voice.wake_word][1]
    blip = mixer.track("chime", Prio.MONITOR, content_rate=24_000) if cfg.voice.wake_chime else None

    def on_wake(at: float, score: float) -> None:
        if blip is not None:
            blip.write(chime())
        print(f"[awake] heard '{phrase}' ({score:.2f})")
        try:
            con = db.open_db(db_path)
            try:
                presence.note_heard(con, "wakeword", text=phrase, actor="desk")
            finally:
                con.close()
        except Exception as exc:  # noqa: BLE001 - presence is a bonus; the wake already happened
            print(f"[presence] {type(exc).__name__}: {exc}")

    def on_sleep(at: float) -> None:
        print(f"[asleep] say '{phrase}' to wake me")

    print(f"{OK}  wake word: '{phrase}' (threshold {cfg.voice.wake_threshold})")
    return WakeWatch(
        reader=graph.reader("wake"),
        model=model,
        turn=turn,
        phrase=phrase,
        detector=WakeDetector(threshold=cfg.voice.wake_threshold),
        on_wake=on_wake,
        on_sleep=on_sleep,
    )


#: The session event kinds worth a line on the terminal. Taken from the
#: ``_emit`` calls in :mod:`jarvis.live.session` and asserted against them by a
#: test, because a kind that does not exist is a branch that never fires and
#: nothing anywhere is an error — the first draft of this listened for
#: "reconnect" and "tool_error", neither of which the session has ever emitted.
DESK_EVENTS: tuple[str, ...] = (
    "connected",
    "connect_failed",
    "disconnected",
    "go_away",
    "revoked",
    "stream_error",
    "uplink_error",
    "tool_call",
    "tool_failed",
    "tool_denied",
    "tool_unroutable",
)


def _desk_event_printer(transcript: Any) -> Any:
    """Feed the transcript, and put one line per event on the terminal.

    The transcript is fed HERE rather than inside the session because what
    counts as the user's own words is a policy question — see
    :class:`jarvis.voice.tools.Transcript` — and the session is a transport.
    """

    def on_event(event: Any) -> None:
        if event.kind == "input_transcript":
            transcript.heard(str(event.detail.get("text", "")))
            print(f"  you: {event.detail.get('text', '')}")
        elif event.kind == "output_transcript":
            print(f"jarvis: {event.detail.get('text', '')}")
        elif event.kind in DESK_EVENTS:
            print(f"[{event.kind}] {event.detail}")

    return on_event


def _refused(db_path: str | None, sentence: str, action: str | None) -> int:
    """Say why the desk will not start where the window can read it, and exit 2.

    Printing alone reached nobody once the desk became a child of the app with
    no console. Exit 2 is what tells the supervisor not to restart a refusal.
    """
    from jarvis.bus import publish

    print(sentence, file=sys.stderr)
    try:
        con = db.open_db(db_path)
    except Exception as exc:  # noqa: BLE001 - stderr above is still in the log
        print(f"[refused] could not record it: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    try:
        publish(
            con,
            "desk.refused",
            "desk",
            {"sentence": sentence, "action": action},
            redactor=desk_redactor(),
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[refused] could not record it: {type(exc).__name__}: {exc}", file=sys.stderr)
    finally:
        con.close()
    return 2


def cmd_desk(args: argparse.Namespace) -> int:
    import asyncio

    from jarvis.live.session import LiveDown, LiveUnavailable, key_refused

    try:
        desk = _build_desk(args)
    except StartupRefused as exc:
        return _refused(args.db, str(exc), exc.action)
    except secrets.MissingSecret as exc:
        return _refused(args.db, str(exc), f"secret:{exc.secret.name}")
    leg, graph, session, questions = desk.leg, desk.graph, desk.session, desk.questions

    async def watch_for_questions() -> None:
        """Claim and read aloud every question routed to this desk.

        A separate task rather than a hook in the receive loop: reading a
        question is seconds of synthesis and speech, and the loop it would
        otherwise block is the one carrying the user's own voice.
        """
        from jarvis.voice.desk import consume_say_commands
        from jarvis.voice.router import Utterance

        loop = asyncio.get_running_loop()
        say_from_thread = None
        if desk.reader is not None:

            def say_from_thread(utt: Any) -> int:
                """Block the worker thread until the reader has finished the clip.

                `run_coroutine_threadsafe` is the whole bridge: the poll runs off
                the loop (it does blocking SQLite), the reader runs on it, and
                the thread waits. Without it the coroutine is never awaited and
                the desk silently reads nothing.
                """
                return asyncio.run_coroutine_threadsafe(desk.reader.speak(utt), loop).result(120)

            if questions.speak is None:
                questions.speak = say_from_thread

        def say_for_the_window() -> int:
            """The window's "say this aloud", through THIS process's speaker."""
            if say_from_thread is None:
                return 0
            con = db.open_db(args.db)
            try:
                return consume_say_commands(
                    con,
                    lambda text: say_from_thread(
                        Utterance(text=text, fidelity="faithful", tag="window:say")
                    ),
                )
            finally:
                con.close()

        while True:
            try:
                await asyncio.to_thread(questions.poll)
            except Exception as exc:  # noqa: BLE001 - a bad row must not end the conversation
                print(f"[questions] {type(exc).__name__}: {exc}")
            try:
                await asyncio.to_thread(say_for_the_window)
            except Exception as exc:  # noqa: BLE001 - nor a bad say command
                print(f"[say] {type(exc).__name__}: {exc}")
            await asyncio.sleep(DESK_POLL_S)

    async def run() -> None:
        leg.open(graph)
        if desk.wake is not None:
            desk.wake.start()
            print(f"asleep. say '{desk.wake.phrase}' to talk. ctrl-c to stop.")
        else:
            print("listening. ctrl-c to stop.")
        if desk.publisher is not None:
            desk.publisher.start()
        watcher = asyncio.create_task(watch_for_questions())
        try:
            await session.run()
        finally:
            watcher.cancel()
            if desk.wake is not None:
                desk.wake.stop()
            await session.close()
            leg.close()
            if desk.publisher is not None:
                # Last: the final drain carries the goodbye events, and the
                # final beat says "offline" so the window does not wait.
                desk.publisher.stop()

    from jarvis.audio.devices import DeviceError

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nstopped.")
    except LiveUnavailable as exc:
        return _refused(args.db, str(exc), None)
    except LiveDown as exc:
        if said := key_refused(exc):
            return _refused(
                args.db,
                f"Gemini turned down the API key ({said}). Paste a new one in Settings.",
                "secret:gemini_api_key",
            )
        # Anything else is the network or Gemini having a bad minute: exit 1,
        # which the app retries with a backoff, with this as the reason shown.
        print(f"I can't reach Gemini right now ({exc}); I'll try again.", file=sys.stderr)
        return 1
    except DeviceError as exc:
        # Opening the stream is the last step of starting, so a device that
        # will not open is a refusal like any other: held, not crash-looped.
        return _refused(args.db, str(exc), "device")
    return 0


# ───────────────────────────── the app ─────────────────────────────


def cmd_app(args: argparse.Namespace) -> int:
    """The double-clickable app: the window, the desk, the scheduler and the bot. Wiring only.

    Every decision is in :mod:`jarvis.app` — what a second copy does
    (``instance``), what an exit means (``supervisor``), what the tray offers
    (``tray``). This binds them to real callables, in the order that matters:
    the lock before anything, the server before its address is published, the
    children after the window can show their failures.
    """
    from jarvis.app import instance, selftest
    from jarvis.window.launch import open_window

    if args.selftest:
        return selftest.main(report=args.report)
    lock = instance.acquire()
    if lock is None:
        print(instance.hand_off(open_window))
        return 0
    try:
        return _run_app(args)
    finally:
        lock.release()


def _run_app(args: argparse.Namespace) -> int:
    import threading

    from jarvis import __version__
    from jarvis.app import autostart, instance, tray
    from jarvis.app import supervisor as sup
    from jarvis.bus import publish
    from jarvis.window.launch import open_window
    from jarvis.window.server import make_server

    try:
        cfg = cfgmod.load(args.config)
    except (ValueError, OSError) as exc:
        # The window is where a broken config.toml gets explained (its settings
        # screen reads the file itself and says what is wrong), so the app must
        # still get as far as opening it.
        print(f"[config] {exc}; starting with the defaults", file=sys.stderr)
        cfg = cfgmod.Config()
    con = db.open_db(args.db)  # migrated before any child or request needs a table
    try:
        publish(con, "app.started", "app", {"version": __version__})
    finally:
        con.close()

    # Children find the same database and config through the environment, the
    # way every process here already looks for them.
    env = dict(os.environ)
    if args.db:
        env["JARVIS_DB"] = str(Path(args.db).expanduser().resolve())
    if args.config:
        env["JARVIS_CONFIG"] = str(Path(args.config).expanduser().resolve())
    quit_event = threading.Event()
    supervisor = sup.Supervisor(
        sup.app_specs(),
        env=env,
        on_exit=sup.exit_recorder(lambda: db.connect(args.db), redactor=desk_redactor()),
    )
    control = sup.Control(supervisor, quit_event)
    services = build_window_services(
        cfg,
        args.db,
        control=control,
        setup=_setup_service(cfg, args, supervisor, control),
        late_key=True,
        reload=lambda: _config_or(cfg, args.config),
    )
    server = instance.bind_remembered(lambda port: make_server(services, port=port))
    server.start()
    poller = threading.Thread(
        target=sup.poll_forever, args=(supervisor, quit_event), name="supervisor", daemon=True
    )
    try:
        instance.publish(server.port, server.token)
        try:
            autostart.sync(cfg.app.start_with_windows)
        except Exception as exc:  # noqa: BLE001 - a registry hiccup must not stop the app
            print(f"[autostart] {type(exc).__name__}: {exc}", file=sys.stderr)
        boot = sup.boot_names(
            telegram_token=bool(secrets.get("telegram_bot_token")),
            start_telegram=cfg.app.start_telegram,
        )
        for name in boot:
            supervisor.start(name)
        poller.start()
        print(f"jarvis {__version__} on http://127.0.0.1:{server.port}/ running {', '.join(boot)}")
        if not args.no_window and cfg.app.open_window_on_start:
            open_window(server.url)
        tray.run(
            open_window=lambda: open_window(server.url),
            restart_voice=lambda: supervisor.restart("desk"),
            quit=quit_event.set,
            stop=quit_event,
        )
    except KeyboardInterrupt:
        pass
    finally:
        quit_event.set()
        if poller.is_alive():
            poller.join(timeout=5)
        supervisor.stop()
        server.shutdown()
    return 0


def _config_or(fallback: cfgmod.Config, path: str | None) -> cfgmod.Config:
    """The config as it is on disk now, or ``fallback`` while the file is broken.

    The settings screen says what is wrong with a broken file; until it is
    fixed, the window keeps working with what it had.
    """
    try:
        return cfgmod.load(path)
    except (ValueError, OSError):
        return fallback


def _setup_service(
    cfg: cfgmod.Config, args: argparse.Namespace, supervisor: Any, control: Any
) -> Any:
    """The settings screen's SetupService, with the real machine behind it. Wiring only."""
    from jarvis.app import adapters
    from jarvis.app.setup import SetupService

    preview = None
    if _installed("google.genai"):
        preview = adapters.gemini_preview(
            key=lambda: secrets.get("gemini_api_key"), model=cfg.voice.tts_model, play=_play
        )
    return SetupService(
        config_path=args.config,
        db_path=args.db,
        list_devices=adapters.list_devices,
        wake_ready=_wake_ready,
        download_wake=adapters.download_wake,
        preview_voice=preview,
        restart=supervisor.restart,
        autostart=adapters.autostart_switch(),
        claude_login=lambda: adapters.claude_login(claude_cli_path()),
        control=control,
    )


# ───────────────────────────── the parser ─────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m jarvis", description=__doc__.splitlines()[0])
    p.add_argument("--db", default=os.environ.get("JARVIS_DB"), help="database path")
    p.add_argument("--config", default=None, help="config.toml path")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="what is missing, and how to fix it").set_defaults(fn=cmd_doctor)

    s = sub.add_parser("secrets", help="credentials, in the OS keyring")
    s.add_argument("action", choices=("list", "set", "forget"))
    s.add_argument("name", nargs="?", default="", help=", ".join(x.name for x in secrets.SECRETS))
    s.set_defaults(fn=cmd_secrets)

    c = sub.add_parser("config", help="settings that are not credentials")
    c.add_argument("action", choices=("show", "path", "init"), nargs="?", default="show")
    c.add_argument("--force", action="store_true", help="overwrite an existing config file")
    c.set_defaults(fn=cmd_config)

    sub.add_parser("status", help="what is running, what it cost, where you are").set_defaults(
        fn=cmd_status
    )

    t = sub.add_parser("tools", help="the tool surface, per channel")
    t.add_argument("-v", "--verbose", action="store_true", help="show what the model reads")
    t.set_defaults(fn=cmd_tools)

    r = sub.add_parser("run", help="start a build and drive Claude Code")
    r.add_argument("prompt", nargs="?", default="", help="what to build")
    r.add_argument("--into", default=None, help="the directory to build in (default: cwd)")
    r.add_argument("--title", default=None, help="what the briefing calls it")
    r.add_argument("--model", default=None, help="opus|sonnet|haiku, or a full model id")
    r.add_argument("--effort", default=None, choices=("low", "medium", "high", "xhigh", "max"))
    r.add_argument(
        "--permission-mode",
        default=None,
        # 'dontAsk' is absent on purpose: it DENIES AskUserQuestion, which is the
        # whole mechanism this project is built on. The schema refuses it too.
        choices=("plan", "default", "acceptEdits", "bypassPermissions"),
        help="'plan' (the default) plans and asks before writing anything",
    )
    r.add_argument("--resume", action="store_true", help="pick up every job parked on an answer")
    r.set_defaults(fn=cmd_run)

    b = sub.add_parser("build", help="carry every spoken build request forward a step")
    b.add_argument("--retry", action="store_true", help="re-queue builds that parked")
    b.set_defaults(fn=cmd_build)

    sub.add_parser("pending", help="the questions waiting on you, numbered").set_defaults(
        fn=cmd_pending
    )

    a = sub.add_parser("answer", help="answer a pending question by its option numbers")
    a.add_argument("which", help="the request id from `pending` (or its position)")
    a.add_argument("picks", nargs="*", type=int, help="the option numbers you were read")
    a.add_argument("--text", default=None, help="'none of these' — your own words")
    a.set_defaults(fn=cmd_answer)

    sy = sub.add_parser("say", help="speak a sentence with the reader voice")
    sy.add_argument("text", nargs="+")
    sy.add_argument("--lang", default="en")
    sy.add_argument("--engine", default=None, help="kokoro|edge|system|gemini")
    sy.add_argument("--exact", action="store_true", help="only voices trusted with exact text")
    sy.add_argument("--out", default=None, help="write a WAV file instead of playing")
    sy.set_defaults(fn=cmd_say)

    g = sub.add_parser("geo", help="where Jarvis thinks you are; refresh GeoLite2")
    g.add_argument("action", choices=("where", "update"), nargs="?", default="where")
    g.set_defaults(fn=cmd_geo)

    w = sub.add_parser("weather", help="the weather, here or somewhere")
    w.add_argument("place", nargs="*", help="a place; empty for here")
    w.add_argument("--when", default="now", choices=("now", "today", "tomorrow", "week"))
    w.set_defaults(fn=cmd_weather)

    ch = sub.add_parser("chat", help="talk to Jarvis by text, over the Gemini API")
    ch.add_argument("message", nargs="*", help="one message; empty for a conversation")
    ch.add_argument("--speak", action="store_true", help="also say the answers out loud")
    ch.add_argument("--lang", default="en", help="the language to speak in")
    ch.set_defaults(fn=cmd_chat)

    wn = sub.add_parser("window", help="the HUD: status, conversation and tools in a window")
    wn.add_argument("--port", type=int, default=0, help="a fixed port (default: any free one)")
    wn.add_argument(
        "--no-open", action="store_true", help="print the address instead of opening a window"
    )
    wn.set_defaults(fn=cmd_window)

    rm = sub.add_parser("remind", help="reminders: list, or cancel one")
    rm.add_argument("action", choices=("list", "cancel"), nargs="?", default="list")
    rm.add_argument("words", nargs="*", help="cancel: which one")
    rm.set_defaults(fn=cmd_remind)

    wk = sub.add_parser("wake", help="the wake word: download its model, or test a phrase")
    wk.add_argument("action", choices=("download", "test"))
    wk.add_argument("words", nargs="*", help="test: a phrase for the OS voice to say")
    wk.add_argument("--wav", default=None, help="test: score a recording instead")
    wk.set_defaults(fn=cmd_wake)

    h = sub.add_parser("hearing", help="the words Jarvis corrects when it mishears you")
    h.add_argument("action", choices=("list", "test", "teach", "forget"))
    h.add_argument("words", nargs="*", help="test: a sentence; teach: <heard> <meant>")
    h.set_defaults(fn=cmd_hearing)

    sub.add_parser("desk", help="listen, talk, and drive Claude Code").set_defaults(fn=cmd_desk)

    ap = sub.add_parser("app", help="the app: the window, the desk and the rest (the default)")
    ap.add_argument("--no-window", action="store_true", help="start without opening the window")
    ap.add_argument("--selftest", action="store_true", help="check this install, write JSON, exit")
    ap.add_argument("--report", default=None, help="--selftest: also write the report here")
    ap.set_defaults(fn=cmd_app)
    return p


def _with_default_command(argv: list[str]) -> list[str]:
    """``argv``, with ``app`` appended when it names no command at all.

    The parser itself still requires a command, so ``--help`` and a typo say
    what the commands are; only an EMPTY command line — a double-click, or
    ``python -m jarvis --db x`` — means the app.
    """
    pre = argparse.ArgumentParser(add_help=False, exit_on_error=False)
    pre.add_argument("--db")
    pre.add_argument("--config")
    try:
        _, rest = pre.parse_known_args(argv)
    except argparse.ArgumentError:
        return argv  # e.g. `--db` with no path: the real parser says so properly
    return argv if rest else [*argv, "app"]


def _tolerant_output() -> None:
    """Replace what the console cannot show instead of crashing on it.

    Windows' piped output uses the ANSI code page, which has no box-drawing
    characters or em dashes; one of those in a doctor line was a
    UnicodeEncodeError at the very end of the report. ``hasattr`` because
    under pythonw stdout is None, and tests swap in objects without it.
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            with suppress(ValueError, OSError):
                stream.reconfigure(errors="replace")


def main(argv: Sequence[str] | None = None) -> int:
    _tolerant_output()
    args = build_parser().parse_args(
        _with_default_command(list(sys.argv[1:] if argv is None else argv))
    )
    if args.command == "secrets" and args.action in ("set", "forget") and not args.name:
        print("which secret? " + ", ".join(s.name for s in secrets.SECRETS), file=sys.stderr)
        return 1
    if args.command == "run" and not args.prompt and not args.resume:
        print('what should I build? e.g. python -m jarvis run "a todo CLI"', file=sys.stderr)
        return 1
    if args.command == "answer" and not args.picks and args.text is None:
        print("which option? e.g. python -m jarvis answer 1 2", file=sys.stderr)
        return 1
    return int(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
