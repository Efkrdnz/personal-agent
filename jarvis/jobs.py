"""The session-agnostic job registry.

A job is a unit of work that OUTLIVES the process which created it: a Claude Code
build started by the voice app at 9am is still running at 11am when the voice app
has been restarted twice. Nothing about a job lives in memory. The row is the job.

Three things here are load-bearing.

LIVENESS WITHOUT LYING. "Is this job's process still there?" has three honest
answers, not two, and this module returns all three. A PID alone is worthless:
after a reboot PID 4217 is somebody else's process, and a reconciler that
believes it would leave a dead build in ``running`` forever. So a row records
``boot_id`` (the kernel's per-boot UUID), ``pid``, ``pgid`` and
``proc_start_ticks`` (field 22 of ``/proc/<pid>/stat``), and a process is called
*alive* only when all three still agree. Where that evidence cannot be gathered —
no ``/proc``, i.e. macOS — the answer is ``"unknown"`` and the caller is told so,
because "I cannot tell" spoken aloud is cheap and "it's still running" when it is
not costs forty minutes of a stalled build.

TRANSITIONS ARE ENFORCED. ``done`` is final. A late message from a process that
has not noticed it lost cannot resurrect a finished job, because the check runs
inside the same ``BEGIN IMMEDIATE`` as the write and SQLite serialises writers,
so two processes cannot both read ``running`` and both win.

``permission_mode='dontAsk'`` IS REFUSED LOUDLY. It denies ``AskUserQuestion``,
which is the entire plan-mode feature. The schema has a CHECK constraint, but a
CHECK gives the caller ``sqlite3.IntegrityError: CHECK constraint failed`` at
some later INSERT, which names neither the column nor the reason. We raise first,
in words.
"""

from __future__ import annotations

import errno
import os
import sqlite3
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

from jarvis.bus import publish
from jarvis.db import tx
from jarvis.ids import nid, now, parse_ts

__all__ = [
    "ACTIVE_STATES",
    "FORBIDDEN_PERMISSION_MODES",
    "JOB_STATES",
    "LEGAL_TRANSITIONS",
    "PROC",
    "TERMINAL_STATES",
    "ForbiddenPermissionMode",
    "IllegalTransition",
    "Job",
    "JobState",
    "JobStore",
    "Liveness",
    "UnknownJob",
    "attach_process",
    "blocked_longer_than",
    "boot_id",
    "claim_resume",
    "create_job",
    "get",
    "have_proc",
    "heartbeat",
    "is_alive",
    "job_liveness",
    "list_by_state",
    "mark_blocked",
    "pgid_of",
    "proc_start_ticks",
    "process_alive",
    "process_identity",
    "process_liveness",
    "running",
    "set_state",
    "shift_ts",
    "since",
    "unblock",
]

# ───────────────────────────── states ─────────────────────────────

JobState = Literal[
    "queued",
    "starting",
    "running",
    "blocked",
    "deferred",
    "parked",
    "finishing",
    "done",
    "failed",
    "killed",
    "orphaned",
]

JOB_STATES: frozenset[str] = frozenset(JobState.__args__)

#: Nothing leaves these. The whole point of enforcing transitions.
TERMINAL_STATES: frozenset[str] = frozenset(("done", "failed", "killed"))

#: States in which an OS process is supposed to exist. ``deferred`` is absent on
#: purpose: the runner exited by design, and its cc_session_id plus its requests
#: row are the entire resumable state. Orphaning a deferred job would be a lie.
ACTIVE_STATES: frozenset[str] = frozenset(("starting", "running", "blocked", "finishing"))

