"""The undo ledger, tested where it breaks: races, restarts, and lies.

Three kinds of test here and nothing else.

RACES use two real connections to one file. SQLite's locking is per-connection,
not per-process, so two connections racing on one file exercise exactly what two
daemons would. Where a compensation could run twice, both racers are actually
started and the handler counts its own invocations.

RESTARTS write on one connection, close it, and assert from another — because
the entire reason ``undo_plan`` is JSON and not a closure is that the process
which promised the undo is usually dead by the time anyone asks for it.

HONESTY tests are the point of the module. They are written so that a future
edit which makes the wording promise more than the class allows FAILS THE SUITE:
the promise vocabulary is asserted against every reachable verdict, the verdict
is asserted to be the LAST thing in the sentence, and the templates themselves
are swapped for over-promising ones to prove the guard fires rather than merely
existing.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from jarvis import effects as fx
from jarvis.db import connect, migrate
from jarvis.ids import now

JOB = "job_effecttests"

# ───────────────────────────── fixtures ─────────────────────────────


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """A throwaway database file. Never the real one at ~/.local/state."""
    p = tmp_path / "jarvis.db"
    c = connect(p)
    migrate(c)
    # effects.job_id is a real FK and foreign_keys is ON, so a job must exist.
    c.execute(
        """INSERT INTO jobs (id, kind, title, state, created_at, updated_at, created_by)
           VALUES (?, 'claude_code', 'the todo app build', 'running', ?, ?, 'test')""",
        (JOB, now(), now()),
    )
    c.close()
    return p


@pytest.fixture
def con(db_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(db_path)
    yield c
    c.close()


@pytest.fixture
def other(db_path: Path) -> Iterator[sqlite3.Connection]:
    """A SECOND connection to the same file — the phone, or a resumed driver."""
    c = connect(db_path)
    yield c
    c.close()


@pytest.fixture(autouse=True)
def clean_registry() -> Iterator[None]:
    """The handler table is process-global by design; don't let tests leak into it.

    Reaching into the private dict is deliberate: the registry has no public
    removal API precisely because production code should only ever add to it at
    import time.
    """
    saved = dict(fx._HANDLERS)
    yield
    fx._HANDLERS.clear()
    fx._HANDLERS.update(saved)


# ───────────────────────────── helpers ─────────────────────────────


def an_edit(con: sqlite3.Connection, **kw: Any) -> fx.Effect:
    """A reversible effect with a real plan: the ordinary case."""
    return fx.record_effect(
        con,
        kind="fs.edit",
        summary="I edited main.py",
        reversibility="reversible",
        job_id=JOB,
        undo_plan={"op": "fs.restore", "args": {"path": "main.py", "previous": "old bytes"}},
        **kw,
    )


def a_telegram_send(con: sqlite3.Connection, *, deadline_s: float) -> fx.Effect:
    """A compensatable effect with a window — the 48h Telegram delete."""
    return fx.record_effect(
        con,
        kind="telegram.send_document",
        summary="I sent the screenshot to Telegram",
        reversibility="compensatable",
        job_id=JOB,
        undo_plan={
            "op": "telegram.delete",
            "args": {"chat_id": 1, "message_id": 7},
            "speaks": "delete the message from the chat",
        },
        undo_deadline=fx.deadline_in(deadline_s),
    )


def count_effects(con: sqlite3.Connection) -> int:
    return int(con.execute("SELECT count(*) FROM effects").fetchone()[0])


def event_kinds(con: sqlite3.Connection, effect_id: str) -> list[str]:
    return [
        str(r["kind"])
        for r in con.execute("SELECT kind FROM events WHERE effect_id=? ORDER BY seq", (effect_id,))
    ]


# ───────────────────────── the plan is data, not a closure ─────────────────────────


def test_a_closure_can_never_be_stored_as_a_plan(con: sqlite3.Connection) -> None:
    """The whole reason the reference build's undo is useless, refused at write time."""
    with pytest.raises(fx.NotDeclarative):
        fx.record_effect(
            con,
            kind="fs.edit",
            summary="I edited main.py",
            reversibility="reversible",
            undo_plan=lambda: "put it back",  # type: ignore[arg-type]
        )
    # ...and nested, which is the version that actually gets written by accident.
    with pytest.raises(fx.NotDeclarative):
        fx.record_effect(
            con,
            kind="fs.edit",
            summary="I edited main.py",
            reversibility="reversible",
            undo_plan={"op": "fs.restore", "args": {"restore": lambda: None}},
        )
    assert count_effects(con) == 0


def test_a_plan_that_will_not_serialise_is_refused(con: sqlite3.Connection) -> None:
    """A Path, a datetime, an open connection — anything a future process cannot read."""
    for bad_args in ({"path": Path("/tmp/x")}, {"con": con}, {"when": object()}):
        with pytest.raises(fx.NotDeclarative):
            fx.record_effect(
                con,
                kind="fs.edit",
                summary="I edited main.py",
                reversibility="reversible",
                undo_plan={"op": "fs.restore", "args": bad_args},
            )
    assert count_effects(con) == 0


