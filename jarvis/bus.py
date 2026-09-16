"""The event bus, which is the activity log, which is one SQLite table.

There is no broker. ``events`` is the transport *and* the history: "what did you
do today" is a ``SELECT``, not a retention policy. Three SQLite properties do the
work and none of them survives a multi-writer store:

1. Exactly one write transaction at a time, so ``INTEGER PRIMARY KEY
   AUTOINCREMENT`` is handed out in commit order. A subscriber reading
   ``WHERE seq > cursor ORDER BY seq`` therefore can never see a hole that later
   fills in — which is the entire reason a durable cursor is only one integer.
2. ``INSERT`` on a UNIQUE ``idem_key`` is the whole dedupe mechanism.
3. The backup story is ``cp``.

Everything in this module assumes the reader is in ANOTHER PROCESS, possibly
started after the writer died. Nothing is cached in memory, nothing is a
singleton, and every function takes the caller's open connection.

THE POKE IS NOT THE TRANSPORT. ``Peer`` binds a Unix datagram socket so a
publish can wake a sleeping subscriber in about a millisecond, but a subscriber
that never receives a single poke is still *correct* — it polls at
``Peer.POLL_S`` and is at most 250 ms late. Every send error is swallowed on
purpose; see :func:`poke`.
"""

from __future__ import annotations

import hashlib
import json
import os
import select
import socket
import sqlite3
import tempfile
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, TypedDict

from jarvis.db import tx
from jarvis.ids import canon, nid, now, parse_ts

__all__ = [
    "EVENT_KINDS",
    "GENESIS_HASH",
    "Capabilities",
    "ChannelKind",
    "Event",
    "EventKind",
    "Peer",
    "Redactor",
    "commit_cursor",
    "cursor_value",
    "event_by_id",
    "last_seq",
    "null_detail_older_than",
    "poke",
    "poke_attached",
    "publish",
    "read_since",
    "verify_chain",
]

EventKind = Literal[
    "job.created",
    "job.started",
    "job.progress",
    "job.blocked",
    "job.deferred",
    "job.resumed",
    "job.parked",
    "job.orphaned",
    "job.finished",
    "job.failed",
    "job.killed",
    "request.created",
    "request.offered",
    "request.answered",
    "request.consumed",
    "request.expired",
    "request.cancelled",
    "request.superseded",
    "effect.recorded",
    "effect.undone",
    "effect.undo_failed",
    "presence.changed",
    "channel.attached",
    "channel.detached",
    "command.issued",
    "command.acked",
    "tool.used",
    "tool.denied",
    "spend.recorded",
    "speech.said",
    "call.placed",
    "call.ended",
    "telegram.sent",
]

# The Literal above is documentation and a type-checker aid, NOT a runtime gate.
# publish() takes any str: the spike's real activity log already contains
# 'tool.pre' and 'question.answer_replayed', and a bus that refuses an unplanned
# kind at 3am turns a log line into an outage.
EVENT_KINDS: frozenset[str] = frozenset(EventKind.__args__)

ChannelKind = Literal["desk", "phone", "telegram", "hud", "scheduler", "runner", "cli"]


class Capabilities(TypedDict):
    """What a channel can actually do. ``human=False`` never RECEIVES requests."""

    human: bool
    speak: bool
    verbatim: bool
    listen: bool
    free_text: bool
    dtmf: bool
    buttons: bool
    images: bool
    max_options: int


class NulledDetail(TypedDict):
    """What is left of a payload after :func:`null_detail_older_than`."""

    _jarvis_detail_nulled: Literal[True]
    detail_sha256: str
    counts: dict[str, float | int | bool]
    keys: list[str]
    bytes: int


@dataclass(frozen=True, slots=True)
class Event:
    """One row of ``events``. This dataclass IS the wire format."""

    seq: int
    id: str
    ts: str
    kind: str
    actor: str
    payload: dict[str, Any]
    job_id: str | None = None
    request_id: str | None = None
    channel_id: str | None = None
    effect_id: str | None = None
    redacted: bool = False
    detail_nulled_at: str | None = None

    @property
    def detail_nulled(self) -> bool:
        return self.detail_nulled_at is not None