#: The legal state machine. Read it as "from -> what may follow".
#:
#: ``orphaned`` is deliberately NOT terminal: reconcile's whole job is to move it
#: back to ``starting`` with ``--resume``. ``parked`` is a human pause, so it
#: returns to ``queued``. Every non-terminal state may reach ``failed`` and
#: ``killed``, because the kill switch is allowed to interrupt anything.
LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset(("starting", "parked", "failed", "killed")),
    "starting": frozenset(
        (
            "running",
            "blocked",
            "deferred",
            "parked",
            "finishing",
            "done",
            "failed",
            "killed",
            "orphaned",
        )
    ),
    "running": frozenset(
        ("blocked", "deferred", "parked", "finishing", "done", "failed", "killed", "orphaned")
    ),
    "blocked": frozenset(
        ("running", "deferred", "parked", "finishing", "done", "failed", "killed", "orphaned")
    ),
    "deferred": frozenset(("queued", "starting", "parked", "failed", "killed", "orphaned")),
    "parked": frozenset(("queued", "starting", "running", "failed", "killed")),
    "finishing": frozenset(("done", "failed", "killed", "orphaned")),
    "done": frozenset(),
    "failed": frozenset(),
    "killed": frozenset(),
    "orphaned": frozenset(("queued", "starting", "failed", "killed")),
}

#: Which event kind a state change publishes. The bus is the activity log, so a
#: transition nobody can see afterwards did not happen as far as the user is
#: concerned. ``job.resumed`` is missing here on purpose — it belongs to
#: :func:`claim_resume`, which is a transition and not a state.
_EVENT_FOR_STATE: dict[str, str] = {
    "queued": "job.progress",
    "starting": "job.started",
    "running": "job.started",
    "blocked": "job.blocked",
    "deferred": "job.deferred",
    "parked": "job.parked",
    "finishing": "job.progress",
    "done": "job.finished",
    "failed": "job.failed",
    "killed": "job.killed",
    "orphaned": "job.orphaned",
}

#: ``dontAsk`` DENIES AskUserQuestion outright, which silently removes plan mode —
#: the feature this whole system exists to drive. Forbidden by a CHECK constraint
#: in the schema and by a readable exception here.
FORBIDDEN_PERMISSION_MODES: frozenset[str] = frozenset(("dontAsk",))

#: Columns ``set_state`` will write. A whitelist rather than an escape, because
#: the column name is interpolated into the UPDATE and a caller-supplied key must
#: never reach SQL text. ``state`` and ``updated_at`` are absent: they are the
#: function's own business.
_MUTABLE_FIELDS: frozenset[str] = frozenset(
    (
        "kind",
        "title",
        "host",
        "cwd",
        "repo",
        "branch",
        "cc_session_id",
        "model",
        "effort",
        "permission_mode",
        "prompt_text",
        "prompt_request_id",
        "pid",
        "pgid",
        "boot_id",
        "proc_start_ticks",
        "heartbeat_at",
        "stream_path",
        "stop_reason",
        "result_summary",
        "resume_policy",
        "resume_count",
        "blocked_request_id",
        "blocked_since",
        "kill_epoch",
    )
)

_COLUMNS: tuple[str, ...] = (
    "id",
    "kind",
    "title",
    "state",
    "created_at",
    "updated_at",
    "created_by",
    "host",
    "cwd",
    "repo",
    "branch",
    "cc_session_id",
    "model",
    "effort",
    "permission_mode",
    "prompt_text",
    "prompt_request_id",
    "pid",
    "pgid",
    "boot_id",
    "proc_start_ticks",
    "heartbeat_at",
    "stream_path",
    "stop_reason",
    "result_summary",
    "resume_policy",
    "resume_count",
    "blocked_request_id",
    "blocked_since",
    "kill_epoch",
)


# ───────────────────────────── errors ─────────────────────────────


class UnknownJob(KeyError):
    """No such job id. Distinct from "the job exists but refused"."""

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        super().__init__(f"no job {job_id!r}")


class IllegalTransition(ValueError):
    """A state change the machine forbids, or one another process already won."""

    def __init__(self, job_id: str, frm: str, to: str, *, expected: str | None = None) -> None:
        self.job_id, self.frm, self.to, self.expected = job_id, frm, to, expected
        if expected is not None and expected != frm:
            msg = (
                f"job {job_id}: expected state {expected!r} but it is {frm!r} — "
                f"another process moved it first, so {to!r} was not applied"
            )
        else:
            legal = ", ".join(sorted(LEGAL_TRANSITIONS.get(frm, frozenset()))) or "nothing"
            msg = f"job {job_id}: {frm!r} -> {to!r} is not legal; from {frm!r} only {legal}"
        super().__init__(msg)


