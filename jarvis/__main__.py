"""``python -m jarvis`` — the composition root, and the only file allowed to be one.

Everything else in this tree points inwards; this file points at everything. That
is its job and it is the reason ``tools/check_layers.py`` exempts exactly one
path. The rule it still obeys, which no checker can enforce: THERE ARE NO
DECISIONS HERE. Anything below that is worth a unit test lives in a layer that
has one — the tool surface in :mod:`jarvis.tools`, the transcript window and the
tool adapter in :mod:`jarvis.voice.tools`, every sentence in the module that owns
the data behind it.

The commands, in the order somebody new to the machine needs them:

    python -m jarvis doctor         what is missing, and the command that fixes it
    python -m jarvis secrets set X  put a credential in the OS keyring
    python -m jarvis config init    write a config file with the defaults in it
    python -m jarvis status         what is running, as text
    python -m jarvis tools          the tool surface, per channel
    python -m jarvis desk           listen, talk, and drive Claude Code

``doctor`` is the important one. A voice assistant that fails at startup fails
with no screen and no log the user will find, so the whole of "why won't it
start" is one command that names the gap AND the command that closes it.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jarvis import config as cfgmod
from jarvis import db, ledger, presence, reconcile, secrets

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
    ("edge_tts", "tts", "no reader voice, so Gemini reads load-bearing text", False),
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
    import sqlite3

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
    r.add(OK, f"timezone {cfg.tz}, workspace {cfg.workspace_path}")
    r.add(OK, f"claude {cfg.desk.model}, effort {cfg.desk.effort}, mode {cfg.desk.permission_mode}")
    r.add(OK, f"gemini {cfg.voice.model}, voice {cfg.voice.gemini_voice}")
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
                f"{module} — without it, {breaks}  (pip install -e '.[{extra}]')",
            )


def _check_claude_cli(r: Report) -> None:
    r.section("claude code")
    exe = shutil.which("claude")
    if exe is None:
        r.add(BAD, "the `claude` CLI is not on PATH — the driver runs it as a subprocess")
        return
    try:
        out = subprocess.run(  # noqa: S603 - a fixed argv, no shell
            [exe, "--version"], capture_output=True, text=True, timeout=20, check=False
        )
        version = (out.stdout or out.stderr).strip().splitlines()[:1]
        r.add(OK, f"{exe}  {version[0] if version else '(version unknown)'}")
    except (OSError, subprocess.SubprocessError) as exc:
        r.add(WARN, f"{exe} would not report a version: {type(exc).__name__}")


def _check_audio(r: Report) -> None:
    r.section("audio")
    if not _installed("sounddevice"):
        r.add(WARN, "sounddevice is not installed, so devices cannot be listed")
        return
    try:
        from jarvis.audio.devices import PortAudioProbe

        found = list(PortAudioProbe().devices())
    except Exception as exc:  # noqa: BLE001 - no PortAudio, no sound server, no devices
        r.add(WARN, f"no audio devices readable: {exc}")
        return
    duplex = [d for d in found if d.duplex]
    # Duplex specifically: the desk leg opens ONE stream for both directions so
    # that the AEC reference is bit-exact with what the speaker played. A box
    # with a microphone and speakers on two different devices cannot run it.
    r.add(OK if duplex else BAD, f"{len(found)} device(s), {len(duplex)} full-duplex")
    for d in duplex[:6]:
        r.add(OK, f"  [{d.index}] {d.name}  ({d.hostapi}, {d.default_samplerate:.0f} Hz)")


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


def cmd_doctor(args: argparse.Namespace) -> int:
    r = Report(lines=[])
    _check_runtime(r)
    _check_config(r, args.config)
    _check_secrets(r)
    _check_packages(r)
    _check_database(r, args.db)
    _check_claude_cli(r)
    _check_audio(r)
    _check_tool_surface(r)

    print("\n".join(r.lines).strip())
    print()
    if r.blocking:
        print(f"{r.blocking} thing(s) must be fixed before `python -m jarvis desk` will start.")
        return 1
    print("Everything required is present. `python -m jarvis desk` should start.")
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
            "the keyring package is not installed (pip install -e '.[secrets]').\n"
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


# ───────────────────────────── desk ─────────────────────────────


class StartupRefused(RuntimeError):
    """The desk cannot start, and the message says what to run."""


def _desk_reader(cfg: cfgmod.Config, mixer: Any) -> Any | None:
    """The deterministic reader, or None with the reason already printed.

    Returning None is a supported state, not a degraded one: :class:`LiveTools`
    hands the sentence to the model instead and says so in the response. What is
    lost is the audible "these are somebody else's exact words" marker, which is
    worth saying out loud at startup rather than discovering during a read-back.
    """
    from jarvis.audio.mixer import Prio
    from jarvis.voice.cache import PcmCache
    from jarvis.voice.engines import EdgeEngine
    from jarvis.voice.router import TrackSink
    from jarvis.voice.verbatim import VerbatimSpeaker

    if not _installed("edge_tts"):
        print(f"{WARN}  no reader voice (pip install -e '.[tts]') — Gemini will read everything")
        return None
    track = mixer.track("verbatim", Prio.VERBATIM, content_rate=24_000)
    return VerbatimSpeaker(
        engines=(
            EdgeEngine(voices={"en": cfg.voice.reader_voice, "tr": cfg.voice.reader_voice_tr}),
        ),
        cache=PcmCache(),
        sink=TrackSink(track),
    )


def _build_desk(args: argparse.Namespace) -> tuple[Any, Any, Any]:
    """Wire the desk. Returns (leg, graph, session). Raises :class:`StartupRefused`.

    Read top to bottom: this is the whole architecture in one function, which is
    the point of having exactly one place allowed to know every layer.
    """
    missing = [m for m, _, _, required in EXTRAS if required and not _installed(m)]
    if missing:
        raise StartupRefused(
            f"missing packages: {', '.join(missing)}. Run `python -m jarvis doctor` for the "
            "install commands, or `pip install -e '.[cc,voice,live,tts]'`."
        )

    from jarvis.audio import BLOCK, DEV_RATE, MIC_RATE
    from jarvis.audio.devices import DeviceError
    from jarvis.audio.dsp import EnergyVad
    from jarvis.audio.graph import AudioGraph, QueuedEventSink
    from jarvis.audio.legs import DeskLeg
    from jarvis.audio.micbus import MicBus
    from jarvis.audio.mixer import PlaybackMixer, Prio
    from jarvis.audio.turn import TurnController
    from jarvis.live.profiles import DESK, SessionProfile
    from jarvis.live.session import GenaiConnector, LiveSession, QueuedUplink
    from jarvis.tools.default import registry
    from jarvis.voice.router import TrackSink
    from jarvis.voice.tools import LiveTools, Transcript

    cfg = cfgmod.load(args.config)
    key = secrets.require("gemini_api_key")  # raises MissingSecret with instructions

    con = db.open_db(args.db)
    try:
        reconcile.reconcile(con, actor="desk")
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
        raise StartupRefused(str(exc)) from exc
    print(f"{OK}  microphone and speaker: {selection.name}")
    turn = TurnController(mixer=mixer, vad=EnergyVad(), uplink=uplink)
    graph = AudioGraph(
        mixer=mixer,
        micbus=micbus,
        turn=turn,
        aec=leg.make_aec(),
        device_rate=leg.device_rate,
        block=leg.block,
        on_event=QueuedEventSink(),
    )
    if leg.aec_degraded:
        print(f"{WARN}  no echo canceller — use a headset, or open speakers will hear themselves")

    # ── the voices ───────────────────────────────────────────────────────
    reader = _desk_reader(cfg, mixer)
    live_track = mixer.track("live", Prio.LIVE, content_rate=24_000)

    # ── what a sentence is allowed to do ─────────────────────────────────
    transcript = Transcript()
    tools = LiveTools(
        registry=registry(),
        # A fresh connection per tool call, opened in the worker thread that
        # runs it. See jarvis/voice/tools.py.
        open_db=lambda: db.open_db(args.db),
        channel="desk",
        actor="desk",
        transcript=transcript,
        speak=(lambda utt: reader.speak(utt)) if reader is not None else None,
        extra={"spend_threshold_usd": cfg.spend_threshold_usd},
    )

    profile: SessionProfile = DESK
    if cfg.voice.model != profile.model or cfg.voice.gemini_voice != profile.voice:
        from dataclasses import replace

        profile = replace(profile, model=cfg.voice.model, voice=cfg.voice.gemini_voice)

    session = LiveSession(
        profile,
        uplink,
        TrackSink(live_track),
        connector=GenaiConnector(api_key=key),
        tools=tools,
        on_event=_desk_event_printer(transcript),
    )
    return leg, graph, session


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
        elif event.kind in ("connected", "reconnect", "go_away", "tool_error"):
            print(f"[{event.kind}] {event.detail}")

    return on_event


def cmd_desk(args: argparse.Namespace) -> int:
    import asyncio

    from jarvis.live.session import LiveUnavailable

    try:
        leg, graph, session = _build_desk(args)
    except (StartupRefused, secrets.MissingSecret) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    async def run() -> None:
        leg.open(graph)
        print("listening. ctrl-c to stop.")
        try:
            await session.run()
        finally:
            await session.close()
            leg.close()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nstopped.")
    except LiveUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


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

    sub.add_parser("desk", help="listen, talk, and drive Claude Code").set_defaults(fn=cmd_desk)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "secrets" and args.action in ("set", "forget") and not args.name:
        print("which secret? " + ", ".join(s.name for s in secrets.SECRETS), file=sys.stderr)
        return 1
    return int(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
