"""The kill switch: three triggers, ONE path, and it goes through the database.

THREE TRIGGERS. A spoken phrase heard by jarvis-voice, DTMF ``*9`` heard by
jarvis-phone, and ``/kill`` typed into Telegram. They are three different
processes and none of them is the parent of the Claude Code runner they must
stop. So there is exactly one path and it is not an in-process reference: a row
in ``commands``, a bump of ``kill_epoch``, a best-effort poke, and — separately,
by whoever actually owns the process group — a signal.

WHY A ROW AND NOT A CALL. The DTMF handler lives in jarvis-phone. The runner it
must kill was spawned detached by jarvis-dispatch, possibly before jarvis-phone
existed. There is no object either of them could hold. A row in one SQLite file
is the only thing all three can see, it survives the death of the process that
wrote it, and it is still there when the wedged runner finally wakes up.

WHY AN EPOCH AS WELL AS A COMMAND, since they look redundant. They cover
different windows and neither covers both:

``commands``     the LIVE path. Healthy processes see it within a poke (~1ms) or
                 a poll, act, and ack. It EXPIRES, because a process that wakes
                 up an hour later must not reap a build the user started since.
``kill_epoch``   the DURABLE path, a monotonic counter. Everything started
                 before the current epoch is doomed, forever, with no expiry and
                 no delivery requirement. This is what closes the resurrection
                 race: a runner that was mid-spawn when the kill fired, or one
                 respawned by a reconcile that had not noticed, re-reads the
                 epoch before doing work and refuses.

Every process therefore does two things: it stamps its job with
:func:`current_epoch` at spawn, and it calls :func:`assert_epoch` before
starting work and after every resume.

WHAT THIS MODULE DOES NOT DO. It does not own the process group of anything. The
architecture's sketch has ``stop_everything`` calling ``os.killpg`` inline; here
the DB write and the signalling are two functions, because the DTMF handler has
no business signalling process groups it did not create and because network-ish
I/O inside a write transaction is the house rule's exact prohibition.
:func:`stop_everything` writes and pokes; :func:`reap_all` — called in
jarvis-dispatch, which IS the parent — signals and acks. The test proves the
seam: one connection issues, another connection reaps a real process it never
had a handle to.
"""

from __future__ import annotations

import json
import os
import signal as signalmod
import sqlite3
import time
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import Any, Literal

from jarvis.bus import poke_attached, publish
from jarvis.db import tx
from jarvis.ids import canon, nid, now
from jarvis.jobs import (
    PROC,
    TERMINAL_STATES,
    IllegalTransition,
    Job,
    UnknownJob,
    get,
    process_liveness,
    set_state,
    shift_ts,
)

__all__ = [
    "ACTIVE_JOB_STATES",
    "DEFAULT_TTL_S",
    "GRACE_S",
    "KILL_TTL_S",
    "KILL_VERBS",
    "TARGET_KINDS",
    "VERBS",
    "Ack",
    "Command",
    "CommandState",
    "KillEpochAdvanced",
    "TargetKind",
    "TerminateOutcome",
    "UnknownCommand",
    "Verb",
    "ack_command",
    "acked_by",
    "acks_for",
    "assert_epoch",
    "bump_epoch",
    "cancel_command",
    "claim_command",
    "current_epoch",
    "doomed_jobs",
    "epoch_is_current",
    "expire_commands",
    "finish_command",
    "get_command",
    "issue_command",
    "pending_commands",
    "reap_all",
    "reap_job",
    "stamp_job_epoch",
    "stop_everything",
    "terminate",
]

# ───────────────────────────── the vocabulary ─────────────────────────────

Verb = Literal["stop_all", "kill", "interrupt", "pause", "resume", "hangup", "nav", "reload"]
TargetKind = Literal["all", "job", "channel", "process"]
CommandState = Literal["pending", "done", "expired", "cancelled"]
TerminateOutcome = Literal["no_pid", "stale", "gone", "term", "kill", "denied"]

VERBS: frozenset[str] = frozenset(Verb.__args__)
TARGET_KINDS: frozenset[str] = frozenset(TargetKind.__args__)