def test_a_plan_needs_an_op_and_declared_keys(con: sqlite3.Connection) -> None:
    for bad in (
        {"args": {}},
        {"op": "", "args": {}},
        {"op": "fs.restore", "args": []},
        {"op": "fs.restore", "args": {}, "callback": "run"},
    ):
        with pytest.raises(fx.NotDeclarative):
            fx.record_effect(
                con,
                kind="fs.edit",
                summary="I edited main.py",
                reversibility="reversible",
                undo_plan=bad,  # type: ignore[arg-type]
            )


def test_an_irreversible_effect_may_not_carry_a_plan(con: sqlite3.Connection) -> None:
    """A stored plan for something that cannot be undone is a promise waiting to be spoken."""
    with pytest.raises(ValueError, match="irreversible"):
        fx.record_effect(
            con,
            kind="github.repo_create",
            summary="I created the repo",
            reversibility="irreversible",
            undo_plan={"op": "github.repo_delete", "args": {"full_name": "a/b"}},
        )


def test_a_deadline_with_no_plan_is_refused(con: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="undo_deadline"):
        fx.record_effect(
            con,
            kind="telegram.send",
            summary="I sent a message",
            reversibility="compensatable",
            undo_deadline=fx.deadline_in(60),
        )


def test_a_deadline_in_the_wrong_shape_is_refused(con: sqlite3.Connection) -> None:
    """Deadlines are compared as STRINGS, so the format is load-bearing.

    ``...:00Z`` and ``...:00.000Z`` are the same instant and sort in OPPOSITE
    directions, so a deadline in the wrong shape would silently never expire.
    The assertion below is the footgun itself, spelled out.
    """
    assert "2026-09-16T12:00:00Z" > "2026-09-16T12:00:00.000Z"
    for bad in ("2026-09-16T12:00:00Z", "2026-09-16", "tomorrow", ""):
        with pytest.raises(ValueError, match="undo_deadline"):
            fx.record_effect(
                con,
                kind="telegram.send",
                summary="I sent a message",
                reversibility="compensatable",
                undo_plan={"op": "telegram.delete", "args": {}},
                undo_deadline=bad,
            )


# ───────────────────────── classification happens first ─────────────────────────


def test_an_unclassified_kind_cannot_be_acted_on() -> None:
    """Acting first and learning the blast radius afterwards has no spelling here."""
    with pytest.raises(KeyError):
        fx.reversibility_of("stripe.charge")
    assert fx.reversibility_of("stripe.charge", default="irreversible") == "irreversible"


def test_the_class_may_be_pessimistic_but_never_optimistic(con: sqlite3.Connection) -> None:
    """An fs.edit with no captured bytes really is worse than reversible; a repo create
    is never better than irreversible."""
    worse = fx.record_effect(
        con,
        kind="fs.edit",
        summary="I edited main.py without capturing the old bytes",
        reversibility="irreversible",
    )
    assert worse.reversibility == "irreversible"

    for lie in ("reversible", "compensatable"):
        with pytest.raises(ValueError, match="github.repo_create"):
            fx.record_effect(
                con,
                kind="github.repo_create",
                summary="I created the repo",
                reversibility=lie,  # type: ignore[arg-type]
            )


def test_the_github_reconciliation_is_data_not_memory() -> None:
    """The token has no delete_repo, so the class and the gate follow from it."""
    assert fx.reversibility_of("github.repo_create") == "irreversible"
    assert fx.confirm_strength_for_kind("github.repo_create") == "confirm_readback"
    assert fx.CONFIRM_STRENGTH == {
        "reversible": "notify",
        "compensatable": "confirm",
        "irreversible": "confirm_readback",
    }


def test_a_repo_jarvis_creates_must_be_harmless_if_wrong(con: sqlite3.Connection) -> None:
    """Cannot be undone, so the harm is engineered down instead: private and empty."""
    for private, empty in ((False, True), (True, False), (False, False)):
        with pytest.raises(ValueError, match="delete_repo"):
            fx.github_repo_create_effect(
                con, full_name="Efkrdnz/todo", private=private, empty=empty, job_id=JOB
            )
    e = fx.github_repo_create_effect(
        con, full_name="Efkrdnz/todo", private=True, empty=True, job_id=JOB
    )
    assert e.reversibility == "irreversible"
    assert e.undo_plan is None
    assert e.confirm_strength == "confirm_readback"


# ───────────────────────────── the spoken line ─────────────────────────────


def _one_of_each(con: sqlite3.Connection) -> list[fx.Effect]:
    """Every reachable (class, situation) pair, as real rows."""
    rows = [
        an_edit(con),  # reversible, actionable
        a_telegram_send(con, deadline_s=fx.TELEGRAM_DELETE_WINDOW_S),  # compensatable + window
        fx.record_effect(  # compensatable, no window
            con,
            kind="git.push",
            summary="I pushed to main",
            reversibility="compensatable",
            undo_plan={
                "op": "git.revert",
                "args": {"sha": "abc"},
                "speaks": "push a revert commit",
            },
        ),
        fx.record_effect(  # compensatable, NO plan at all
            con, kind="git.push", summary="I pushed to main", reversibility="compensatable"
        ),
        fx.record_effect(  # reversible, NO plan at all
            con, kind="git.commit", summary="I committed the change", reversibility="reversible"
        ),
        fx.record_effect(
            con, kind="phone.call", summary="I called the restaurant", reversibility="irreversible"
        ),
        fx.github_repo_create_effect(con, full_name="Efkrdnz/todo", private=True, empty=True),
        a_telegram_send(con, deadline_s=-3600),  # window already closed
    ]
    # ...and each terminal state, so no state can reach a line nobody checked.
    for state in ("undone", "undo_failed", "expired"):
        e = an_edit(con)
        con.execute("UPDATE effects SET state=?, undone_at=? WHERE id=?", (state, now(), e.id))
        rows.append(fx.get_effect(con, e.id))  # type: ignore[arg-type]
    return rows


