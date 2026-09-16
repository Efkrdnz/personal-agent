"""The spend ledger, tested where it lies.

The happy path of this module is one INSERT and one SUM, and neither is where it
breaks. Four kinds of test here:

HONESTY. The module's entire reason to exist is that the units do not reconcile.
So the tests assert that a rate-limit window never becomes dollars, that three
call meters are never added together, and — the load-bearing one — that every
unpriced meter is NAMED in the spoken sentence. :func:`test_spoken_line_names_every_unpriced_meter`
walks the meters it recorded and demands each one by name, so a future edit that
drops the unpriced clause (or quietly stops listing one provider) fails here
rather than turning "$0.00" into "today was free" out loud.

RACES use two real connections to one file, which is exactly what two daemons
are: SQLite locks per connection, not per process. Both racers are actually
started against one barrier and the test counts how many warnings came out.

RESTARTS write on one connection, close it, and assert from another — including
the threshold latch, because the process that crosses a threshold is usually not
the process still running when the next check happens.

REFUSALS. Every guard is proved to fire AND to leave the table untouched, since a
half-written spend row is worse than a rejected one.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from jarvis import ledger as led
from jarvis.db import connect, migrate
from jarvis.ids import now, parse_ts

# A config with round numbers, so a failure message reads like arithmetic.
CFG = led.LedgerConfig(threshold_usd=20.0, warn_ratio=0.8)
API_KEY_CFG = led.LedgerConfig(threshold_usd=20.0, claude_auth="api_key")


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """A throwaway database file. Never the real one at ~/.local/state."""
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


def _ago(seconds: float) -> str:
    return led._stamp(parse_ts(now()) - timedelta(seconds=seconds))


def _rows(con: sqlite3.Connection) -> int:
    return int(con.execute("SELECT COUNT(*) AS n FROM spend").fetchone()["n"])


# ───────────────────────── the contract with ids.py ─────────────────────────


def test_window_bound_format_matches_ids_now() -> None:
    """Window bounds are compared to ``ts`` lexicographically in SQL.

    That comparison is only true if both sides are the same fixed-width format,
    so the day ids.now() changes shape this test must fail rather than the
    windows quietly starting to include or exclude the wrong rows.
    """
    stamp = now()
    assert led._stamp(parse_ts(stamp)) == stamp


# ───────────────────────── windows ─────────────────────────


def test_window_excludes_what_falls_out_of_it(con: sqlite3.Connection) -> None:
    led.record(con, "gemini", "usd_est", 1.0, usd_equiv=1.0, ts=_ago(6 * 3600))
    led.record(con, "gemini", "usd_est", 2.0, usd_equiv=2.0, ts=_ago(60))

    assert led.status(con, "5h", config=CFG).priced_usd == 2.0
    assert led.status(con, timedelta(hours=7), config=CFG).priced_usd == 3.0
    assert led.status(con, "all", config=CFG).priced_usd == 3.0


@pytest.mark.parametrize("bad", ["", "5", "5x", "hour", "-3h", "0h", "yesterday", "  "])
def test_unparseable_window_raises_rather_than_widening(bad: str) -> None:
    """A typo must not silently become all-time: that direction under-reports the
    percentage against the threshold, which is the direction that hurts."""
    with pytest.raises(ValueError):
        led.window_bounds(bad)


def test_negative_timedelta_window_raises() -> None:
    with pytest.raises(ValueError):
        led.window_bounds(timedelta(hours=-1))


def test_today_follows_the_speaking_zone_not_utc(
    con: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'Today' means the day the human is in. Two zones 25 hours apart put the
    same instant on different sides of local midnight, and the ledger must
    agree with the human, not with UTC."""
    monkeypatch.setenv("JARVIS_TZ", "Pacific/Kiritimati")  # UTC+14
    east, _ = led.window_bounds("today")
    monkeypatch.setenv("JARVIS_TZ", "Pacific/Niue")  # UTC-11
    west, _ = led.window_bounds("today")
    assert east is not None and west is not None
    later, earlier = max(east, west), min(east, west)
    assert later != earlier

    # A row placed between the two local midnights belongs to one day and not
    # the other.
    between = led._stamp(parse_ts(later) - timedelta(seconds=30))
    led.record(con, "gemini", "usd_est", 5.0, usd_equiv=5.0, ts=between)

    monkeypatch.setenv("JARVIS_TZ", "Pacific/Niue" if west == later else "Pacific/Kiritimati")
    assert led.status(con, "today", config=CFG).priced_usd == 0.0
    monkeypatch.setenv("JARVIS_TZ", "Pacific/Kiritimati" if west == later else "Pacific/Niue")
    assert led.status(con, "today", config=CFG).priced_usd == 5.0