#: Verbs that stop work rather than steer it. Only these bump the epoch.
KILL_VERBS: frozenset[str] = frozenset(("stop_all", "kill"))

#: How long a command stays honourable. Five minutes, and the number is a
#: judgement the doc does not make: long enough that a busy runner polling every
#: few seconds cannot miss it, short enough that a process resuming from a
#: suspended laptop does not execute a kill the user issued before lunch. The
#: epoch is what covers the long window, so this one does not have to.
DEFAULT_TTL_S = 300
KILL_TTL_S = 300

#: SIGTERM, then SIGKILL this long after. Two seconds is from the architecture.
GRACE_S = 2.0

#: Job states a stop_all must act on. ``deferred``/``queued``/``parked`` have no
#: process to signal but MUST still be marked killed: otherwise the next
#: reconcile helpfully resurrects the very job the user just stopped.
ACTIVE_JOB_STATES: tuple[str, ...] = (
    "queued",
    "starting",
    "running",
    "blocked",
    "deferred",
    "parked",
    "finishing",
    "orphaned",
)


class UnknownCommand(KeyError):
    def __init__(self, command_id: str) -> None:
        self.command_id = command_id
        super().__init__(f"no command {command_id!r}")


class KillEpochAdvanced(RuntimeError):
    """The kill switch fired after this process was spawned. Do not start work.

    Carries both numbers because the only useful thing to say out loud is "you
    stopped everything after this job started", and that needs the pair.
    """

    def __init__(self, spawned_with: int, current: int) -> None:
        self.spawned_with = spawned_with
        self.current = current
        super().__init__(
            f"kill epoch advanced from {spawned_with} to {current}; everything older is doomed"
        )


@dataclass(frozen=True, slots=True)
class Command:
    id: str
    ts: str
    verb: str
    target_kind: str
    target_id: str | None
    args: dict[str, Any]
    issued_by: str
    expires_at: str
    state: str

    def live(self, now_ts: str) -> bool:
        return self.state == "pending" and self.expires_at > now_ts


@dataclass(frozen=True, slots=True)
class Ack:
    command_id: str
    actor: str
    ts: str
    result: str | None


def _to_command(row: sqlite3.Row) -> Command:
    raw = row["args"]
    return Command(
        id=str(row["id"]),
        ts=str(row["ts"]),
        verb=str(row["verb"]),
        target_kind=str(row["target_kind"]),
        target_id=row["target_id"],
        args=json.loads(raw) if raw else {},
        issued_by=str(row["issued_by"]),
        expires_at=str(row["expires_at"]),
        state=str(row["state"]),
    )


# ───────────────────────────── the epoch ─────────────────────────────


def current_epoch(con: sqlite3.Connection) -> int:
    """The current epoch. ZERO when nobody has ever killed anything.

    The migration creates the table but inserts no row, so "no row" and "epoch 0"
    must mean the same thing — and they do, because ``jobs.kill_epoch`` also
    defaults to 0. A fresh system is therefore self-consistent without a seed row
    that a second process could race to insert.
    """
    row = con.execute("SELECT epoch FROM kill_epoch WHERE id=1").fetchone()
    return 0 if row is None else int(row["epoch"])


def _bump(t: sqlite3.Connection) -> int:
    """Increment and return, in ONE statement.

    Read-then-write would lose a bump when two triggers fire together — the user
    mashing ``*9`` while also saying the phrase — and a lost bump means a job
    that should be doomed is not. The upsert makes the increment atomic even
    against another connection, and RETURNING hands back the value this
    transaction actually wrote rather than whatever a later SELECT sees.
    """
    row = t.execute(
        """INSERT INTO kill_epoch (id, epoch) VALUES (1, 1)
             ON CONFLICT(id) DO UPDATE SET epoch = kill_epoch.epoch + 1
           RETURNING epoch""",
    ).fetchone()
    return int(row["epoch"])


