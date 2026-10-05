"""Weather, location and time as tools: the place is always named, the guess always admitted."""

from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import mmdb_writer  # noqa: E402
from test_geo import FORECAST, GEOCODE_ANKARA, FakeNet  # noqa: E402

from jarvis.db import connect, migrate  # noqa: E402
from jarvis.geo import (  # noqa: E402
    Locator,  # noqa: E402
    locate,
)
from jarvis.geo import weather as wx
from jarvis.geo.http import GeoUnavailable  # noqa: E402
from jarvis.tools.builtin import world  # noqa: E402
from jarvis.tools.ctx import ToolCtx  # noqa: E402
from jarvis.tools.default import registry  # noqa: E402

pytest.importorskip("maxminddb")

TOKYO = {
    "results": [
        {
            "name": "Tokyo",
            "latitude": 35.6895,
            "longitude": 139.69171,
            "country": "Japan",
            "country_code": "JP",
            "timezone": "Asia/Tokyo",
        }
    ]
}


@pytest.fixture
def con(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "j.db")
    migrate(c)
    yield c
    c.close()


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return mmdb_writer.write(
        tmp_path / "GeoLite2-City.mmdb",
        {
            "81.215.0.0/16": mmdb_writer.city_record(
                city="Kadıköy",
                region="Istanbul",
                country="Türkiye",
                iso="TR",
                lat=40.99,
                lon=29.03,
                tz="Europe/Istanbul",
                accuracy_km=10,
            )
        },
    )


def ctx(con: sqlite3.Connection, locator: Locator, **extra: object) -> ToolCtx:
    return ToolCtx(con=con, channel="desk", actor="desk", extra={world.LOCATOR: locator, **extra})


def net(**routes: object) -> FakeNet:
    base: dict = {
        "https://checkip.amazonaws.com": b"81.215.4.9",
        wx.FORECAST_URL: json.dumps(FORECAST).encode(),
    }
    return FakeNet({**base, **routes})


def test_weather_here_names_the_place_and_admits_it_is_approximate(
    con: sqlite3.Connection, db: Path
) -> None:
    said = world.weather(ctx(con, Locator(db_path=db, fetch=net())))
    assert said.startswith("In Kadıköy, Türkiye it's 18 degrees")
    assert "internet address" in said and "within 10 kilometres" in said


def test_weather_for_a_named_place_is_not_hedged(con: sqlite3.Connection, db: Path) -> None:
    n = net(**{locate.GEOCODE_URL: json.dumps(GEOCODE_ANKARA).encode()})
    said = world.weather(ctx(con, Locator(db_path=db, fetch=n)), place="Ankara", when="tomorrow")
    assert said.startswith("Tomorrow in Ankara, Türkiye: light rain")
    assert "internet address" not in said


def test_imperial_units_come_from_the_composition_root(con: sqlite3.Connection, db: Path) -> None:
    n = net()
    world.weather(ctx(con, Locator(db_path=db, fetch=n), **{world.UNITS: "imperial"}))
    assert any("temperature_unit=fahrenheit" in u for u in n.asked)


def test_an_unknown_window_is_refused_in_words(con: sqlite3.Connection, db: Path) -> None:
    with pytest.raises(world.ToolError, match="not next month"):
        world.weather(ctx(con, Locator(db_path=db, fetch=net())), when="next month")


def test_no_network_is_a_sentence_through_the_registry(con: sqlite3.Connection, db: Path) -> None:
    """Through the registry, where a bare exception would become 'Sorry — weather failed'."""
    dead = FakeNet({"https://": GeoUnavailable("I couldn't reach checkip.amazonaws.com")})
    said = registry().dispatch("weather", {}, ctx(con, Locator(db_path=db, fetch=dead)))
    assert "Sorry" not in said
    assert "couldn't" in said


def test_no_database_tells_you_how_to_get_one(con: sqlite3.Connection, tmp_path: Path) -> None:
    said = registry().dispatch(
        "weather", {}, ctx(con, Locator(db_path=tmp_path / "missing.mmdb", fetch=net()))
    )
    assert "python -m jarvis geo update" in said


def test_where_am_i_by_ip_says_how_it_knows(con: sqlite3.Connection, db: Path) -> None:
    said = world.where_am_i(ctx(con, Locator(db_path=db, fetch=net())))
    assert "Kadıköy in Istanbul, Türkiye" in said
    assert "give or take 10 kilometres" in said
    assert "VPN" in said


def test_where_am_i_with_a_configured_city_does_not_hedge(con: sqlite3.Connection) -> None:
    said = world.where_am_i(ctx(con, Locator(city="Home", latitude=41.0, longitude=29.0)))
    assert said == "You've told me you're in Home."


def test_the_time_somewhere_else_names_the_difference(
    con: sqlite3.Connection, db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVIS_TZ", "Europe/Istanbul")
    n = net(**{locate.GEOCODE_URL: json.dumps(TOKYO).encode()})
    said = world.local_time(ctx(con, Locator(db_path=db, fetch=n)), place="Tokyo")
    assert "in Tokyo, Japan" in said
    assert "6 hours ahead of you" in said


def test_the_time_here_needs_no_network(con: sqlite3.Connection) -> None:
    dead = FakeNet({})
    said = world.local_time(ctx(con, Locator(fetch=dead)))
    assert said.startswith("It's ")
    assert dead.asked == []


def test_every_world_tool_is_offered_everywhere() -> None:
    reg = registry()
    for channel in ("desk", "telegram", "phone", "cli"):
        for name in ("weather", "where_am_i", "local_time"):
            assert name in reg.names(channel), (name, channel)


def test_the_time_here_is_in_the_configured_zone_not_the_process_one(
    con: sqlite3.Connection, db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The app's Settings change [tz]; the desk's process environment stays
    # what it was. "What time is it" must follow the setting, as reminders do.
    monkeypatch.setenv("JARVIS_TZ", "Pacific/Kiritimati")  # UTC+14
    c = ctx(con, Locator(db_path=db, fetch=FakeNet({})))
    c.extra["tz"] = "Pacific/Midway"  # UTC-11: a day apart, never the same hour
    said = world.local_time(c)
    from datetime import datetime
    from zoneinfo import ZoneInfo

    want = datetime.now(ZoneInfo("Pacific/Midway"))
    assert f"{want:%H:%M}" in said or f"{want:%A}" in said
    assert f"on {want:%A}" in said