# ───────────────────────── refusals ─────────────────────────


def test_a_rate_window_can_never_be_priced(con: sqlite3.Connection) -> None:
    """ADR 0004: usd_equiv is NULL for a subscription rate window. Pricing one
    would put a fiction straight into priced_usd, which is the failure this
    whole module exists to prevent."""
    with pytest.raises(ValueError, match="not money"):
        led.record(con, "claude_code", "rate_window_pct", 50.0, usd_equiv=3.0)
    assert _rows(con) == 0


@pytest.mark.parametrize(
    ("kwargs", "args"),
    [
        ({}, ("gemini", "gemini_sec", -1.0)),
        ({}, ("gemini", "gemini_sec", float("nan"))),
        ({}, ("gemini", "gemini_sec", float("inf"))),
        ({"usd_equiv": -0.5}, ("gemini", "gemini_sec", 1.0)),
        ({"usd_equiv": float("inf")}, ("gemini", "gemini_sec", 1.0)),
        ({}, ("", "gemini_sec", 1.0)),
        ({}, ("gemini", "  ", 1.0)),
        ({}, ("claude_code", "rate_window_pct", 101.0)),
    ],
)
def test_bad_rows_are_refused_and_nothing_is_written(
    con: sqlite3.Connection, kwargs: dict[str, float], args: tuple[str, str, float]
) -> None:
    with pytest.raises(ValueError):
        led.record(con, *args, **kwargs)
    assert _rows(con) == 0


def test_off_format_timestamp_is_refused(con: sqlite3.Connection) -> None:
    """A row stamped '2026-09-16 10:00:00' sorts before every RFC3339 row and
    would silently vanish from every window."""
    with pytest.raises(ValueError):
        led.record(con, "gemini", "gemini_sec", 1.0, ts="2026-09-16 10:00:00")
    assert _rows(con) == 0


def test_config_validation_refuses_nonsense() -> None:
    with pytest.raises(ValueError):
        led.LedgerConfig(threshold_usd=0.0)
    with pytest.raises(ValueError):
        led.LedgerConfig(warn_ratio=1.5)
    with pytest.raises(ValueError):
        led.LedgerConfig(claude_auth="oauth")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        led.LedgerConfig(rate_window_warn_pct=99.0, rate_window_over_pct=90.0)


def test_config_from_mapping_survives_a_stale_toml_key() -> None:
    cfg = led.LedgerConfig.from_mapping({"threshold_usd": 5.0, "leftover_from_last_year": True})
    assert cfg.threshold_usd == 5.0 and cfg.claude_auth == "subscription"


# ───────────────────────── the units that do not reconcile ─────────────────────────


def test_rate_windows_are_a_position_not_a_running_total(con: sqlite3.Connection) -> None:
    """Two readings of the same window do not add up. 40 then 62 is 62."""
    led.record_claude_code(con, rate_window_pct=40.0)
    led.record_claude_code(con, rate_window_pct=62.0)

    meter = led.status(con, "5h", config=CFG).by_provider["claude_code"].meters[0]
    assert meter.unit == "rate_window_pct"
    assert meter.amount == 62.0
    assert meter.rows == 2