class ForbiddenPermissionMode(ValueError):
    """``dontAsk``, named with its consequence rather than as a constraint name."""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        super().__init__(
            f"permission_mode={mode!r} is forbidden: it DENIES AskUserQuestion, which "
            f"silently removes plan mode — every question Jarvis exists to ask would be "
            f"auto-denied instead of reaching you. Use 'plan', 'default' or 'acceptEdits'."
        )


# ───────────────────────────── the row ─────────────────────────────


@dataclass(frozen=True, slots=True)
class Job:
    """One row of ``jobs``. Frozen: a Job is a snapshot, never a handle."""

    id: str
    kind: str
    title: str
    state: JobState
    created_at: str
    updated_at: str
    created_by: str
    host: str = "local"
    cwd: str | None = None
    repo: str | None = None
    branch: str | None = None
    cc_session_id: str | None = None
    model: str | None = None
    effort: str | None = None
    permission_mode: str | None = None
    prompt_text: str | None = None
    prompt_request_id: str | None = None
    pid: int | None = None
    pgid: int | None = None
    boot_id: str | None = None
    proc_start_ticks: int | None = None
    heartbeat_at: str | None = None
    stream_path: str | None = None
    stop_reason: str | None = None
    result_summary: str | None = None
    resume_policy: str = "auto"
    resume_count: int = 0
    blocked_request_id: str | None = None
    blocked_since: str | None = None
    kill_epoch: int = 0

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES


def to_job(row: sqlite3.Row) -> Job:
    return Job(**{c: row[c] for c in _COLUMNS})


# ───────────────────────────── liveness ─────────────────────────────

Liveness = Literal["alive", "dead", "unknown"]

#: Overridable for tests, which is the only honest way to exercise the
#: no-``/proc`` path from Linux CI.
PROC = Path("/proc")


def have_proc() -> bool:
    """Whether this kernel exposes ``/proc``. False on macOS, and that is fine."""
    try:
        return (PROC / "self").is_dir()
    except OSError:  # pragma: no cover - a sandbox that refuses even the stat
        return False


def boot_id() -> str | None:
    """The kernel's per-boot UUID, or None where it cannot be read.

    Paired with a pid this is what defeats PID reuse across a reboot: the pid may
    well exist again, but it cannot have been started in a boot that has ended.
    """
    try:
        return (PROC / "sys/kernel/random/boot_id").read_text().strip() or None
    except OSError:
        return None


def _parse_stat_starttime(stat: str) -> int | None:
    """Field 22 (``starttime``) of a ``/proc/<pid>/stat`` line.

    Parsed from the LAST ``)`` onward, never by splitting the whole line: field 2
    is ``comm``, which is the executable name in parentheses and may itself
    contain spaces AND parentheses (``(my )( app)`` is a legal process name).
    Naive ``stat.split()[21]`` reads a different field for such a process and
    silently returns a number that never matches — every job of that name would
    look dead. After the last ``)`` the fields are fixed-width in count: index 0
    is field 3 (``state``), so field 22 is index 19.
    """
    close = stat.rfind(")")
    if close < 0:
        return None
    rest = stat[close + 1 :].split()
    if len(rest) < 20:
        return None
    try:
        return int(rest[19])
    except ValueError:
        return None


def proc_start_ticks(pid: int) -> int | None:
    """Start time of ``pid`` in clock ticks since boot, or None if it is gone.

    None is ambiguous by itself — "no such process" and "no ``/proc`` on this
    platform" both land here — so callers must consult :func:`have_proc` before
    reading anything into it. :func:`process_liveness` does.
    """
    try:
        return _parse_stat_starttime((PROC / str(pid) / "stat").read_text())
    except (OSError, ValueError):
        return None


def pgid_of(pid: int) -> int | None:
    """Process group id, which is what the kill switch actually signals.

    ``os.getpgid`` rather than field 5 of ``stat``: it is one syscall, it works
    on every POSIX platform including the ones with no ``/proc``, and it cannot
    be fooled by a process name full of parentheses.
    """
    try:
        return os.getpgid(pid)
    except (OSError, ProcessLookupError):
        return None


