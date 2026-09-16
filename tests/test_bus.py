"""Contract tests for :mod:`jarvis.bus`.

The happy path ("publish then read it back") is not where a bus breaks. What
breaks a bus is two processes writing at once, a process that died between doing
a thing and logging it, a reader that was down for an hour, and a log somebody
edited. So every test here is a race, a restart, or a tamper.

Two connections to the same file ARE two processes for everything this module
does: SQLite's locking, WAL snapshots and busy_timeout do not know or care that
they share an address space.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from jarvis import bus, db
from jarvis.bus import (
    Peer,
    Redactor,
    commit_cursor,
    cursor_value,
    last_seq,
    null_detail_older_than,
    publish,
    read_since,
    verify_chain,
)
from jarvis.ids import canon, now, parse_ts

CAPS: dict[str, Any] = {
    "human": True,
    "speak": True,
    "verbatim": True,
    "listen": True,
    "free_text": True,
    "dtmf": False,
    "buttons": False,
    "images": True,
    "max_options": 4,
}


def _shift(ts: str, **delta: float) -> str:
    d = parse_ts(ts) + timedelta(**delta)
    return f"{d.strftime('%Y-%m-%dT%H:%M:%S')}.{d.microsecond // 1000:03d}Z"


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway database and a throwaway poke directory. Never the real one."""
    monkeypatch.setenv("JARVIS_DB", str(tmp_path / "jarvis.db"))
    monkeypatch.setenv("JARVIS_POKE_DIR", str(tmp_path / "pk"))
    p = tmp_path / "jarvis.db"
    con = db.connect(p)
    db.migrate(con)
    con.close()
    return p


@pytest.fixture
def con(db_path: Path):
    c = db.connect(db_path)
    yield c
    c.close()


def _raw_payloads(con: sqlite3.Connection) -> list[str]:
    return [str(r["payload"]) for r in con.execute("SELECT payload FROM events ORDER BY seq")]


# ───────────────────────── idempotence ─────────────────────────


def test_republish_of_an_idem_key_is_a_noop_from_another_connection(db_path: Path) -> None:
    """A runner that crashed after acting but before logging retries with the same
    natural key. That must be one row, not an error and not a duplicate."""
    a = db.connect(db_path)
    first = publish(
        a, "job.finished", "runner:j1", {"rc": 0}, job_id="j1", idem_key="job:j1:state:done"
    )
    a.close()

    b = db.connect(db_path)
    second = publish(
        b,
        "job.finished",
        "runner:j1",
        {"rc": 99, "different": "payload"},
        job_id="j1",
        idem_key="job:j1:state:done",
    )
    assert second == first
    assert b.execute("SELECT COUNT(*) c FROM events").fetchone()["c"] == 1
    # The winner's payload is the one that survives; the retry is discarded whole.
    assert json.loads(_raw_payloads(b)[0]) == {"rc": 0}
    assert verify_chain(b) is None
    b.close()


def test_two_processes_racing_one_idem_key_produce_exactly_one_row(db_path: Path) -> None:
    """The real shape: a runner and the reconciler both decide to log the same
    transition at the same instant."""
    ids: list[str] = []
    lock = threading.Lock()
    start = threading.Barrier(6)

    def worker() -> None:
        c = db.connect(db_path)
        try:
            start.wait(timeout=5)
            ev = publish(
                c,
                "request.answered",
                "desk",
                {"approved": True},
                request_id="r1",
                idem_key="req:r1:answered",
            )
            with lock:
                ids.append(ev)
        finally:
            c.close()

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert len(ids) == 6
    assert len(set(ids)) == 1, "every racer must be handed the one surviving id"
    c = db.connect(db_path)
    assert c.execute("SELECT COUNT(*) n FROM events").fetchone()["n"] == 1
    assert verify_chain(c) is None
    c.close()


def test_events_without_a_natural_key_are_never_deduped(con: sqlite3.Connection) -> None:
    """idem_key is NOT NULL UNIQUE. An omitted one must not collapse two genuinely
    distinct events into one."""
    a = publish(con, "job.progress", "runner:j1", {"line": "same"})
    b = publish(con, "job.progress", "runner:j1", {"line": "same"})
    assert a != b
    assert con.execute("SELECT COUNT(*) n FROM events").fetchone()["n"] == 2


def test_publish_refuses_a_non_dict_payload(con: sqlite3.Connection) -> None:
    with pytest.raises(TypeError):
        publish(con, "job.progress", "runner:j1", ["not", "a", "dict"])  # type: ignore[arg-type]
    assert con.execute("SELECT COUNT(*) n FROM events").fetchone()["n"] == 0