def test_claude_under_a_subscription_is_unpriced_and_named_verbatim(
    con: sqlite3.Connection,
) -> None:
    """The doc's canonical string, quoted. It is the example the whole idea is
    built on; an edit that reworded it would change what a person hears."""
    led.record_claude_code(con, config=CFG, rate_window_pct=47.0, usd_est=0.87, tokens=120_000)

    st = led.status(con, "5h", config=CFG)
    assert st.priced_usd == 0.0
    assert "claude_code (Max subscription: rate-limit windows, not dollars)" in st.unpriced
    # The dollar figure the CLI reported is KEPT — it is wanted the day someone
    # compares the subscription to metered billing — but it is not money.
    usd_meter = next(m for m in st.by_provider["claude_code"].meters if m.unit == "usd_est")
    assert usd_meter.amount == 0.87 and usd_meter.usd_equiv is None


def test_the_same_turn_under_an_api_key_is_money(con: sqlite3.Connection) -> None:
    """Auth mode is config, not a literal: the same call priced differently."""
    led.record_claude_code(con, config=API_KEY_CFG, rate_window_pct=47.0, usd_est=0.87)

    st = led.status(con, "5h", config=API_KEY_CFG)
    assert st.priced_usd == 0.87
    # The rate window is STILL not dollars, under either auth mode.
    assert any("rate-limit windows, not dollars" in u for u in st.unpriced)


def test_record_claude_code_with_nothing_to_record_raises(con: sqlite3.Connection) -> None:
    with pytest.raises(ValueError):
        led.record_claude_code(con, config=CFG)


def test_the_three_call_meters_are_never_added_up(con: sqlite3.Connection) -> None:
    """Agent minutes, SIP minutes and carrier minutes bill on three cycles at
    three rates. 7.5 + 11 + 13 is a number that is true of nothing, and it must
    not appear anywhere in the status or the sentence."""
    led.record(con, *led.LIVEKIT_AGENT_MIN, 7.5)
    led.record(con, *led.LIVEKIT_SIP_MIN, 11.0)
    led.record(con, *led.CARRIER_MIN, 13.0)

    st = led.status(con, "5h", config=CFG)
    assert len(st.by_provider["livekit"].meters) == 2
    assert len(st.unpriced) == 3
    said = led.spoken_status(st)
    for forbidden in ("31.5", "24.0", "18.5", "20.5"):  # any pair or the whole sum
        assert forbidden not in said
    for required in ("agent minutes", "SIP minutes", "carrier minutes"):
        assert required in said


def test_an_unknown_meter_is_never_silently_priced(con: sqlite3.Connection) -> None:
    """record() accepts an unplanned unit — losing the measurement at 3am is
    worse — but an unpriced unit it has no rule for must still be spoken."""
    led.record(con, "some_new_api", "widgets", 12.0)

    st = led.status(con, "5h", config=CFG)
    assert st.priced_usd == 0.0
    assert len(st.unpriced) == 1
    assert "some new api" in led.spoken_status(st)


def test_a_partly_priced_meter_is_counted_and_still_named(con: sqlite3.Connection) -> None:
    """Half a conversion is a gap too: the dollars count, and the provider is
    still listed, so nobody reads the total as complete."""
    led.record(con, "gemini", "gemini_sec", 100.0, usd_equiv=0.50)
    led.record(con, "gemini", "gemini_sec", 200.0)  # price table had no answer

    st = led.status(con, "5h", config=CFG)
    assert st.priced_usd == 0.50
    assert any(u.startswith("gemini (") for u in st.unpriced)


# ───────────────────────── the spoken line ─────────────────────────


