#!/usr/bin/env python3
"""Probe S4 — two concurrent ``gemini-3.8-live`` sessions, one Turkish, held 45 minutes.

FOUR UNVERIFIED LOAD-BEARING CLAIMS, ONE HOUR, UNDER A DOLLAR:

1. THE MODEL ID. ``gemini-3.8-live`` is what the research says is current and
   ``gemini-3.1-flash-live-preview`` — which the build sheet names — is legacy.
   If the id is wrong, everything below fails at connect with a message that
   reads like an auth problem.
2. THE CONCURRENCY CEILING. Sources conflict wildly: 3, 1000, 5000. The answer
   decides whether a desk conversation and an outbound restaurant call can
   coexist at all, or whether the desk must always checkpoint and get off the
   line first. ``LiveLease`` ships at capacity 1 either way, so a low answer
   costs nothing and a high answer is a one-line config change.
3. THE SESSION-LIMIT AND RESUMPTION STORY. Does a 45-minute call actually get a
   GoAway, does the resumption handle actually restore the conversation, and
   does context-window compression keep it alive across the boundary?
4. TURKISH. ``speech_config.language_code`` was REFUTED for native-audio
   models, so the only mechanism left is a Turkish system instruction and
   NOTHING confirms it holds for a whole call. This prints the output
   transcripts so a native speaker can score them; it does not pretend a
   character-class heuristic is a judgement.

It needs a Gemini API key, which this machine does not have — so with no key it
refuses, says exactly what it wants, and exits 2. ``--dry-run`` runs the entire
flow against the scripted transport instead, which is how the probe itself is
kept working on a machine where it can never be run for real.

    python tools/probe_live.py --dry-run                     # the harness works
    python tools/probe_live.py --minutes 45 --out s4.json    # the real run

Cost: roughly 45 session-minutes plus a couple of short turns, well under a
dollar at the third-party rate. Read usage_metadata in the output rather than
trusting that number.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.live import fake  # noqa: E402
from jarvis.live.profiles import AGENT_CALL, DESK, SessionProfile  # noqa: E402
from jarvis.live.session import (  # noqa: E402
    Connector,
    GenaiConnector,
    LiveEvent,
    LiveSession,
    LiveUnavailable,
)

KEYRING_SERVICE = "jarvis"
KEYRING_USER = "gemini_api_key"

#: Something short, in each language, that makes the model talk for a few
#: seconds. The point is output to transcribe, not a conversation.
PROBES_EN = [
    "In one sentence, what is on my calendar metaphorically speaking? Keep it short.",
    "Count from one to five, slowly.",
]
PROBES_TR = [
    "Kendini bir cümleyle tanıt.",
    "Birden beşe kadar yavaşça say.",
]


def _keyring_key() -> str | None:
    """The one place a credential is allowed to live. Never a file in the tree."""
    try:
        import keyring
    except ImportError:
        return None
    try:
        return keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
    except Exception:  # noqa: BLE001 - a locked or missing backend is a "no key"
        return None


def _refusal() -> str:
    return (
        "probe_live needs a Gemini API key and there is none.\n"
        f"  put one in the OS keyring:  keyring set {KEYRING_SERVICE} {KEYRING_USER}\n"
        "  or pass it for one run:      python tools/probe_live.py --api-key ...\n"
        "The environment is deliberately NOT read: a process that silently picks up\n"
        "GEMINI_API_KEY from a shell is a process that bills somebody without asking.\n"
        "To check the probe itself without a key:  python tools/probe_live.py --dry-run"
    )


def _dry_run_connector(turns: int, *, go_away: bool = True) -> Connector:
    """A scripted server that does everything the real one is being asked about."""
    script: list[Any] = [fake.handle("h-dry-1"), fake.audio(b"\x00" * 480)]
    for i in range(turns):
        script += [
            fake.ServerEvent(output_transcript=f"dry run turn {i + 1}"),
            fake.audio(b"\x00" * 960),
            fake.usage(120, prompt=70, response=50, **{"in:AUDIO": 70, "out:AUDIO": 50}),
            fake.turn_complete(),
        ]
    if go_away:
        script += [fake.go_away(5.0), fake.turn_complete()]
    # No hangup at the end: a quiet, open connection is what a real one looks
    # like between turns, and a dry run that reconnected in a loop would be
    # testing the scripted connector rather than the probe.
    after = [fake.handle("h-dry-2"), fake.audio(b"\x00" * 480)]
    return fake.ScriptedConnector(scripts=[script, after], repeat_last=True)


class Recorder:
    """Every session event, plus the few numbers the four questions need."""

    def __init__(self) -> None:
        self.events: list[LiveEvent] = []
        self.transcripts: list[str] = []
        self.usage: list[dict[str, Any]] = []

    def __call__(self, event: LiveEvent) -> None:
        self.events.append(event)
        if event.kind == "output_transcript":
            self.transcripts.append(str(event.detail.get("text", "")))
        elif event.kind == "usage":
            self.usage.append(dict(event.detail))

    def kinds(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for event in self.events:
            counts[event.kind] = counts.get(event.kind, 0) + 1
        return counts


async def _hold(
    profile: SessionProfile,
    connector: Connector,
    *,
    seconds: float,
    prompts: list[str],
    turn_every: float,
    label: str,
) -> dict[str, Any]:
    """Open a session, speak to it every ``turn_every`` seconds, hold for ``seconds``."""
    recorder = Recorder()
    sink = fake.BytesSink()
    session = LiveSession(profile, None, sink, connector=connector, on_event=recorder)
    task = asyncio.create_task(session.run())
    started = time.monotonic()
    turn = 0
    error = ""
    try:
        while time.monotonic() - started < seconds:
            await asyncio.sleep(min(turn_every, max(0.0, seconds - (time.monotonic() - started))))
            if not session.connected:
                continue
            prompt = prompts[turn % len(prompts)]
            turn += 1
            try:
                await session.prefill(prompt, turn_complete=True)
            except Exception as exc:  # noqa: BLE001 - the failure IS the finding
                error = f"{type(exc).__name__}: {exc}"
                break
    finally:
        await session.close()
        outcome = (await asyncio.gather(task, return_exceptions=True))[0]
        if isinstance(outcome, BaseException):
            error = error or f"{type(outcome).__name__}: {outcome}"

    return {
        "label": label,
        "profile": profile.name,
        "model": profile.model,
        "voice": profile.voice,
        "language": profile.language,
        "language_caveat": profile.language_caveat,
        "held_s": round(time.monotonic() - started, 1),
        "turns_sent": turn,
        "connects": session.connects,
        "resumes": session.resumes,
        "reconnects": session.reconnects,
        "go_aways": session.go_aways,
        "downlink_bytes": sink.nbytes,
        "handle_survived": session.resume_handle is not None,
        "event_counts": recorder.kinds(),
        "usage_reports": recorder.usage[-5:],
        "output_transcripts": recorder.transcripts,
        "error": error,
    }


async def _concurrency(make: Callable[[], Connector], want: int) -> dict[str, Any]:
    """How many sessions this account will actually hold open at once."""
    sessions: list[LiveSession] = []
    tasks: list[asyncio.Task[None]] = []
    opened = 0
    first_error = ""
    for i in range(want):
        profile = DESK.with_voice("Zephyr") if i == 0 else DESK.with_voice("Puck")
        session = LiveSession(profile, None, fake.BytesSink(), connector=make(), reconnect=False)
        task = asyncio.create_task(session.run())
        sessions.append(session)
        tasks.append(task)
        for _ in range(2000):
            if session.connects > 0 or task.done():
                break
            await asyncio.sleep(0.005)
        if task.done() and task.exception() is not None:
            first_error = f"{type(task.exception()).__name__}: {task.exception()}"
            break
        if session.connected:
            opened += 1
    for session in sessions:
        await session.close()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException) and not first_error:
            first_error = f"{type(result).__name__}: {result}"
    return {"requested": want, "opened_concurrently": opened, "first_error": first_error}


async def run(args: argparse.Namespace) -> dict[str, Any]:
    seconds = args.minutes * 60.0

    def connector_factory(*, quiet: bool = False) -> Connector:
        """``quiet`` means "just stay open" — what the concurrency count needs."""
        if args.dry_run:
            return _dry_run_connector(turns=0 if quiet else 2, go_away=not quiet)
        return GenaiConnector(api_key=args.api_key)

    report: dict[str, Any] = {
        "probe": "S4",
        "dry_run": bool(args.dry_run),
        "model": DESK.model,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "requested_minutes": args.minutes,
    }

    report["concurrency"] = await _concurrency(
        lambda: connector_factory(quiet=True), args.concurrency
    )

    turkish, english = await asyncio.gather(
        _hold(
            AGENT_CALL,
            connector_factory(),
            seconds=min(seconds, args.turkish_minutes * 60.0),
            prompts=PROBES_TR,
            turn_every=args.turn_every,
            label="turkish",
        ),
        _hold(
            DESK,
            connector_factory(),
            seconds=seconds,
            prompts=PROBES_EN,
            turn_every=args.turn_every,
            label="english_long_hold",
        ),
    )
    report["turkish"] = turkish
    report["long_hold"] = english

    report["answers"] = {
        "model_id_exists": not (english["error"] or turkish["error"]),
        "concurrent_ceiling_at_least": report["concurrency"]["opened_concurrently"],
        "go_away_seen": english["go_aways"] > 0,
        "resumed_after_go_away": english["resumes"] > 0,
        "handle_survived_every_reconnect": english["handle_survived"],
        "turkish_output_transcripts": turkish["output_transcripts"],
        "turkish_verdict": "SCORE THESE BY EAR — a native speaker, twenty minutes, drift and "
        "input-transcription language. No heuristic here is a judgement.",
    }
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--minutes", type=float, default=45.0, help="how long to hold the long session")
    ap.add_argument("--turkish-minutes", type=float, default=20.0)
    ap.add_argument("--turn-every", type=float, default=60.0)
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--api-key", default=None, help="one-off key; prefer the OS keyring")
    ap.add_argument(
        "--dry-run", action="store_true", help="scripted transport, no key, seconds not minutes"
    )
    ap.add_argument("--out", default=None, help="write the JSON report here as well as stdout")
    args = ap.parse_args(argv)

    if args.dry_run:
        # Fractions of a second, not minutes: the point is that the harness runs,
        # not that it waits, and this path is also a test.
        args.minutes = min(args.minutes, 0.003)
        args.turkish_minutes = min(args.turkish_minutes, 0.003)
        args.turn_every = min(args.turn_every, 0.02)
    else:
        args.api_key = args.api_key or _keyring_key()
        if not args.api_key:
            print(_refusal(), file=sys.stderr)
            return 2

    try:
        report = asyncio.run(run(args))
    except LiveUnavailable as exc:
        print(f"{exc}\n\n{_refusal()}", file=sys.stderr)
        return 2

    text = json.dumps(report, indent=2, default=str)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text)
    print(text)
    answers = report["answers"]
    return 0 if answers["model_id_exists"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