def _pid_exists(pid: int) -> Liveness:
    """Signal-0 probe: "dead" only when the kernel says ESRCH.

    EPERM means the process exists and belongs to somebody else — existence is
    what we asked. This cannot distinguish a reused pid from the original, so it
    never returns "alive"; it is the fallback for platforms with no ``/proc``.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "dead"
    except PermissionError:
        return "unknown"
    except OSError as e:  # pragma: no cover - defensive
        return "dead" if e.errno == errno.ESRCH else "unknown"
    return "unknown"


def process_liveness(pid: int | None, rec_boot: str | None, rec_ticks: int | None) -> Liveness:
    """Three honest answers about a recorded process.

    ``"alive"``   — pid exists AND the row names this boot AND the process
                    started at the recorded instant. All three, or it is not
                    provably the process we started.
    ``"dead"``    — proven gone: no pid recorded, or a different boot, or
                    ``/proc`` has no such pid, or the start time disagrees (the
                    pid was reused).
    ``"unknown"`` — the evidence is not available. On a platform without
                    ``/proc`` we can only ask whether *some* process holds the
                    pid, which after a reboot is exactly the question that lies.
                    We refuse to guess; reconcile leaves such jobs alone and says
                    so out loud in the briefing.
    """
    if pid is None or pid <= 0:
        # No pid was ever recorded. For a job claiming to be running that is not
        # "maybe" — there is provably no process of ours to find.
        return "dead"

    current_boot = boot_id()
    if rec_boot and current_boot and rec_boot != current_boot:
        return "dead"

    if not have_proc():
        # macOS and friends. os.kill(pid, 0) can disprove existence but can never
        # establish identity, so the best truthful answers are "dead" or
        # "unknown" — never "alive".
        return _pid_exists(pid)

    ticks = proc_start_ticks(pid)
    if ticks is None:
        return "dead"
    if rec_ticks is not None and ticks != rec_ticks:
        return "dead"  # pid reused within this same boot
    if rec_ticks is None or not rec_boot or not current_boot:
        # The pid is held by *something*, but we never recorded enough to say it
        # is ours. Claiming "alive" here is the exact lie this module exists to
        # avoid. Start-ticks WITHOUT a boot id is the subtle case: ticks count
        # from boot, so a row written before a reboot can match a fresh process
        # whose start offset happens to coincide, and nothing in the row can
        # contradict it.
        return "unknown"
    return "alive"


def process_alive(pid: int | None, rec_boot: str | None, rec_ticks: int | None) -> bool:
    """True only when liveness is PROVEN.

    Note the asymmetry, because it is a footgun otherwise: ``not process_alive()``
    does NOT mean dead, it means "not proven alive". Anything that destroys state
    on the strength of a process being gone must test
    ``process_liveness(...) == "dead"`` instead.
    """
    return process_liveness(pid, rec_boot, rec_ticks) == "alive"


def job_liveness(job: Job) -> Liveness:
    """:func:`process_liveness` for the process recorded on a job row."""
    return process_liveness(job.pid, job.boot_id, job.proc_start_ticks)


def is_alive(job: Job) -> bool:
    """True only when the job's recorded process is PROVEN to still be there."""
    return job_liveness(job) == "alive"


def process_identity(pid: int | None = None) -> dict[str, Any]:
    """The four liveness columns for ``pid`` (default: this process).

    Returned as a dict of column names so it can be splatted straight into
    :func:`create_job` or :func:`set_state`. Read once, at attach time: reading
    them later would record whatever holds the pid *then*, which is the bug.
    """
    p = os.getpid() if pid is None else pid
    return {
        "pid": p,
        "pgid": pgid_of(p),
        "boot_id": boot_id(),
        "proc_start_ticks": proc_start_ticks(p) if have_proc() else None,
    }


# ───────────────────────────── writes ─────────────────────────────


def _check_permission_mode(mode: Any) -> None:
    if isinstance(mode, str) and mode in FORBIDDEN_PERMISSION_MODES:
        raise ForbiddenPermissionMode(mode)


