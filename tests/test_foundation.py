"""ids, clock and db — the contracts every other module is built on."""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from jarvis import clock, db
from jarvis.ids import canon, dedupe_key, nid, now, parse_ts

# ─── ids ─────────────────────────────────────────────────────────────────────

RFC3339_MS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def test_now_is_utc_rfc3339_with_exactly_three_decimals() -> None:
    # Fixed width matters: these strings are compared lexicographically in SQL,
    # so a varying number of decimals would silently break ordering.
    assert RFC3339_MS.match(now())


def test_now_round_trips_and_is_aware_utc() -> None:
    t = parse_ts(now())
    assert t.tzinfo == UTC


def test_timestamps_sort_lexicographically_in_time_order() -> None:
    a = "2026-09-16T09:59:59.999Z"
    b = "2026-09-16T10:00:00.000Z"
    assert a < b
    assert parse_ts(a) < parse_ts(b)


def test_nid_is_prefixed_and_unique() -> None:
    ids = {nid("req") for _ in range(2000)}
    assert len(ids) == 2000
    assert all(i.startswith("req_") for i in ids)


def test_nid_rejects_a_prefix_that_would_produce_an_unreadable_id() -> None:
    for bad in ("", "req-x", "1req", "req id"):
        with pytest.raises(ValueError):
            nid(bad)


def test_canon_is_key_order_independent() -> None:
    assert canon({"b": 1, "a": 2}) == canon({"a": 2, "b": 1})


def test_canon_preserves_turkish_rather_than_escaping_it() -> None:
    # Escaping would make two identical payloads hash differently depending on
    # how they were parsed — and Turkish text is everywhere in this project.
    assert canon({"k": "üç şey"}) == '{"k":"üç şey"}'


def test_dedupe_key_is_stable_across_key_order_and_sensitive_to_content() -> None:
    a = dedupe_key("job_1", "AskUserQuestion", {"q": 1, "r": 2})
    b = dedupe_key("job_1", "AskUserQuestion", {"r": 2, "q": 1})
    c = dedupe_key("job_1", "AskUserQuestion", {"q": 1, "r": 3})
    assert a == b
    assert a != c
    assert len(a) == 64


def test_dedupe_key_does_not_collide_on_argument_boundaries() -> None:
    # A naive concatenation would make ("ab","c") and ("a","bc") collide.
    assert dedupe_key("ab", "c") != dedupe_key("a", "bc")


# ─── clock ───────────────────────────────────────────────────────────────────