def bump_epoch(con: sqlite3.Connection, *, actor: str = "system", reason: str = "") -> int:
    """Advance the epoch and say so on the bus. Returns the new value."""
    with tx(con) as t:
        epoch = _bump(t)
        publish(
            t,
            "command.issued",
            actor,
            {"verb": "epoch_bump", "epoch": epoch, "reason": reason},
            # The epoch is monotonic, so it is its own natural key: two processes
            # that somehow describe the same bump publish it once.
            idem_key=f"kill:epoch:{epoch}",
        )
    return epoch


def epoch_is_current(con: sqlite3.Connection, epoch: int) -> bool:
    return current_epoch(con) == epoch


def assert_epoch(con: sqlite3.Connection, epoch: int) -> None:
    """Called before starting work and after every resume. Raises, loudly.

    Deliberately not a boolean: the failure mode this closes is a process that
    checked and carried on anyway, so the check that is easiest to write is the
    one that stops the process.
    """
    actual = current_epoch(con)
    if actual != epoch:
        raise KillEpochAdvanced(epoch, actual)


def stamp_job_epoch(con: sqlite3.Connection, job_id: str, *, actor: str = "dispatch") -> int:
    """Stamp a job with the epoch it was spawned under. Returns that epoch.

    ``jobs.create_job`` defaults ``kill_epoch`` to 0, which is correct on a
    virgin system and WRONG after the first kill — a job created afterwards would
    be born doomed. Whoever spawns a runner calls this (or passes
    ``kill_epoch=current_epoch(con)`` to ``create_job``); there is no way for
    this module to do it for them without owning job creation.
    """
    job = get(con, job_id)
    if job is None:
        raise UnknownJob(job_id)
    epoch = current_epoch(con)
    # Same state in, same state out: jobs.set_state treats that as a no-op
    # transition and publishes nothing, which is what we want for a stamp.
    set_state(con, job_id, job.state, actor=actor, kill_epoch=epoch)  # type: ignore[arg-type]
    return epoch


def doomed_jobs(con: sqlite3.Connection, *, epoch: int | None = None) -> list[Job]:
    """Non-terminal jobs stamped with an older epoch. These must not run.

    This is the query a reconcile runs at startup, and it is the reason the epoch
    needs no delivery: a job can be doomed by a kill that nobody ever told it
    about, including one issued while the machine was off.
    """
    current = current_epoch(con) if epoch is None else epoch
    rows = con.execute(
        f"""SELECT id FROM jobs
             WHERE kill_epoch < ? AND state NOT IN ({",".join("?" * len(TERMINAL_STATES))})
             ORDER BY updated_at""",
        (current, *sorted(TERMINAL_STATES)),
    ).fetchall()
    jobs = (get(con, str(r["id"])) for r in rows)
    return [j for j in jobs if j is not None]


# ───────────────────────────── commands ─────────────────────────────


def issue_command(
    con: sqlite3.Connection,
    *,
    verb: Verb,
    issued_by: str,
    target_kind: TargetKind = "all",
    target_id: str | None = None,
    args: dict[str, Any] | None = None,
    ttl_s: int = DEFAULT_TTL_S,
    now_ts: str | None = None,
    poke: bool = True,
) -> Command:
    """Write one command, publish it, then poke. Returns the row.

    THE ORDER MATTERS AND IT IS NOT THE OBVIOUS ONE. The poke happens AFTER the
    transaction commits, never inside it. A peer woken mid-transaction reads the
    pre-commit snapshot, finds nothing, goes back to sleep, and then the poke it
    already consumed is gone — a wakeup that provably arrives too early is worse
    than no wakeup at all, because the 250ms poll floor would have caught it.
    """
    if verb not in VERBS:
        raise ValueError(f"unknown verb {verb!r}; expected one of {sorted(VERBS)}")
    if target_kind not in TARGET_KINDS:
        raise ValueError(f"unknown target kind {target_kind!r}")
    if not issued_by:
        raise ValueError("issued_by must name the trigger that fired this")
    if ttl_s <= 0:
        raise ValueError(f"ttl_s must be positive, got {ttl_s}")

    ts = now_ts or now()
    cmd = Command(
        id=nid("cmd"),
        ts=ts,
        verb=verb,
        target_kind=target_kind,
        target_id=target_id,
        args=dict(args or {}),
        issued_by=issued_by,
        expires_at=shift_ts(ts, ttl_s),
        state="pending",
    )
    with tx(con) as t:
        _insert_command(t, cmd)
    if poke:
        poke_attached(con)
    return cmd