def _check_fields(fields: dict[str, Any]) -> None:
    unknown = sorted(set(fields) - _MUTABLE_FIELDS)
    if unknown:
        raise ValueError(
            f"not settable on a job row: {', '.join(unknown)}"
            f" (settable: {', '.join(sorted(_MUTABLE_FIELDS))})"
        )
    _check_permission_mode(fields.get("permission_mode"))


def create_job(
    con: sqlite3.Connection,
    *,
    kind: str,
    title: str,
    created_by: str,
    job_id: str | None = None,
    state: JobState = "queued",
    actor: str | None = None,
    **fields: Any,
) -> Job:
    """Insert a job row and publish ``job.created``.

    ``title`` is SPOKEN ("the todo app build"), so it is required rather than
    defaulted to an id nobody can say out loud.

    For ``kind='claude_code'`` a ``cc_session_id`` is generated here unless the
    caller supplies one, because resume needs that UUID to exist before the first
    runner process does — it is ours, not the CLI's, and a job that cannot name
    its session cannot be resumed after a reboot.
    """
    if state not in JOB_STATES:
        raise ValueError(f"unknown job state {state!r}")
    _check_fields(fields)
    if state in ("blocked", "deferred") and not fields.get("blocked_request_id"):
        # Same rule as _transition, applied at the front door: a job born parked
        # on nothing can never be unparked, and creating one is how a caller
        # would sneak past the transition check.
        raise ValueError(f"a job created {state!r} needs a blocked_request_id")

    ts = now()
    row: dict[str, Any] = {
        "id": job_id or nid("job"),
        "kind": kind,
        "title": title,
        "state": state,
        "created_at": ts,
        "updated_at": ts,
        "created_by": created_by,
        "host": "local",
        "resume_policy": "auto",
        "resume_count": 0,
        "kill_epoch": 0,
    }
    row.update(fields)
    if kind == "claude_code" and not row.get("cc_session_id"):
        # A real UUID, not nid(): the CLI's --session-id wants UUID shape.
        row["cc_session_id"] = str(uuid.uuid4())

    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    with tx(con) as t:
        try:
            t.execute(f"INSERT INTO jobs ({cols}) VALUES ({marks})", tuple(row.values()))
        except sqlite3.IntegrityError as e:
            # The CHECK constraint is the backstop for a caller that bypassed
            # _check_fields (raw kwargs are validated, but a future column might
            # not be). Translate it back into the sentence that explains it.
            if "permission_mode" in str(e):
                raise ForbiddenPermissionMode(str(row.get("permission_mode"))) from e
            raise
        publish(
            t,
            "job.created",
            actor or created_by,
            {"title": title, "kind": kind, "state": state},
            job_id=str(row["id"]),
            idem_key=f"job:{row['id']}:created",
        )
        fresh = t.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
    return to_job(fresh)


#: ``(row) -> (column updates, whether to publish an event)``.
_Deriver = Callable[[sqlite3.Row], tuple[dict[str, Any], bool]]


