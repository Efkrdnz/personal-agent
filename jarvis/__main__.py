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
    python -m jarvis run "..."      start a build and drive Claude Code
    python -m jarvis build          carry every spoken build request forward a step
    python -m jarvis pending        the questions waiting on you, numbered
    python -m jarvis answer 1 2     answer one, by the numbers you were read
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
import sqlite3
import subprocess
import sys
import time
from collections.abc import Sequence
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
    r.add(
        WARN,
        f"tidy model {cfg.voice.tidy_model} — UNVERIFIED; a 404 on your first "
        f"`build` means change voice.tidy_model in config.toml",
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
                f"{module} — without it, {breaks}  (pip install -e '.[{extra}]')",
            )


def claude_cli_path() -> str | None:
    """The CLI the DRIVER would actually run, which is usually not the one on PATH.

    ``claude-agent-sdk`` ships its own pinned CLI and prefers it; ``shutil.which``
    is only its fallback. Checking PATH alone gets this wrong in BOTH directions:
    it reports "missing" on a machine where ``pip install -e '.[cc]'`` is all that
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
        r.add(BAD, "no `claude` CLI — the SDK bundles one; pip install -e '.[cc]'")
        return
    bundled = "bundled with the SDK" if "claude_agent_sdk" in exe else "from PATH"
    try:
        out = subprocess.run(  # noqa: S603 - a fixed argv, no shell
            [exe, "--version"], capture_output=True, text=True, timeout=20, check=False
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
        why = "sounddevice is not installed (pip install -e '.[voice]')"
        r.add(BAD, why)
        return False, why
    from jarvis.audio import DEV_RATE
    from jarvis.audio.devices import DeviceError, PortAudioProbe, select_duplex_device

    probe = PortAudioProbe()
    try:
        found = list(probe.devices())
        r.add(OK, f"{len(found)} device(s), {sum(1 for d in found if d.duplex)} full-duplex")
        for d in [x for x in found if x.duplex][:6]:
            r.add(OK, f"  [{d.index}] {d.name}  ({d.hostapi}, {d.default_samplerate:.0f} Hz)")
    except DeviceError as exc:
        r.add(BAD, str(exc))
        return False, str(exc)

    name = cfg.voice.input_device if cfg is not None else None
    try:
        selection = select_duplex_device(probe, name=name, samplerate=DEV_RATE)
    except DeviceError as exc:
        r.add(BAD, f"{type(exc).__name__}: {exc}")
        return False, str(exc)
    r.add(OK, f"desk would use: {selection.name}")
    return True, ""


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
    verdict(not desk_why, "python -m jarvis desk", desk_why)

    cc_why = "" if claude_cli_path() else "no claude CLI (pip install -e '.[cc]')"
    verdict(not cc_why, "python -m jarvis.cc", cc_why)
    verdict(not cc_why, "python -m jarvis run", cc_why)
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
        f"{'python -m jarvis.schedule':<28} — arms the 10am gate AND routes questions to channels",
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
    return subprocess.Popen(
        argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )


def cmd_run(args: argparse.Namespace) -> int:
    """Create the `claude_code` job and drive it. The front door the driver lacked.

    Until this existed the only caller of `create_job(kind='claude_code')`
    outside tests was a spike script, so "drive Claude Code" meant "write Python".
    """
    if not _installed("claude_agent_sdk") or claude_cli_path() is None:
        print(
            "Claude Code is not installed here: pip install -e '.[cc]'\n"
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
            print("  answer with:  python -m jarvis answer 1 <number>")

    while child.poll() is None:
        sweep()
        time.sleep(RUN_POLL_S)
    # ONE MORE SWEEP after the child is gone. A question raised between the last
    # poll and the exit is not a corner case — it is the entire defer path, where
    # the row is written and the process leaves immediately.
    sweep()

    job = jobs.get(con, job_id)
    state = job.state if job else "gone"
    print(f"\nrunner exited {child.returncode}; job is {state}")
    if state == "deferred":
        print("parked on a question. Answer it, then:  python -m jarvis run --resume")
    elif job and job.result_summary:
        print(job.result_summary.strip()[:500])
    return 0 if child.returncode == 0 else 1


def _render_request(req: rq.Request, n: int) -> str:
    """One open question as numbered lines. The SAME numbering every channel uses.

    Built from ``presentation``, not from the raw payload: every request kind has
    a presentation and only ``plan_question`` has an AskUserQuestion payload, so
    rendering from the payload would silently show nothing for an exit-plan or a
    permission question — which are most of them.
    """
    pres = req.presentation
    lines = [f"[{n}] {req.short_label}  ({req.kind})", f"    {pres['intro']}"]
    lines += [f"    {item['index']}. {item['label']}" for item in pres["items"]]
    if pres.get("allows_free_text"):
        lines.append('    or:  python -m jarvis answer <n> --text "your own words"')
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
        if not 1 <= args.which <= len(open_):
            print(
                f"there {'is' if len(open_) == 1 else 'are'} {len(open_)} open question(s); "
                f"run `python -m jarvis pending`",
                file=sys.stderr,
            )
            return 1
        req = open_[args.which - 1]
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
            # The driver is not sitting on this row: it exited when it deferred.
            # Without this line the user answers and nothing ever happens.
            print("that job is parked — resume it:  python -m jarvis run --resume")
    finally:
        con.close()
    return 0


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
            model=cfg.voice.tidy_model,
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
                print(f"  now run:  python -m jarvis run --resume   (job {step.child_job_id})")
    finally:
        con.close()
    return 0


# ───────────────────────────── desk ─────────────────────────────


class StartupRefused(RuntimeError):
    """The desk cannot start, and the message says what to run."""


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


def _build_desk(args: argparse.Namespace) -> Desk:
    """Wire the desk. Raises :class:`StartupRefused` with a sentence, never a traceback.

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
    from jarvis.audio.dsp import AecUnavailable, EnergyVad
    from jarvis.audio.graph import AudioGraph, QueuedEventSink
    from jarvis.audio.legs import DeskLeg
    from jarvis.audio.micbus import MicBus
    from jarvis.audio.mixer import PlaybackMixer, Prio
    from jarvis.audio.turn import TurnController
    from jarvis.live.profiles import DESK, SessionProfile
    from jarvis.live.session import GenaiConnector, LiveSession, QueuedUplink
    from jarvis.tools.default import registry
    from jarvis.voice.router import TrackSink
    from jarvis.voice.tools import DeskQuestions, LiveTools, Transcript

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
    try:
        aec = leg.make_aec()
    except AecUnavailable as exc:
        # Reached only when the config says assume_headset = false, which is the
        # user asking for open speakers. That is a refusal with a fix, not a
        # traceback: AecUnavailable is a plain RuntimeError and nothing above
        # here would have caught it.
        raise StartupRefused(
            f"{exc}\nYou have voice.assume_headset = false, which means open speakers and "
            "therefore a real echo canceller. Either `pip install -e '.[aec]'`, or set "
            "assume_headset = true and use a headset."
        ) from exc
    graph = AudioGraph(
        mixer=mixer,
        micbus=micbus,
        turn=turn,
        aec=aec,
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
    return Desk(leg=leg, graph=graph, session=session, questions=questions, reader=reader)


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


def cmd_desk(args: argparse.Namespace) -> int:
    import asyncio

    from jarvis.live.session import LiveUnavailable

    try:
        desk = _build_desk(args)
    except (StartupRefused, secrets.MissingSecret) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    leg, graph, session, questions = desk.leg, desk.graph, desk.session, desk.questions

    async def watch_for_questions() -> None:
        """Claim and read aloud every question routed to this desk.

        A separate task rather than a hook in the receive loop: reading a
        question is seconds of synthesis and speech, and the loop it would
        otherwise block is the one carrying the user's own voice.
        """
        loop = asyncio.get_running_loop()
        if questions.speak is None and desk.reader is not None:

            def say_from_thread(utt: Any) -> int:
                """Block the worker thread until the reader has finished the clip.

                `run_coroutine_threadsafe` is the whole bridge: the poll runs off
                the loop (it does blocking SQLite), the reader runs on it, and
                the thread waits. Without it the coroutine is never awaited and
                the desk silently reads nothing.
                """
                return asyncio.run_coroutine_threadsafe(desk.reader.speak(utt), loop).result(120)

            questions.speak = say_from_thread
        while True:
            try:
                await asyncio.to_thread(questions.poll)
            except Exception as exc:  # noqa: BLE001 - a bad row must not end the conversation
                print(f"[questions] {type(exc).__name__}: {exc}")
            await asyncio.sleep(DESK_POLL_S)

    async def run() -> None:
        leg.open(graph)
        print("listening. ctrl-c to stop.")
        watcher = asyncio.create_task(watch_for_questions())
        try:
            await session.run()
        finally:
            watcher.cancel()
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
    a.add_argument("which", type=int, help="which question, from `pending`")
    a.add_argument("picks", nargs="*", type=int, help="the option numbers you were read")
    a.add_argument("--text", default=None, help="'none of these' — your own words")
    a.set_defaults(fn=cmd_answer)

    sub.add_parser("desk", help="listen, talk, and drive Claude Code").set_defaults(fn=cmd_desk)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
