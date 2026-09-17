"""One tick, and the process around it. The reboot, end to end.

``tests/test_schedule_store.py`` proves the arithmetic of a late fire; this file
proves what a late fire actually DOES: one question, delivered to whichever
channel presence picked, and nothing on the second pass.

Nothing here sleeps. ``run()`` takes its ``sleep`` as an argument precisely so
this file can assert that a loop of ten ticks costs no wall-clock time at all.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from jarvis import jobs, presence
from jarvis import requests as rq
from jarvis.db import connect, migrate
from jarvis.ids import now
from jarvis.jobs import shift_ts
from jarvis.schedule import __main__ as daemon
from jarvis.schedule import gate, loop, store
from jarvis.schedule import recurrence as rec

TZ = "Europe/Istanbul"
AT = "10:00"
SCHEDULE = daemon.BRIEFING_SCHEDULE


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "jarvis.db"
    c = connect(p)
    migrate(c)
    c.close()
    return p


@pytest.fixture
def con(db_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(db_path)
    yield c
    c.close()


def ten_am() -> str:
    return rec.last_at_or_before(now(), AT, TZ)


def installed(con: sqlite3.Connection, *, at_ts: str | None = None) -> store.Schedule:
    return daemon.install(con, at_local=AT, tz=TZ, now_ts=at_ts or shift_ts(ten_am(), -3600))


# ───────────────────────────── the morning, end to end ─────────────────────────────


def test_the_reboot_produces_one_question_delivered_to_one_channel(
    con: sqlite3.Connection,
) -> None:
    """Down at 09:50, up at 10:05: the exit criterion, all the way through."""
    installed(con, at_ts=shift_ts(ten_am(), -600))
    back_at = shift_ts(ten_am(), 300)
    presence.record_signal(con, "utterance", {"text": "morning"}, now_ts=back_at)

    report = loop.tick(con, claimed_by="scheduler:1", now_ts=back_at)
    assert [f.outcome for f in report.fired] == ["fired"]
    assert report.fired[0].late_s == pytest.approx(300.0)
    assert report.fired[0].due_at == ten_am()

    asked = rq.open_requests(con)
    assert len(asked) == 1
    assert asked[0].kind == "briefing_gate"
    assert [d.channel_kind for d in rq.due_deliveries(con, back_at)] == ["desk"]

    # The pointer is on the row, not in this process.
    assert store.get_schedule(con, SCHEDULE).last_request_id == asked[0].id


def test_a_second_tick_a_minute_later_asks_nothing(con: sqlite3.Connection) -> None:
    installed(con, at_ts=shift_ts(ten_am(), -600))
    loop.tick(con, claimed_by="scheduler:1", now_ts=shift_ts(ten_am(), 300))
    again = loop.tick(con, claimed_by="scheduler:1", now_ts=shift_ts(ten_am(), 360))
    assert again.fired == ()
    assert con.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1


def test_a_fire_that_arrives_hours_late_asks_nothing_and_says_it_missed(
    con: sqlite3.Connection,
) -> None:
    installed(con, at_ts=shift_ts(ten_am(), -600))
    lunchtime = shift_ts(ten_am(), 6 * 3600)

    report = loop.tick(con, claimed_by="scheduler:1", now_ts=lunchtime)
    assert [f.outcome for f in report.fired] == ["missed"]
    assert con.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0
    kinds = [r[0] for r in con.execute("SELECT kind FROM events")]
    assert "schedule.missed" in kinds


def test_a_handler_this_build_does_not_have_leaves_the_morning_owed(
    con: sqlite3.Connection,
) -> None:
    store.ensure_schedule(
        con,
        name="something_new",
        fires="from_a_later_version",
        at_local=AT,
        tz=TZ,
        now_ts=shift_ts(ten_am(), -3600),
    )
    at = shift_ts(ten_am(), 60)
    report = loop.tick(con, claimed_by="scheduler:1", now_ts=at)
    assert report.unknown_handlers == ("from_a_later_version",)
    assert store.get_schedule(con, "something_new").next_run_at == ten_am()
    assert [s.name for s in store.due(con, at)] == ["something_new"]


def test_a_fire_that_raises_is_retried_rather_than_lost(con: sqlite3.Connection) -> None:
    installed(con)
    at = shift_ts(ten_am(), 60)
    calls: list[str] = []

    def explode(c: sqlite3.Connection, held: store.Claim, actor: str, now_ts: str) -> str | None:
        calls.append(held.due_at)
        raise RuntimeError("the disk is full")

    report = loop.tick(
        con, claimed_by="scheduler:1", now_ts=at, handlers={"briefing_gate": explode}
    )
    assert [f.outcome for f in report.fired] == ["error"]
    assert store.get_schedule(con, SCHEDULE).next_run_at == ten_am(), "the pointer moved anyway"

    # The next tick tries again, for the same morning.
    loop.tick(
        con, claimed_by="scheduler:1", now_ts=shift_ts(at, 30), handlers={"briefing_gate": explode}
    )
    assert calls == [ten_am(), ten_am()]


def test_two_daemons_ticking_at_once_ask_one_question(db_path: Path) -> None:
    first = connect(db_path)
    second = connect(db_path)
    try:
        installed(first, at_ts=shift_ts(ten_am(), -600))
        at = shift_ts(ten_am(), 300)
        a = loop.tick(first, claimed_by="scheduler:1", now_ts=at)
        b = loop.tick(second, claimed_by="scheduler:2", now_ts=at)
        assert len(a.fired) + len(b.fired) == 1, "both daemons believed they should brief"
        assert first.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1
    finally:
        first.close()
        second.close()


# ───────────────────────────── the answer comes back ─────────────────────────────


def answer_the_gate(con: sqlite3.Connection, label: str) -> None:
    sched = store.get_schedule(con, SCHEDULE)
    req = rq.get_request(con, sched.last_request_id)
    rq.answer_request(
        con,
        req.id,
        {
            "answers": {gate.GATE_QUESTION: label},
            "text": label,
            "sources": {gate.GATE_QUESTION: "option"},
        },
        "telegram",
        "button",
    )


def test_an_answer_that_arrives_on_another_channel_is_acted_on_by_the_next_tick(
    con: sqlite3.Connection,
) -> None:
    """Nothing is held in the process that asked. That is the whole resume story."""
    installed(con)
    at = shift_ts(ten_am(), 60)
    loop.tick(con, claimed_by="scheduler:1", now_ts=at)
    answer_the_gate(con, gate.NOW_LABEL)

    report = loop.tick(con, claimed_by="scheduler:1", now_ts=shift_ts(at, 15))
    assert [o.action for o in report.answers] == ["start"]
    assert "briefing.started" in [r[0] for r in con.execute("SELECT kind FROM events")]


def test_five_minutes_re_asks_without_touching_the_schedule(con: sqlite3.Connection) -> None:
    installed(con)
    at = shift_ts(ten_am(), 60)
    loop.tick(con, claimed_by="scheduler:1", now_ts=at)
    armed = store.get_schedule(con, SCHEDULE)
    answer_the_gate(con, gate.FIVE_LABEL)

    report = loop.tick(con, claimed_by="scheduler:1", now_ts=shift_ts(at, 15))
    assert [o.action for o in report.answers] == ["snoozed"]

    after = store.get_schedule(con, SCHEDULE)
    assert after.next_run_at == armed.next_run_at
    assert after.fire_count == armed.fire_count
    assert after.last_request_id != armed.last_request_id, "the pointer follows the re-ask"

    # Due in five minutes, not now.
    assert rq.due_deliveries(con, shift_ts(at, 20)) == []
    assert rq.due_deliveries(con, shift_ts(at, 15 + gate.SNOOZE_S))


def test_the_snoozed_question_is_the_one_the_next_tick_sees_answered(
    con: sqlite3.Connection,
) -> None:
    installed(con)
    at = shift_ts(ten_am(), 60)
    loop.tick(con, claimed_by="scheduler:1", now_ts=at)
    answer_the_gate(con, gate.FIVE_LABEL)
    loop.tick(con, claimed_by="scheduler:1", now_ts=shift_ts(at, 15))

    answer_the_gate(con, gate.NOW_LABEL)
    report = loop.tick(con, claimed_by="scheduler:1", now_ts=shift_ts(at, 400))
    assert [o.action for o in report.answers] == ["start"]


# ───────────────────────────── finished jobs ─────────────────────────────


def test_a_job_that_finished_is_announced_once_and_routed_by_presence(
    con: sqlite3.Connection,
) -> None:
    # Anchored to the real clock rather than to this morning's ten o'clock: the
    # jobs rows are stamped by the spine's own now(), and a cursor set an hour
    # into the past would re-see them however well it worked.
    at = shift_ts(now(), 60)
    presence.record_signal(con, "idle", {"idle_s": 900.0}, now_ts=at)
    job = jobs.create_job(con, kind="claude_code", title="the todo app", created_by="desk")
    jobs.set_state(con, job.id, "starting", actor="desk")
    jobs.set_state(con, job.id, "running", actor="desk")
    jobs.set_state(con, job.id, "finishing", actor="desk")
    jobs.set_state(con, job.id, "done", actor="desk", result_summary="Seven tests pass.")

    report = loop.tick(con, claimed_by="scheduler:1", now_ts=at)
    assert [n.job_id for n in report.notices] == [job.id]
    kinds = {d.channel_kind for d in rq.due_deliveries(con, at)}
    assert kinds == {"telegram"}

    # The cursor moved; the same job is not news twice.
    again = loop.tick(con, claimed_by="scheduler:1", now_ts=shift_ts(at, 30))
    assert again.notices == ()


def test_the_completion_cursor_is_inclusive_and_the_repeat_costs_nothing(
    con: sqlite3.Connection,
) -> None:
    at = shift_ts(now(), 60)
    job = jobs.create_job(con, kind="claude_code", title="the scraper", created_by="desk")
    jobs.set_state(con, job.id, "starting", actor="desk")
    jobs.set_state(con, job.id, "running", actor="desk")
    jobs.set_state(con, job.id, "failed", actor="desk", stop_reason="it broke")

    loop.notify_finished(con, actor="scheduler", now_ts=at)
    # Wind the cursor back to exactly the job's own timestamp: the collision the
    # briefing cursor's comment measures at ~80% of back-to-back writes.
    loop.set_completion_cursor(con, jobs.get(con, job.id).updated_at)
    second = loop.notify_finished(con, actor="scheduler", now_ts=at)
    assert len(second) == 1, "the inclusive cursor should re-see it"
    assert con.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1, "and re-ask nothing"


# ───────────────────────────── the process ─────────────────────────────


def test_installing_is_what_a_restart_does_and_it_changes_nothing(
    con: sqlite3.Connection,
) -> None:
    first = daemon.install(con, at_local=AT, tz=TZ, now_ts=shift_ts(ten_am(), -3600))
    second = daemon.install(con, at_local=AT, tz=TZ, now_ts=shift_ts(ten_am(), 600))
    assert (first.id, first.next_run_at) == (second.id, second.next_run_at)


def test_retiming_needs_asking_for(con: sqlite3.Connection) -> None:
    daemon.install(con, at_local=AT, tz=TZ, now_ts=shift_ts(ten_am(), -3600))
    same = daemon.install(con, at_local="08:30", tz=TZ, now_ts=shift_ts(ten_am(), -3600))
    assert same.at_local == AT, "a stray --at must not silently move the briefing"
    moved = daemon.install(
        con, at_local="08:30", tz=TZ, retime=True, now_ts=shift_ts(ten_am(), -3600)
    )
    assert moved.at_local == "08:30"


def test_one_tick_from_the_command_line(db_path: Path) -> None:
    def never(seconds: float) -> None:
        raise AssertionError("--once must not sleep")

    assert daemon.main(["--db", str(db_path), "--once"], sleep=never) == daemon.EXIT_OK
    con = connect(db_path)
    try:
        assert store.get_schedule(con, SCHEDULE) is not None
    finally:
        con.close()


def test_a_time_that_is_not_a_time_is_refused_before_anything_opens(db_path: Path) -> None:
    assert daemon.main(["--db", str(db_path), "--once", "--at", "25:00"]) == daemon.EXIT_REFUSED
    assert (
        daemon.main(["--db", str(db_path), "--once", "--tz", "Mars/Olympus_Mons"])
        == daemon.EXIT_REFUSED
    )


def test_disable_and_enable_from_the_command_line(db_path: Path) -> None:
    assert daemon.main(["--db", str(db_path), "--disable"]) == daemon.EXIT_OK
    con = connect(db_path)
    try:
        assert store.get_schedule(con, SCHEDULE).enabled is False
        assert daemon.main(["--db", str(db_path), "--enable"]) == daemon.EXIT_OK
        assert store.get_schedule(con, SCHEDULE).enabled is True
    finally:
        con.close()


def test_the_loop_stops_when_asked_and_never_sleeps_for_real(con: sqlite3.Connection) -> None:
    installed(con)
    slept: list[float] = []
    ticks = 0

    def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    def stop() -> bool:
        nonlocal ticks
        ticks += 1
        return ticks >= 3

    reports = daemon.run(
        con,
        actor="scheduler",
        claimed_by="scheduler:1",
        interval_s=15.0,
        sleep=fake_sleep,
        stop=stop,
    )
    assert len(reports) == 3
    assert slept == [15.0, 15.0], "it sleeps between ticks and not after the last one"


def test_a_pointer_at_something_that_is_not_a_gate_is_left_alone(
    con: sqlite3.Connection,
) -> None:
    """A future schedule fires something else; its answer is not the gate's to read."""
    from jarvis.schedule import completion

    sched = store.ensure_schedule(
        con,
        name="something_new",
        fires="from_a_later_version",
        at_local=AT,
        tz=TZ,
        now_ts=shift_ts(ten_am(), -3600),
    )
    job = jobs.create_job(con, kind="claude_code", title="the scraper", created_by="desk")
    jobs.set_state(con, job.id, "starting", actor="desk")
    jobs.set_state(con, job.id, "running", actor="desk")
    jobs.set_state(con, job.id, "failed", actor="desk", stop_reason="it broke")
    notice = completion.raise_completion(con, job.id, now_ts=now())
    store.note_request(con, sched.id, notice.request.id)
    rq.answer_request(
        con, notice.request.id, {"text": completion.TELL_ME_LABEL}, "telegram", "button"
    )

    report = loop.tick(con, claimed_by="scheduler:1", now_ts=shift_ts(now(), 60))
    assert report.answers == ()
    assert rq.get_request(con, notice.request.id).state == "answered", "it was consumed"