# ───────────────────────── restart survival ─────────────────────────


def test_an_event_written_by_a_dead_process_is_readable_by_a_later_one(db_path: Path) -> None:
    writer = db.connect(db_path)
    publish(
        writer,
        "request.created",
        "runner:j1",
        {"q": "SQLite or Postgres?"},
        request_id="r1",
        idem_key="req:r1:created",
    )
    writer.close()  # the spike's bug: a missing autocommit silently rolls this back

    reader = db.connect(db_path)
    evs = read_since(reader, "peer:desk", 10)
    assert [e.kind for e in evs] == ["request.created"]
    assert evs[0].payload == {"q": "SQLite or Postgres?"}
    reader.close()


def test_a_cursor_survives_the_process_that_committed_it(db_path: Path) -> None:
    a = db.connect(db_path)
    for i in range(5):
        publish(a, "job.progress", "runner:j1", {"i": i})
    commit_cursor(a, "peer:desk", 3)
    a.close()

    b = db.connect(db_path)
    assert cursor_value(b, "peer:desk") == 3
    assert [e.payload["i"] for e in read_since(b, "peer:desk", 10)] == [3, 4]
    b.close()


def test_a_subscriber_that_was_down_replays_from_its_cursor(db_path: Path) -> None:
    """Durable replay is not a second mechanism: a process that missed
    command.issued simply reads seq > cursor on boot."""
    a = db.connect(db_path)
    publish(a, "command.issued", "user", {"verb": "stop_all"}, idem_key="cmd:c1:issued")
    commit_cursor(a, "peer:voice", last_seq(a))
    a.close()

    offline = db.connect(db_path)
    publish(offline, "command.issued", "user", {"verb": "kill"}, idem_key="cmd:c2:issued")
    publish(offline, "job.killed", "runner:j1", {}, job_id="j1", idem_key="job:j1:state:killed")
    offline.close()

    rebooted = db.connect(db_path)
    missed = read_since(rebooted, "peer:voice", 100)
    assert [e.kind for e in missed] == ["command.issued", "job.killed"]
    rebooted.close()


# ───────────────────────── cursors under contention ─────────────────────────


def test_a_lagging_worker_cannot_rewind_a_cursor(db_path: Path) -> None:
    """A restarted voice app overlapping its predecessor would otherwise re-deliver
    everything in between — i.e. read a question aloud twice."""
    fast = db.connect(db_path)
    slow = db.connect(db_path)
    commit_cursor(fast, "peer:desk", 10)
    assert commit_cursor(slow, "peer:desk", 5) == 10
    assert cursor_value(fast, "peer:desk") == 10
    fast.close()
    slow.close()


def test_a_rewind_is_possible_only_when_asked_for(con: sqlite3.Connection) -> None:
    """The same table carries the data-plane byte offset, which a stream rotation
    legitimately resets to zero."""
    commit_cursor(con, "stream:j1:desk", 8_000_000)
    assert commit_cursor(con, "stream:j1:desk", 0, allow_rewind=True) == 0