def _insert_command(t: sqlite3.Connection, cmd: Command) -> None:
    t.execute(
        """INSERT INTO commands
             (id, ts, verb, target_kind, target_id, args, issued_by, expires_at, state)
             VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            cmd.id,
            cmd.ts,
            cmd.verb,
            cmd.target_kind,
            cmd.target_id,
            canon(cmd.args),
            cmd.issued_by,
            cmd.expires_at,
            cmd.state,
        ),
    )
    publish(
        t,
        "command.issued",
        cmd.issued_by,
        {"verb": cmd.verb, "target_kind": cmd.target_kind, "target_id": cmd.target_id, **cmd.args},
        job_id=cmd.target_id if cmd.target_kind == "job" else None,
        idem_key=f"cmd:{cmd.id}:issued",
    )


def get_command(con: sqlite3.Connection, command_id: str) -> Command | None:
    row = con.execute("SELECT * FROM commands WHERE id=?", (command_id,)).fetchone()
    return None if row is None else _to_command(row)


def pending_commands(
    con: sqlite3.Connection,
    *,
    actor: str,
    verbs: Iterable[str] | None = None,
    now_ts: str | None = None,
    limit: int = 100,
) -> list[Command]:
    """Commands THIS actor has not acked yet, still pending, not yet expired.

    Visibility is driven by ``command_acks``, not by ``commands.state``, and that
    is the whole broadcast mechanism: a stop_all is seen by every process that
    has not acked it, independently, with no fan-out table and no per-process
    cursor to get wrong.
    """
    ts = now_ts or now()
    sql = [
        "SELECT c.* FROM commands c",
        " LEFT JOIN command_acks a ON a.command_id = c.id AND a.actor = ?",
        " WHERE c.state='pending' AND c.expires_at > ? AND a.command_id IS NULL",
    ]
    params: list[Any] = [actor, ts]
    wanted = sorted(set(verbs)) if verbs is not None else None
    if wanted:
        sql.append(f" AND c.verb IN ({','.join('?' * len(wanted))})")
        params.extend(wanted)
    sql.append(" ORDER BY c.ts, c.id LIMIT ?")
    params.append(limit)
    rows = con.execute("".join(sql), tuple(params)).fetchall()
    return [_to_command(r) for r in rows]


def claim_command(
    con: sqlite3.Connection,
    command_id: str,
    actor: str,
    *,
    now_ts: str | None = None,
) -> bool:
    """Take responsibility for executing a command. False means somebody else did.

    The primary key is ``(command_id, actor)``, and that single fact gives two
    different delivery semantics from one table, chosen by what the caller passes
    as ``actor``:

    * a PER-PROCESS actor (``"runner:job_ab12"``) makes the claim a broadcast
      acknowledgement — every process claims its own slot and all of them act;
    * a SHARED actor (``"reaper"``) makes it MUTUALLY EXCLUSIVE — the first
      process to claim wins and the rest get False.

    A stop_all wants the first. Reaping process groups wants the second, because
    two reapers racing means SIGKILL arriving before SIGTERM's grace has run.
    """
    ts = now_ts or now()
    with tx(con) as t:
        row = t.execute("SELECT * FROM commands WHERE id=?", (command_id,)).fetchone()
        if row is None:
            raise UnknownCommand(command_id)
        cmd = _to_command(row)
        if not cmd.live(ts):
            return False
        cur = t.execute(
            "INSERT OR IGNORE INTO command_acks (command_id, actor, ts, result)"
            " VALUES (?,?,?,NULL)",
            (command_id, actor, ts),
        )
        return cur.rowcount == 1


def ack_command(
    con: sqlite3.Connection,
    command_id: str,
    actor: str,
    *,
    result: str | None = None,
    now_ts: str | None = None,
) -> bool:
    """Record what this actor DID. False means its result was already recorded.

    The first result sticks. An ack is a statement about something that already
    happened, so a second one is either a retry (harmless, and dropping it keeps
    the log honest) or a bug (and overwriting would hide it).
    """
    ts = now_ts or now()
    try:
        with tx(con) as t:
            row = t.execute(
                """INSERT INTO command_acks (command_id, actor, ts, result) VALUES (?,?,?,?)
                     ON CONFLICT(command_id, actor) DO UPDATE SET ts=excluded.ts,
                       result=excluded.result
                     WHERE command_acks.result IS NULL
                   RETURNING actor""",
                (command_id, actor, ts, result),
            ).fetchone()
            if row is None:
                return False
            publish(
                t,
                "command.acked",
                actor,
                {"command_id": command_id, "result": result},
                idem_key=f"cmd:{command_id}:ack:{actor}",
            )
    except sqlite3.IntegrityError as e:
        # foreign_keys=ON, so this is "no such command" and not a lost race.
        raise UnknownCommand(command_id) from e
    return True


def acks_for(con: sqlite3.Connection, command_id: str) -> list[Ack]:
    rows = con.execute(
        "SELECT * FROM command_acks WHERE command_id=? ORDER BY ts, actor", (command_id,)
    ).fetchall()
    return [
        Ack(
            command_id=str(r["command_id"]),
            actor=str(r["actor"]),
            ts=str(r["ts"]),
            result=r["result"],
        )
        for r in rows
    ]


def acked_by(con: sqlite3.Connection, command_id: str, actor: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM command_acks WHERE command_id=? AND actor=?", (command_id, actor)
    ).fetchone()
    return row is not None


def finish_command(
    con: sqlite3.Connection,
    command_id: str,
    *,
    state: CommandState = "done",
    now_ts: str | None = None,
) -> bool:
    """Close a command so no later poller picks it up. CAS on ``pending``."""
    if state == "pending":
        raise ValueError("finish_command cannot set state back to pending")
    cur = con.execute(
        "UPDATE commands SET state=? WHERE id=? AND state='pending'", (state, command_id)
    )
    return cur.rowcount == 1


def cancel_command(con: sqlite3.Connection, command_id: str) -> bool:
    return finish_command(con, command_id, state="cancelled")


def expire_commands(con: sqlite3.Connection, *, now_ts: str | None = None) -> int:
    """Mark every overdue pending command expired. Idempotent, safe to race."""
    ts = now_ts or now()
    cur = con.execute(
        "UPDATE commands SET state='expired' WHERE state='pending' AND expires_at <= ?", (ts,)
    )
    return int(cur.rowcount)


# ───────────────────────────── the one path ─────────────────────────────


def stop_everything(
    con: sqlite3.Connection,
    issued_by: str,
    reason: str = "",
    *,
    now_ts: str | None = None,
    ttl_s: int = KILL_TTL_S,
) -> str:
    """STOP. Returns the stop_all command id.

    ``issued_by`` names the trigger — ``"voice"``, ``"dtmf"``, ``"telegram"`` —
    and it is the ONLY thing that differs between the three. Same row, same
    epoch bump, same event, same hangups. That is what "three triggers, one path"
    has to mean if it is to be worth saying: not three code paths that agree, one
    code path with three callers.

    Everything is in ONE transaction, so a crash between the epoch bump and the
    command row cannot leave a system that is half-killed. The poke is outside
    it, deliberately — see :func:`issue_command`.
    """
    if not issued_by:
        raise ValueError("issued_by must name the trigger (voice|dtmf|telegram|hotkey|cli)")
    ts = now_ts or now()
    stop = Command(
        id=nid("cmd"),
        ts=ts,
        verb="stop_all",
        target_kind="all",
        target_id=None,
        args={"reason": reason, "origin": issued_by},
        issued_by=issued_by,
        expires_at=shift_ts(ts, ttl_s),
        state="pending",
    )
    with tx(con) as t:
        epoch = _bump(t)
        # slots=True means no __dict__ to splat; replace() is the only spelling.
        stop = replace(stop, args={**stop.args, "epoch": epoch})
        _insert_command(t, stop)
        # Step 4 of the architecture's sketch: a live call is not stopped by
        # killing a build, and a user who just said "stop" while on the phone
        # means the phone too.
        live_phones = t.execute(
            "SELECT id FROM channels WHERE kind='phone' AND state='attached'"
        ).fetchall()
        for row in live_phones:
            _insert_command(
                t,
                Command(
                    id=nid("cmd"),
                    ts=ts,
                    verb="hangup",
                    target_kind="channel",
                    target_id=str(row["id"]),
                    args={
                        "reason": reason,
                        "origin": issued_by,
                        "epoch": epoch,
                        "stop_command_id": stop.id,
                    },
                    issued_by=issued_by,
                    expires_at=shift_ts(ts, ttl_s),
                    state="pending",
                ),
            )
    poke_attached(con)
    return stop.id


# ───────────────────────────── the signal half ─────────────────────────────


def _zombie(pid: int) -> bool:
    """A dead-but-unreaped child still answers signal 0. It is not running.

    This only ever matters when the caller happens to be the process's parent,
    which in production it is not (runners are spawned detached by systemd-run).
    It matters enormously in a test, where the subprocess IS a child: without
    this, every terminate() would wait out the full grace period and escalate to
    SIGKILL against a corpse, and the test would pass while measuring nothing.

    NO ``/proc`` MEANS NO ANSWER, and the answer must then be False. On macOS
    every read here fails, and answering True would make :func:`_gone` report any
    live process as gone the instant after SIGTERM: ``terminate`` would return
    "term" without ever escalating, and a runner that ignores SIGTERM would
    survive the kill switch on that platform while the log said it died.
    """
    try:
        if not (PROC / "self").is_dir():
            return False
        stat = (PROC / str(pid) / "stat").read_text()
    except OSError:
        return True
    after_comm = stat[stat.rfind(")") + 1 :].split()
    return bool(after_comm) and after_comm[0] == "Z"


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        # Somebody else's process now holds the pid. Ours is gone.
        return True
    return _zombie(pid)


def terminate(
    pid: int | None,
    pgid: int | None = None,
    *,
    boot_id: str | None = None,
    start_ticks: int | None = None,
    grace_s: float = GRACE_S,
    poll_s: float = 0.02,
    allow_unknown: bool = True,
) -> TerminateOutcome:
    """SIGTERM the group, then SIGKILL after ``grace_s``. Never raises.

    THE PID-REUSE GUARD IS THE POINT, not the signalling. A kill switch that
    signals a recorded pid without checking identity will, after a reboot or a
    busy afternoon, SIGKILL whatever innocent process inherited that number. So
    the recorded ``boot_id`` and start-ticks are checked first and a proven
    mismatch returns ``"stale"`` WITHOUT SIGNALLING ANYTHING.

    ``allow_unknown`` covers the case where identity cannot be established at all
    (no ``/proc``, nothing recorded). It defaults to True and that is a judgement
    the doc does not make: the user pressed the button, and a kill switch that
    silently declines because it could not read ``/proc`` is a kill switch that
    does not work. The risk is bounded by the fact that a job we spawned always
    has its identity recorded, so ``"unknown"`` means something already went
    wrong.

    The group is signalled, not the pid: Claude Code spawns children, and
    ``SIGTERM`` to one process leaves a build running with nobody watching it.

    OUR OWN GROUP IS NEVER SIGNALLED, and this guard is not theoretical.
    ``jobs.process_identity`` records ``os.getpgid(child)``, which for a child
    spawned WITHOUT ``start_new_session`` is the spawner's own group — so one
    runner started down a fallback path that forgot ``setsid`` would turn the
    kill switch into SIGKILL for jarvis-dispatch, the voice app, and everything
    else sharing that group. The kill switch must be the one thing that cannot
    take the system down with it, so in that case only the recorded pid is
    signalled and the runner's own children are left to the epoch.
    """
    if pid is None or pid <= 0:
        return "no_pid"

    liveness = process_liveness(pid, boot_id, start_ticks)
    if liveness == "dead":
        # Either genuinely gone or a pid we no longer own. Both mean: do not
        # signal. "stale" says so honestly instead of claiming a successful kill.
        return "gone" if _gone(pid) else "stale"
    if liveness == "unknown" and not allow_unknown:
        return "denied"

    group = pgid if pgid and pgid > 0 and pgid != os.getpgrp() else None

    def send(sig: int) -> bool:
        """True when the signal reached something. Falls back to the pid.

        A recorded pgid can be wrong or empty while the pid is very much alive
        (a runner that called setsid after we read its group, a row written by
        an older build). Reporting the ESRCH from ``killpg`` as "gone" would
        mean the kill switch declaring success over a process it never
        signalled, which is the one lie this module cannot afford.
        """
        if group is not None:
            try:
                os.killpg(group, sig)
            except (ProcessLookupError, PermissionError):
                pass
            else:
                return True
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            return False
        return True

    if not send(signalmod.SIGTERM):
        return "gone"

    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if _gone(pid):
            return "term"
        time.sleep(poll_s)

    send(signalmod.SIGKILL)
    # No second wait: SIGKILL is not refusable, and blocking the kill switch on
    # a process stuck in uninterruptible I/O would be the one failure worse than
    # reporting it optimistically.
    return "kill"


def reap_job(
    con: sqlite3.Connection,
    job_id: str,
    *,
    actor: str = "dispatch",
    grace_s: float = GRACE_S,
    command_id: str | None = None,
) -> TerminateOutcome:
    """Signal a job's process group and mark the row killed. Idempotent.

    The row is marked killed even when the process was already gone: the user
    said stop, and a job left ``running`` with no process is exactly what the
    reconciler would resurrect.
    """
    job = get(con, job_id)
    if job is None:
        raise UnknownJob(job_id)
    outcome = terminate(
        job.pid,
        job.pgid,
        boot_id=job.boot_id,
        start_ticks=job.proc_start_ticks,
        grace_s=grace_s,
    )
    if job.state not in TERMINAL_STATES:
        # IllegalTransition here means somebody else finished the job between the
        # read and now. Their terminal state is as true as ours would have been,
        # so the loser of that race says nothing rather than fighting over it.
        with suppress(IllegalTransition):
            set_state(
                con,
                job_id,
                "killed",
                actor=actor,
                reason=f"kill switch ({outcome})",
                stop_reason="kill_switch",
            )
    if command_id is not None:
        ack_command(con, command_id, f"{actor}:{job_id}", result=outcome)
    return outcome


def reap_all(
    con: sqlite3.Connection,
    *,
    actor: str = "dispatch",
    grace_s: float = GRACE_S,
    command_id: str | None = None,
) -> dict[str, TerminateOutcome]:
    """The dispatch half of the kill: signal every non-terminal job.

    Called by whoever owns the process groups, after seeing a ``stop_all`` row
    written by a process it has never spoken to. That is the whole cross-process
    contract, and the test for this module is exactly that sentence.

    THE CALLER MUST CLAIM FIRST — ``claim_command(con, id, "reaper")`` under a
    SHARED actor — when more than one process could reap. This function does not
    take that lock itself, because the same call under a per-process actor is how
    a broadcast works (see :func:`claim_command`) and only the caller knows which
    of the two it is. Two unclaimed reapers means one of them sends SIGKILL while
    the other's SIGTERM grace is still running, and the job never gets to flush.
    """
    rows = con.execute(
        f"""SELECT id FROM jobs WHERE state IN ({",".join("?" * len(ACTIVE_JOB_STATES))})
             ORDER BY created_at""",
        ACTIVE_JOB_STATES,
    ).fetchall()
    outcomes: dict[str, TerminateOutcome] = {}
    for row in rows:
        job_id = str(row["id"])
        outcomes[job_id] = reap_job(con, job_id, actor=actor, grace_s=grace_s)
    if command_id is not None:
        ack_command(con, command_id, actor, result=canon(outcomes))
        finish_command(con, command_id)
    return outcomes