# ───────────────────────────── redaction ─────────────────────────────

REDACTED = "[redacted]"

# A hash chain needs a defined zero. Sixty-four zeros is not a valid sha256 of
# anything anyone will ever publish, so "the first row" is unambiguous.
GENESIS_HASH = "0" * 64


@dataclass(frozen=True, slots=True)
class Redactor:
    """Literal-secret replacement, applied AT WRITE TIME.

    The caller supplies the literals. This module deliberately does not import
    ``keyring`` and deliberately has no module-level "loaded secrets" slot: the
    architecture sketches ``load_secrets(values)`` as a module function, but that
    is process-global mutable state, which the house rules forbid and which would
    mean a library import order decides whether a token reaches the log. A
    ``Redactor`` is built once at process start and passed down explicitly, so
    "was this event redacted?" has an answer you can read off the call site.
    """

    secrets: tuple[str, ...] = ()
    placeholder: str = REDACTED

    @classmethod
    def of(cls, values: Iterable[str], *, placeholder: str = REDACTED) -> Redactor:
        """Build a redactor, longest secret first.

        Order matters: if a short secret is a substring of a long one, replacing
        the short one first would leave a mangled but still recognisable tail of
        the long one in the log.
        """
        uniq = {v for v in values if v}  # "" would splice the placeholder between every character
        return cls(tuple(sorted(uniq, key=len, reverse=True)), placeholder)

    def apply(self, value: Any) -> tuple[Any, bool]:
        """Return ``(redacted_value, hit)``, recursing through dicts and lists.

        Secrets hide in nested structures — a tool input is ``{"env": {"TOKEN":
        "ghp_…"}}`` and a Claude Code transcript is a list of dicts — so a
        top-level-only scan is the same as no scan at all.
        """
        if not self.secrets:
            return value, False
        return self._walk(value)

    def _walk(self, value: Any) -> tuple[Any, bool]:
        if isinstance(value, str):
            out = value
            for s in self.secrets:
                out = out.replace(s, self.placeholder)
            return out, out != value
        if isinstance(value, dict):
            hit = False
            out_d: dict[Any, Any] = {}
            for k, v in value.items():
                nk, kh = self._walk(k) if isinstance(k, str) else (k, False)
                nv, vh = self._walk(v)
                hit = hit or kh or vh
                # Two keys that differed only inside a secret collapse to one.
                # First wins rather than last, so the surviving value is the one
                # a reader scanning top-down would have expected.
                if nk not in out_d:
                    out_d[nk] = nv
            return out_d, hit
        if isinstance(value, (list, tuple)):
            walked = [self._walk(v) for v in value]
            return [v for v, _ in walked], any(h for _, h in walked)
        return value, False


_NO_REDACTION = Redactor()


# ───────────────────────────── the hash chain ─────────────────────────────


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _row_hash(
    *,
    prev_hash: str,
    seq: int,
    id: str,
    ts: str,
    kind: str,
    actor: str,
    job_id: str | None,
    request_id: str | None,
    channel_id: str | None,
    effect_id: str | None,
    idem_key: str,
    redacted: int,
    payload_digest: str,
) -> str:
    """The link. Note it binds the payload by DIGEST, not by content.

    That indirection is what lets :func:`null_detail_older_than` destroy 90-day-old
    payload text without severing the chain: the nulled row keeps the digest of
    what it used to hold, and the digest is itself covered by this hash, so the
    digest cannot be swapped for one matching a forged payload. The honest limit
    is stated on that function.
    """
    return _sha256(
        canon(
            [
                prev_hash,
                seq,
                id,
                ts,
                kind,
                actor,
                job_id,
                request_id,
                channel_id,
                effect_id,
                idem_key,
                redacted,
                payload_digest,
            ]
        )
    )