def test_one_unusable_row_does_not_cost_the_whole_tick(con: sqlite3.Connection) -> None:
    """The 2am edit, which is the failure mode this table's own ADR invites.

    ``docs/adr/0011-scheduler-is-a-table.md`` argues for a plain table partly
    because you can read and FIX it with the sqlite3 CLI when the briefing did
    not arrive. So the typo made that way is a case, not an accident: an unknown
    zone used to raise straight out of ``store.claim``, through ``fire_due`` and
    out of ``tick`` — taking the expiry sweep, the gate answers and the
    completion notices with it, every tick, with the daemon exiting 5 in a loop.
    """
    sched = installed(con)
    store.ensure_schedule(
        con,
        name="other",
        fires="briefing_gate",
        at_local="09:00",
        tz=TZ,
        now_ts=shift_ts(ten_am(), -2 * 3600),
    )
    con.execute("UPDATE schedules SET tz='Europe/Istanbol' WHERE id=?", (sched.id,))
    at = shift_ts(ten_am(), 300)
    presence.record_signal(con, "utterance", {"text": "morning"}, now_ts=at)

    report = loop.tick(con, claimed_by="scheduler:1", now_ts=at)

    by_name = {f.schedule: f for f in report.fired}
    assert by_name[SCHEDULE].outcome == "error"
    assert "unknown timezone" in by_name[SCHEDULE].error
    assert "other" in by_name, "the healthy schedule was still looked at"

    kinds = [r["kind"] for r in con.execute("SELECT kind FROM events WHERE kind='schedule.failed'")]
    assert kinds == ["schedule.failed"], "it is on the bus, not in a traceback"

    # Not re-spammed every fifteen seconds while nobody is awake to fix it.
    loop.tick(con, claimed_by="scheduler:1", now_ts=shift_ts(at, 60))
    again = con.execute("SELECT COUNT(*) FROM events WHERE kind='schedule.failed'").fetchone()[0]
    assert again == 1

    # And the morning is still OWED: fixing the typo inside the grace window
    # delivers it, late, rather than having quietly eaten the day.
    con.execute("UPDATE schedules SET tz=? WHERE id=?", (TZ, sched.id))
    fixed = loop.tick(con, claimed_by="scheduler:1", now_ts=shift_ts(at, 120))
    assert [(f.schedule, f.outcome) for f in fixed.fired if f.schedule == SCHEDULE] == [
        (SCHEDULE, "fired")
    ]
    assert store.get_schedule(con, SCHEDULE).fire_count == 1