def _transition(
    con: sqlite3.Connection,
    job_id: str,
    state: JobState,
    *,
    actor: str,
    derive: _Deriver,
    expect: JobState | None = None,
    event: str | None = None,
    reason: str | None = None,
) -> Job:
    """Read, check and write one transition inside a single BEGIN IMMEDIATE.

    The read and the write are in the same transaction on purpose. SQLite allows
    exactly one writer, so no second process can read ``running`` after we have
    committed ``done`` and then also write — which is the entire reason "a done
    job cannot go back to running" holds across processes and not just within one.
    """
    if state not in JOB_STATES:
        raise ValueError(f"unknown job state {state!r}")

    with tx(con) as t:
        row = t.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise UnknownJob(job_id)
        old = str(row["state"])
        if expect is not None and old != expect:
            raise IllegalTransition(job_id, old, state, expected=expect)
        # .get(): a row written by something that is not this module (a raw
        # INSERT, a future migration) must not make reconcile — which runs in
        # EVERY process — die with a KeyError. An unknown state has no legal
        # successor, so it refuses in the same words as any other bad move.
        if old != state and state not in LEGAL_TRANSITIONS.get(old, frozenset()):
            raise IllegalTransition(job_id, old, state)

        updates, emit = derive(row)
        _check_fields(updates)

        blocking = state in ("blocked", "deferred")
        req = updates.get("blocked_request_id", row["blocked_request_id"])
        if blocking and not req:
            # A job parked on nothing is a hang with no gate: nobody can answer
            # it, nothing will ever unblock it, and the briefing cannot name what
            # it is waiting for.
            raise ValueError(f"job {job_id}: {state!r} needs a blocked_request_id")
        if state in TERMINAL_STATES:
            # A finished job is not waiting for anybody. Leaving these set would
            # put a dead job in the briefing's "still blocked" list forever.
            updates.setdefault("blocked_request_id", None)
            updates.setdefault("blocked_since", None)

        if updates or emit or old != state:
            # Keys come from _MUTABLE_FIELDS, checked above; values stay bound.
            assign = "".join(f", {k}=?" for k in updates)
            t.execute(
                f"UPDATE jobs SET state=?, updated_at=?{assign} WHERE id=?",
                (state, now(), *updates.values(), job_id),
            )
        # else: nothing changed, so updated_at must not move. It means "the state
        # last changed" and staleness is measured with it — a runner that
        # re-announces an unchanged block every thirty seconds would otherwise
        # keep a wedged job looking freshly active forever.
        if emit:
            payload: dict[str, Any] = {"from": old, "to": state, "title": row["title"]}
            if reason:
                payload["reason"] = reason
            publish(
                t,
                event or _EVENT_FOR_STATE[state],
                actor,
                payload,
                job_id=job_id,
                # The event is written in the SAME atom as the row, so there is
                # no crash window for a natural key to dedupe. It is unique per
                # real transition instead: deduping would drop the second of two
                # genuine blocks and quietly hide a stall from the activity log.
                idem_key=f"job:{job_id}:state:{state}:{row['resume_count']}:{nid('t')}",
            )
        fresh = t.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return to_job(fresh)


def set_state(
    con: sqlite3.Connection,
    job_id: str,
    state: JobState,
    *,
    actor: str = "system",
    expect: JobState | None = None,
    reason: str | None = None,
    **fields: Any,
) -> Job:
    """Move a job to ``state``, enforcing the legal transitions.

    Raises :class:`IllegalTransition` for anything the machine forbids — notably
    every path out of ``done``, ``failed`` and ``killed``.

    A transition to the state the job is already in is allowed and publishes
    nothing: :func:`jarvis.reconcile.reconcile` runs in every process, so "no
    change" must be free and silent rather than an exception or a log entry.

    ``expect`` makes the write a compare-and-swap. Pass it when losing a race to
    another process is expected and meaningful; the loser gets
    :class:`IllegalTransition` naming who moved it.
    """
    _check_fields(fields)

    def derive(row: sqlite3.Row) -> tuple[dict[str, Any], bool]:
        return dict(fields), str(row["state"]) != state

    return _transition(con, job_id, state, actor=actor, derive=derive, expect=expect, reason=reason)


def attach_process(
    con: sqlite3.Connection,
    job_id: str,
    *,
    pid: int | None = None,
    state: JobState | None = "running",
    actor: str = "system",
    **fields: Any,
) -> Job:
    """Record who is running this job, and (by default) mark it ``running``.

    Called by the runner itself, in the runner's own process, which is the only
    process that can honestly answer "which pid". ``state=None`` records the
    identity without a transition, for a runner that re-execs.
    """
    ident = process_identity(pid)
    ident.update(fields)
    if state is None:
        _check_fields(ident)
        assign = ", ".join(f"{k}=?" for k in ident)
        with tx(con) as t:
            cur = t.execute(
                f"UPDATE jobs SET {assign}, updated_at=? WHERE id=?",
                (*ident.values(), now(), job_id),
            )
            if cur.rowcount == 0:
                raise UnknownJob(job_id)
            fresh = t.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return to_job(fresh)
    return set_state(con, job_id, state, actor=actor, **ident)