def test_no_verdict_ever_promises_more_than_its_class_allows(con: sqlite3.Connection) -> None:
    """The honesty requirement, made mechanical over every reachable row shape.

    A verdict that cannot act may contain no promise vocabulary at all and must
    contain something a user would hear as a refusal. A compensatable verdict
    may never claim exact restoration. Generating the line runs the same check
    internally, so this also asserts the guard is actually wired in.
    """
    for e in _one_of_each(con):
        verdict = fx.undo_verdict(e).lower()
        situation = fx._situation(e, now())
        if situation in fx._CANNOT_ACT:
            assert not [w for w in fx.PROMISE_WORDS if w in verdict], (e.kind, situation, verdict)
            assert any(m in verdict for m in fx.REFUSAL_MARKERS), (e.kind, situation, verdict)
        if situation == fx._ACTIONABLE and e.reversibility == "compensatable":
            assert not [w for w in fx.EXACT_WORDS if w in verdict], (e.kind, verdict)
            assert any(m in verdict for m in fx.REFUSAL_MARKERS), (e.kind, verdict)


def test_the_verdict_is_the_last_thing_said(con: sqlite3.Connection) -> None:
    """Nothing may be appended after the part that says what is and is not possible."""
    for e in _one_of_each(con):
        line = fx.spoken_effect_line(e)
        assert line.endswith(fx.undo_verdict(e)), line
        assert line.startswith(e.summary.rstrip(".")), line


def test_an_irreversible_effect_says_plainly_that_nothing_can_be_done(
    con: sqlite3.Connection,
) -> None:
    """The doc's requirement, verbatim: Jarvis says "I can't", it does not pretend."""
    repo = fx.github_repo_create_effect(
        con, full_name="Efkrdnz/todo-app", private=True, empty=True, job_id=JOB
    )
    line = fx.spoken_effect_line(repo)
    assert "nothing I can do" in line
    assert "delete_repo" in line  # the REASON is spoken, not just a flat no
    assert not [w for w in fx.PROMISE_WORDS if w in fx.undo_verdict(repo).lower()]

    call = fx.record_effect(
        con, kind="phone.call", summary="I called the restaurant", reversibility="irreversible"
    )
    assert "nothing I can do" in fx.spoken_effect_line(call)


def test_a_compensatable_effect_offers_the_compensation_and_not_a_restoration(
    con: sqlite3.Connection,
) -> None:
    e = a_telegram_send(con, deadline_s=fx.TELEGRAM_DELETE_WINDOW_S)
    line = fx.spoken_effect_line(e)
    assert "delete the message from the chat" in line
    assert "can't reverse" in line
    assert "exactly" not in line


def test_an_expired_window_stops_offering_the_compensation(con: sqlite3.Connection) -> None:
    """The offer must disappear the moment the window shuts, not when the API says no."""
    e = a_telegram_send(con, deadline_s=-3600)
    line = fx.spoken_effect_line(e)
    assert "delete the message from the chat" not in line
    assert "too late" in line
    assert fx.undo_window_remaining_s(e) is not None
    assert fx.undo_window_remaining_s(e) < 0


def test_a_reversible_effect_with_no_stored_plan_does_not_promise(con: sqlite3.Connection) -> None:
    """Recorded honestly rather than refused — but it must not offer what it cannot do."""
    e = fx.record_effect(
        con, kind="git.commit", summary="I committed the change", reversibility="reversible"
    )
    line = fx.spoken_effect_line(e)
    assert "no plan was stored" in line
    assert "exactly" not in line