def test_spoken_line_names_every_unpriced_meter(con: sqlite3.Connection) -> None:
    """THE test. Every meter that is not in the dollar figure must be audible.

    Written to walk the meters rather than to match one fixed sentence, so a
    change that drops the unpriced clause — or that keeps the clause but quietly
    stops listing one provider — fails here.
    """
    led.record_claude_code(con, config=CFG, rate_window_pct=47.0)
    led.record(con, "gemini", "gemini_sec", 312.0)
    led.record(con, *led.LIVEKIT_AGENT_MIN, 7.5)
    led.record(con, *led.LIVEKIT_SIP_MIN, 11.0)
    led.record(con, *led.CARRIER_MIN, 13.0)
    led.record(con, "gemini", "usd_est", 1.25, usd_equiv=1.25)

    st = led.status(con, "5h", config=CFG)
    said = led.spoken_status(st)

    assert len(st.unpriced) == 5
    # Every entry of `unpriced`, provider and reason, reaches the sentence.
    for entry in st.unpriced:
        provider, _, reason = entry.partition(" (")
        assert provider.replace("_", " ") in said, f"{provider} missing from: {said}"
        assert reason.rstrip(")") in said, f"reason for {provider} missing from: {said}"
    # And the distinguishing words of each meter, by name.
    for phrase in (
        "rate-limit windows, not dollars",
        "unverified price table",
        "agent minutes",
        "SIP minutes",
        "carrier minutes",
    ):
        assert phrase in said, f"{phrase!r} missing from: {said}"
    assert "does not include" in said


def test_zero_priced_is_never_spoken_as_free(con: sqlite3.Connection) -> None:
    """A Max subscription can work all day for $0.00. The sentence must say so
    in a way nobody can hear as 'today cost nothing'."""
    led.record_claude_code(con, config=CFG, rate_window_pct=61.0)
    led.record(con, *led.CARRIER_MIN, 4.0)

    st = led.status(con, "today", config=CFG)
    assert st.priced_usd == 0.0
    said = led.spoken_status(st)
    assert "Zero priced is not the same as free." in said
    assert "claude code" in said and "carrier minutes" in said


def test_an_empty_ledger_says_nothing_recorded_not_zero_dollars(
    con: sqlite3.Connection,
) -> None:
    """No rows and no meters is a different fact from 'zero dollars spent', and
    conflating them would make an unconfigured meter sound like thrift."""
    said = led.spoken_status(led.status(con, "5h", config=CFG))
    assert said == "Nothing recorded in the last 5 hours."


def test_a_fully_priced_window_says_so(con: sqlite3.Connection) -> None:
    led.record(con, "gemini", "usd_est", 3.0, usd_equiv=3.0, estimated=False)
    said = led.spoken_status(led.status(con, "5h", config=CFG))
    assert "Every meter this window converts to dollars." in said
    assert "estimated" not in said  # this figure came off an invoice


def test_an_estimated_dollar_figure_says_it_is_estimated(con: sqlite3.Connection) -> None:
    led.record(con, "gemini", "usd_est", 3.0, usd_equiv=3.0, estimated=True)
    assert "an estimated 3 dollars" in led.spoken_status(led.status(con, "5h", config=CFG))


def test_an_unpriced_estimate_does_not_make_an_invoice_an_estimate(
    con: sqlite3.Connection,
) -> None:
    """A measured invoice line does not become an estimate because an unpriced
    meter sits next to it; if it did, the honest word would mean nothing."""
    led.record(con, "gemini", "usd_est", 3.0, usd_equiv=3.0, estimated=False)
    led.record_claude_code(con, config=CFG, rate_window_pct=20.0)

    st = led.status(con, "5h", config=CFG)
    assert st.priced_estimated is False
    assert "an estimated" not in led.spoken_status(st)


# ───────────────────────── thresholds: once per crossing ─────────────────────────


def test_threshold_fires_once_and_then_stays_quiet(con: sqlite3.Connection) -> None:
    led.record(con, "gemini", "usd_est", 17.0, usd_equiv=17.0)

    first = led.check_threshold(con, "5h", config=CFG)
    assert first is not None and first.level == "warn"
    assert "warning line" in first.spoken

    assert led.check_threshold(con, "5h", config=CFG) is None
    # More spend at the SAME level is not a new crossing.
    led.record(con, "gemini", "usd_est", 0.5, usd_equiv=0.5)
    assert led.check_threshold(con, "5h", config=CFG) is None


