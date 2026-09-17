"""The recurrence maths and the schedules table: the reboot, and the two daemons.

Two failures are worth a test each and everything else here is scaffolding for
them. THE REBOOT: the machine went down at 09:50 and came back at 10:05, and the
briefing must arrive once, late, with the lateness recorded — not five times, and
not never. THE RACE: two scheduler processes, both awake, both looking at the same
row, and exactly one of them fires it.

No test sleeps. Every function takes ``now_ts``, and the clock moves because a
string says a different time, which is the only way the reboot case is testable
at all.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from jarvis.db import MIGRATIONS_DIR, connect, migrate
from jarvis.ids import now, parse_ts
from jarvis.jobs import shift_ts
from jarvis.schedule import recurrence as rec
from jarvis.schedule import store
from jarvis.schedule.gate import raise_gate

TZ = "Europe/Istanbul"
AT = "10:00"
DAY_S = 86400.0


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


@pytest.fixture
def other(db_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(db_path)
    yield c
    c.close()


def ten_am() -> str:
    """The most recent real 10:00 Istanbul, as a UTC timestamp.

    Anchoring the fake timeline to a REAL occurrence rather than to a hard-coded
    date keeps every fabricated instant within a day of the wall clock, so rows
    the spine stamps with its own ``now()`` still sort sensibly against them.
    """
    return rec.last_at_or_before(now(), AT, TZ)


def arm(
    con: sqlite3.Connection, *, at_ts: str, grace_s: int = store.DEFAULT_GRACE_S
) -> store.Schedule:
    """A morning briefing armed as if the daemon had started at ``at_ts``."""
    return store.ensure_schedule(
        con,
        name="morning_briefing",
        fires="briefing_gate",
        at_local=AT,
        tz=TZ,
        grace_s=grace_s,
        now_ts=at_ts,
    )


# ───────────────────────────── the recurrence maths ─────────────────────────────


def test_ten_in_istanbul_is_seven_in_utc() -> None:
    """Turkey is UTC+3 with no DST. Stored UTC, spoken local, and never mixed up."""
    fired = rec.next_after("2026-09-17T05:00:00.000Z", AT, TZ)
    assert fired == "2026-09-17T07:00:00.000Z"


def test_the_next_one_is_tomorrow_once_today_has_passed() -> None:
    assert rec.next_after("2026-09-17T07:00:00.000Z", AT, TZ) == "2026-09-18T07:00:00.000Z"
    # Strictly after: firing at exactly 10:00 must not re-arm to the same instant
    # and fire again forever.
    assert rec.next_after("2026-09-17T06:59:59.999Z", AT, TZ) == "2026-09-17T07:00:00.000Z"


def test_the_occurrence_a_late_fire_is_for_is_todays_not_the_pointers() -> None:
    """The reboot, as arithmetic. 10:05 belongs to today's 10:00, not yesterday's."""
    assert rec.last_at_or_before("2026-09-17T07:05:00.000Z", AT, TZ) == "2026-09-17T07:00:00.000Z"
    assert rec.last_at_or_before("2026-09-17T06:55:00.000Z", AT, TZ) == "2026-09-16T07:00:00.000Z"


def test_the_daemon_may_run_in_another_zone_and_ten_still_means_ten_there() -> None:
    """The case it is easy to be casual about: the box is not where the user is."""
    utc_ten = rec.next_after("2026-09-17T00:00:00.000Z", AT, "UTC")
    ist_ten = rec.next_after("2026-09-17T00:00:00.000Z", AT, TZ)
    assert utc_ten == "2026-09-17T10:00:00.000Z"
    assert ist_ten == "2026-09-17T07:00:00.000Z"


def test_a_zone_with_dst_keeps_the_wall_clock_and_moves_the_instant() -> None:
    """Istanbul has no DST; the code must not assume every zone is that easy."""
    before = rec.next_after("2026-03-07T12:00:00.000Z", AT, "America/New_York")
    after = rec.next_after("2026-03-09T12:00:00.000Z", AT, "America/New_York")
    assert before == "2026-03-07T15:00:00.000Z"  # EST, UTC-5
    assert after == "2026-03-09T14:00:00.000Z"  # EDT, UTC-4, the SAME local 10:00


def test_every_occurrence_is_strictly_later_than_the_last_across_a_dst_shift() -> None:
    cursor = "2026-03-06T12:00:00.000Z"
    seen = []
    for _ in range(6):
        cursor = rec.next_after(cursor, AT, "America/New_York")
        seen.append(cursor)
    assert seen == sorted(seen)
    assert len(set(seen)) == len(seen)


def test_a_three_day_outage_counts_as_three_mornings_slept_through() -> None:
    start = "2026-09-14T07:00:00.000Z"
    assert rec.missed_occurrences(start, "2026-09-17T07:00:00.000Z", AT, TZ) == 3
    assert rec.missed_occurrences(start, start, AT, TZ) == 0


def test_a_time_that_is_not_a_time_is_refused_at_the_door() -> None:
    for bad in ("25:00", "10", "1000", "10:0", "ten"):
        with pytest.raises(ValueError, match="at_local"):
            rec.at_local_parts(bad)
    with pytest.raises(ValueError, match="timezone"):
        rec.zone("Mars/Olympus_Mons")


def test_every_timestamp_it_returns_is_in_the_one_format_the_database_sorts_on() -> None:
    """A second formatter here would sort wrong against every other row."""
    pattern = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
    assert pattern.match(rec.next_after(now(), AT, TZ))
    assert pattern.match(rec.last_at_or_before(now(), AT, TZ))


# ───────────────────────────── arming ─────────────────────────────


def test_arming_is_idempotent_and_a_restart_does_not_move_the_pointer(
    con: sqlite3.Connection,
) -> None:
    base = shift_ts(ten_am(), -3600)
    first = arm(con, at_ts=base)
    assert first.next_run_at == ten_am()

    # The daemon restarts an hour later, i.e. after the fire was due.
    again = arm(con, at_ts=shift_ts(ten_am(), 600))
    assert again.id == first.id
    assert again.next_run_at == first.next_run_at, "a restart re-armed the schedule past its fire"


def test_disabling_survives_a_restart_and_enabling_re_arms_from_now(
    con: sqlite3.Connection,
) -> None:
    arm(con, at_ts=shift_ts(ten_am(), -3600))
    store.set_enabled(con, "morning_briefing", False)
    assert arm(con, at_ts=shift_ts(ten_am(), 600)).enabled is False
    assert store.due(con, shift_ts(ten_am(), 600)) == []

    back = store.set_enabled(con, "morning_briefing", True, now_ts=shift_ts(ten_am(), 600))
    assert back.enabled is True
    # Tomorrow, not the one it owed from while it was off.
    assert back.next_run_at == rec.next_after(shift_ts(ten_am(), 600), AT, TZ)


def test_retiming_moves_the_next_fire(con: sqlite3.Connection) -> None:
    arm(con, at_ts=shift_ts(ten_am(), -3600))
    moved = store.retime(
        con, "morning_briefing", at_local="08:30", tz=TZ, now_ts=shift_ts(ten_am(), -3600)
    )
    assert moved.at_local == "08:30"
    assert moved.next_run_at == rec.next_after(shift_ts(ten_am(), -3600), "08:30", TZ)


def test_an_unknown_schedule_is_a_raise_and_not_a_silent_no_op(con: sqlite3.Connection) -> None:
    with pytest.raises(store.UnknownSchedule):
        store.retime(con, "nothing", at_local=AT, tz=TZ)
    with pytest.raises(store.UnknownSchedule):
        store.set_enabled(con, "nothing", True)


# ───────────────────────────── the reboot ─────────────────────────────


def test_a_reboot_across_the_fire_delivers_once_late_and_says_how_late(
    con: sqlite3.Connection,
) -> None:
    """Down at 09:50, back at 10:05. The exit criterion the roadmap names."""
    arm(con, at_ts=shift_ts(ten_am(), -600))  # armed at 09:50
    back_at = shift_ts(ten_am(), 300)  # booted at 10:05

    assert [s.name for s in store.due(con, back_at)] == ["morning_briefing"]
    held = store.claim(
        con, store.get_schedule(con, "morning_briefing").id, "sched:1", now_ts=back_at
    )
    assert held is not None
    assert held.due_at == ten_am(), "the late fire is for this morning's ten o'clock"
    assert held.late_s == pytest.approx(300.0)
    assert held.within_grace is True
    assert held.late is True

    after = store.complete(con, held, outcome="fired", now_ts=back_at)
    assert after is not None
    assert after.last_due_at == ten_am()
    assert after.last_late_s == pytest.approx(300.0)
    assert after.last_outcome == "fired"
    assert after.fire_count == 1

    # And it does not fire again this morning.
    assert store.due(con, shift_ts(ten_am(), 360)) == []
    assert after.next_run_at == rec.next_after(back_at, AT, TZ)


def test_four_days_off_produces_one_fire_and_records_the_three_it_slept_through(
    con: sqlite3.Connection,
) -> None:
    """The other half of misfire handling: no catch-up storm."""
    arm(con, at_ts=shift_ts(ten_am(), -3 * DAY_S - 3600))
    back_at = shift_ts(ten_am(), 300)

    sched = store.get_schedule(con, "morning_briefing")
    assert sched.next_run_at == shift_ts(ten_am(), -3 * DAY_S)

    held = store.claim(con, sched.id, "sched:1", now_ts=back_at)
    assert held is not None
    assert held.due_at == ten_am()
    assert held.missed == 3
    assert held.within_grace is True

    after = store.complete(con, held, outcome="fired", now_ts=back_at)
    assert after.fire_count == 1, "one briefing, not four"
    assert after.missed_count == 3, "and the log says three mornings went unsaid"
    assert store.due(con, back_at) == []


def test_a_clock_that_jumps_backwards_does_not_deliver_a_second_briefing(
    con: sqlite3.Connection,
) -> None:
    """The mirror image of the reboot, and the one nobody thinks to try.

    NTP corrects a box that had drifted forwards, or a VM is resumed from a
    snapshot, and suddenly it is ten o'clock again. The pointer is what decides,
    not the wall clock's opinion of which morning it is: ``complete`` has already
    moved ``next_run_at`` to tomorrow, so every one of these instants — five
    minutes before the fire, a whole day before it — finds nothing due.
    """
    arm(con, at_ts=shift_ts(ten_am(), -600))
    back_at = shift_ts(ten_am(), 300)
    held = store.claim(con, store.get_schedule(con, "morning_briefing").id, "s", now_ts=back_at)
    assert held is not None
    after = store.complete(con, held, outcome="fired", now_ts=back_at)
    assert after.fire_count == 1

    for rewound in (
        shift_ts(ten_am(), -300),  # back to 09:55, before this morning's fire
        ten_am(),  # back to the instant it fired
        shift_ts(ten_am(), -DAY_S),  # back a whole day
    ):
        assert store.due(con, rewound) == [], f"nothing is owed at {rewound}"
        assert store.claim(con, after.id, "s", now_ts=rewound) is None

    assert store.get_schedule(con, "morning_briefing").fire_count == 1


def test_a_fire_later_than_the_grace_window_is_dropped_and_recorded(
    con: sqlite3.Connection,
) -> None:
    arm(con, at_ts=shift_ts(ten_am(), -600), grace_s=3600)
    lunchtime = shift_ts(ten_am(), 6 * 3600)

    held = store.claim(con, store.get_schedule(con, "morning_briefing").id, "s", now_ts=lunchtime)
    assert held is not None
    assert held.within_grace is False, "a briefing at four in the afternoon is not a late briefing"

    after = store.complete(con, held, outcome="missed", now_ts=lunchtime)
    assert after.last_outcome == "missed"
    assert after.fire_count == 0
    assert after.missed_count == 1


def test_lateness_is_measured_in_real_seconds_not_inferred_from_two_timestamps(
    con: sqlite3.Connection,
) -> None:
    arm(con, at_ts=shift_ts(ten_am(), -3600))
    at = shift_ts(ten_am(), 47.0)
    held = store.claim(con, store.get_schedule(con, "morning_briefing").id, "s", now_ts=at)
    assert held.late_s == pytest.approx(
        (parse_ts(at) - parse_ts(ten_am())).total_seconds(), abs=0.002
    )


# ───────────────────────────── two processes ─────────────────────────────


def test_two_daemons_looking_at_the_same_schedule_produce_one_fire(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    """Two briefings, or two phone calls. The one that bites."""
    arm(con, at_ts=shift_ts(ten_am(), -3600))
    at = shift_ts(ten_am(), 60)
    sched_id = store.get_schedule(con, "morning_briefing").id

    # Both see it as due — the sweep is a read and takes no lock.
    assert [s.id for s in store.due(con, at)] == [sched_id]
    assert [s.id for s in store.due(other, at)] == [sched_id]

    first = store.claim(con, sched_id, "sched:1", now_ts=at)
    second = store.claim(other, sched_id, "sched:2", now_ts=at)
    assert first is not None
    assert second is None, "both processes claimed the same morning"


def test_a_simultaneous_claim_on_two_real_connections_still_has_one_winner(
    db_path: Path,
) -> None:
    setup = connect(db_path)
    arm(setup, at_ts=shift_ts(ten_am(), -3600))
    sched_id = store.get_schedule(setup, "morning_briefing").id
    setup.close()

    at = shift_ts(ten_am(), 60)
    gate = threading.Barrier(2)
    won: list[str] = []
    lock = threading.Lock()

    def claimer(who: str) -> None:
        c = connect(db_path)
        try:
            gate.wait(timeout=5)
            held = store.claim(c, sched_id, who, now_ts=at)
            if held is not None:
                with lock:
                    won.append(who)
        finally:
            c.close()

    threads = [threading.Thread(target=claimer, args=(f"sched:{i}",)) for i in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert len(won) == 1, f"{len(won)} daemons believed they should fire"


def test_a_daemon_that_dies_holding_a_claim_loses_the_lease_not_the_morning(
    con: sqlite3.Connection,
) -> None:
    arm(con, at_ts=shift_ts(ten_am(), -3600))
    sched_id = store.get_schedule(con, "morning_briefing").id
    at = shift_ts(ten_am(), 60)

    dead = store.claim(con, sched_id, "sched:dead", now_ts=at, lease_s=120)
    assert dead is not None
    assert store.due(con, at) == [], "a live claim hides the row"

    # It never came back. Two minutes later the row is claimable again, and the
    # occurrence is still this morning's.
    later = shift_ts(at, 121)
    assert [s.id for s in store.due(con, later)] == [sched_id]
    retry = store.claim(con, sched_id, "sched:new", now_ts=later)
    assert retry is not None
    assert retry.due_at == ten_am()


def test_completing_a_claim_somebody_else_has_taken_over_changes_nothing(
    con: sqlite3.Connection,
) -> None:
    arm(con, at_ts=shift_ts(ten_am(), -3600))
    sched_id = store.get_schedule(con, "morning_briefing").id
    at = shift_ts(ten_am(), 60)

    stale = store.claim(con, sched_id, "sched:old", now_ts=at, lease_s=1)
    taken = store.claim(con, sched_id, "sched:new", now_ts=shift_ts(at, 2))
    assert taken is not None
    assert store.complete(con, stale, outcome="fired", now_ts=shift_ts(at, 3)) is None
    assert store.get_schedule(con, "morning_briefing").fire_count == 0


def test_releasing_a_claim_leaves_the_schedule_due(con: sqlite3.Connection) -> None:
    arm(con, at_ts=shift_ts(ten_am(), -3600))
    sched_id = store.get_schedule(con, "morning_briefing").id
    at = shift_ts(ten_am(), 60)

    held = store.claim(con, sched_id, "sched:1", now_ts=at)
    assert store.release(con, held, now_ts=at) is True
    assert [s.id for s in store.due(con, at)] == [sched_id]
    assert store.get_schedule(con, "morning_briefing").next_run_at == ten_am()


def test_the_pointer_to_the_last_request_is_a_row_and_not_a_variable(
    con: sqlite3.Connection,
) -> None:
    """A dropped channel resumes from here; nothing important lives in a process."""
    arm(con, at_ts=shift_ts(ten_am(), -3600))
    sched = store.get_schedule(con, "morning_briefing")
    assert sched.last_request_id is None

    # A REAL request id: the column is a foreign key, so a pointer at a question
    # that does not exist is refused by the schema rather than discovered at ten
    # in the morning when nothing can be found to ask.
    req = raise_gate(con, schedule=sched.name, occurrence=ten_am(), now_ts=ten_am())
    assert store.note_request(con, sched.id, req.id, now_ts=ten_am()) is True
    assert store.get_schedule(con, "morning_briefing").last_request_id == req.id
    with pytest.raises(sqlite3.IntegrityError):
        store.note_request(con, sched.id, "req_that_never_was")


# ───────────────────────────── the schema itself ─────────────────────────────


def test_no_two_migrations_share_a_number() -> None:
    """A duplicate number is invisible: db.migrate applies one and SKIPS the other.

    Two stage-5 components each added a migration in the same week, and the only
    thing that would have caught them both calling it 002 is this assertion.
    """
    numbers = [int(f.name[:3]) for f in MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql")]
    assert len(numbers) == len(set(numbers)), f"duplicate migration numbers: {sorted(numbers)}"


@pytest.mark.parametrize("bad", ["25:99", "25:00", "24:00", "29:59", "1:00", "0100"])
def test_the_schedules_table_refuses_a_time_that_is_not_one(
    con: sqlite3.Connection, bad: str
) -> None:
    """Both halves of the clock, because only one of them used to be checked.

    ``'25:99'`` alone proves nothing about hours: it fails on the MINUTES. The
    hour half was ``[0-2][0-9]``, which happily accepts 25:00 — and an hour no
    occurrence can be computed from is exactly what a 2am edit produces on a
    table whose whole selling point is being editable with the sqlite3 CLI.
    """
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            """INSERT INTO schedules
                 (id, name, fires, payload, at_local, tz, next_run_at, created_at, updated_at)
               VALUES ('s1','bad','briefing_gate','{}',?,'UTC',?,?,?)""",
            (bad, now(), now(), now()),
        )


def test_a_claim_it_cannot_compute_an_occurrence_for_gives_the_lease_back(
    con: sqlite3.Connection,
) -> None:
    """The zone is a typo. The claim must not hold a row it can do nothing with.

    ``claim`` takes the lease in the UPDATE and only then asks the recurrence
    which morning this is for, so a row edited to an unknown zone used to end up
    claimed by a process that had already given up on it: invisible for the whole
    lease, then claimed again by the next tick, forever.
    """
    arm(con, at_ts=shift_ts(ten_am(), -600))
    sched = store.get_schedule(con, "morning_briefing")
    con.execute("UPDATE schedules SET tz='Europe/Istanbol' WHERE id=?", (sched.id,))

    with pytest.raises(ValueError, match="unknown timezone"):
        store.claim(con, sched.id, "s", now_ts=shift_ts(ten_am(), 300))

    after = store.get_schedule(con, "morning_briefing")
    assert after.claimed_by is None
    assert after.claim_expires_at is None
    assert after.next_run_at == sched.next_run_at, "the morning is still owed"


# ───────────────────────── the zone, not the daemon's own clock ─────────────────────────


@pytest.fixture
def daemon_in(request: pytest.FixtureRequest) -> Iterator[None]:
    """Run the body with the PROCESS in an absurd timezone, then put it back.

    ``TZ`` is what ``datetime.now()`` and ``strftime`` read, and the one thing
    this module must never do is read them. The assertion is that the instants do
    not move at all.
    """
    before = os.environ.get("TZ")
    os.environ["TZ"] = request.param
    time.tzset()
    try:
        yield
    finally:
        if before is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = before
        time.tzset()


@pytest.mark.parametrize(
    "daemon_in",
    ["UTC", "Pacific/Kiritimati", "Pacific/Midway", "America/Los_Angeles"],
    indirect=True,
)
def test_the_zone_decides_the_instant_not_the_box_the_daemon_runs_on(daemon_in: None) -> None:
    """Ten in Istanbul is seven UTC on a box fourteen hours either side of it.

    Pacific/Kiritimati is UTC+14 and Pacific/Midway is UTC-11: between them they
    put the daemon's own idea of "today" on either side of the user's. If any of
    these numbers moved, something in the recurrence had reached for the local
    clock instead of the stored zone.
    """
    base = "2026-09-17T00:00:00.000Z"
    assert rec.next_after(base, AT, TZ) == "2026-09-17T07:00:00.000Z"
    assert rec.last_at_or_before(base, AT, TZ) == "2026-09-16T07:00:00.000Z"
    assert rec.next_after(base, AT, "UTC") == "2026-09-17T10:00:00.000Z"
    # And across a DST shift in a zone that has one, which is where a local
    # strftime would have gone wrong by exactly an hour.
    assert rec.next_after("2026-03-07T12:00:00.000Z", AT, "America/New_York") == (
        "2026-03-07T15:00:00.000Z"
    )
    assert rec.next_after("2026-03-09T12:00:00.000Z", AT, "America/New_York") == (
        "2026-03-09T14:00:00.000Z"
    )


# ───────────────────────────── the migration, on an OLD file ─────────────────────────────


def test_the_schedules_table_works_on_a_file_that_predates_it(tmp_path: Path) -> None:
    """Arm a schedule on an UPGRADED database, not a freshly-created one.

    ``tests/test_foundation.py`` already proves the upgrade keeps its data and
    grows the right tables. This proves the thing that assertion cannot: that the
    table is USABLE afterwards. A CHECK or a foreign key that only resolves on a
    fresh file would satisfy "the table exists" and fail the first morning.
    """
    only_001 = tmp_path / "only_001"
    only_001.mkdir()
    shutil.copy(MIGRATIONS_DIR / "001_init.sql", only_001 / "001_init.sql")

    old = tmp_path / "stage4.db"
    con = connect(old)
    assert migrate(con, migrations_dir=only_001) == 1
    assert "schedules" not in _tables(con)
    con.close()

    upgraded = connect(old)
    assert upgraded.execute("PRAGMA user_version").fetchone()[0] == 1, "it really is an OLD file"
    highest = max(int(f.name[:3]) for f in MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))
    assert migrate(upgraded) == highest
    assert "schedules" in _tables(upgraded)

    arm(upgraded, at_ts=shift_ts(ten_am(), -600))
    held = store.claim(
        upgraded,
        store.get_schedule(upgraded, "morning_briefing").id,
        "s",
        now_ts=shift_ts(ten_am(), 300),
    )
    assert held is not None
    # last_request_id REFERENCES requests(id), a table 001 built: the foreign key
    # has to resolve ACROSS the two migrations, which is the join a fresh file
    # never has to make.
    req = raise_gate(upgraded, schedule="morning_briefing", occurrence=held.due_at)
    assert store.note_request(upgraded, held.schedule.id, req.id)
    assert store.complete(upgraded, held, outcome="fired").fire_count == 1
    upgraded.close()


def _tables(con: sqlite3.Connection) -> set[str]:
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