def test_an_over_promising_template_fails_loudly(
    con: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE regression guard: a future edit that over-promises cannot ship.

    Both directions are covered — adding a promise, and quietly deleting the
    refusal so the sentence merely trails off.
    """
    e = fx.record_effect(
        con, kind="phone.call", summary="I called the restaurant", reversibility="irreversible"
    )
    templates = dict(fx._VERDICTS)

    monkeypatch.setattr(fx, "_VERDICTS", templates)
    for key in ("irreversible", "irreversible_because"):
        templates[key] = "I'll see what I can do — I can probably undo that."
    with pytest.raises(fx.OverPromise, match="promises"):
        fx.spoken_effect_line(e)

    for key in ("irreversible", "irreversible_because"):
        templates[key] = "That one is done."
    with pytest.raises(fx.OverPromise, match="refusal"):
        fx.spoken_effect_line(e)


def test_a_plan_whose_op_would_over_promise_must_name_its_own_phrase(
    con: sqlite3.Connection,
) -> None:
    """A row that is accepted at write time but can NEVER be spoken is the worst shape.

    With no ``speaks`` the verdict falls back to a phrase built from the op — and
    the op is caller text, so it can carry promise vocabulary straight into the
    sentence the honesty check then rejects. That combination wrote a row happily
    and raised OverPromise on every later attempt to say it, including from
    ``undo`` itself. It has to fail at WRITE time, where the author is standing.
    """
    plan: dict[str, Any] = {"op": "fs.restore", "args": {"path": "main.py"}}
    with pytest.raises(fx.OverPromise, match="speaks"):
        fx.record_effect(
            con,
            kind="fs.edit",
            summary="I edited main.py",
            reversibility="compensatable",  # pessimistic, which record_effect allows
            undo_plan=plan,
        )
    assert count_effects(con) == 0

    # Naming the phrase is all it takes, and then the row really is speakable.
    e = fx.record_effect(
        con,
        kind="fs.edit",
        summary="I edited main.py",
        reversibility="compensatable",
        undo_plan={**plan, "speaks": "write the previous contents to a .bak beside it"},
    )
    assert "write the previous contents" in fx.spoken_effect_line(e)

    # A reversible effect never speaks the fallback, so the same op is fine there.
    assert fx.record_effect(
        con,
        kind="fs.edit",
        summary="I edited other.py",
        reversibility="reversible",
        undo_plan=plan,
    )


def test_a_compensation_phrase_may_not_claim_restoration(con: sqlite3.Connection) -> None:
    """Checked at WRITE time, so the lie lands in the stack of whoever wrote it."""
    for lie in ("put it back exactly", "restore the previous message", "leave no trace"):
        with pytest.raises(fx.OverPromise):
            fx.record_effect(
                con,
                kind="telegram.send",
                summary="I sent a message",
                reversibility="compensatable",
                undo_plan={"op": "telegram.delete", "args": {"message_id": 1}, "speaks": lie},
            )
    assert count_effects(con) == 0


# ───────────────────────────── restart survival ─────────────────────────────


def test_the_plan_outlives_the_process_that_wrote_it(db_path: Path) -> None:
    """The entire argument for declarative JSON, as a test.

    The writer is gone. A brand-new process reads the plan off the row, looks up
    the handler by name, and compensates. A closure could not have crossed this
    line, which is why the reference build's undo does not work.
    """
    writer = connect(db_path)
    effect_id = an_edit(writer).id
    writer.close()

    seen: dict[str, Any] = {}

    reader = connect(db_path)

    @fx.undo_handler("fs.restore")
    def restore(c: sqlite3.Connection, e: fx.Effect, args: dict[str, Any]) -> fx.Compensation:
        seen.update(args)
        return fx.Compensation(
            kind="fs.edit",
            summary=f"I put {args['path']} back",
            reversibility="reversible",
        )

    result = fx.undo(reader, effect_id)
    assert result.outcome == "undone"
    assert seen == {"path": "main.py", "previous": "old bytes"}

    # And the link is durable too: a third connection can walk it.
    reader.close()
    third = connect(db_path)
    row = third.execute(
        """SELECT e.state, e.undo_effect_id, c.summary AS comp_summary
             FROM effects e JOIN effects c ON c.id = e.undo_effect_id WHERE e.id=?""",
        (effect_id,),
    ).fetchone()
    assert row["state"] == "undone"
    assert row["comp_summary"] == "I put main.py back"
    third.close()


def test_a_process_without_the_handler_says_so_instead_of_faking_it(db_path: Path) -> None:
    """``no_handler`` is a real outcome, and it must stay retryable.

    A process that never imported the handler package has not proved the undo is
    impossible — only that it cannot do it. The row records the attempt, keeps
    the plan, and a later process with the import finishes the job.
    """
    first = connect(db_path)
    effect_id = an_edit(first).id

    result = fx.undo(first, effect_id)
    assert result.outcome == "no_handler"
    stored = fx.get_effect(first, effect_id)
    assert stored is not None
    assert stored.state == "undo_failed"
    assert "fs.restore" in (stored.undo_error or "")
    assert stored.undo_effect_id is None
    assert stored.undone_at is None  # the claim was released, so a retry can take it
    assert stored.undo_plan == {
        "op": "fs.restore",
        "args": {"path": "main.py", "previous": "old bytes"},
    }
    first.close()

    second = connect(db_path)

    @fx.undo_handler("fs.restore")
    def restore(c: sqlite3.Connection, e: fx.Effect, args: dict[str, Any]) -> fx.Compensation:
        return fx.Compensation(
            kind="fs.edit", summary="I put main.py back", reversibility="reversible"
        )

    assert fx.undo(second, effect_id).outcome == "undone"
    finished = fx.get_effect(second, effect_id)
    assert finished is not None and finished.state == "undone"
    second.close()


# ───────────────────────────── races ─────────────────────────────


def test_two_processes_racing_to_undo_compensate_exactly_once(db_path: Path) -> None:
    """The one that matters: a Telegram delete run twice is noise, a revert
    commit pushed twice is a mess. Both racers are really started."""
    writer = connect(db_path)
    effect_id = an_edit(writer).id
    writer.close()

    started = threading.Event()
    release = threading.Event()
    calls: list[str] = []
    lock = threading.Lock()

    @fx.undo_handler("fs.restore")
    def restore(c: sqlite3.Connection, e: fx.Effect, args: dict[str, Any]) -> fx.Compensation:
        with lock:
            calls.append(e.id)
        started.set()
        release.wait(5)
        return fx.Compensation(
            kind="fs.edit", summary="I put main.py back", reversibility="reversible"
        )

    outcomes: list[str] = []

    def racer() -> None:
        c = connect(db_path)
        try:
            outcomes.append(fx.undo(c, effect_id).outcome)
        finally:
            c.close()

    a = threading.Thread(target=racer)
    a.start()
    assert started.wait(5), "the first racer never reached the handler"

    # While the handler is on the "network", the database must still be writable
    # by anyone else. If this blocks, someone put I/O inside a transaction.
    bystander = connect(db_path)
    bystander.execute(
        "INSERT INTO cursors (name, value, updated_at) VALUES ('probe','1',?)", (now(),)
    )
    assert count_effects(bystander) == 1  # no compensation row yet either
    bystander.close()

    b = threading.Thread(target=racer)
    b.start()
    b.join(5)
    release.set()
    a.join(5)

    assert sorted(outcomes) == ["busy", "undone"]
    assert len(calls) == 1, "the compensation ran twice"

    final = connect(db_path)
    assert count_effects(final) == 2  # the effect and exactly one compensation
    row = fx.get_effect(final, effect_id)
    assert row is not None and row.state == "undone" and row.undo_effect_id is not None
    final.close()


def test_a_compensation_is_never_recorded_without_its_link(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    """If the lease is stolen while the handler is out, the whole atom rolls back.

    An unlinked compensation row is worse than none: the ledger would claim
    something was done with nothing saying what it undid.
    """
    e = an_edit(con)

    @fx.undo_handler("fs.restore")
    def restore(c: sqlite3.Connection, eff: fx.Effect, args: dict[str, Any]) -> fx.Compensation:
        # Another process terminates the row mid-flight.
        other.execute("UPDATE effects SET state='expired', undone_at=NULL WHERE id=?", (eff.id,))
        return fx.Compensation(
            kind="fs.edit", summary="I put main.py back", reversibility="reversible"
        )

    result = fx.undo(con, e.id)
    assert result.outcome == "lost_claim"
    assert not result.ok
    assert count_effects(con) == 1  # no orphan compensation row


def test_a_lost_claim_still_says_the_compensation_happened(
    db_path: Path, con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    """The hole a rollback opens: the row is gone, so the EVENT has to carry it.

    Reachable with NO hand-written SQL. ``release_undo_claim`` cannot tell a hung
    handler from a dead process, so a human clearing what looks like a stale
    claim can pull the lease out from under a compensation that then succeeds.
    The Telegram message really is deleted; if nothing records that, the next ask
    deletes a second one — the double-dial hazard, through this module's own API.
    """
    e = a_telegram_send(con, deadline_s=fx.TELEGRAM_DELETE_WINDOW_S)

    @fx.undo_handler("telegram.delete")
    def delete(c: sqlite3.Connection, eff: fx.Effect, args: dict[str, Any]) -> fx.Compensation:
        fx.release_undo_claim(other, eff.id, reason="looked hung, cleared it by hand")
        return fx.Compensation(
            kind="telegram.delete",
            summary="I deleted the screenshot from the chat",
            reversibility="irreversible",
        )

    result = fx.undo(con, e.id)
    assert result.outcome == "lost_claim"
    assert count_effects(con) == 1

    payload = con.execute(
        """SELECT payload FROM events
            WHERE kind='effect.undo_failed' AND effect_id=? ORDER BY seq DESC LIMIT 1""",
        (e.id,),
    ).fetchone()["payload"]
    assert "I deleted the screenshot from the chat" in payload, (
        "the compensation happened in the world; if the ledger cannot say so, "
        "the next ask does it twice"
    )
    # And the sentence must not offer to do again the thing already done.
    assert "delete the message from the chat" not in result.spoken
    assert "couldn't record it" in result.spoken


def test_two_sweepers_expire_each_window_once(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    ids = {a_telegram_send(con, deadline_s=-60).id for _ in range(5)}
    swept = fx.expire_windows(con) + fx.expire_windows(other)
    assert sorted(swept) == sorted(ids), "one sweeper must claim each row, and only one"
    states = []
    for i in ids:
        row = fx.get_effect(con, i)
        assert row is not None
        states.append(row.state)
    assert set(states) == {"expired"}


def test_a_sweep_cannot_close_a_window_underneath_a_running_handler(db_path: Path) -> None:
    """Expiring a claimed row would make a live compensation look like a lie."""
    writer = connect(db_path)
    effect_id = a_telegram_send(writer, deadline_s=fx.TELEGRAM_DELETE_WINDOW_S).id
    writer.close()

    started, release = threading.Event(), threading.Event()

    @fx.undo_handler("telegram.delete")
    def delete(c: sqlite3.Connection, e: fx.Effect, args: dict[str, Any]) -> fx.Compensation:
        started.set()
        release.wait(5)
        return fx.Compensation(
            kind="telegram.delete", summary="I deleted the message", reversibility="irreversible"
        )

    outcomes: list[str] = []

    def racer() -> None:
        c = connect(db_path)
        try:
            outcomes.append(fx.undo(c, effect_id).outcome)
        finally:
            c.close()

    t = threading.Thread(target=racer)
    t.start()
    assert started.wait(5)

    sweeper = connect(db_path)
    # The sweeper's clock says the window shut while the handler was out.
    assert fx.expire_windows(sweeper, now_ts=fx.deadline_in(fx.TELEGRAM_DELETE_WINDOW_S * 2)) == []
    sweeper.close()

    release.set()
    t.join(5)
    assert outcomes == ["undone"]


# ───────────────────────────── failure paths ─────────────────────────────


def test_an_expired_window_is_reported_not_attempted(con: sqlite3.Connection) -> None:
    """The requirement, exactly: expired, NOT "tried and the API said no".

    Calling Telegram at 48h + 1s and relaying the error is a different and less
    honest sentence, and it costs a round trip to produce.
    """
    calls: list[str] = []

    @fx.undo_handler("telegram.delete")
    def delete(c: sqlite3.Connection, e: fx.Effect, args: dict[str, Any]) -> fx.Compensation:
        calls.append(e.id)
        raise AssertionError("the handler must not run once the window has closed")

    e = a_telegram_send(con, deadline_s=-1)
    result = fx.undo(con, e.id)

    assert result.outcome == "expired"
    assert calls == []
    after = fx.get_effect(con, e.id)
    assert after is not None
    assert after.state == "expired"
    assert after.undo_effect_id is None
    assert "too late" in result.spoken
    assert count_effects(con) == 1


def test_undoing_an_irreversible_effect_changes_nothing(con: sqlite3.Connection) -> None:
    """Not "failed": we never tried, and a ledger that says we tried is lying."""
    e = fx.record_effect(
        con, kind="phone.call", summary="I called the restaurant", reversibility="irreversible"
    )
    result = fx.undo(con, e.id)

    assert result.outcome == "irreversible"
    assert not result.ok
    after = fx.get_effect(con, e.id)
    assert after is not None
    assert (after.state, after.undone_at, after.undo_error) == ("applied", None, None)
    assert "nothing I can do" in result.spoken
    # The refusal is still logged: "he asked me to undo the call" is history.
    assert "effect.undo_failed" in event_kinds(con, e.id)


def test_asking_to_undo_a_plan_less_effect_does_not_make_the_ledger_claim_it_tried(
    con: sqlite3.Connection,
) -> None:
    """Same rule as the irreversible branch: we never tried, so the row must not say we did.

    Recording this as ``undo_failed`` also destroyed the honest standing verdict
    PERMANENTLY — a plan is written once at record time and never added later, so
    "no plan was stored, so there is nothing I can act on" became "I already
    tried and it did not work" for the rest of that row's life, on one ask.
    """
    e = fx.record_effect(
        con, kind="git.commit", summary="I committed the change", reversibility="reversible"
    )
    before = fx.spoken_effect_line(e)
    assert "no plan was stored" in before

    result = fx.undo(con, e.id)
    assert result.outcome == "no_plan"
    assert "no plan was stored" in result.spoken
    assert "already tried" not in result.spoken

    after = fx.get_effect(con, e.id)
    assert after is not None
    assert (after.state, after.undone_at, after.undo_error) == ("applied", None, None)
    assert fx.spoken_effect_line(after) == before, "one ask must not rewrite the verdict"
    # The ask itself is still history — it just belongs on the bus, not on the row.
    assert "effect.undo_failed" in event_kinds(con, e.id)


def test_a_spoken_time_is_the_time_the_user_is_living_in(
    con: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The column is UTC by construction; the user is not.

    Slicing HH:MM off the stored string says 11:05 for a 14:05 event. jarvis.clock
    is the one place the conversion is allowed to happen, so it has to be the one
    doing it here.
    """
    monkeypatch.setenv("JARVIS_TZ", "Europe/Istanbul")  # not the ambient zone of the CI box
    e = an_edit(con)
    con.execute(
        "UPDATE effects SET state='undone', undone_at=? WHERE id=?",
        ("2026-09-16T11:05:00.000Z", e.id),
    )
    done = fx.get_effect(con, e.id)
    assert done is not None
    assert "14:05" in fx.spoken_effect_line(done)  # Europe/Istanbul is +03
    assert "11:05" not in fx.spoken_effect_line(done)

    # A column that will not parse must not break a sentence: this is the
    # speaking path, and refusing to talk is not an available failure mode.
    con.execute("UPDATE effects SET undone_at='not a timestamp' WHERE id=?", (e.id,))
    broken = fx.get_effect(con, e.id)
    assert broken is not None
    assert fx.spoken_effect_line(broken)


def test_a_handler_that_raises_leaves_a_retryable_row(con: sqlite3.Connection) -> None:
    attempts: list[int] = []

    @fx.undo_handler("fs.restore")
    def restore(c: sqlite3.Connection, e: fx.Effect, args: dict[str, Any]) -> fx.Compensation:
        attempts.append(1)
        if len(attempts) == 1:
            raise TimeoutError("github unreachable")
        return fx.Compensation(
            kind="fs.edit", summary="I put main.py back", reversibility="reversible"
        )

    e = an_edit(con)
    first = fx.undo(con, e.id)
    assert first.outcome == "failed"
    assert first.error == "github unreachable"

    mid = fx.get_effect(con, e.id)
    assert mid is not None
    assert mid.state == "undo_failed"
    assert "TimeoutError" in (mid.undo_error or "")
    assert mid.undone_at is None, "a failed attempt must release its claim"
    assert "nothing has changed" in first.spoken
    assert count_effects(con) == 1  # no half-written compensation

    second = fx.undo(con, e.id)
    assert second.outcome == "undone"
    assert len(attempts) == 2
    done = fx.get_effect(con, e.id)
    assert done is not None and done.undo_error is None


def test_a_handler_returning_junk_is_a_failure_not_a_success(con: sqlite3.Connection) -> None:
    @fx.undo_handler("fs.restore")
    def restore(c: sqlite3.Connection, e: fx.Effect, args: dict[str, Any]) -> Any:
        return "put it back, honest"

    e = an_edit(con)
    assert fx.undo(con, e.id).outcome == "failed"
    after = fx.get_effect(con, e.id)
    assert after is not None and after.state == "undo_failed"
    assert count_effects(con) == 1


def test_undoing_twice_compensates_once(con: sqlite3.Connection) -> None:
    calls: list[str] = []

    @fx.undo_handler("fs.restore")
    def restore(c: sqlite3.Connection, e: fx.Effect, args: dict[str, Any]) -> fx.Compensation:
        calls.append(e.id)
        return fx.Compensation(
            kind="fs.edit", summary="I put main.py back", reversibility="reversible"
        )

    e = an_edit(con)
    first = fx.undo(con, e.id)
    second = fx.undo(con, e.id)

    assert (first.outcome, second.outcome) == ("undone", "already_undone")
    assert second.undo_effect_id == first.undo_effect_id
    assert len(calls) == 1
    assert count_effects(con) == 2


def test_undoing_an_unknown_effect_raises_rather_than_shrugging(con: sqlite3.Connection) -> None:
    with pytest.raises(fx.UnknownEffect):
        fx.undo(con, "eff_doesnotexist")


def test_a_claim_left_by_a_dead_process_is_reported_not_auto_retried(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    """A compensation that may have half-happened is the double-dial hazard.

    So a crashed undo leaves a claim that a HUMAN releases, with a reason, and
    the reason lands in the ledger.
    """
    e = an_edit(con)
    assert fx._claim(con, e.id, fx.deadline_in(-3600))  # a process that then died

    stuck = fx.stale_undo_claims(other, older_than_s=60)
    assert [s.id for s in stuck] == [e.id]
    assert stuck[0].undo_claimed

    # Nothing may quietly pick it up in the meantime.
    assert fx.undo(other, e.id).outcome == "busy"
    assert fx.expire_windows(other, now_ts=fx.deadline_in(86400)) == []

    with pytest.raises(ValueError, match="reason"):
        fx.release_undo_claim(other, e.id, reason="  ")
    assert fx.release_undo_claim(other, e.id, reason="checked the repo by hand, nothing changed")
    assert not fx.release_undo_claim(other, e.id, reason="again")  # no claim left to release

    released = fx.get_effect(con, e.id)
    assert released is not None
    assert released.undone_at is None
    assert "checked the repo by hand" in (released.undo_error or "")


def test_a_compensation_is_never_recorded_more_optimistically_than_its_kind(
    con: sqlite3.Connection,
) -> None:
    """The compensation already happened, so it is widened, never refused.

    Losing the row that says what was done would be worse than one extra
    confirmation later.
    """

    @fx.undo_handler("fs.restore")
    def restore(c: sqlite3.Connection, e: fx.Effect, args: dict[str, Any]) -> fx.Compensation:
        return fx.Compensation(
            kind="github.repo_create",  # classified irreversible
            summary="I created a replacement repo",
            reversibility="reversible",  # the handler's optimistic claim
        )

    e = an_edit(con)
    result = fx.undo(con, e.id)
    assert result.outcome == "undone"
    comp = fx.get_effect(con, result.undo_effect_id or "")
    assert comp is not None
    assert comp.reversibility == "irreversible"


def test_a_handler_cannot_put_a_class_in_the_ledger_that_is_not_a_class(
    con: sqlite3.Connection,
) -> None:
    """A durable row whose class is nonsense breaks the GATE, not just the wording.

    ``confirm_strength`` is a dict lookup on that column, so a row carrying
    anything outside the three classes makes "how hard do I confirm this?" raise
    KeyError forever instead of answering. The compensation already happened, so
    it is recorded at the most pessimistic class rather than refused.
    """

    @fx.undo_handler("fs.restore")
    def restore(c: sqlite3.Connection, e: fx.Effect, args: dict[str, Any]) -> fx.Compensation:
        return fx.Compensation(
            kind="mystery.op",  # unclassified, so the table cannot correct it
            summary="   ",  # and record_effect would have refused this outright
            reversibility="probably fine",  # type: ignore[arg-type]
        )

    e = an_edit(con)
    result = fx.undo(con, e.id)
    assert result.outcome == "undone"

    comp = fx.get_effect(con, result.undo_effect_id or "")
    assert comp is not None
    assert comp.reversibility == "irreversible"
    assert comp.confirm_strength == "confirm_readback"  # answers instead of raising
    assert comp.summary.strip(), "every row is spoken eventually"
    assert fx.spoken_effect_line(comp)
    payload = con.execute(
        "SELECT payload FROM events WHERE kind='effect.undone' AND effect_id=?", (e.id,)
    ).fetchone()["payload"]
    assert "probably fine" in payload  # the degradation is on the record, not silent


def test_a_handler_cannot_store_a_window_that_never_closes(con: sqlite3.Connection) -> None:
    """The deadline footgun, arriving through the one door that did not check it.

    ``record_effect`` validates ``undo_deadline``; a Compensation's went straight
    to the column. A deadline with no millis sorts AFTER every well-formed one,
    so the sweep never closes that window, and every parse-based reader raises on
    the row instead. The plan goes with it: a compensation offered with no window
    reads as unbounded, which is the worse of the two lies.
    """

    @fx.undo_handler("fs.restore")
    def restore(c: sqlite3.Connection, e: fx.Effect, args: dict[str, Any]) -> fx.Compensation:
        return fx.Compensation(
            kind="telegram.send",
            summary="I sent a retraction",
            reversibility="compensatable",
            undo_plan={"op": "telegram.delete", "args": {"message_id": 9}, "speaks": "delete it"},
            undo_deadline="2026-09-18T12:00:00Z",  # no millis
        )

    e = an_edit(con)
    comp_id = fx.undo(con, e.id).undo_effect_id or ""
    comp = fx.get_effect(con, comp_id)
    assert comp is not None
    assert comp.undo_deadline is None
    assert comp.undo_plan is None
    assert fx.undo_window_remaining_s(comp) is None  # no longer raises on its own row
    assert "no plan was stored" in fx.spoken_effect_line(comp)
    assert fx.expire_windows(con, now_ts=fx.deadline_in(365 * 86400)) == []


def test_a_malformed_onward_plan_does_not_lose_the_compensation_row(
    con: sqlite3.Connection,
) -> None:
    """It happened. Record it with no plan and say why on the bus."""

    @fx.undo_handler("fs.restore")
    def restore(c: sqlite3.Connection, e: fx.Effect, args: dict[str, Any]) -> fx.Compensation:
        return fx.Compensation(
            kind="fs.edit",
            summary="I put main.py back",
            reversibility="reversible",
            undo_plan={"op": "fs.restore", "args": {"cb": lambda: None}},  # type: ignore[dict-item]
        )

    e = an_edit(con)
    result = fx.undo(con, e.id)
    assert result.outcome == "undone"
    comp = fx.get_effect(con, result.undo_effect_id or "")
    assert comp is not None and comp.undo_plan is None
    payload = con.execute(
        "SELECT payload FROM events WHERE kind='effect.undone' AND effect_id=?", (e.id,)
    ).fetchone()
    assert "compensation_plan_rejected" in payload["payload"]


# ───────────────────────────── the ledger as history ─────────────────────────────


def test_every_effect_and_every_refusal_reaches_the_activity_log(con: sqlite3.Connection) -> None:
    e = fx.record_effect(
        con, kind="phone.call", summary="I called the restaurant", reversibility="irreversible"
    )
    fx.undo(con, e.id)
    fx.undo(con, e.id)
    kinds = event_kinds(con, e.id)
    assert kinds == ["effect.recorded", "effect.undo_failed", "effect.undo_failed"], (
        "a user asking twice must appear twice; deduping refusals hides the pattern"
    )
    recorded = con.execute(
        "SELECT idem_key, payload FROM events WHERE kind='effect.recorded' AND effect_id=?", (e.id,)
    ).fetchone()
    assert recorded["idem_key"] == f"eff:{e.id}:applied"
    assert "confirm_readback" in recorded["payload"]


def test_the_ledger_survives_a_restart_and_reads_back_in_order(db_path: Path) -> None:
    writer = connect(db_path)
    before = fx.deadline_in(-1)  # now() is millisecond-granular; `since` is strict
    first = an_edit(writer)
    second = a_telegram_send(writer, deadline_s=600)
    ids = [first.id, second.id]
    writer.close()

    # Both order-bys tie-break on the random id, so two rows sharing a
    # millisecond would make the assertions below a coin flip rather than a
    # statement about ordering. Fail here, legibly, rather than 26% of the time
    # somewhere else on a faster disk.
    assert first.ts < second.ts, "same-millisecond writes make this test meaningless, not wrong"

    reader = connect(db_path)
    assert [e.id for e in fx.effects_since(reader, before)] == ids
    assert [e.id for e in fx.recent_effects(reader, limit=1)] == [ids[-1]]
    assert [e.id for e in fx.recent_effects(reader, job_id=JOB)] == list(reversed(ids))
    # The plan came back as a dict, not as a string of JSON nobody parsed.
    assert fx.recent_effects(reader)[0].undo_plan["op"] == "telegram.delete"  # type: ignore[index]
    reader.close()


def test_an_effect_with_no_summary_cannot_be_recorded(con: sqlite3.Connection) -> None:
    """Every row is spoken eventually; a row with nothing to say is a bug, not a log line."""
    with pytest.raises(ValueError, match="summary"):
        fx.record_effect(con, kind="fs.edit", summary="   ", reversibility="reversible")
    with pytest.raises(ValueError):
        fx.record_effect(con, kind="fs.edit", summary="ok", reversibility="maybe")  # type: ignore[arg-type]