def test_escalation_to_over_is_a_new_crossing(con: sqlite3.Connection) -> None:
    led.record(con, "gemini", "usd_est", 17.0, usd_equiv=17.0)
    assert led.check_threshold(con, "5h", config=CFG).level == "warn"

    led.record(con, "gemini", "usd_est", 5.0, usd_equiv=5.0)
    second = led.check_threshold(con, "5h", config=CFG)
    assert second is not None and second.level == "over"
    assert "ceiling" in second.spoken
    assert led.check_threshold(con, "5h", config=CFG) is None


def test_falling_back_re_arms_so_the_next_crossing_fires(con: sqlite3.Connection) -> None:
    """'Once per crossing', not 'once ever'. A rate window that drains and
    refills is two crossings and deserves two warnings."""
    led.record_claude_code(con, config=CFG, rate_window_pct=84.0)
    assert led.check_threshold(con, "5h", config=CFG) is not None
    assert led.check_threshold(con, "5h", config=CFG) is None

    led.record_claude_code(con, config=CFG, rate_window_pct=12.0)  # window rolled over
    assert led.check_threshold(con, "5h", config=CFG) is None  # the reset is SILENT

    led.record_claude_code(con, config=CFG, rate_window_pct=88.0)
    again = led.check_threshold(con, "5h", config=CFG)
    assert again is not None and again.level == "warn"


def test_the_two_ladders_latch_independently(con: sqlite3.Connection) -> None:
    """ADR 0004: under a subscription 'near the limit' is a rate window, not a
    currency amount — and both can be true. Crossing one must not suppress the
    other, and must not re-announce it either."""
    led.record(con, "gemini", "usd_est", 17.0, usd_equiv=17.0)
    first = led.check_threshold(con, "5h", config=CFG)
    assert first is not None
    assert [s.name for s in first.signals] == ["priced_usd"]

    led.record_claude_code(con, config=CFG, rate_window_pct=90.0)
    second = led.check_threshold(con, "5h", config=CFG)
    assert second is not None
    assert [s.name for s in second.signals] == ["claude_code:rate_window_pct"]
    assert "Priced spend" not in second.spoken


def test_a_warning_names_the_unpriced_meters_too(con: sqlite3.Connection) -> None:
    """The warning is the moment somebody acts on the number, so it is the worst
    possible moment to present the priced part as the whole bill."""
    led.record(con, "gemini", "usd_est", 17.0, usd_equiv=17.0)
    led.record(con, *led.CARRIER_MIN, 9.0)

    crossing = led.check_threshold(con, "5h", config=CFG)
    assert crossing is not None
    assert "carrier minutes" in crossing.spoken


def test_a_corrupt_latch_re_arms_rather_than_raising(con: sqlite3.Connection) -> None:
    led.record(con, "gemini", "usd_est", 17.0, usd_equiv=17.0)
    assert led.check_threshold(con, "5h", config=CFG) is not None
    con.execute("UPDATE cursors SET value='{not json' WHERE name LIKE 'spend_threshold:%'")

    # One duplicated warning beats a ledger that refuses to answer at all.
    assert led.check_threshold(con, "5h", config=CFG) is not None


def test_different_windows_keep_different_latches(con: sqlite3.Connection) -> None:
    led.record(con, "gemini", "usd_est", 17.0, usd_equiv=17.0)
    assert led.check_threshold(con, "5h", config=CFG) is not None
    assert led.check_threshold(con, "today", config=CFG) is not None
    assert led.check_threshold(con, "5h", config=CFG) is None


def test_check_threshold_leaves_no_transaction_open(con: sqlite3.Connection) -> None:
    """A ledger read that parked a write lock would block every other process."""
    led.record(con, "gemini", "usd_est", 17.0, usd_equiv=17.0)
    led.check_threshold(con, "5h", config=CFG)
    assert con.in_transaction is False
    led.check_threshold(con, "5h", config=CFG)
    assert con.in_transaction is False