def heartbeat(con: sqlite3.Connection, job_id: str) -> bool:
    """Stamp ``heartbeat_at``. False if the job is gone or already terminal.

    Deliberately does NOT touch ``updated_at``: that column means "the state last
    changed", and it is what tells the briefing how long something has been
    stuck. A 30-second ping is not a change, and letting it look like one would
    make every stalled job look freshly started.

    Refusing to heartbeat a terminal job matters because a runner that has not
    noticed it was killed would otherwise keep a finished row looking warm.
    """
    # Built from TERMINAL_STATES rather than spelled out, so adding a terminal
    # state cannot leave this one statement quietly warming it.
    terminal = sorted(TERMINAL_STATES)
    marks = ",".join("?" for _ in terminal)
    cur = con.execute(
        f"UPDATE jobs SET heartbeat_at=? WHERE id=? AND state NOT IN ({marks})",
        (now(), job_id, *terminal),
    )
    return cur.rowcount == 1


def mark_blocked(
    con: sqlite3.Connection,
    job_id: str,
    request_id: str,
    *,
    actor: str = "system",
    state: JobState = "blocked",
) -> Job:
    """Park a job on a requests row.

    ``blocked_since`` measures how long THE QUESTION has been unanswered, so it
    is keyed on the request id alone and survives both a re-announcement and a
    change of state. A runner re-announcing its block (a retry, a reconnect, a
    resumed driver re-firing the identical question — S1: ``tool_use_id`` is
    stable across defer and resume) must not restart the clock, and neither must
    the ``blocked`` -> ``deferred`` handoff when the user turns out to be away:
    that path is the one where the wait gets LONG, and the ">15 minutes means say
    it in the morning briefing" rule is the only backstop against a question
    nobody ever hears. A resetting timer would silence it forever.
    """
    if not request_id:
        raise ValueError("mark_blocked needs a request id; blocking on nothing is a hang")

    def derive(row: sqlite3.Row) -> tuple[dict[str, Any], bool]:
        same_request = row["blocked_request_id"] == request_id
        updates: dict[str, Any] = {}
        if not same_request:
            updates["blocked_request_id"] = request_id
        if not same_request or row["blocked_since"] is None:
            updates["blocked_since"] = now()
        # A genuine move (blocked -> deferred) is worth logging even though the
        # clock keeps running; only re-announcing the identical block is silent.
        return updates, not same_request or row["state"] != state

    return _transition(con, job_id, state, actor=actor, derive=derive)


def unblock(
    con: sqlite3.Connection,
    job_id: str,
    *,
    state: JobState = "running",
    actor: str = "system",
) -> Job:
    """Clear the block and move on — normally back to ``running``."""

    def derive(row: sqlite3.Row) -> tuple[dict[str, Any], bool]:
        return {"blocked_request_id": None, "blocked_since": None}, True

    return _transition(con, job_id, state, actor=actor, derive=derive)


def claim_resume(
    con: sqlite3.Connection,
    job_id: str,
    *,
    actor: str,
    expect_state: JobState,
    expect_resume_count: int,
    reason: str = "reconcile",
) -> Job | None:
    """Atomically claim the right to respawn this job. None if someone else won.

    ``reconcile()`` runs at the start of EVERY process, so two of them can see
    the same orphan in the same second. Without a claim both would spawn a runner
    and the job would exist twice. The compare-and-swap is on ``state`` AND
    ``resume_count``, and it also CLEARS the process identity — the old pid must
    not be able to make the next reconcile think the new runner is already alive.

    Losing is normal, not exceptional, so the loser gets None rather than a
    traceback. The winner, and only the winner, spawns.
    """
    ts = now()
    with tx(con) as t:
        row = t.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise UnknownJob(job_id)
        if row["state"] != expect_state or row["resume_count"] != expect_resume_count:
            return None
        if "starting" not in LEGAL_TRANSITIONS[str(row["state"])]:
            raise IllegalTransition(job_id, str(row["state"]), "starting")
        # stop_reason is NOT overwritten: it holds why the job STOPPED, which is
        # what the briefing reads out, and the reason it is being picked up again
        # belongs on the job.resumed event instead.
        t.execute(
            "UPDATE jobs SET state='starting', updated_at=?, resume_count=resume_count+1,"
            " pid=NULL, pgid=NULL, boot_id=NULL, proc_start_ticks=NULL, heartbeat_at=?"
            " WHERE id=? AND state=? AND resume_count=?",
            (ts, ts, job_id, expect_state, expect_resume_count),
        )
        publish(
            t,
            "job.resumed",
            actor,
            {
                "from": expect_state,
                "title": row["title"],
                "resume_count": expect_resume_count + 1,
                "reason": reason,
            },
            job_id=job_id,
            idem_key=f"job:{job_id}:resumed:{expect_resume_count + 1}",
        )
        fresh = t.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return to_job(fresh)


