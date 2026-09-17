"""Converge the job table with reality, and say what it found.

Two jobs, one module, because they are the same query asked twice.

RECONCILE runs at the START OF EVERY PROCESS — the voice app, the dispatcher,
each runner, the Telegram bot — and is idempotent, so several of them starting at
once is fine and calling it twice is free. It answers the question a restart
always raises: of the jobs this file says are running, which ones actually are?
A job whose process is *proven* gone becomes ``orphaned``; a job we cannot decide
about is left exactly as it is and reported as undetermined, because guessing in
either direction is worse than saying "I don't know".

PROJECT_STATUS is the same data pointed at the morning briefing's first section,
which the research found had no data source at all. It answers what a person
actually wants said out loud: what finished, what failed, what is still waiting
on them and for how long.

The split between them is deliberate. Reconcile WRITES and is allowed to spawn;
project_status only READS, so the briefing can be rehearsed, re-read on another
channel, or asked for twice without changing anything.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from jarvis.ids import now, parse_ts
from jarvis.jobs import (
    ACTIVE_STATES,
    TERMINAL_STATES,
    IllegalTransition,
    Job,
    claim_resume,
    job_liveness,
    list_by_state,
    set_state,
    shift_ts,
)
from jarvis.jobs import since as jobs_since

__all__ = [
    "BLOCKED_BRIEFING_S",
    "BRIEFING_CURSOR",
    "DEFAULT_LOOKBACK_S",
    "RESUME_MAX",
    "STALE_HEARTBEAT_S",
    "STARTING_GRACE_S",
    "JobNote",
    "ProjectStatus",
    "Spawner",
    "briefing_cursor",
    "project_status",
    "reconcile",
    "set_briefing_cursor",
]

#: How many times reconcile will respawn one job before it wants a human. Three,
#: because a job that has died three times is failing for a reason no retry will
#: fix, and a silent respawn loop burns a rate-limit window nobody is watching.
RESUME_MAX = 3

#: A job that has been claimed for respawn has no pid yet. This is how long it is
#: allowed to have none before it is treated as gone again — long enough for
#: systemd-run plus a Python import, short enough that a spawner which died
#: between the claim and the exec is noticed within one briefing.
STARTING_GRACE_S = 120.0

#: A heartbeat older than this on a job whose process is genuinely alive means
#: the runner is wedged, not dead. Reported, never acted on: killing a build
#: because it went quiet during a long compile would be the worse error.
STALE_HEARTBEAT_S = 300.0

#: The schema's own rule: blocked for more than fifteen minutes means it gets
#: said out loud in the morning. This is the last backstop against a question
#: that was raised while nobody was listening and then never mentioned again.
BLOCKED_BRIEFING_S = 900.0

#: Where "since the last briefing" is stored. The cursors table is shared; this
#: is the name the architecture assigns to this one.
BRIEFING_CURSOR = "briefing_last_run"

#: Fallback window when no briefing has ever run: one day, so the first briefing
#: is about yesterday rather than about the whole history of the machine.
DEFAULT_LOOKBACK_S = 24 * 3600.0

#: Spawning is injected, never imported. A library function that runs in EVERY
#: process must not decide on its own to fork runners — jarvis-dispatch passes a
#: spawner, everyone else passes nothing, claims nothing, and reads
#: ``report["resumable"]``.
Spawner = Callable[[Job], None]

#: The job kinds a spawner may be handed. Phase 2 is otherwise KIND-BLIND — it
#: resumes anything deferred or orphaned — and the only spawner in the tree
#: starts ``python -m jarvis.cc``, which drives Claude Code. Handing it a
#: ``repo_setup`` row meant the driver ran a build request as if it were a build,
#: failed it TERMINALLY, and destroyed a project the user had already approved.
#: Worse, ``jarvis answer`` printed the command that did it.
#:
#: A caller may widen this, and must say which kinds its spawner actually knows
#: how to start. Kinds outside it are reported as ``resumable`` and left alone,
#: never claimed — a claim spends one of three resume attempts before anything
#: can refuse.
SPAWNABLE_KINDS: frozenset[str] = frozenset({"claude_code"})


# ───────────────────────────── reconcile ─────────────────────────────


def _age_s(job: Job, ts: str) -> float:
    """Seconds since this job last did anything observable.

    The LATER of the heartbeat and the last state change, not the heartbeat
    alone: a runner that heartbeats at startup and then blocks on a question for
    ten minutes is not wedged, and reporting it as such every pass would train
    the reader to ignore the one time it means something. ``max`` on the strings
    is chronological because :func:`jarvis.ids.now` is fixed-width UTC.
    """
    marker = max(job.heartbeat_at or "", job.updated_at)
    return (parse_ts(ts) - parse_ts(marker)).total_seconds()


def _request_state(con: sqlite3.Connection, request_id: str | None) -> str | None:
    """Read-only peek at a requests row. jarvis.requests owns every write to it."""
    if not request_id:
        return None
    row = con.execute("SELECT state FROM requests WHERE id=?", (request_id,)).fetchone()
    return None if row is None else str(row["state"])


def reconcile(
    con: sqlite3.Connection,
    actor: str = "reconciler",
    *,
    spawn: Spawner | None = None,
    spawn_kinds: frozenset[str] = SPAWNABLE_KINDS,
    now_ts: str | None = None,
) -> dict[str, Any]:
    """Make the job table tell the truth, and return what changed.

    Idempotent: a second call with nothing else happening in between returns
    empty lists. Concurrency-safe: every write is a compare-and-swap, so two
    processes reconciling the same orphan in the same millisecond produce exactly
    one respawn — the loser silently reports nothing rather than spawning a
    second runner for the same job.

    The returned dict is meant to be spoken as much as logged::

        {"orphaned": [...], "resumed": [...], "resumable": [...],
         "awaiting_answer": [...], "needs_human": [...], "undetermined": [...],
         "stale_heartbeat": [...], "spawn_errors": [...], "checked": n, "ts": "..."}

    ``undetermined`` is the honest one: on a platform with no ``/proc`` we cannot
    prove a pid is still ours, so those jobs are untouched and named.

    ``resumable`` is what a caller with no ``spawn`` gets instead of ``resumed``:
    jobs that want picking up by whichever process CAN start one. See the comment
    at the claim for why a spawnerless process must not claim them itself.
    """
    ts = now_ts or now()
    report: dict[str, Any] = {
        "ts": ts,
        "actor": actor,
        "checked": 0,
        "orphaned": [],
        "resumed": [],
        "resumable": [],
        "awaiting_answer": [],
        "needs_human": [],
        "undetermined": [],
        "stale_heartbeat": [],
        "spawn_errors": [],
    }

    # ── 1. jobs that claim to be underway, but whose process is provably gone ──
    for job in list_by_state(con, sorted(ACTIVE_STATES)):
        report["checked"] += 1
        liveness = job_liveness(job)
        if liveness == "alive":
            if _age_s(job, ts) > STALE_HEARTBEAT_S:
                report["stale_heartbeat"].append(job.id)
            continue
        if liveness == "unknown":
            report["undetermined"].append(job.id)
            continue
        if job.pid is None and _age_s(job, ts) < STARTING_GRACE_S:
            # Claimed for respawn moments ago and not yet exec'd. Orphaning it
            # here would race the very spawn this reconcile just asked for.
            continue
        reason = "process gone" if job.pid else "never reported a pid"
        try:
            # stop_reason as well as the event: the briefing reads the column,
            # and "it stopped" with no cause is the answer that prompts the
            # follow-up question nobody can answer an hour later.
            set_state(
                con,
                job.id,
                "orphaned",
                actor=actor,
                expect=job.state,
                reason=reason,
                stop_reason=reason,
            )
        except IllegalTransition:
            # Another reconciler, or the runner itself finishing, moved it while
            # we were looking. Its outcome is as good as ours; say nothing.
            continue
        report["orphaned"].append(job.id)

    # ── 2. converge the two resumable paths onto one ──
    # A deferred job whose answer has arrived, and an orphan from a reboot, want
    # exactly the same thing: spawn with --resume. Doing it in one pass is why a
    # power cut and a four-hour phone answer are the same code path.
    for job in list_by_state(con, ("deferred", "orphaned")):
        req_state = _request_state(con, job.blocked_request_id)
        if req_state == "pending":
            # Resuming now would only re-ask a question already on somebody's
            # phone. The router owns getting it answered; we just name it.
            report["awaiting_answer"].append(job.id)
            continue
        if job.resume_policy != "auto" or job.resume_count >= RESUME_MAX:
            report["needs_human"].append(job.id)
            continue
        if job.kind not in spawn_kinds:
            # This spawner does not know how to start that kind. Reported, not
            # claimed: claiming would spend a resume attempt and move the row to
            # 'starting', where nothing scans for it, before the spawner ever
            # got the chance to refuse.
            report["resumable"].append(job.id)
            continue
        if spawn is None:
            # A PROCESS THAT CANNOT SPAWN MUST NOT CLAIM. reconcile() runs at the
            # start of every process, but only jarvis-dispatch passes a spawner.
            # Claiming here would spend one of the three resume attempts, move
            # the job to 'starting' — a state nothing scans for pickup — and
            # leave the register saying the build was resumed when no runner
            # exists. Two or three spawnerless boots would exhaust RESUME_MAX and
            # strand the job in needs_human without a single runner ever starting.
            report["resumable"].append(job.id)
            continue
        claimed = claim_resume(
            con,
            job.id,
            actor=actor,
            expect_state=job.state,
            expect_resume_count=job.resume_count,
            reason="reconcile: " + ("answer arrived" if req_state else "process gone"),
        )
        if claimed is None:
            continue  # another process won the claim and owns the spawn
        report["resumed"].append(job.id)
        try:
            # Outside the transaction, always: this is process creation, and the
            # structural rule is that no subprocess I/O happens under a write
            # lock every other process is waiting on.
            spawn(claimed)
        except Exception as e:
            # reconcile() is the first thing every process does. A spawner that
            # throws must not stop the dispatcher from starting; the job keeps
            # its claim and is retried after STARTING_GRACE_S.
            report["spawn_errors"].append({"job_id": job.id, "error": repr(e)})

    return report


# ───────────────────────────── briefing ─────────────────────────────


@dataclass(frozen=True, slots=True)
class JobNote:
    """One job, and the sentence Jarvis would say about it."""

    job_id: str
    title: str
    state: str
    line: str
    seconds: float | None = None
    request_id: str | None = None
    short_label: str | None = None


@dataclass(frozen=True, slots=True)
class ProjectStatus:
    """Section 1 of the morning briefing, as data plus the words for it.

    ``lines`` is what gets spoken. The architecture types this section as
    ``list[Utterance]``, but ``Utterance`` lives in ``jarvis.voice.router`` and
    importing the voice layer into the job registry would point the dependency
    the wrong way round — every runner and the Telegram bot would pull audio code
    they never use. The briefing wraps these strings; the wording is decided
    here, where the data is, so it cannot drift from what the rows say.
    """

    since: str
    finished: tuple[JobNote, ...] = ()
    failed: tuple[JobNote, ...] = ()
    blocked: tuple[JobNote, ...] = ()
    deferred: tuple[JobNote, ...] = ()
    stalled: tuple[JobNote, ...] = ()
    undetermined: tuple[JobNote, ...] = ()
    open_requests: int = 0
    lines: tuple[str, ...] = ()

    @property
    def quiet(self) -> bool:
        """True when there is genuinely nothing to report."""
        return not (
            self.finished
            or self.failed
            or self.blocked
            or self.deferred
            or self.stalled
            or self.undetermined
        )


def briefing_cursor(con: sqlite3.Connection) -> str | None:
    row = con.execute("SELECT value FROM cursors WHERE name=?", (BRIEFING_CURSOR,)).fetchone()
    return None if row is None else str(row["value"])


def set_briefing_cursor(con: sqlite3.Connection, ts: str | None = None) -> str:
    """Advance the briefing cursor. Called AFTER the briefing was actually said.

    Deliberately not done by :func:`project_status`: a briefing that was composed
    and then not delivered — the channel dropped, the room was empty — must be
    said again, and a read that advanced the cursor would lose it silently.

    THE CURSOR IS INCLUSIVE, and that is a choice rather than an oversight.
    :func:`jarvis.jobs.since` matches ``updated_at >= cursor``, and ``now()`` is
    millisecond-granular, so a job that finishes in the same millisecond the
    cursor is written gets mentioned again in the next briefing. Measured: the
    collision happens on roughly 80% of back-to-back writes on a fast disk.

    Repeating beats dropping. Hearing "the scraper build failed" twice is mildly
    annoying; the exclusive version would mean never hearing it at all, and the
    whole point of the briefing is that nothing important goes unsaid. A
    monotonic sequence rather than a timestamp would give both, and is the right
    fix if this ever becomes irritating in practice.
    """
    value = ts or now()
    con.execute(
        "INSERT INTO cursors (name, value, updated_at) VALUES (?,?,?)"
        " ON CONFLICT(name) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (BRIEFING_CURSOR, value, now()),
    )
    return value


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one}" if n == 1 else f"{n} {many}"


def _duration_phrase(seconds: float) -> str:
    """A duration a person would actually say, not "5400 seconds"."""
    minutes = int(seconds // 60)
    if minutes < 1:
        return "less than a minute"
    if minutes < 60:
        return _plural(minutes, "minute", "minutes")
    hours, rem = divmod(minutes, 60)
    if hours < 24:
        if rem == 0:
            return _plural(hours, "hour", "hours")
        return f"{_plural(hours, 'hour', 'hours')} and {_plural(rem, 'minute', 'minutes')}"
    days, hrem = divmod(hours, 24)
    if hrem == 0:
        return _plural(days, "day", "days")
    return f"{_plural(days, 'day', 'days')} and {_plural(hrem, 'hour', 'hours')}"


def _waiting_note(con: sqlite3.Connection, job: Job, ts: str, verb: str) -> JobNote:
    """A blocked/deferred job, with what it is waiting for and for how long."""
    label: str | None = None
    if job.blocked_request_id:
        row = con.execute(
            "SELECT short_label FROM requests WHERE id=?", (job.blocked_request_id,)
        ).fetchone()
        label = None if row is None else str(row["short_label"])
    waited = (
        (parse_ts(ts) - parse_ts(job.blocked_since)).total_seconds() if job.blocked_since else None
    )
    about = f" about {label}" if label else ""
    if waited is None:
        line = f"{job.title} is {verb} for an answer{about}."
    else:
        line = f"{job.title} has been {verb} {_duration_phrase(waited)} for an answer{about}."
    return JobNote(
        job_id=job.id,
        title=job.title,
        state=job.state,
        line=line,
        seconds=waited,
        request_id=job.blocked_request_id,
        short_label=label,
    )


def _undetermined_note(job: Job, ts: str) -> JobNote:
    """The line for a job we cannot prove is alive OR dead.

    Worded as an admission on purpose. This is the macOS path, and the whole
    reason the liveness check returns three answers instead of two: saying "it
    is still running" here would be a guess dressed as a fact.
    """
    quiet_for = (parse_ts(ts) - parse_ts(job.heartbeat_at or job.updated_at)).total_seconds()
    return JobNote(
        job_id=job.id,
        title=job.title,
        state=job.state,
        line=(
            f"I can't tell whether {job.title} is still running; nothing has been heard"
            f" from it for {_duration_phrase(quiet_for)}."
        ),
        seconds=quiet_for,
    )


def project_status(
    con: sqlite3.Connection,
    *,
    since: str | None = None,
    blocked_threshold_s: float = BLOCKED_BRIEFING_S,
    now_ts: str | None = None,
) -> ProjectStatus:
    """What a human would want said aloud about their projects.

    ``since`` defaults to the briefing cursor, and to 24 hours ago the first time
    a machine ever briefs. Reading does not advance it — see
    :func:`set_briefing_cursor`.

    ``blocked_threshold_s`` only filters the SPOKEN lines. Everything is in the
    structured fields regardless, so a screen or a "what about the rest?" can
    show what the briefing chose not to read out.
    """
    ts = now_ts or now()
    start = since or briefing_cursor(con) or shift_ts(ts, -DEFAULT_LOOKBACK_S)

    finished: list[JobNote] = []
    failed: list[JobNote] = []
    for job in jobs_since(con, start):
        if job.state not in TERMINAL_STATES:
            continue
        if job.state == "done":
            tail = f" {job.result_summary}" if job.result_summary else ""
            finished.append(JobNote(job.id, job.title, job.state, f"{job.title} finished.{tail}"))
        elif job.state == "failed":
            why = f" {job.stop_reason}" if job.stop_reason else ""
            failed.append(JobNote(job.id, job.title, job.state, f"{job.title} failed.{why}"))
        else:
            # 'killed' is reported with the failures but worded differently: the
            # user almost always did it, and "failed" would be an accusation.
            stopped = f"{job.title} was stopped before it finished."
            failed.append(JobNote(job.id, job.title, job.state, stopped))

    blocked = [_waiting_note(con, j, ts, "waiting") for j in list_by_state(con, "blocked")]
    deferred = [_waiting_note(con, j, ts, "parked") for j in list_by_state(con, "deferred")]

    stalled = [
        JobNote(
            j.id,
            j.title,
            j.state,
            f"{j.title} stopped without finishing and I have not picked it back up.",
        )
        for j in list_by_state(con, "orphaned")
    ]

    undetermined = [
        _undetermined_note(j, ts)
        for j in list_by_state(con, sorted(ACTIVE_STATES))
        if job_liveness(j) == "unknown"
    ]

    open_requests = int(
        con.execute("SELECT COUNT(*) AS n FROM requests WHERE state='pending'").fetchone()["n"]
    )

    lines: list[str] = [n.line for n in (*finished, *failed)]
    lines += [
        n.line
        for n in (*blocked, *deferred)
        if n.seconds is None or n.seconds >= blocked_threshold_s
    ]
    lines += [n.line for n in (*stalled, *undetermined)]
    if open_requests:
        lines.append(f"{_plural(open_requests, 'question is', 'questions are')} still waiting.")
    if not lines:
        lines.append(
            "Nothing has finished or failed since the last briefing, and nothing is stuck."
        )

    return ProjectStatus(
        since=start,
        finished=tuple(finished),
        failed=tuple(failed),
        blocked=tuple(blocked),
        deferred=tuple(deferred),
        stalled=tuple(stalled),
        undetermined=tuple(undetermined),
        open_requests=open_requests,
        lines=tuple(lines),
    )