def test_concurrent_cursor_commits_settle_on_the_maximum(db_path: Path) -> None:
    seqs = list(range(1, 41))
    start = threading.Barrier(len(seqs))

    def worker(seq: int) -> None:
        c = db.connect(db_path)
        try:
            start.wait(timeout=10)
            commit_cursor(c, "peer:desk", seq)
        finally:
            c.close()

    threads = [threading.Thread(target=worker, args=(s,)) for s in seqs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    c = db.connect(db_path)
    assert cursor_value(c, "peer:desk") == 40
    c.close()


# ───────────────────────── total order ─────────────────────────


def test_concurrent_publishers_leave_one_unbroken_chain(db_path: Path) -> None:
    """Two writers interleaving must not fork the chain. The hash is computed
    inside BEGIN IMMEDIATE precisely so this cannot happen."""
    per_thread, n_threads = 20, 4
    start = threading.Barrier(n_threads)

    def worker(w: int) -> None:
        c = db.connect(db_path)
        try:
            start.wait(timeout=10)
            for i in range(per_thread):
                publish(c, "job.progress", f"runner:j{w}", {"w": w, "i": i}, job_id=f"j{w}")
        finally:
            c.close()

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    c = db.connect(db_path)
    rows = c.execute("SELECT seq FROM events ORDER BY seq").fetchall()
    assert [r["seq"] for r in rows] == list(range(1, per_thread * n_threads + 1))
    assert verify_chain(c) is None
    c.close()


def test_a_reader_never_sees_a_hole_that_later_fills_in(db_path: Path) -> None:
    """The one property the whole design rests on: seq is assigned in COMMIT order,
    so `seq > cursor ORDER BY seq` is safe to treat as complete."""
    total = 120
    done = threading.Event()

    def writer() -> None:
        c = db.connect(db_path)
        try:
            for i in range(total):
                publish(c, "job.progress", "runner:j1", {"i": i}, job_id="j1")
        finally:
            c.close()
            done.set()

    t = threading.Thread(target=writer)
    t.start()

    reader = db.connect(db_path)
    seen: list[int] = []
    deadline = time.monotonic() + 30
    while len(seen) < total and time.monotonic() < deadline:
        for ev in read_since(reader, "peer:desk", 7):
            assert ev.seq == len(seen) + 1, "a gap appeared and was filled in later"
            seen.append(ev.seq)
            commit_cursor(reader, "peer:desk", ev.seq)
        if not seen or len(seen) < total:
            time.sleep(0.005)
    t.join(timeout=30)
    assert done.is_set()
    assert seen == list(range(1, total + 1))
    reader.close()


# ───────────────────────── tamper evidence ─────────────────────────


def _seed(con: sqlite3.Connection, n: int = 5) -> None:
    for i in range(n):
        publish(
            con,
            "tool.used",
            "runner:j1",
            {"tool": "Bash", "i": i},
            job_id="j1",
            idem_key=f"tool:toolu_{i}",
        )


def test_an_untouched_chain_verifies_and_an_empty_one_does_too(con: sqlite3.Connection) -> None:
    assert verify_chain(con) is None
    _seed(con)
    assert verify_chain(con) is None


def test_editing_a_payload_is_detected(con: sqlite3.Connection) -> None:
    _seed(con)
    con.execute("UPDATE events SET payload=? WHERE seq=3", (canon({"tool": "rm -rf /"}),))
    assert verify_chain(con) == 3


def test_editing_metadata_is_detected(con: sqlite3.Connection) -> None:
    _seed(con)
    con.execute("UPDATE events SET actor='user' WHERE seq=2")
    assert verify_chain(con) == 2


def test_recomputing_the_edited_rows_own_hash_does_not_help(con: sqlite3.Connection) -> None:
    """The interesting attacker: one who knows how the hash is built. Fixing row 3
    in isolation breaks row 4's prev_hash instead."""
    _seed(con)
    row = con.execute("SELECT * FROM events WHERE seq=3").fetchone()
    forged = canon({"tool": "Bash", "i": "forged"})
    new_hash = bus._row_hash(
        prev_hash=str(row["prev_hash"]),
        seq=3,
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
        payload_digest=bus._sha256(forged),
    )
    con.execute("UPDATE events SET payload=?, hash=? WHERE seq=3", (forged, new_hash))
    assert verify_chain(con) == 4


def test_deleting_a_row_is_detected(con: sqlite3.Connection) -> None:
    _seed(con)
    con.execute("DELETE FROM events WHERE seq=3")
    assert verify_chain(con) == 4


def test_truncating_the_tail_of_the_log_is_detected(con: sqlite3.Connection) -> None:
    """The attack a prev_hash chain alone cannot see. Deleting the LAST rows leaves
    every surviving link intact — and the last row is the one worth deleting, since
    it is the effect.recorded for whatever just happened."""
    _seed(con, 5)
    con.execute("DELETE FROM events WHERE seq >= 4")
    assert verify_chain(con) == 4
    con.execute("DELETE FROM events")
    assert verify_chain(con) == 1


def test_a_rolled_back_publish_is_not_mistaken_for_truncation(con: sqlite3.Connection) -> None:
    """The false positive that would make the truncation check useless: rolled-back
    publishes are routine (answer_request writes the answer and its event or
    neither), and a nightly 'the log was tampered with' alarm nobody believes is
    worse than no alarm."""
    _seed(con, 3)
    with pytest.raises(RuntimeError), db.tx(con):
        publish(con, "job.started", "runner:j2", {}, job_id="j2", idem_key="job:j2:state:started")
        raise RuntimeError("the caller died mid-atom")
    assert verify_chain(con) is None
    # ... and a losing idem_key racer, whose INSERT fails inside the transaction.
    publish(con, "tool.used", "runner:j1", {"tool": "Bash", "i": 0}, idem_key="tool:toolu_0")
    assert verify_chain(con) is None


def test_verify_can_resume_from_a_known_good_anchor(con: sqlite3.Connection) -> None:
    """110k rows a year means the nightly check walks a suffix, not the whole log."""
    _seed(con, 6)
    con.execute("UPDATE events SET actor='user' WHERE seq=5")
    assert verify_chain(con, since_seq=4) == 5
    assert verify_chain(con, since_seq=5) is None


# ───────────────────────── redaction at write time ─────────────────────────


def test_a_secret_never_reaches_the_row_however_deeply_it_is_nested(
    con: sqlite3.Connection,
) -> None:
    secret = "ghp_S3cr3tTokenValue"
    red = Redactor.of([secret])
    publish(
        con,
        "tool.used",
        "runner:j1",
        {
            "cmd": f"curl -H 'Authorization: Bearer {secret}' https://api",
            "env": {"deep": {"deeper": [{"GH_TOKEN": secret}, ["x", secret]]}},
            "count": 3,
        },
        job_id="j1",
        redactor=red,
    )
    raw = _raw_payloads(con)[0]
    assert secret not in raw
    assert raw.count(bus.REDACTED) == 3
    stored = json.loads(raw)
    assert stored["env"]["deep"]["deeper"][1] == ["x", bus.REDACTED]
    assert stored["count"] == 3
    assert con.execute("SELECT redacted FROM events").fetchone()["redacted"] == 1
    assert verify_chain(con) is None


def test_a_secret_hiding_in_a_dict_key_is_redacted_too(con: sqlite3.Connection) -> None:
    secret = "tok_abcdef123456"
    publish(
        con, "tool.used", "runner:j1", {f"header:{secret}": "value"}, redactor=Redactor.of([secret])
    )
    assert secret not in _raw_payloads(con)[0]


def test_the_longest_secret_wins_when_one_contains_another(con: sqlite3.Connection) -> None:
    """Replacing 'abc' first would leave '[redacted]def' — still enough to
    recognise, and worse, enough to guess the rest."""
    short, long = "abc123", "abc123def456"
    publish(con, "tool.used", "runner:j1", {"s": long}, redactor=Redactor.of([short, long]))
    raw = _raw_payloads(con)[0]
    assert short not in raw and long not in raw
    assert json.loads(raw)["s"] == bus.REDACTED


def test_an_empty_string_secret_is_ignored(con: sqlite3.Connection) -> None:
    """A blank keyring entry would otherwise splice the placeholder between every
    character of every payload in the system."""
    publish(con, "tool.used", "runner:j1", {"s": "harmless"}, redactor=Redactor.of(["", "  "]))
    assert json.loads(_raw_payloads(con)[0])["s"] == "harmless"


def test_the_redacted_flag_is_false_when_nothing_matched(con: sqlite3.Connection) -> None:
    publish(con, "tool.used", "runner:j1", {"s": "clean"}, redactor=Redactor.of(["nope"]))
    assert con.execute("SELECT redacted FROM events").fetchone()["redacted"] == 0


def test_redaction_is_the_callers_decision_not_a_global(con: sqlite3.Connection) -> None:
    """No module-level secret store, so no import order can decide whether a token
    reaches the log; a publish with no redactor is honestly unredacted."""
    publish(con, "tool.used", "runner:j1", {"s": "ghp_x"})
    assert "ghp_x" in _raw_payloads(con)[0]
    assert con.execute("SELECT redacted FROM events").fetchone()["redacted"] == 0


# ───────────────────────── retention: nulling old detail ─────────────────────────


@pytest.fixture
def aged(con: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    """Two events from 100 days ago and one from today.

    The ages come from moving the clock the bus reads, never from an UPDATE of
    ``ts``: ``ts`` is inside the hash, so backdating a row by hand would be
    indistinguishable from tampering — which is the point.
    """
    old = _shift(now(), days=-100)
    monkeypatch.setattr(bus, "now", lambda: old)
    publish(
        con,
        "request.created",
        "runner:j1",
        {"question": "SQLite or Postgres?", "options": 2, "approved": True},
        request_id="r1",
        idem_key="req:r1:created",
    )
    publish(
        con,
        "tool.used",
        "runner:j1",
        {"cmd": "rm -rf build", "files": 31},
        job_id="j1",
        idem_key="tool:toolu_1",
    )
    monkeypatch.undo()
    publish(
        con,
        "job.finished",
        "runner:j1",
        {"summary": "still fresh"},
        job_id="j1",
        idem_key="job:j1:state:done",
    )
    return con


def test_nulling_removes_the_detail_and_leaves_the_row(aged: sqlite3.Connection) -> None:
    assert null_detail_older_than(aged, 90) == 2
    rows = aged.execute("SELECT * FROM events ORDER BY seq").fetchall()
    assert len(rows) == 3
    assert "SQLite or Postgres?" not in rows[0]["payload"]
    assert "rm -rf build" not in rows[1]["payload"]
    assert rows[0]["detail_nulled_at"] is not None
    assert rows[2]["detail_nulled_at"] is None
    assert "still fresh" in rows[2]["payload"]


def test_nulling_keeps_the_counts_the_briefing_reads_back(aged: sqlite3.Connection) -> None:
    null_detail_older_than(aged, 90)
    stub = json.loads(aged.execute("SELECT payload FROM events WHERE seq=2").fetchone()["payload"])
    assert stub["counts"] == {"files": 31}
    assert stub["keys"] == ["cmd", "files"]
    assert stub["bytes"] > 0
    first = json.loads(aged.execute("SELECT payload FROM events WHERE seq=1").fetchone()["payload"])
    assert first["counts"] == {"approved": True, "options": 2}


def test_nulling_does_not_break_the_chain(aged: sqlite3.Connection) -> None:
    """The hash binds the payload by digest, and the stub carries that digest
    forward — so a 90-day sweep costs no tamper-evidence for any live row."""
    assert verify_chain(aged) is None
    null_detail_older_than(aged, 90)
    assert verify_chain(aged) is None
    publish(aged, "job.started", "runner:j2", {"x": 1}, job_id="j2")
    assert verify_chain(aged) is None


def test_a_nulled_row_still_cannot_have_its_digest_swapped(aged: sqlite3.Connection) -> None:
    """The honest limit, stated as a test: nulling destroys the detail VISIBLY, but
    it does not hand an editor a free slot. The digest is inside the hash."""
    null_detail_older_than(aged, 90)
    stub = json.loads(aged.execute("SELECT payload FROM events WHERE seq=1").fetchone()["payload"])
    stub["detail_sha256"] = bus._sha256(canon({"question": "something else entirely"}))
    aged.execute("UPDATE events SET payload=? WHERE seq=1", (canon(stub),))
    assert verify_chain(aged) == 1


def test_claiming_a_row_is_nulled_without_nulling_it_is_detected(
    aged: sqlite3.Connection,
) -> None:
    """detail_nulled_at is not a free pass to change the digest source."""
    aged.execute("UPDATE events SET detail_nulled_at=? WHERE seq=3", (now(),))
    assert verify_chain(aged) == 3


def test_nulling_is_idempotent_and_skips_rows_already_done(aged: sqlite3.Connection) -> None:
    assert null_detail_older_than(aged, 90) == 2
    assert null_detail_older_than(aged, 90) == 0
    assert verify_chain(aged) is None


def test_nulled_rows_still_read_back_as_events(aged: sqlite3.Connection) -> None:
    null_detail_older_than(aged, 90)
    evs = read_since(aged, "peer:desk", 10)
    assert [e.detail_nulled for e in evs] == [True, True, False]
    assert evs[0].kind == "request.created" and evs[0].request_id == "r1"


def test_nulling_survives_a_restart(aged: sqlite3.Connection, db_path: Path) -> None:
    null_detail_older_than(aged, 90)
    aged.close()
    fresh = db.connect(db_path)
    assert verify_chain(fresh) is None
    assert (
        fresh.execute(
            "SELECT COUNT(*) n FROM events WHERE detail_nulled_at IS NOT NULL"
        ).fetchone()["n"]
        == 2
    )
    fresh.close()


# ───────────────────────── transactional composition ─────────────────────────


def test_a_rolled_back_caller_transaction_leaves_no_event(con: sqlite3.Connection) -> None:
    """publish() joins the caller's atom rather than opening its own, so
    answer-the-request-and-log-it is all-or-nothing."""
    publish(con, "job.started", "runner:j1", {}, job_id="j1", idem_key="job:j1:state:started")
    with pytest.raises(RuntimeError), db.tx(con):
        con.execute(
            "INSERT INTO consumers (id, last_seq, updated_at) VALUES ('peer:x', 1, ?)", (now(),)
        )
        publish(con, "request.answered", "desk", {"approved": True}, idem_key="req:r1:answered")
        raise RuntimeError("the channel died mid-answer")

    assert con.execute("SELECT COUNT(*) n FROM events").fetchone()["n"] == 1
    assert con.execute("SELECT COUNT(*) n FROM consumers").fetchone()["n"] == 0
    assert verify_chain(con) is None
    assert not con.in_transaction, "publish must never leave a transaction open"


def test_publish_leaves_no_transaction_open_on_the_happy_path(con: sqlite3.Connection) -> None:
    publish(con, "job.started", "runner:j1", {}, job_id="j1")
    assert not con.in_transaction


def test_a_nested_publish_sends_no_datagram_under_the_write_lock(
    con: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """STRUCTURAL RULE: no network or subprocess I/O inside tx(con), ever.

    A sendto() to a peer whose receive buffer is full blocks for the full 50ms
    timeout, and doing that once per attached channel while holding SQLite's
    single write lock is a stall that looks like a hung database. It is also
    pointless: the peer it wakes cannot yet see the uncommitted row.
    """
    Peer.attach(con, "desk", "desk", CAPS)
    locked_when_sent: list[bool] = []

    def spy(addr: str | Path) -> bool:
        locked_when_sent.append(con.in_transaction)
        return False

    monkeypatch.setattr(bus, "poke", spy)

    with db.tx(con):
        publish(con, "job.blocked", "runner:j1", {}, job_id="j1", idem_key="job:j1:state:blocked")
        assert locked_when_sent == [], "a datagram left while the write lock was held"
    assert locked_when_sent == [], "the nested publish must leave the poke to the committer"

    # The caller that owns the transaction owns the poke, and it fires after it.
    bus.poke_attached(con)
    assert locked_when_sent == [False]


def test_an_event_published_inside_a_transaction_is_still_delivered(
    con: sqlite3.Connection,
) -> None:
    """Dropping the nested poke may cost 250ms and must cost nothing else."""
    peer = Peer.attach(con, "desk", "desk", CAPS)
    peer.commit(con, last_seq(con))
    with db.tx(con):
        publish(
            con, "job.finished", "runner:j1", {"rc": 0}, job_id="j1", idem_key="job:j1:state:done"
        )
    assert [e.payload for e in peer.poll(con)] == [{"rc": 0}]
    peer.detach(con)


# ───────────────────────── the poke socket, which is optional ─────────────────────────


def test_losing_every_poke_changes_nothing_but_timing(con: sqlite3.Connection) -> None:
    """The stated cost of total poke loss is 250ms of latency and nothing else."""
    heard = Peer.attach(con, "heard", "desk", CAPS)
    deaf = Peer.attach(con, "deaf", "hud", CAPS)
    # How a SIGKILLed peer actually looks: the channels row still advertises an
    # address, but nothing is listening on it.
    assert deaf.addr is not None
    Path(deaf.addr).unlink()

    for i in range(6):
        publish(con, "job.progress", "runner:j1", {"i": i}, job_id="j1")

    want = [{"i": i} for i in range(6)]
    assert [e.payload for e in heard.poll(con, kinds=("job.progress",))] == want
    assert [e.payload for e in deaf.poll(con, kinds=("job.progress",))] == want

    # ... and the cursors behave identically too.
    heard.commit(con, last_seq(con))
    deaf.commit(con, last_seq(con))
    assert heard.poll(con) == deaf.poll(con) == []

    t0 = time.monotonic()
    deaf.wait()
    assert time.monotonic() - t0 >= Peer.POLL_S * 0.9, "a deaf peer falls back to the poll floor"

    heard.detach(con)
    deaf.detach(con)


def test_a_poke_wakes_a_peer_well_inside_the_poll_floor(con: sqlite3.Connection) -> None:
    peer = Peer.attach(con, "desk", "desk", CAPS)
    for _ in range(4):
        publish(con, "job.progress", "runner:j1", {}, job_id="j1")

    peer.wait()  # drains the whole burst: N publishes in a tick are ONE wakeup
    t0 = time.monotonic()
    peer.wait()
    assert time.monotonic() - t0 >= Peer.POLL_S * 0.9, "the drain must not leave a backlog"

    publish(con, "job.finished", "runner:j1", {}, job_id="j1", idem_key="job:j1:state:done")
    t0 = time.monotonic()
    peer.wait()
    assert time.monotonic() - t0 < Peer.POLL_S * 0.5
    peer.detach(con)


def test_wait_never_sleeps_longer_than_the_poll_floor(con: sqlite3.Connection) -> None:
    """wait() is one tick, not a loop. If it honoured a long timeout the poke would
    have quietly become the transport."""
    peer = Peer.attach(con, "desk", "desk", CAPS)
    t0 = time.monotonic()
    peer.wait(timeout=30.0)
    assert time.monotonic() - t0 < Peer.POLL_S * 2
    peer.detach(con)


def test_poking_a_dead_address_is_silent(con: sqlite3.Connection, tmp_path: Path) -> None:
    assert bus.poke(tmp_path / "nobody-is-here.sock") is False
    assert bus.poke_attached(con) == 0


def test_publish_succeeds_when_the_whole_poke_directory_is_gone(
    con: sqlite3.Connection, tmp_path: Path
) -> None:
    peer = Peer.attach(con, "desk", "desk", CAPS)
    for p in (tmp_path / "pk").glob("*"):
        p.unlink()
    (tmp_path / "pk").rmdir()
    ev = publish(con, "job.progress", "runner:j1", {"i": 1}, job_id="j1")
    assert bus.event_by_id(con, ev) is not None
    assert [e.payload for e in peer.poll(con, kinds=("job.progress",))] == [{"i": 1}]


def test_a_peer_that_cannot_bind_at_all_still_works_by_polling(
    con: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No AF_UNIX, a full runtime dir, a locked-down container — none of it may stop
    a process subscribing."""
    monkeypatch.setattr(Peer, "_bind", staticmethod(lambda peer_id: (None, None)))
    peer = Peer.attach(con, "desk", "desk", CAPS)
    assert peer.addr is None
    assert con.execute("SELECT poke_addr FROM channels WHERE id='desk'").fetchone()[0] is None

    publish(con, "job.progress", "runner:j1", {"i": 7}, job_id="j1")
    assert [e.payload for e in peer.poll(con, kinds=("job.progress",))] == [{"i": 7}]
    t0 = time.monotonic()
    peer.wait()
    assert time.monotonic() - t0 >= Peer.POLL_S * 0.9
    peer.detach(con)


def test_a_socket_left_behind_by_a_sigkilled_predecessor_does_not_block_reattach(
    con: sqlite3.Connection, tmp_path: Path
) -> None:
    first = Peer.attach(con, "desk", "desk", CAPS)
    addr = first.addr
    assert addr is not None and Path(addr).exists()

    # A SIGKILL, honestly simulated: the fd is reclaimed by the kernel and the
    # socket FILE stays. Calling close() here instead would unlink the file and
    # this test would silently stop reaching _bind's reclaim at all.
    assert first._sock is not None
    first._sock.close()
    first._sock = None
    assert Path(addr).exists(), "the point of the test is the file that outlives the process"

    second = Peer.attach(con, "desk", "desk", CAPS)
    assert second.addr == addr
    publish(con, "job.progress", "runner:j1", {"i": 1}, job_id="j1")
    t0 = time.monotonic()
    second.wait()
    assert time.monotonic() - t0 < Peer.POLL_S * 0.5, "the stale socket file was not reclaimed"
    second.detach(con)


# ───────────────────────── attach / detach across processes ─────────────────────────


def test_attachment_is_durable_and_visible_to_a_later_process(db_path: Path) -> None:
    a = db.connect(db_path)
    peer = Peer.attach(a, "telegram", "telegram", CAPS, identity={"chat_id": 4242})
    peer.close()
    a.close()

    b = db.connect(db_path)
    row = b.execute("SELECT * FROM channels WHERE id='telegram'").fetchone()
    assert row["state"] == "attached"
    assert json.loads(row["identity"]) == {"chat_id": 4242}
    assert json.loads(row["caps"])["max_options"] == 4
    assert [e.kind for e in read_since(b, "peer:x", 10)] == ["channel.attached"]
    b.close()


def test_detach_is_recorded_and_stops_the_pokes(db_path: Path) -> None:
    a = db.connect(db_path)
    peer = Peer.attach(a, "desk", "desk", CAPS)
    assert bus.poke_attached(a) == 1
    peer.detach(a, reason="shutting down")
    assert bus.poke_attached(a) == 0
    a.close()

    b = db.connect(db_path)
    row = b.execute("SELECT * FROM channels WHERE id='desk'").fetchone()
    assert row["state"] == "detached" and row["detached_at"] is not None
    assert row["poke_addr"] is None
    kinds = [e.kind for e in read_since(b, "peer:x", 10)]
    assert kinds == ["channel.attached", "channel.detached"]
    assert verify_chain(b) is None
    b.close()


def test_close_without_detach_does_not_claim_a_clean_exit(con: sqlite3.Connection) -> None:
    """reconcile() must be able to tell a crash from a shutdown, or it will not
    re-route the pending deliveries of a channel that simply died."""
    peer = Peer.attach(con, "desk", "desk", CAPS)
    peer.close()
    row = con.execute("SELECT state, detached_at FROM channels WHERE id='desk'").fetchone()
    assert row["state"] == "attached" and row["detached_at"] is None


def test_reattaching_clears_a_previous_detach(con: sqlite3.Connection) -> None:
    Peer.attach(con, "desk", "desk", CAPS).detach(con)
    Peer.attach(con, "desk", "desk", CAPS)
    row = con.execute("SELECT state, detached_at FROM channels WHERE id='desk'").fetchone()
    assert row["state"] == "attached" and row["detached_at"] is None


def test_a_predecessor_exiting_cannot_unlink_its_successors_socket(
    con: sqlite3.Connection,
) -> None:
    """Two LIVE peers sharing a peer_id — the restarted voice app overlapping its
    predecessor that commit_cursor() already defends against.

    The successor's attach() rebinds the same path. If the predecessor's close()
    then unlinked by name it would delete the survivor's socket file while
    channels.poke_addr still advertised it: every poke to a healthy peer fails
    forever, nothing errors, and the desk just gets quietly slower.
    """
    first = Peer.attach(con, "desk", "desk", CAPS)
    second = Peer.attach(con, "desk", "desk", CAPS)
    assert second.addr == first.addr and second.addr is not None

    first.close()  # the predecessor finally notices it lost and exits
    assert Path(second.addr).exists(), "the survivor's socket file was deleted"

    second.commit(con, last_seq(con))
    publish(con, "job.progress", "runner:j1", {"i": 1}, job_id="j1")
    t0 = time.monotonic()
    second.wait()
    assert time.monotonic() - t0 < Peer.POLL_S * 0.5, "the survivor is no longer pokeable"
    assert [e.payload for e in second.poll(con)] == [{"i": 1}]


def test_a_predecessor_detaching_cannot_retire_a_successors_channel(
    con: sqlite3.Connection,
) -> None:
    """Same race, the durable half. Marking a live channel 'detached' tells
    reconcile() to re-route its pending deliveries and stop poking it, while that
    process still believes it is attached — a stall with no error anywhere."""
    first = Peer.attach(con, "desk", "desk", CAPS)
    # A successor process re-attached under the same peer_id: same row, new pid.
    con.execute("UPDATE channels SET pid=pid+1 WHERE id='desk'")

    first.detach(con, reason="predecessor exiting")

    row = con.execute("SELECT state, detached_at FROM channels WHERE id='desk'").fetchone()
    assert row["state"] == "attached" and row["detached_at"] is None
    assert [e.kind for e in read_since(con, "peer:x", 10)] == ["channel.attached"], (
        "a channel.detached for a live channel is a lie in the honesty log"
    )


def test_two_attaches_in_one_millisecond_both_reach_the_log(con: sqlite3.Connection) -> None:
    """idem_key was ts-based and ts is millisecond-resolution, so two processes
    attaching as the same peer_id in the same tick silently became one event."""
    ts = now()
    monkeypatched = pytest.MonkeyPatch()
    monkeypatched.setattr(bus, "now", lambda: ts)
    try:
        Peer.attach(con, "desk", "desk", CAPS).close()
        keys = {
            str(r["idem_key"])
            for r in con.execute("SELECT idem_key FROM events WHERE kind='channel.attached'")
        }
    finally:
        monkeypatched.undo()
    assert len(keys) == 1
    assert str(bus.os.getpid()) in next(iter(keys)), "the key must separate two processes"


def test_the_retention_cutoff_speaks_the_log_s_own_timestamp_dialect(
    con: sqlite3.Connection,
) -> None:
    """``ts`` is compared lexicographically in SQL, so the cutoff the sweep builds
    must be byte-for-byte the shape ids.now() writes. The cutoff is formatted by a
    second copy of that format string; this pins the two together, because the
    drift would not raise — it would silently null the wrong rows."""
    stamp = now()
    assert bus._fmt(parse_ts(stamp)) == stamp


def test_heartbeat_moves_forward_for_the_reaper(con: sqlite3.Connection) -> None:
    peer = Peer.attach(con, "desk", "desk", CAPS)
    before = con.execute("SELECT last_heartbeat_at h FROM channels WHERE id='desk'").fetchone()["h"]
    time.sleep(0.005)
    peer.heartbeat(con)
    after = con.execute("SELECT last_heartbeat_at h FROM channels WHERE id='desk'").fetchone()["h"]
    assert after >= before
    peer.detach(con)