def test_istanbul_is_utc_plus_three_with_no_dst(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVIS_TZ", raising=False)
    # Two dates that would straddle a DST boundary in Europe. Turkey has had no
    # DST since 2016; if this ever fails, the briefing fires at the wrong hour.
    for stamp in ("2026-01-15T09:00:00.000Z", "2026-07-15T09:00:00.000Z"):
        assert clock.spoken_time(stamp) == "12:00"
        assert clock.to_local(stamp).utcoffset().total_seconds() == 3 * 3600


def test_local_zone_is_overridable_for_travel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_TZ", "UTC")
    assert clock.spoken_time("2026-01-15T09:00:00.000Z") == "09:00"


def test_spoken_date_has_no_year_and_names_the_day(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_TZ", "UTC")
    assert clock.spoken_date("2026-09-16T09:00:00.000Z") == "Wednesday 16 September"


# ─── db ──────────────────────────────────────────────────────────────────────


@pytest.fixture()
def dbfile(tmp_path: Path) -> Path:
    return tmp_path / "jarvis.db"


def test_open_db_applies_every_migration_and_sets_user_version(dbfile: Path) -> None:
    con = db.open_db(dbfile)
    assert con.execute("PRAGMA user_version").fetchone()[0] >= 1
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    # The nine areas the architecture names, spot-checked.
    assert {"events", "jobs", "requests", "effects", "spend", "outbox"} <= tables


def test_migrate_is_idempotent(dbfile: Path) -> None:
    con = db.open_db(dbfile)
    v1 = db.migrate(con)
    v2 = db.migrate(con)
    assert v1 == v2


def test_connection_pragmas_are_actually_set(dbfile: Path) -> None:
    # foreign_keys and busy_timeout are PER CONNECTION. Setting them in a
    # migration would silently do nothing for every process opening the file
    # afterwards, which is exactly the bug this asserts against.
    con = db.connect(dbfile)
    assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert con.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert con.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_foreign_keys_are_enforced_not_merely_declared(dbfile: Path) -> None:
    con = db.open_db(dbfile)
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO requests(id,job_id,kind,state,dedupe_key,short_label,"
            "presentation,payload,created_at) VALUES"
            "('r1','job_does_not_exist','plan_question','pending','k','l','{}','{}','t')"
        )


def test_writes_are_visible_to_another_process_without_an_explicit_commit(dbfile: Path) -> None:
    # Spike S1 lost an answer to a deferred transaction rolled back on close.
    # This is that bug, pinned: one connection writes, closes; another reads.
    con = db.open_db(dbfile)
    con.execute("INSERT INTO cursors(name,value,updated_at) VALUES ('k','v',?)", (now(),))
    con.close()

    other = db.connect(dbfile)
    assert other.execute("SELECT value FROM cursors WHERE name='k'").fetchone()[0] == "v"


def test_tx_rolls_back_the_whole_unit_on_failure(dbfile: Path) -> None:
    con = db.open_db(dbfile)
    with pytest.raises(RuntimeError), db.tx(con):
        con.execute("INSERT INTO cursors(name,value,updated_at) VALUES ('a','1',?)", (now(),))
        raise RuntimeError("boom")
    assert con.execute("SELECT count(*) FROM cursors").fetchone()[0] == 0
    # And the connection is usable afterwards — a leaked transaction would wedge
    # every other process on the file.
    con.execute("INSERT INTO cursors(name,value,updated_at) VALUES ('b','2',?)", (now(),))
    assert con.execute("SELECT count(*) FROM cursors").fetchone()[0] == 1


def test_tx_commits_on_success(dbfile: Path) -> None:
    con = db.open_db(dbfile)
    with db.tx(con):
        con.execute("INSERT INTO cursors(name,value,updated_at) VALUES ('c','3',?)", (now(),))
    row = db.connect(dbfile).execute("SELECT value FROM cursors WHERE name='c'").fetchone()
    assert row[0] == "3"


def test_default_path_is_not_in_the_repo_and_is_overridable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The DB holds the activity log, every spend row, and the text of every
    # question ever asked. It must never land in the git tree.
    monkeypatch.delenv("JARVIS_DB", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert db.default_path() == tmp_path / "jarvis" / "jarvis.db"
    monkeypatch.setenv("JARVIS_DB", "/tmp/elsewhere.db")
    assert db.default_path() == Path("/tmp/elsewhere.db")


def test_spine_imports_with_no_third_party_packages() -> None:
    """The spine must load on a bare interpreter.

    Every process opens this database, including ones that will never install
    google-genai or the Claude SDK. A stray import here would couple them all.
    """
    src = Path(__file__).parent.parent
    r = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            f"import sys; sys.path.insert(0, {str(src)!r}); "
            "import jarvis.db, jarvis.ids, jarvis.clock; print('ok')",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONNOUSERSITE": "1", "PYTHONPATH": ""},
    )
    assert r.returncode == 0, r.stderr
    assert "ok" in r.stdout


def test_sqlite_is_new_enough_for_update_returning(dbfile: Path) -> None:
    # The first-answer-wins compare-and-swap in jarvis.requests has no atomic
    # form without RETURNING, so this is a hard floor rather than a nicety.
    con = db.open_db(dbfile)
    con.execute("INSERT INTO cursors(name,value,updated_at) VALUES ('x','1',?)", (now(),))
    row = con.execute(
        "UPDATE cursors SET value='2' WHERE name='x' AND value='1' RETURNING value"
    ).fetchone()
    assert row[0] == "2"


def test_migrations_are_numbered_and_ordered() -> None:
    files = sorted(db.MIGRATIONS_DIR.glob("*.sql"))
    assert files, "no migrations found"
    for f in files:
        assert re.match(r"^\d{3}_[a-z0-9_]+\.sql$", f.name), f.name


def test_migration_contains_no_connection_scoped_pragmas() -> None:
    # If a PRAGMA foreign_keys line reappears in a migration, someone has
    # reintroduced the bug where it silently applies to exactly one connection.
    for f in db.MIGRATIONS_DIR.glob("*.sql"):
        for line in f.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("--"):
                continue
            assert "PRAGMA foreign_keys" not in stripped, f"{f.name}: {line}"
            assert "PRAGMA busy_timeout" not in stripped, f"{f.name}: {line}"


def test_dontask_permission_mode_is_rejected_by_the_schema(dbfile: Path) -> None:
    # dontAsk DENIES AskUserQuestion, which would silently break plan mode —
    # the single feature this whole project exists for. The schema refuses it.
    con = db.open_db(dbfile)
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO jobs(id,kind,title,state,created_at,updated_at,created_by,"
            "permission_mode) VALUES ('j1','claude_code','t','queued',?,?,'test','dontAsk')",
            (now(), now()),
        )


def test_datetime_helpers_agree_with_the_stdlib() -> None:
    before = datetime.now(UTC)
    t = parse_ts(now())
    after = datetime.now(UTC)
    assert before.replace(microsecond=0) <= t <= after
