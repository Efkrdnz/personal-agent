"""Presence, tested where it breaks: the asymmetry, stale evidence, and lies.

Four kinds of test and nothing else.

THE ASYMMETRY is the design, so both directions are asserted explicitly and
against each other: one decisive positive makes ``present`` instantly from any
depth of idle, and no single mild negative makes ``away``. A change that made the
system symmetric would pass a happy-path suite and fail these.

STALE EVIDENCE is the failure mode that killed this subsystem in every previous
design: a signal that outlives its writer pins presence to whatever it last saw.
So every expiry boundary is tested from the WRONG side — a poller that died, a
probe that timed out ten minutes ago, an override nobody cleared.

LIES are tested with the Wayland trap as the model: a probe that returns a
plausible constant forever is worse than one that fails, and the only thing that
can tell them apart is that idle time must RISE. That test exists because
nothing else in the system would notice.

RACES AND RESTARTS use two real connections to one file, because every reader of
this table is in another process and usually a later one.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from jarvis import presence as pr
from jarvis.db import connect, migrate
from jarvis.ids import now
from jarvis.jobs import shift_ts

# A fixed instant, so "night" and "day" are facts rather than whenever CI runs.
# Europe/Istanbul is UTC+03 with no DST, so this is 02:30 local: quiet hours.
NIGHT = "2026-09-16T23:30:00.000Z"
# 15:00 local. Not quiet hours, whatever else is true.
DAY = "2026-09-16T12:00:00.000Z"


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


def sig(source: str, value: dict, *, age_s: float = 0.0, ttl_s: int | None = None) -> pr.Signal:
    """A signal ``age_s`` old relative to :data:`DAY`, for the pure tests."""
    return pr.Signal(
        source=source,
        value=value,
        ts=shift_ts(DAY, -age_s),
        ttl_s=pr.DEFAULT_TTL_S[source] if ttl_s is None else ttl_s,
    )


def verdict(*signals: pr.Signal, override: pr.Override | None = None, at: str = DAY) -> pr.Presence:
    return pr.decide({s.source: s for s in signals}, override, at)


# ───────────────────── the asymmetry, in both directions ─────────────────────


def test_one_word_beats_fifty_minutes_of_idle() -> None:
    """INSTANT to become present. This is the cheap direction and it is free."""
    v = verdict(sig("idle", {"idle_s": 3000.0}), sig("wakeword", {"text": "hey jarvis"}))
    assert v.state == "present"
    assert "desk" in v.reachable


def test_crossing_the_first_threshold_does_not_reach_away() -> None:
    """SLOW to become away. 121 seconds of quiet is not evidence of an empty room.

    The gap between IDLE_MAYBE_S and IDLE_AWAY_S is the hysteresis, and a change
    that collapsed them would make a coffee refill look like leaving the house.
    """
    assert verdict(sig("idle", {"idle_s": pr.IDLE_MAYBE_S + 1})).state == "maybe"
    assert verdict(sig("idle", {"idle_s": pr.IDLE_AWAY_S - 1})).state == "maybe"
    assert verdict(sig("idle", {"idle_s": pr.IDLE_AWAY_S})).state == "away"


def test_a_tie_between_presence_and_absence_resolves_to_away() -> None:
    """The direction of the tie-break IS the cost model, so it is pinned here.

    A false away costs one redundant Telegram message. A false present costs
    forty minutes of a stalled build. Equal evidence must therefore fall toward
    away, and if somebody flips the comparison this fails.
    """
    same = shift_ts(DAY, -10)
    both = pr.decide(
        {
            "wakeword": pr.Signal("wakeword", {}, same, 300),
            "probe": pr.Signal("probe", {"answered": False}, same, 600),
        },
        None,
        DAY,
    )
    assert both.state == "away"


def test_a_lock_after_speech_means_gone_and_speech_after_a_lock_means_back() -> None:
    """Ordering, not priority. Either signal can be the newer one."""
    left = verdict(sig("wakeword", {}, age_s=120), sig("lock", {"locked": True}, age_s=5))
    assert left.state == "away"

    returned = verdict(sig("lock", {"locked": True}, age_s=120), sig("wakeword", {}, age_s=5))
    assert returned.state == "present"


def test_an_unlocked_screen_is_not_by_itself_evidence_of_anything() -> None:
    """locked=False must fall through to idle, not count as a positive.

    The lock poller rewrites this row every few seconds, so if "not locked" were
    treated as presence it would be the newest positive forever and the system
    could never become away at all.
    """
    v = verdict(sig("idle", {"idle_s": 900.0}), sig("lock", {"locked": False}))
    assert v.state == "away"


# ───────────────────── stale evidence is not evidence ─────────────────────


def test_a_dead_poller_produces_unknown_and_not_the_last_thing_it_saw(
    con: sqlite3.Connection,
) -> None:
    """The failure that silently kills this subsystem, tested from the wrong side.

    jarvis-dispatch writes ``idle`` every five seconds with a fifteen-second TTL.
    If it dies while the user is typing, the last row says "present" forever. It
    must decay to ``unknown`` — and ``unknown`` must keep the phone reachable.
    """
    pr.record_signal(con, "idle", {"idle_s": 1.0}, now_ts=DAY)
    assert pr.evaluate_presence(con, DAY).state == "present"

    later = shift_ts(DAY, pr.DEFAULT_TTL_S["idle"] + 1)
    stale = pr.evaluate_presence(con, later)
    assert stale.state == "unknown"
    assert "phone" in stale.reachable, "being blind must never remove a way to reach the user"


def test_expiry_is_exclusive_at_the_boundary() -> None:
    ttl = pr.DEFAULT_TTL_S["idle"]
    assert verdict(sig("idle", {"idle_s": 1.0}, age_s=ttl - 0.001)).state == "present"
    assert verdict(sig("idle", {"idle_s": 1.0}, age_s=ttl)).state == "unknown"


def test_an_expired_probe_stops_being_decisive() -> None:
    """Ten minutes after an unanswered question, silence proves nothing new."""
    fresh = verdict(sig("idle", {"idle_s": 1.0}), sig("probe", {"answered": False}))
    assert fresh.state == "away"

    expired = verdict(
        sig("idle", {"idle_s": 1.0}),
        sig("probe", {"answered": False}, age_s=pr.DEFAULT_TTL_S["probe"] + 1),
    )
    assert expired.state == "present"


def test_no_signals_at_all_is_unknown_with_every_channel_open() -> None:
    v = verdict()
    assert v.state == "unknown"
    assert v.reachable == ("desk", "telegram", "phone")
    assert not pr.should_defer(v.state), "ignorance must not turn into silence"


# ───────────────────── the probe that lies ─────────────────────


def test_self_test_rejects_a_probe_stuck_at_a_constant() -> None:
    """XScreenSaver under Wayland, in one line: plausible, constant, and a lie."""
    assert pr.idle_probe_self_test(lambda: 0.0, pause_s=0.01) is False


def test_self_test_accepts_a_probe_whose_idle_time_rises() -> None:
    readings = iter([1.0, 2.0])
    assert pr.idle_probe_self_test(lambda: next(readings), pause_s=0.01) is True


def test_self_test_fails_rather_than_raising_when_there_is_no_probe() -> None:
    assert pr.idle_probe_self_test(lambda: None, pause_s=0.01) is False


def test_a_blind_platform_writes_null_and_says_so(con: sqlite3.Connection) -> None:
    """None must be STORED, not coerced to zero and not left unwritten.

    Zero means "typing right now" — the exact lie. Writing nothing is a different
    bug with the same symptom, so the two blindnesses get different sentences and
    both keep the phone reachable.
    """
    idle, locked = pr.poll_idle(con, idle_probe=lambda: None, lock_probe=lambda: None, now_ts=DAY)
    assert idle is None and locked is None

    v = pr.evaluate_presence(con, DAY)
    assert v.state == "unknown"
    assert v.reason == "I can't read the idle time on this desktop."
    assert "phone" in v.reachable


def test_poll_idle_does_not_invent_a_lock_state(con: sqlite3.Connection) -> None:
    """None from the lock probe is not False. An unknown lock writes no row."""
    pr.poll_idle(con, idle_probe=lambda: 5.0, lock_probe=lambda: None, now_ts=DAY)
    assert "lock" not in pr.active_signals(con, DAY)


# ───────────────────── the free sensor ─────────────────────


def test_an_unanswered_question_beats_a_warm_keyboard(con: sqlite3.Connection) -> None:
    """The whole point of the cheapest sensor: the screen is not idle, the room is.

    OS idle time cannot produce this reading, which is why the unanswered
    question is worth having at all.
    """
    pr.record_signal(con, "idle", {"idle_s": 3.0}, now_ts=DAY)
    asked = shift_ts(DAY, -(pr.UNANSWERED_PROBE_S + 1))

    assert pr.note_probe(con, asked_at=asked, request_id="req_x", now_ts=DAY) == "unanswered"
    v = pr.evaluate_presence(con, DAY)
    assert v.state == "away"
    assert pr.should_defer(v.state)


def test_the_probe_waits_before_its_window_and_writes_nothing(con: sqlite3.Connection) -> None:
    asked = shift_ts(DAY, -(pr.UNANSWERED_PROBE_S - 5))
    assert pr.note_probe(con, asked_at=asked, now_ts=DAY) == "waiting"
    assert "probe" not in pr.active_signals(con, DAY)


def test_any_noise_answers_the_probe_and_retires_the_negative(
    con: sqlite3.Connection,
) -> None:
    """A grunt proves the room is occupied as well as a sentence does.

    And the negative must be RETIRED, not left to time out: otherwise one
    unanswered question keeps a demonstrably present user "away" for ten minutes.
    """
    asked = shift_ts(DAY, -60)
    pr.note_probe(con, asked_at=asked, now_ts=shift_ts(DAY, -20))
    assert pr.evaluate_presence(con, DAY).state == "away"

    pr.note_heard(con, "utterance", text="yes go on", now_ts=shift_ts(DAY, -5))
    assert pr.note_probe(con, asked_at=asked, now_ts=DAY) == "answered"
    assert pr.evaluate_presence(con, DAY).state == "present"


def test_speech_from_before_the_question_does_not_answer_it(con: sqlite3.Connection) -> None:
    """Only audio AFTER the question counts, or every question self-answers."""
    pr.note_heard(con, "utterance", now_ts=shift_ts(DAY, -120))
    asked = shift_ts(DAY, -60)
    assert pr.note_probe(con, asked_at=asked, now_ts=DAY) == "unanswered"


# ───────────────────── the override ─────────────────────


def test_override_beats_every_sensor(con: sqlite3.Connection) -> None:
    pr.record_signal(con, "idle", {"idle_s": 0.5}, now_ts=DAY)
    pr.note_heard(con, "wakeword", now_ts=DAY)
    pr.set_override(con, "away", by="voice", now_ts=DAY)

    v = pr.evaluate_presence(con, DAY)
    assert v.state == "away"
    assert v.confidence == 1.0


def test_an_override_always_expires(con: sqlite3.Connection) -> None:
    """An unbounded override is how a system goes silent for a week."""
    pr.record_signal(con, "idle", {"idle_s": 1.0}, now_ts=DAY)
    ov = pr.set_override(con, "away", 3600, by="telegram", now_ts=DAY)
    assert ov.until is not None

    before, after = shift_ts(DAY, 3599), shift_ts(DAY, 3601)
    # The idle poller is still running in both instants; only the override moves.
    pr.record_signal(con, "idle", {"idle_s": 1.0}, now_ts=before)
    assert pr.evaluate_presence(con, before).state == "away"
    pr.record_signal(con, "idle", {"idle_s": 1.0}, now_ts=after)
    assert pr.evaluate_presence(con, after).state == "present"


def test_clearing_an_override_hands_control_back_to_the_sensors(
    con: sqlite3.Connection,
) -> None:
    pr.record_signal(con, "idle", {"idle_s": 1.0}, now_ts=DAY)
    pr.set_override(con, "away", by="voice", now_ts=DAY)
    pr.set_override(con, None, by="voice", now_ts=DAY)

    assert pr.read_override(con).mode is None
    assert pr.evaluate_presence(con, DAY).state == "present"
    assert "override" not in pr.active_signals(con, DAY)


def test_an_override_written_as_a_signal_cannot_disagree_with_the_table(
    con: sqlite3.Connection,
) -> None:
    """Two spellings, ONE source of truth. The schema lists ``override`` as a
    signal source and the doc puts it in its own table; a system that stored both
    independently would drift, and the drift would be invisible until the day it
    mattered."""
    pr.record_signal(con, "override", {"mode": "dnd", "by": "telegram"}, 60, now_ts=DAY)

    assert pr.read_override(con).mode == "dnd"
    assert pr.active_signals(con, DAY)["override"].value["mode"] == "dnd"
    v = pr.evaluate_presence(con, DAY)
    assert "phone" not in v.reachable, "don't call me means don't call me"


def test_an_unknown_override_mode_is_refused(con: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="unknown override mode"):
        pr.set_override(con, "asleep", by="voice")  # type: ignore[arg-type]


def test_desk_only_narrows_the_ladder_to_the_desk(con: sqlite3.Connection) -> None:
    pr.record_signal(con, "idle", {"idle_s": 1.0}, now_ts=DAY)
    pr.set_override(con, "desk_only", by="voice", now_ts=DAY)
    assert pr.evaluate_presence(con, DAY).reachable == ("desk",)


# ───────────────────── night, and the 2am call ─────────────────────


def test_deep_idle_at_night_removes_the_phone_from_the_ladder(con: sqlite3.Connection) -> None:
    pr.record_signal(con, "idle", {"idle_s": pr.IDLE_ASLEEP_S + 60}, now_ts=NIGHT)
    v = pr.evaluate_presence(con, NIGHT)
    assert v.state == "asleep"
    assert v.reachable == ("telegram",)


def test_the_same_idle_in_daylight_is_merely_away(con: sqlite3.Connection) -> None:
    pr.record_signal(con, "idle", {"idle_s": pr.IDLE_ASLEEP_S + 60}, now_ts=DAY)
    assert pr.evaluate_presence(con, DAY).state == "away"


def test_one_unanswered_question_at_night_does_not_prove_sleep(
    con: sqlite3.Connection,
) -> None:
    """``asleep`` needs all three legs, and this is why.

    Without the idle requirement, a single unanswered question at 23:31 would
    remove the phone rung for the rest of the night on the strength of one
    missed reply.
    """
    pr.record_signal(con, "idle", {"idle_s": 5.0}, now_ts=NIGHT)
    pr.record_signal(con, "probe", {"answered": False}, now_ts=NIGHT)
    v = pr.evaluate_presence(con, NIGHT)
    assert v.state == "away"
    assert "phone" in v.reachable


# ───────────────────── reachability is a second axis ─────────────────────


def test_a_live_call_takes_the_desk_out_and_puts_the_phone_in() -> None:
    v = verdict(sig("idle", {"idle_s": 1.0}), sig("call", {"active": True}))
    assert "desk" not in v.reachable
    assert "phone" in v.reachable
    assert v.state != "present", "present means the ROOM can hear me"


def test_being_out_of_the_building_removes_the_desk_however_warm_the_keyboard() -> None:
    v = verdict(sig("idle", {"idle_s": 1.0}), sig("geofence", {"at_home": False}))
    assert v.state == "away"
    assert "desk" not in v.reachable


def test_should_defer_is_true_only_where_nobody_could_answer() -> None:
    assert pr.should_defer("away") and pr.should_defer("asleep")
    assert not any(pr.should_defer(s) for s in ("present", "maybe", "unknown"))


# ───────────────────── races ─────────────────────


def test_a_slower_poller_cannot_drag_presence_backwards(db_path: Path) -> None:
    """Two writers, one row, out of order — and the older reading must LOSE.

    Two pollers overlap whenever dispatch restarts. Without the monotonic guard
    the slow one's stale reading lands last and presence walks backwards in time,
    which looks exactly like a user who left and came back.
    """
    a = connect(db_path)
    b = connect(db_path)
    try:
        newer = DAY
        older = shift_ts(DAY, -10)

        assert pr.record_signal(a, "idle", {"idle_s": 1.0}, now_ts=newer) is True
        assert pr.record_signal(b, "idle", {"idle_s": 900.0}, now_ts=older) is False

        assert pr.evaluate_presence(a, DAY).state == "present"
    finally:
        a.close()
        b.close()


def test_two_processes_noticing_the_same_change_publish_it_once(db_path: Path) -> None:
    """Both pollers see the same transition in the same instant. One event.

    Each thread opens its OWN connection, which is what makes this a real test:
    sqlite3 forbids sharing one across threads for the same reason two processes
    cannot share one, and a shared handle would quietly serialise the race away.
    """
    setup = connect(db_path)
    pr.record_signal(setup, "idle", {"idle_s": 1.0}, now_ts=DAY)
    pr.update_presence(setup, actor="dispatch", now_ts=DAY)
    pr.record_signal(setup, "idle", {"idle_s": 900.0}, now_ts=DAY)
    setup.close()

    ready = threading.Barrier(2)
    errors: list[BaseException] = []

    def go() -> None:
        c = connect(db_path)
        try:
            ready.wait(timeout=5)
            pr.update_presence(c, actor="dispatch", now_ts=DAY)
        except BaseException as e:  # noqa: BLE001 - re-raised by the assert below
            errors.append(e)
        finally:
            c.close()

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, errors
    after = connect(db_path)
    try:
        rows = after.execute(
            "SELECT payload FROM events WHERE kind='presence.changed' AND payload LIKE '%away%'"
        ).fetchall()
        assert len(rows) == 1
        assert pr.read_presence(after) is not None
    finally:
        after.close()


# ───────────────────── restart survival ─────────────────────


def test_the_verdict_survives_the_death_of_every_writer(db_path: Path) -> None:
    """Write on one connection, close it, read the answer on another.

    Every reader of presence is in a different process from the poller, and
    usually one that started later. Nothing here may live in memory.
    """
    writer = connect(db_path)
    pr.record_signal(writer, "idle", {"idle_s": 900.0}, now_ts=DAY)
    pr.set_override(writer, "dnd", 600, by="telegram", now_ts=DAY)
    pr.update_presence(writer, actor="dispatch", now_ts=DAY)
    writer.close()

    reader = connect(db_path)
    try:
        live = pr.evaluate_presence(reader, DAY)
        stored = pr.read_presence(reader)
        assert stored is not None
        assert stored.state == live.state == "maybe"
        assert "phone" not in stored.reachable
        assert stored.reason == live.reason
    finally:
        reader.close()


def test_since_stops_walking_forward_once_the_state_holds(db_path: Path) -> None:
    """``since`` is the one thing the pure function cannot know.

    "Away since 14:02" must stay 14:02 across a hundred polls and across a
    restart, because the briefing reads it out and the router escalates on it. A
    derived ``since`` would creep forward every five seconds and nothing would
    ever look old.
    """
    first = connect(db_path)
    pr.record_signal(first, "idle", {"idle_s": 900.0}, now_ts=DAY)
    began = pr.update_presence(first, actor="dispatch", now_ts=DAY).since
    first.close()

    later = shift_ts(DAY, 300)
    second = connect(db_path)
    try:
        pr.record_signal(second, "idle", {"idle_s": 1200.0}, now_ts=later)
        again = pr.update_presence(second, actor="dispatch", now_ts=later)
        assert again.state == "away"
        assert again.since == began
    finally:
        second.close()


def test_a_real_state_change_moves_since_and_publishes(db_path: Path) -> None:
    c = connect(db_path)
    try:
        pr.record_signal(c, "idle", {"idle_s": 900.0}, now_ts=DAY)
        away = pr.update_presence(c, actor="dispatch", now_ts=DAY)

        back = shift_ts(DAY, 60)
        pr.note_heard(c, "wakeword", now_ts=back)
        present = pr.update_presence(c, actor="dispatch", now_ts=back)

        assert away.state == "away" and present.state == "present"
        assert present.since != away.since
        kinds = [
            r["kind"]
            for r in c.execute(
                "SELECT kind FROM events WHERE kind='presence.changed' ORDER BY seq"
            ).fetchall()
        ]
        assert len(kinds) == 2
    finally:
        c.close()


# ───────────────────── the spoken sentence ─────────────────────


def test_the_reason_is_a_sentence_a_person_would_say(con: sqlite3.Connection) -> None:
    """It is READ ALOUD on "where do you think I am?", so it is part of the API."""
    pr.note_heard(con, "wakeword", now_ts=DAY)
    reason = pr.evaluate_presence(con, DAY).reason
    assert reason == "You said my name just now."
    assert "0 seconds" not in reason


def test_the_reason_names_the_evidence_that_decided_it(con: sqlite3.Connection) -> None:
    pr.record_signal(con, "idle", {"idle_s": 3.0}, now_ts=DAY)
    pr.record_signal(con, "probe", {"answered": False}, now_ts=shift_ts(DAY, -40))
    assert "nobody answered" in pr.evaluate_presence(con, DAY).reason


def test_signal_sources_outside_the_schema_are_refused(con: sqlite3.Connection) -> None:
    with pytest.raises(pr.UnknownSignal):
        pr.record_signal(con, "webcam", {"seen": True})


def test_a_signal_value_must_be_json(con: sqlite3.Connection) -> None:
    with pytest.raises(TypeError):
        pr.record_signal(con, "idle", "5 seconds")  # type: ignore[arg-type]


def test_purging_expired_rows_never_changes_the_verdict(con: sqlite3.Connection) -> None:
    """Housekeeping must be exactly that. Correctness is at read time."""
    pr.record_signal(con, "idle", {"idle_s": 1.0}, now_ts=DAY)
    pr.record_signal(con, "probe", {"answered": False}, now_ts=shift_ts(DAY, -700))

    before = pr.evaluate_presence(con, DAY)
    assert pr.purge_expired(con, DAY) == 1
    assert pr.evaluate_presence(con, DAY) == before


def test_now_defaults_to_the_wall_clock(con: sqlite3.Connection) -> None:
    """The now_ts arguments are for tests; the default path must work too."""
    pr.record_signal(con, "idle", {"idle_s": 1.0})
    assert pr.evaluate_presence(con).state == "present"
    assert pr.update_presence(con, actor="test").state == "present"
    assert pr.read_presence(con) is not None
    assert now() > DAY


def test_dont_call_me_survives_a_live_call(con: sqlite3.Connection) -> None:
    """A standing prohibition must outlast the modifiers that follow it.

    Without one, the "you are on a call" signal helpfully adds ``phone`` back to
    the ladder of the one user who explicitly asked not to be called — and the
    system would look like it ignored them.
    """
    pr.record_signal(con, "idle", {"idle_s": 1.0}, now_ts=DAY)
    pr.record_signal(con, "call", {"active": True}, now_ts=DAY)
    pr.set_override(con, "dnd", 600, by="telegram", now_ts=DAY)

    assert "phone" not in pr.evaluate_presence(con, DAY).reachable


def test_an_override_beats_the_geofence_on_state_but_not_on_the_desk(
    con: sqlite3.Connection,
) -> None:
    """Overrides beat SENSORS. They do not beat a room you are not sitting in.

    "I'm back" while the phone says you are across town is a contradiction, and
    the honest resolution is to believe the human about the state and the sensor
    about which channels can reach them.
    """
    pr.record_signal(con, "geofence", {"at_home": False}, now_ts=DAY)
    pr.set_override(con, "present", 600, by="voice", now_ts=DAY)

    v = pr.evaluate_presence(con, DAY)
    assert v.state == "present"
    assert "desk" not in v.reachable


# ───────────────────── the refreshed lock, and the reason it lies ─────────────────────


def test_a_refreshed_lock_does_not_outrank_speech_that_came_after_it(
    con: sqlite3.Connection,
) -> None:
    """Speaking at a LOCKED desk must still be the instant path to present.

    This is the production shape and the reason it is tested through poll_idle
    rather than through hand-built signals: the 5-second task rewrites the lock
    row every poll, so the row's ts says "locked one second ago" for as long as
    the screen stays locked. Order against that and a wake word can never win,
    the desk drops out of the ladder, and should_defer starts deferring
    questions from a user who is audibly in the room asking them.
    """

    def poll(i: int) -> None:
        pr.poll_idle(
            con,
            idle_probe=lambda: 60.0,
            lock_probe=lambda: True,
            now_ts=shift_ts(DAY, 5 * i),
        )

    for i in range(3):
        poll(i)
    assert pr.evaluate_presence(con, shift_ts(DAY, 12)).state == "away"

    pr.note_heard(con, "wakeword", now_ts=shift_ts(DAY, 12))
    for i in range(3, 6):
        poll(i)

    v = pr.evaluate_presence(con, shift_ts(DAY, 27))
    assert v.state == "present"
    assert "desk" in v.reachable
    assert not pr.should_defer(v.state)


def test_locking_the_screen_after_speech_still_wins(con: sqlite3.Connection) -> None:
    """The other direction, so the fix above cannot be "ignore the lock"."""
    pr.note_heard(con, "wakeword", now_ts=DAY)
    pr.poll_idle(con, idle_probe=lambda: 30.0, lock_probe=lambda: True, now_ts=shift_ts(DAY, 5))
    assert pr.evaluate_presence(con, shift_ts(DAY, 6)).state == "away"


def test_the_spoken_reason_dates_the_lock_not_the_poll(con: sqlite3.Connection) -> None:
    """It is read aloud on "where do you think I am?", so it must not lie.

    "Your screen has been locked for 1 second" after an hour is the poller's
    refresh rate talking, not the evidence.
    """
    for i in range(12):
        pr.poll_idle(
            con, idle_probe=lambda: 600.0, lock_probe=lambda: True, now_ts=shift_ts(DAY, 5 * i)
        )
    reason = pr.evaluate_presence(con, shift_ts(DAY, 60)).reason
    assert "locked for 60 seconds" in reason, reason


def test_an_unlock_restarts_the_clock(con: sqlite3.Connection) -> None:
    """``since`` is carried forward only while the state HOLDS."""
    pr.poll_idle(con, idle_probe=lambda: 10.0, lock_probe=lambda: True, now_ts=DAY)
    pr.poll_idle(con, idle_probe=lambda: 1.0, lock_probe=lambda: False, now_ts=shift_ts(DAY, 5))
    pr.poll_idle(con, idle_probe=lambda: 1.0, lock_probe=lambda: True, now_ts=shift_ts(DAY, 10))
    lock = pr.active_signals(con, shift_ts(DAY, 10))["lock"]
    assert lock.value["since"] == shift_ts(DAY, 10)


def test_an_idle_reading_ages_along_with_its_row() -> None:
    """A reading is as old as the row that carries it.

    The poller wrote "595 seconds idle" and then nothing touched the keyboard —
    or there would be a newer row. Ten seconds later the truth is 605, and a
    verdict that keeps answering 595 sits one whole TTL behind the user.
    """
    assert verdict(sig("idle", {"idle_s": pr.IDLE_AWAY_S - 5}, age_s=0)).state == "maybe"
    assert verdict(sig("idle", {"idle_s": pr.IDLE_AWAY_S - 5}, age_s=10)).state == "away"


def test_returning_to_a_state_is_published_again(db_path: Path) -> None:
    """present -> away -> present is THREE events, not two.

    The bug this pins: with the same wake word row still the newest evidence,
    the second ``present`` reproduces ``since`` exactly, so an idem_key built
    from (state, since, reachable) deduped the "he's back" event against the
    first one and the router never heard about it. A subscriber that only ever
    learns the user left is worse than one that learns nothing.
    """
    c = connect(db_path)
    try:
        pr.note_heard(c, "wakeword", now_ts=DAY)
        assert pr.update_presence(c, now_ts=DAY).state == "present"

        gone = shift_ts(DAY, 40)
        pr.record_signal(c, "probe", {"answered": False}, now_ts=gone)
        assert pr.update_presence(c, now_ts=gone).state == "away"

        back = shift_ts(DAY, 60)
        pr.clear_signal(c, "probe")
        assert pr.update_presence(c, now_ts=back).state == "present"

        states = [
            r["payload"]
            for r in c.execute(
                "SELECT payload FROM events WHERE kind='presence.changed' ORDER BY seq"
            ).fetchall()
        ]
        assert len(states) == 3, states
        assert '"state":"present"' in states[-1]
    finally:
        c.close()


def test_one_unreadable_row_does_not_blind_every_reader(con: sqlite3.Connection) -> None:
    """``value`` is TEXT in a file five processes write to.

    A row that will not parse must cost only the evidence it carried. Raising
    instead would mean one bad write by one channel taking presence down for the
    router, the briefing and every tool that asks where the user is.
    """
    pr.record_signal(con, "idle", {"idle_s": 1.0}, now_ts=DAY)
    con.execute(
        "INSERT INTO presence_signals (source, value, ts, ttl_s) VALUES ('telegram',?,?,600)",
        ("not json at all", DAY),
    )
    assert "telegram" not in pr.active_signals(con, DAY)
    assert pr.evaluate_presence(con, DAY).state == "present"