def test_check_threshold_composes_into_a_callers_transaction(
    db_path: Path, con: sqlite3.Connection
) -> None:
    """A tool that records the turn and checks the threshold in one atom must
    get both or neither — a warning about a row that was rolled back is a lie."""
    from jarvis.db import tx

    with pytest.raises(RuntimeError), tx(con):
        led.record(con, "gemini", "usd_est", 25.0, usd_equiv=25.0)
        assert led.check_threshold(con, "5h", config=CFG) is not None
        raise RuntimeError("the caller's atom fails after the check")

    other = connect(db_path)
    try:
        assert _rows(other) == 0
        # The latch went back with it, so the crossing can still be announced.
        assert led.check_threshold(other, "5h", config=CFG) is None
        led.record(other, "gemini", "usd_est", 25.0, usd_equiv=25.0)
        assert led.check_threshold(other, "5h", config=CFG) is not None
    finally:
        other.close()


# ───────────────────────── two processes, one file ─────────────────────────


def test_two_processes_produce_exactly_one_warning(db_path: Path) -> None:
    """The race the latch exists for: the dispatcher and the desk both notice
    the same crossing in the same second."""
    writer = connect(db_path)
    led.record(writer, "gemini", "usd_est", 21.0, usd_equiv=21.0)
    writer.close()

    results: list[led.ThresholdCrossing | None] = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def racer() -> None:
        c = connect(db_path)
        try:
            barrier.wait(timeout=10)
            out = led.check_threshold(c, "5h", config=CFG)
        finally:
            c.close()
        with lock:
            results.append(out)

    threads = [threading.Thread(target=racer) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert len(results) == 2
    assert sum(r is not None for r in results) == 1, "a crossing must be claimed exactly once"


def test_a_write_is_visible_to_another_connection_without_a_commit_call(
    db_path: Path,
) -> None:
    """Spike S1's silent rollback, guarded: jarvis.db opens autocommit, so a
    spend row written by one process is readable by another immediately and
    survives the writer closing without an explicit commit."""
    writer = connect(db_path)
    reader = connect(db_path)
    try:
        led.record(writer, "gemini", "usd_est", 2.5, usd_equiv=2.5)
        assert led.status(reader, "5h", config=CFG).priced_usd == 2.5
        writer.close()
        assert led.status(reader, "5h", config=CFG).priced_usd == 2.5
    finally:
        reader.close()


def test_state_survives_a_restart(db_path: Path) -> None:
    """Write with one connection, die, assert with another — because the process
    that spent the money is usually gone by the time anyone asks what it cost."""
    first = connect(db_path)
    led.record_claude_code(first, config=CFG, rate_window_pct=47.0)
    led.record(first, "gemini", "usd_est", 17.0, usd_equiv=17.0)
    led.record(first, *led.CARRIER_MIN, 6.0)
    crossing = led.check_threshold(first, "5h", config=CFG)
    assert crossing is not None
    first.close()

    second = connect(db_path)
    try:
        st = led.status(second, "5h", config=CFG)
        assert st.priced_usd == 17.0
        assert len(st.unpriced) == 2
        assert "carrier minutes" in led.spoken_status(st)
        # The warning does NOT repeat just because the daemon restarted.
        assert led.check_threshold(second, "5h", config=CFG) is None
    finally:
        second.close()


def test_a_racer_that_waited_for_the_lock_does_not_re_announce_a_crossing(
    db_path: Path,
) -> None:
    """The race the two-racer test cannot see: both of those read the SAME
    totals, so a stale snapshot never shows up.

    Here one process reads while another is mid-transaction, then blocks on the
    write lock. If the snapshot were taken before the lock, the waiter would
    wake up holding a view of `spend` the winner had already moved past, write
    `warn` over the winner's `over`, and the very next check would announce the
    same crossing a second time. 'Once per crossing' has to survive a slow
    loser, not just a simultaneous one.
    """
    winner = connect(db_path)
    led.record(winner, "gemini", "usd_est", 17.0, usd_equiv=17.0)  # warn, unannounced

    loser_saw: list[led.ThresholdCrossing | None] = []
    entered = threading.Event()

    def loser() -> None:
        c = connect(db_path)
        try:
            entered.set()
            loser_saw.append(led.check_threshold(c, "5h", config=CFG))
        finally:
            c.close()

    try:
        winner.execute("BEGIN IMMEDIATE")  # the loser will queue behind this
        t = threading.Thread(target=loser)
        t.start()
        entered.wait(timeout=10)
        time.sleep(0.3)  # long enough for the loser to be blocked on the lock
        led.record(winner, "gemini", "usd_est", 8.0, usd_equiv=8.0)  # now 25: over
        claimed = led.check_threshold(winner, "5h", config=CFG)
        winner.execute("COMMIT")
        t.join(timeout=20)

        assert claimed is not None and claimed.level == "over"
        assert loser_saw == [None], "the loser must not claim a crossing the winner took"
        # THE POINT: nobody hears about 25 dollars twice.
        assert led.check_threshold(winner, "5h", config=CFG) is None
    finally:
        winner.close()


def test_the_latch_matches_the_totals_it_was_written_from(db_path: Path) -> None:
    """A latch left disagreeing with `spend` is a warning owed or a warning
    repeated, and a fresh process has no way to tell which."""
    con = connect(db_path)
    try:
        led.record(con, "gemini", "usd_est", 25.0, usd_equiv=25.0)
        assert led.check_threshold(con, "5h", config=CFG) is not None
        stored = con.execute(
            "SELECT value FROM cursors WHERE name LIKE ?", (f"{led.LATCH_PREFIX}%",)
        ).fetchone()["value"]
    finally:
        con.close()

    other = connect(db_path)
    try:
        live = {
            s.name: s.level
            for s in led.threshold_signals(led.status(other, "5h", config=CFG), CFG)
            if s.level != "ok"
        }
        assert led._decode_latch(stored) == live
    finally:
        other.close()


# ───────────────────────── the sentence, said out loud ─────────────────────────


def test_under_a_dollar_is_said_in_cents(con: sqlite3.Connection) -> None:
    """'0 dollars 87' is heard as a broken robot, and the one thing this line
    cannot afford is to sound broken at the moment it reports money."""
    led.record(con, "gemini", "usd_est", 0.87, usd_equiv=0.87)

    said = led.spoken_status(led.status(con, "5h", config=CFG))
    assert "87 cents" in said
    assert "0 dollars" not in said


@pytest.mark.parametrize(
    ("usd", "expected"),
    [(0.01, "1 cent"), (0.87, "87 cents"), (1.0, "1 dollar"), (3.5, "3 dollars 50")],
)
def test_money_is_said_the_way_a_person_says_it(usd: float, expected: str) -> None:
    assert led._spoken_money(usd) == expected


# ───────────────────────── config from a hand-edited file ─────────────────────────


@pytest.mark.parametrize(
    "bad",
    [
        {"threshold_usd": "20"},  # quoted in the TOML
        {"threshold_usd": True},  # `threshold_usd = true`: an int in Python, == $1
        {"warn_ratio": "0.8"},
        {"rate_window_warn_pct": None},
        {"gemini_usd_per_sec": "0.001"},
    ],
)
def test_a_mistyped_config_value_is_refused_as_a_value_error(bad: dict[str, object]) -> None:
    """The daemon that boots on a config it half-understood is worse than the one
    that refuses: `threshold_usd = true` is a ONE DOLLAR ceiling that would warn
    on every turn, and a quoted number crashes with TypeError somewhere far from
    the file that caused it."""
    with pytest.raises(ValueError):
        led.LedgerConfig.from_mapping(bad)