# ───────────────────────────── reads ─────────────────────────────


def get(con: sqlite3.Connection, job_id: str) -> Job | None:
    row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return None if row is None else to_job(row)


def list_by_state(con: sqlite3.Connection, states: str | Iterable[str]) -> list[Job]:
    """Jobs in any of ``states``, oldest change first."""
    wanted = [states] if isinstance(states, str) else list(states)
    if not wanted:
        return []
    unknown = sorted(set(wanted) - JOB_STATES)
    if unknown:
        raise ValueError(f"unknown job state(s): {', '.join(unknown)}")
    marks = ",".join("?" for _ in wanted)
    rows = con.execute(
        f"SELECT * FROM jobs WHERE state IN ({marks}) ORDER BY updated_at, rowid", tuple(wanted)
    ).fetchall()
    return [to_job(r) for r in rows]


def running(con: sqlite3.Connection) -> list[Job]:
    """Everything that believes it is underway — including blocked and starting.

    "Running" spoken aloud means "not finished and not waiting for me to start
    it", which is what a person asking "what's running?" wants.
    """
    return list_by_state(con, sorted(ACTIVE_STATES))


def blocked_longer_than(con: sqlite3.Connection, seconds: int) -> list[Job]:
    """Jobs stuck on a question for more than ``seconds``. Feeds the briefing.

    Compared as strings: :func:`jarvis.ids.now` is fixed-width UTC, so
    lexicographic order IS chronological order.
    """
    cutoff = shift_ts(now(), -float(seconds))
    rows = con.execute(
        "SELECT * FROM jobs WHERE state IN ('blocked','deferred')"
        " AND blocked_since IS NOT NULL AND blocked_since <= ? ORDER BY blocked_since",
        (cutoff,),
    ).fetchall()
    return [to_job(r) for r in rows]


def since(con: sqlite3.Connection, ts: str) -> list[Job]:
    """Jobs whose state changed at or after ``ts``."""
    rows = con.execute(
        "SELECT * FROM jobs WHERE updated_at >= ? ORDER BY updated_at, rowid", (ts,)
    ).fetchall()
    return [to_job(r) for r in rows]


def shift_ts(ts: str, seconds: float) -> str:
    """``ts`` moved by ``seconds``, in the exact shape :func:`jarvis.ids.now` writes.

    Public because every "how long has this been like that" question in the
    system compares these strings lexicographically, and a caller that formats
    its own cutoff a microsecond wider silently changes the comparison.
    """
    t = parse_ts(ts) + timedelta(seconds=seconds)
    return f"{t.strftime('%Y-%m-%dT%H:%M:%S')}.{t.microsecond // 1000:03d}Z"


@dataclass(frozen=True, slots=True)
class JobStore:
    """The architecture's ``JobStore``, as a thin connection-bound facade.

    The functions above are the real API — every one takes the connection first,
    because a process may hold several and a store that owned one would be the
    global singleton this system refuses. This exists so ``ToolCtx.jobs`` has
    something to be, and holds nothing but the connection its caller opened.
    """

    con: sqlite3.Connection

    def create(self, **kw: Any) -> Job:
        return create_job(self.con, **kw)

    def get(self, job_id: str) -> Job | None:
        return get(self.con, job_id)

    def running(self) -> list[Job]:
        return running(self.con)

    def blocked_longer_than(self, seconds: int) -> list[Job]:
        return blocked_longer_than(self.con, seconds)

    def since(self, ts: str) -> list[Job]:
        return since(self.con, ts)

    def set_state(self, job_id: str, state: JobState, **fields: Any) -> Job:
        return set_state(self.con, job_id, state, **fields)