def _payload_digest(row: sqlite3.Row) -> str | None:
    """The digest this row's hash was computed over, or None if the row is bent."""
    if row["detail_nulled_at"] is None:
        return _sha256(row["payload"])
    try:
        stub = json.loads(row["payload"])
    except json.JSONDecodeError:
        return None
    if not isinstance(stub, dict) or not stub.get("_jarvis_detail_nulled"):
        return None
    digest = stub.get("detail_sha256")
    return digest if isinstance(digest, str) else None


@contextmanager
def _atomic(con: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE, unless the caller already owns a transaction.

    publish() must be composable into a caller's atom — ``requests.answer_request``
    has to write the answer and its ``request.answered`` event or neither, or a
    crash between them leaves an answer nobody is ever told about. Nesting BEGIN
    is an error in SQLite, so we join rather than open.
    """
    if con.in_transaction:
        yield con
    else:
        with tx(con) as t:
            yield t


def publish(
    con: sqlite3.Connection,
    kind: str,
    actor: str,
    payload: dict[str, Any],
    *,
    job_id: str | None = None,
    request_id: str | None = None,
    channel_id: str | None = None,
    effect_id: str | None = None,
    idem_key: str | None = None,
    redactor: Redactor | None = None,
    poke_peers: bool = True,
) -> str:
    """Append ONE hash-chained, redacted event and return its id.

    Republishing the same ``idem_key`` is a no-op returning the id already there.
    That is the at-least-once contract: a runner that crashed between doing the
    thing and logging it retries with the same natural key (``job:<id>:state:<s>``,
    ``req:<id>:answered``, ``tool:<tool_use_id>``) and gets one row.

    ``idem_key`` is NOT NULL UNIQUE in the schema, so an omitted one is
    synthesised from this event's own id — unique by construction, i.e. an event
    with no natural key is never deduped against anything.

    WITHOUT a ``redactor`` the payload is stored VERBATIM and kept forever. There
    is no global secret store to fall back on (see :class:`Redactor`), so every
    process that can put a token in a payload must build one at startup and pass
    it here. A missing redactor is not an error and cannot be made one — the bus
    cannot know what a secret looks like — so it is called out here instead.
    """
    if not isinstance(payload, dict):
        raise TypeError(f"payload must be a dict, got {type(payload).__name__}")

    # Whether WE open the transaction decides whether we may send a datagram at
    # the end: see the comment on the poke below.
    nested = con.in_transaction
    body, hit = (redactor or _NO_REDACTION).apply(payload)
    payload_text = canon(body)
    digest = _sha256(payload_text)
    ev_id = nid("ev")
    key = idem_key if idem_key is not None else f"auto:{ev_id}"
    ts = now()
    redacted = 1 if hit else 0

    with _atomic(con):
        row = con.execute("SELECT id FROM events WHERE idem_key=?", (key,)).fetchone()
        if row is not None:
            return str(row["id"])

        prev = con.execute("SELECT hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        prev_hash = str(prev["hash"]) if prev is not None else GENESIS_HASH

        # seq is AUTOINCREMENT and the hash covers it, so the row must exist
        # before its own hash can be computed. Both statements are inside one
        # BEGIN IMMEDIATE, and WAL readers see the pre-commit snapshot, so no
        # other process can ever observe the placeholder hash.
        try:
            cur = con.execute(
                "INSERT INTO events (id, ts, kind, actor, job_id, request_id, channel_id,"
                " effect_id, idem_key, payload, redacted, prev_hash, hash)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'')",
                (
                    ev_id,
                    ts,
                    kind,
                    actor,
                    job_id,
                    request_id,
                    channel_id,
                    effect_id,
                    key,
                    payload_text,
                    redacted,
                    prev_hash,
                ),
            )
        except sqlite3.IntegrityError:
            # Belt and braces for a caller who opened a DEFERRED transaction
            # around us: their snapshot could have missed a commit the SELECT
            # above would otherwise have caught. A failed statement rolls back
            # only itself, so re-reading is safe.
            again = con.execute("SELECT id FROM events WHERE idem_key=?", (key,)).fetchone()
            if again is None:
                raise
            return str(again["id"])

        seq = int(cur.lastrowid or 0)
        h = _row_hash(
            prev_hash=prev_hash,
            seq=seq,
            id=ev_id,
            ts=ts,
            kind=kind,
            actor=actor,
            job_id=job_id,
            request_id=request_id,
            channel_id=channel_id,
            effect_id=effect_id,
            idem_key=key,
            redacted=redacted,
            payload_digest=digest,
        )
        con.execute("UPDATE events SET hash=? WHERE seq=?", (h, seq))

    # AFTER the commit, never inside it, and that is why a nested publish does
    # not poke at all. A peer woken before the commit opens its own connection,
    # reads `seq > cursor`, sees nothing and sleeps a full tick — so the poke is
    # not merely wasted, it consumes the datagram that would have woken it later.
    # Worse, every sendto() here blocks for up to 50ms, and inside a caller's
    # transaction that is socket I/O held under the SQLite write lock, which the
    # architecture forbids outright ("no network or subprocess I/O inside
    # tx(con), ever"). The caller that owns the transaction owns the poke:
    # commit, then call poke_attached(con) once — jarvis.kill already does.
    if poke_peers and not nested:
        poke_attached(con)
    return ev_id


def _highwater(con: sqlite3.Connection) -> int | None:
    """The largest seq ever handed out, or None if nothing ever was.

    ``sqlite_sequence`` is maintained by AUTOINCREMENT and is never lowered by a
    DELETE, which is exactly the property a prev_hash chain lacks. Measured, not
    assumed: a ROLLBACK *does* restore it and a failed INSERT (the idem_key race
    in publish) never bumps it, so neither can look like a truncation.
    """
    try:
        row = con.execute("SELECT seq FROM sqlite_sequence WHERE name='events'").fetchone()
    except sqlite3.OperationalError:
        return None  # sqlite_sequence is not created until the first AUTOINCREMENT insert
    return int(row["seq"]) if row is not None else None


def verify_chain(con: sqlite3.Connection, *, since_seq: int = 0) -> int | None:
    """Walk the chain; return the seq of the first broken link, or None.

    Broken means any of: the recomputed row hash differs, ``prev_hash`` does not
    match the previous row's hash (which is how a DELETE is caught), a row marked
    ``detail_nulled_at`` no longer carries a readable digest stub, or rows are
    missing from the END of the log.

    That last case is the one a hash chain alone cannot see: deleting the tail
    leaves every surviving link intact, and "delete the last row" is the obvious
    attack on an honesty log — it is the ``effect.recorded`` for whatever just
    happened. AUTOINCREMENT's high-water mark is the second witness.

    Streams with a single cursor rather than materialising 110k rows a year.
    """
    prev = GENESIS_HASH
    if since_seq > 0:
        anchor = con.execute(
            "SELECT hash FROM events WHERE seq <= ? ORDER BY seq DESC LIMIT 1", (since_seq,)
        ).fetchone()
        if anchor is not None:
            prev = str(anchor["hash"])
    last = since_seq
    for row in con.execute("SELECT * FROM events WHERE seq > ? ORDER BY seq", (since_seq,)):
        seq = int(row["seq"])
        last = seq
        digest = _payload_digest(row)
        if digest is None or row["prev_hash"] != prev:
            return seq
        expect = _row_hash(
            prev_hash=str(row["prev_hash"]),
            seq=seq,
            id=str(row["id"]),
            ts=str(row["ts"]),
            kind=str(row["kind"]),
            actor=str(row["actor"]),
            job_id=row["job_id"],
            request_id=row["request_id"],
            channel_id=row["channel_id"],
            effect_id=row["effect_id"],
            idem_key=str(row["idem_key"]),
            redacted=int(row["redacted"]),
            payload_digest=digest,
        )
        if expect != row["hash"]:
            return seq
        prev = str(row["hash"])
    hw = _highwater(con)
    if hw is not None and hw > last:
        return last + 1
    return None


# ───────────────────────────── reading ─────────────────────────────


def _to_event(row: sqlite3.Row) -> Event:
    return Event(
        seq=int(row["seq"]),
        id=str(row["id"]),
        ts=str(row["ts"]),
        kind=str(row["kind"]),
        actor=str(row["actor"]),
        payload=json.loads(row["payload"]),
        job_id=row["job_id"],
        request_id=row["request_id"],
        channel_id=row["channel_id"],
        effect_id=row["effect_id"],
        redacted=bool(row["redacted"]),
        detail_nulled_at=row["detail_nulled_at"],
    )


def event_by_id(con: sqlite3.Connection, event_id: str) -> Event | None:
    row = con.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    return _to_event(row) if row is not None else None


def last_seq(con: sqlite3.Connection) -> int:
    """The highest committed seq, or 0. A fresh subscriber starting 'from now'."""
    row = con.execute("SELECT seq FROM events ORDER BY seq DESC LIMIT 1").fetchone()
    return int(row["seq"]) if row is not None else 0


def cursor_value(con: sqlite3.Connection, cursor_name: str) -> int:
    row = con.execute("SELECT last_seq FROM consumers WHERE id=?", (cursor_name,)).fetchone()
    return int(row["last_seq"]) if row is not None else 0


def read_since(
    con: sqlite3.Connection,
    cursor_name: str,
    limit: int = 200,
    *,
    kinds: tuple[str, ...] | None = None,
) -> list[Event]:
    """Events after the durable cursor, oldest first.

    The cursor is NOT advanced here. Delivery is at-least-once by design: you
    handle, then :func:`commit_cursor`, so a crash mid-handle replays rather than
    drops. Every handler in this system is an upsert or is guarded by
    ``deliveries.presented_at``, which is what stops a replay reading a question
    aloud twice.

    A ``kinds`` filter narrows what comes back but not what the cursor skips: a
    caller that commits the seq of the last returned row deliberately steps over
    the kinds it did not ask for.
    """
    if limit <= 0:
        return []
    cursor = cursor_value(con, cursor_name)
    if kinds:
        marks = ",".join("?" * len(kinds))
        sql = f"SELECT * FROM events WHERE seq > ? AND kind IN ({marks}) ORDER BY seq LIMIT ?"
        rows = con.execute(sql, (cursor, *kinds, limit)).fetchall()
    else:
        rows = con.execute(
            "SELECT * FROM events WHERE seq > ? ORDER BY seq LIMIT ?", (cursor, limit)
        ).fetchall()
    return [_to_event(r) for r in rows]


def commit_cursor(
    con: sqlite3.Connection,
    cursor_name: str,
    seq: int,
    *,
    allow_rewind: bool = False,
) -> int:
    """Durably advance a subscriber's cursor. Returns the value now stored.

    Monotonic by default, and that is a race fix rather than tidiness: two
    workers on one cursor (a restarted voice app overlapping its predecessor, or
    two threads sharing ``peer:desk``) can commit out of order, and letting the
    slow one win would silently re-deliver everything in between. ``allow_rewind``
    exists for the data-plane use of this table, where a stream rotation resets
    the byte offset to 0 on purpose.
    """
    ts = now()
    with _atomic(con):
        if allow_rewind:
            con.execute(
                "INSERT INTO consumers (id, last_seq, updated_at) VALUES (?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET last_seq=excluded.last_seq,"
                " updated_at=excluded.updated_at",
                (cursor_name, seq, ts),
            )
        else:
            con.execute(
                "INSERT INTO consumers (id, last_seq, updated_at) VALUES (?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET"
                " last_seq=MAX(consumers.last_seq, excluded.last_seq),"
                " updated_at=excluded.updated_at",
                (cursor_name, seq, ts),
            )
    return cursor_value(con, cursor_name)


# ───────────────────────────── retention ─────────────────────────────


def _fmt(dt: datetime) -> str:
    return f"{dt.strftime('%Y-%m-%dT%H:%M:%S')}.{dt.microsecond // 1000:03d}Z"


def null_detail_older_than(
    con: sqlite3.Connection,
    days: int,
    *,
    at: str | None = None,
    limit: int | None = None,
) -> int:
    """Null the payload detail of events older than ``days``. Returns rows changed.

    The row, its seq, its links and its counts stay. The text — the prompt, the
    question, the transcript fragment — goes.

    WHAT THIS DOES TO THE CHAIN, honestly. The row hash covers a DIGEST of the
    payload, not the payload, and the nulled stub carries that digest forward. So
    :func:`verify_chain` keeps returning None across nulled rows, and the digest
    itself is covered by the hash, so nobody can null a row and substitute a stub
    whose digest matches forged content. What is NOT provable after nulling is
    the detail itself: once the text is gone, the digest is an assertion about
    something no longer present. That is a deliberate trade — nulling is a
    VISIBLE destruction (``detail_nulled_at`` is set and the stub says so), never
    an invisible edit. Tamper-evidence for live rows is total; for nulled rows it
    covers everything except content that the row openly admits it discarded.

    ``counts`` keeps the top-level numeric and boolean fields, because those are
    what the ledger and the briefing read back a year later ("that build touched
    31 files, it was approved") and none of them can carry a secret.
    """
    cutoff = _fmt(parse_ts(at or now()) - timedelta(days=days))
    sql = "SELECT seq, payload FROM events WHERE detail_nulled_at IS NULL AND ts < ? ORDER BY seq"
    params: tuple[Any, ...] = (cutoff,)
    if limit is not None:
        sql += " LIMIT ?"
        params = (cutoff, limit)

    stamp = now()
    changed = 0
    with _atomic(con):
        for row in con.execute(sql, params).fetchall():
            text = str(row["payload"])
            body = json.loads(text)
            counts = (
                {k: v for k, v in body.items() if isinstance(v, (int, float))}
                if isinstance(body, dict)
                else {}
            )
            stub: NulledDetail = {
                "_jarvis_detail_nulled": True,
                "detail_sha256": _sha256(text),
                "counts": counts,
                "keys": sorted(body) if isinstance(body, dict) else [],
                "bytes": len(text.encode("utf-8")),
            }
            con.execute(
                "UPDATE events SET payload=?, detail_nulled_at=? WHERE seq=?",
                (canon(stub), stamp, int(row["seq"])),
            )
            changed += 1
    return changed


# ───────────────────────────── the poke socket ─────────────────────────────

# sun_path is 108 bytes on Linux and 104 on macOS. A bind() past that fails with
# a misleading ENAMETOOLONG deep inside attach, so we detect it and fall back to
# a short hashed path; pokers read the address out of channels.poke_addr and
# never re-derive it, so the fallback costs nothing.
_SUN_PATH_MAX = 100
_POKE_BYTE = b"\x01"


def poke_dir() -> Path:
    """Where poke sockets live. ``XDG_RUNTIME_DIR`` is wiped at logout, which is
    how stale sockets clean themselves up without a reaper."""
    if d := os.environ.get("JARVIS_POKE_DIR"):
        return Path(d)
    if r := os.environ.get("XDG_RUNTIME_DIR"):
        return Path(r) / "jarvis" / "poke"
    return Path(tempfile.gettempdir()) / f"jarvis-{os.getuid()}" / "poke"


def poke_path(peer_id: str) -> Path:
    p = poke_dir() / f"{peer_id}.sock"
    if len(str(p)) <= _SUN_PATH_MAX:
        return p
    short = hashlib.sha256(peer_id.encode("utf-8")).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / f"jarvis-{os.getuid()}-{short}.sock"


def poke(addr: str | Path) -> bool:
    """Send one byte. Never raises. Returns whether it left the process.

    Total loss of every poke this system ever sends costs 250 ms of latency and
    nothing else, so there is no retry, no backoff and no error surface here. The
    dead address is reaped by the heartbeat, not by this call.
    """
    if not hasattr(socket, "AF_UNIX"):
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.settimeout(0.05)
            s.sendto(_POKE_BYTE, str(addr))
        return True
    except OSError:
        return False


def poke_attached(con: sqlite3.Connection, *, exclude: str | None = None) -> int:
    """Best-effort poke to every attached channel. Returns how many were sent."""
    rows = con.execute(
        "SELECT id, poke_addr FROM channels"
        " WHERE state='attached' AND poke_addr IS NOT NULL AND id IS NOT ?",
        (exclude,),
    ).fetchall()
    return sum(1 for r in rows if poke(str(r["poke_addr"])))


def _boot_id() -> str | None:
    """Linux boot id. Paired with a pid it defeats PID reuse across a reboot."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return None


@dataclass(slots=True)
class Peer:
    """A process's attachment to the bus: a poke socket plus a durable cursor.

    Deviates from the architecture sketch in one way, on purpose. The sketch has
    ``Peer.__init__(self, con, ...)`` holding a connection; here every method
    takes the connection as its first argument and the object holds none. Two
    reasons: the house rule that functions never own connections, and the fact
    that a sqlite3 connection is bound to the thread that made it — a Peer stored
    on a long-lived object WILL eventually be used from an audio callback thread,
    and that failure is intermittent rather than loud.

    A Peer is optional. Everything it does over the socket, a caller can do with
    :func:`read_since` and a ``time.sleep``; the socket only buys the 250 ms.
    """

    peer_id: str
    kind: str
    addr: str | None = None
    cursor_name: str = ""
    _sock: socket.socket | None = field(default=None, repr=False)
    # The inode the socket FILE had when we bound it. close() unlinks by name,
    # and by then the name may belong to somebody else — see close().
    _ino: int | None = field(default=None, repr=False)
    # The pid we registered under, so detach() cannot retire a successor's row.
    # None on a hand-built Peer, which then detaches unguarded.
    _pid: int | None = field(default=None, repr=False)

    POLL_S = 0.25

    def __post_init__(self) -> None:
        if not self.cursor_name:
            self.cursor_name = f"peer:{self.peer_id}"

    @classmethod
    def attach(
        cls,
        con: sqlite3.Connection,
        peer_id: str,
        kind: str,
        caps: Capabilities | dict[str, Any],
        identity: dict[str, Any] | None = None,
        *,
        redactor: Redactor | None = None,
    ) -> Peer:
        """Bind the socket, upsert the ``channels`` row, publish ``channel.attached``.

        Binding fails soft. A machine with no ``AF_UNIX``, a full runtime dir or a
        socket left behind by a SIGKILLed predecessor must not stop a process from
        subscribing — it just leaves it polling.
        """
        sock, addr = cls._bind(peer_id)
        ino: int | None = None
        if addr is not None:
            with suppress(OSError):
                ino = Path(addr).stat().st_ino
        ts = now()
        pid = os.getpid()
        with _atomic(con):
            con.execute(
                "INSERT INTO channels (id, kind, state, pid, boot_id, poke_addr, caps,"
                " identity, attached_at, last_heartbeat_at, detached_at)"
                " VALUES (?,?,'attached',?,?,?,?,?,?,?,NULL)"
                " ON CONFLICT(id) DO UPDATE SET kind=excluded.kind, state='attached',"
                " pid=excluded.pid, boot_id=excluded.boot_id, poke_addr=excluded.poke_addr,"
                " caps=excluded.caps, identity=excluded.identity,"
                " attached_at=excluded.attached_at,"
                " last_heartbeat_at=excluded.last_heartbeat_at, detached_at=NULL",
                (
                    peer_id,
                    kind,
                    pid,
                    _boot_id(),
                    addr,
                    canon(caps),
                    canon(identity) if identity is not None else None,
                    ts,
                    ts,
                ),
            )
            publish(
                con,
                "channel.attached",
                peer_id,
                {"kind": kind, "caps": caps, "poke_addr": addr},
                channel_id=peer_id,
                # ts alone is millisecond-resolution, so two processes attaching
                # as the same peer_id in the same millisecond would dedupe and
                # one attach would vanish from the log. The pid separates them.
                idem_key=f"ch:{peer_id}:attached:{ts}:{pid}",
                redactor=redactor,
                poke_peers=False,
            )
        return cls(peer_id=peer_id, kind=kind, addr=addr, _sock=sock, _ino=ino, _pid=pid)

    @staticmethod
    def _bind(peer_id: str) -> tuple[socket.socket | None, str | None]:
        if not hasattr(socket, "AF_UNIX"):
            return None, None
        path = poke_path(peer_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # A predecessor that was SIGKILLed left its socket file behind; bind
            # would then fail with EADDRINUSE forever. Nobody else may hold this
            # name — it is derived from our own peer_id — so unlinking is safe.
            with suppress(FileNotFoundError):
                path.unlink()
            s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            s.bind(str(path))
            os.chmod(path, 0o600)
            s.setblocking(False)
            return s, str(path)
        except OSError:
            return None, None

    def wait(self, timeout: float | None = None) -> None:
        """Sleep until poked, or for one poll tick — whichever comes first.

        This is ONE tick, not a loop: it never sleeps longer than ``POLL_S``, so a
        caller that loops ``wait(); poll()`` is at most 250 ms behind even if
        every datagram is lost. Do not be tempted to honour a long ``timeout`` by
        sleeping it out; that turns the poke from an optimisation into the
        transport.
        """
        budget = self.POLL_S if timeout is None else max(0.0, min(timeout, self.POLL_S))
        if self._sock is None:
            _sleep(budget)
            return
        try:
            ready, _, _ = select.select([self._sock], [], [], budget)
        except OSError:
            _sleep(budget)
            return
        if not ready:
            return
        # Drain the burst. Ten publishes in one tick are one wakeup, and a
        # backlog of datagrams must not make the next wait() return instantly
        # forever.
        while True:
            try:
                self._sock.recv(64)
            except (BlockingIOError, OSError):
                return

    def poll(
        self,
        con: sqlite3.Connection,
        kinds: tuple[str, ...] | None = None,
        limit: int = 200,
    ) -> list[Event]:
        return read_since(con, self.cursor_name, limit, kinds=kinds)

    def commit(self, con: sqlite3.Connection, seq: int) -> int:
        return commit_cursor(con, self.cursor_name, seq)

    def heartbeat(self, con: sqlite3.Connection) -> None:
        """Freshness for reconcile(), which reaps channels stale by >30s."""
        con.execute("UPDATE channels SET last_heartbeat_at=? WHERE id=?", (now(), self.peer_id))

    def close(self) -> None:
        """Release the socket without touching the database.

        Split from detach() because a crashing process wants its fd back but must
        NOT claim it left cleanly — a 'detached' row means reconcile will not
        re-route this channel's pending deliveries.

        The unlink is guarded by inode, not by name. Two live processes can share
        a peer_id — a restarted voice app overlapping its predecessor is the case
        commit_cursor() already defends against — and the second one's attach()
        rebinds the same path. An unconditional unlink here would then delete the
        SURVIVOR's socket file while channels.poke_addr still advertised it,
        which is silent and permanent: every later poke to a healthy peer fails
        forever and nobody learns why.
        """
        if self._sock is not None:
            with suppress(OSError):
                self._sock.close()
            self._sock = None
        if self.addr and self._ino is not None:
            with suppress(OSError):
                if Path(self.addr).stat().st_ino == self._ino:
                    Path(self.addr).unlink()
        self._ino = None

    def detach(self, con: sqlite3.Connection, *, reason: str = "clean") -> None:
        """Retire OUR registration. A no-op if a successor already took the id.

        Guarded by pid for the same reason close() is guarded by inode: if a
        restarted peer re-attached under this peer_id, marking the row detached
        here would tell reconcile() to re-route a LIVE channel's deliveries and
        stop poking it, while that channel still believes it is attached. A stall
        with no error anywhere is the failure this whole system exists to avoid.
        """
        ts = now()
        with _atomic(con):
            cur = con.execute(
                "UPDATE channels SET state='detached', detached_at=?, poke_addr=NULL"
                " WHERE id=? AND (? IS NULL OR pid=?)",
                (ts, self.peer_id, self._pid, self._pid),
            )
            if cur.rowcount:
                publish(
                    con,
                    "channel.detached",
                    self.peer_id,
                    {"kind": self.kind, "reason": reason},
                    channel_id=self.peer_id,
                    idem_key=f"ch:{self.peer_id}:detached:{ts}:{self._pid}",
                    poke_peers=False,
                )
        self.close()


def _sleep(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)
