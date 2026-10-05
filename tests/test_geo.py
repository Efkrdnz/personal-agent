"""Where "here" is, what the weather is there, and saying so honestly.

The GeoLite2 tests use a REAL .mmdb file, built by ``tests/mmdb_writer.py`` and
read by the official ``maxminddb`` reader — so the lookup is tested against the
actual binary format rather than a mock of it. The network services are fakes
returning the exact JSON shapes those services document.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import mmdb_writer  # noqa: E402

from jarvis.geo import locate, weather  # noqa: E402
from jarvis.geo.http import GeoUnavailable  # noqa: E402
from jarvis.geo.place import Place  # noqa: E402

pytest.importorskip("maxminddb")

ISTANBUL_IP = "81.215.4.9"
MTV_IP = "8.8.8.8"


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return mmdb_writer.write(
        tmp_path / "GeoLite2-City.mmdb",
        {
            "81.215.0.0/16": mmdb_writer.city_record(
                city="Istanbul",
                region="Istanbul",
                country="Türkiye",
                iso="TR",
                lat=41.0136,
                lon=28.955,
                tz="Europe/Istanbul",
                accuracy_km=20,
                city_tr="İstanbul",
            ),
            "8.8.8.0/24": mmdb_writer.city_record(
                city="Mountain View",
                region="California",
                country="United States",
                iso="US",
                lat=37.386,
                lon=-122.0838,
                tz="America/Los_Angeles",
                accuracy_km=1000,
            ),
        },
    )


class FakeNet:
    """Answers by URL prefix, and remembers every URL it was asked for."""

    def __init__(self, routes: dict[str, bytes | Exception]) -> None:
        self.routes = routes
        self.asked: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.asked.append(url)
        for prefix, answer in self.routes.items():
            if url.startswith(prefix):
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise GeoUnavailable(f"no route for {url}")


# ───────────────────────────── GeoLite2 ─────────────────────────────


def test_an_ip_is_placed_by_the_local_database(db: Path) -> None:
    place = locate.lookup(db, ISTANBUL_IP)
    assert (place.name, place.country, place.country_code) == ("Istanbul", "Türkiye", "TR")
    assert place.timezone == "Europe/Istanbul"
    assert place.accuracy_km == 20
    assert place.source == "geoip" and place.approximate


def test_names_come_in_the_users_language_with_english_as_the_fallback(db: Path) -> None:
    assert locate.lookup(db, ISTANBUL_IP, language="tr").name == "İstanbul"
    # No Turkish name for this one: English, never nothing.
    assert locate.lookup(db, MTV_IP, language="tr").name == "Mountain View"


def test_a_private_address_is_explained_not_reported_as_missing(db: Path) -> None:
    """ "not found" for 192.168.1.10 sends people looking for a broken database."""
    with pytest.raises(locate.PlaceNotFound, match="private address"):
        locate.lookup(db, "192.168.1.10")


def test_an_address_the_database_does_not_know(db: Path) -> None:
    with pytest.raises(locate.PlaceNotFound, match="no entry"):
        locate.lookup(db, "1.1.1.1")


def test_no_database_says_how_to_get_one(tmp_path: Path) -> None:
    with pytest.raises(locate.NoDatabase) as exc:
        locate.lookup(tmp_path / "nope.mmdb", ISTANBUL_IP)
    assert "python -m jarvis geo update" in str(exc.value)
    assert "[location]" in str(exc.value)


def test_a_corrupt_database_is_a_sentence(tmp_path: Path) -> None:
    bad = tmp_path / "bad.mmdb"
    bad.write_bytes(b"this is not a maxmind database")
    with pytest.raises(GeoUnavailable, match="won't open"):
        locate.lookup(bad, ISTANBUL_IP)


# ───────────────────────────── the public address ─────────────────────────────


def test_the_public_ip_comes_from_the_first_echo_service_that_answers() -> None:
    net = FakeNet(
        {
            "https://checkip.amazonaws.com": GeoUnavailable("down"),
            "https://api.ipify.org": b"81.215.4.9\n",
        }
    )
    assert locate.public_ip(net) == ISTANBUL_IP
    assert len(net.asked) == 2


def test_an_html_error_page_is_never_taken_for_an_address() -> None:
    net = FakeNet(
        {
            "https://checkip.amazonaws.com": b"<html>503 Service Unavailable</html>",
            "https://api.ipify.org": b"<!doctype html>",
        }
    )
    with pytest.raises(GeoUnavailable, match="public internet address"):
        locate.public_ip(net)


# ───────────────────────────── the locator ─────────────────────────────


def test_configured_coordinates_win_and_ask_the_network_nothing(db: Path) -> None:
    net = FakeNet({})
    place = locate.Locator(city="Home", latitude=41.0, longitude=29.0, db_path=db, fetch=net).here()
    assert place.source == "config" and not place.approximate
    assert net.asked == [], "configured coordinates must not leak a lookup"


def test_a_configured_city_is_geocoded_but_not_hedged(db: Path) -> None:
    net = FakeNet({locate.GEOCODE_URL: json.dumps(GEOCODE_ANKARA).encode()})
    place = locate.Locator(city="Ankara", db_path=db, fetch=net).here()
    assert place.name == "Ankara" and place.source == "config"
    assert place.hedge() == ""


def test_with_nothing_configured_here_is_the_ip_lookup(db: Path) -> None:
    net = FakeNet({"https://checkip.amazonaws.com": b"81.215.4.9"})
    place = locate.Locator(db_path=db, fetch=net).here()
    assert place.name == "Istanbul" and place.approximate


def test_a_configured_ip_skips_the_echo_service(db: Path) -> None:
    net = FakeNet({})
    assert locate.Locator(ip=MTV_IP, db_path=db, fetch=net).here().name == "Mountain View"
    assert net.asked == []


def test_the_public_ip_is_cached_for_a_while_not_forever(db: Path) -> None:
    now = [1000.0]
    net = FakeNet({"https://checkip.amazonaws.com": b"81.215.4.9"})
    loc = locate.Locator(db_path=db, fetch=net, clock=lambda: now[0])
    loc.here()
    loc.here()
    assert len(net.asked) == 1
    now[0] += locate.IP_TTL_S + 1
    loc.here()
    assert len(net.asked) == 2, "a laptop carried to a café must get a new answer"


def test_find_treats_here_and_empty_as_here(db: Path) -> None:
    net = FakeNet({"https://checkip.amazonaws.com": b"81.215.4.9"})
    loc = locate.Locator(db_path=db, fetch=net)
    assert loc.find("").name == "Istanbul"
    assert loc.find("here").name == "Istanbul"


# ───────────────────────────── geocoding ─────────────────────────────

GEOCODE_ANKARA = {
    "results": [
        {
            "name": "Ankara",
            "latitude": 39.91987,
            "longitude": 32.85427,
            "country": "Türkiye",
            "country_code": "TR",
            "admin1": "Ankara",
            "timezone": "Europe/Istanbul",
        }
    ]
}


def test_a_name_geocodes_with_its_country_so_you_hear_which_one() -> None:
    paris = {
        "results": [
            {
                "name": "Paris",
                "latitude": 48.85341,
                "longitude": 2.3488,
                "country": "France",
                "country_code": "FR",
                "timezone": "Europe/Paris",
            }
        ]
    }
    net = FakeNet({locate.GEOCODE_URL: json.dumps(paris).encode()})
    place = locate.geocode("  paris  ", net)
    assert place.label == "Paris, France"
    assert "name=paris" in net.asked[0]


def test_a_name_that_finds_nothing_says_so() -> None:
    net = FakeNet({locate.GEOCODE_URL: b'{"generationtime_ms": 0.4}'})
    with pytest.raises(locate.PlaceNotFound, match="Atlantis"):
        locate.geocode("Atlantis", net)


# ───────────────────────────── weather ─────────────────────────────

FORECAST = {
    "timezone": "Europe/Istanbul",
    "current": {
        "time": "2026-10-05T14:00",
        "temperature_2m": 18.4,
        "apparent_temperature": 15.9,
        "relative_humidity_2m": 72,
        "weather_code": 2,
        "wind_speed_10m": 14.2,
        "is_day": 1,
    },
    "daily": {
        "time": ["2026-10-05", "2026-10-06", "2026-10-07"],
        "weather_code": [2, 61, 0],
        "temperature_2m_max": [21.2, 19.0, 23.6],
        "temperature_2m_min": [14.1, 13.4, 12.9],
        "precipitation_probability_max": [5, 70, 0],
        "sunrise": ["2026-10-05T07:03", "2026-10-06T07:04", "2026-10-07T07:05"],
        "sunset": ["2026-10-05T18:41", "2026-10-06T18:39", "2026-10-07T18:37"],
    },
}

ISTANBUL = Place("Istanbul", 41.0136, 28.955, "geoip", country="Türkiye", accuracy_km=20)
PARIS = Place("Paris", 48.85, 2.35, "geocoded", country="France")


def fc(place: Place = PARIS, units: weather.Units = "metric", body: dict | None = None):
    net = FakeNet({weather.FORECAST_URL: json.dumps(body or FORECAST).encode()})
    return weather.forecast(place, net, units=units), net


def test_now_says_the_place_the_temperature_and_the_sky() -> None:
    f, _ = fc()
    said = weather.spoken_weather(f, "now")
    assert said.startswith("In Paris, France it's 18 degrees and partly cloudy")
    assert "feels like 16" in said
    assert "Today: partly cloudy, high of 21, low of 14." in said


def test_a_small_chance_of_rain_is_not_mentioned_and_a_real_one_is() -> None:
    f, _ = fc()
    assert "percent chance" not in weather.spoken_weather(f, "now")
    assert "70 percent chance of rain" in weather.spoken_weather(f, "tomorrow")


def test_tomorrow_is_its_own_sentence() -> None:
    f, _ = fc()
    assert weather.spoken_weather(f, "tomorrow") == (
        "Tomorrow in Paris, France: light rain, high of 19, low of 13, 70 percent chance of rain."
    )


def test_an_ip_located_forecast_says_it_is_approximate_and_by_how_much() -> None:
    """A VPN can put "here" in another country. The user hears that before dressing for it."""
    f, _ = fc(ISTANBUL)
    said = weather.spoken_weather(f, "now")
    assert "worked out from your internet address" in said
    assert "within 20 kilometres" in said


def test_a_named_place_is_not_hedged() -> None:
    f, _ = fc(PARIS)
    assert "internet address" not in weather.spoken_weather(f, "now")


def test_imperial_units_are_asked_for_and_spoken() -> None:
    f, net = fc(units="imperial")
    assert "temperature_unit=fahrenheit" in net.asked[0]
    assert "wind_speed_unit=mph" in net.asked[0]
    assert "degrees Fahrenheit" in weather.spoken_weather(f, "now")


def test_the_place_learns_its_timezone_from_the_forecast() -> None:
    f, _ = fc(PARIS)
    assert f.place.timezone == "Europe/Istanbul"  # what the fake said, kept


def test_strong_wind_is_mentioned_and_calm_wind_is_not() -> None:
    windy = json.loads(json.dumps(FORECAST))
    windy["current"]["wind_speed_10m"] = 48.0
    f, _ = fc(body=windy)
    assert "wind at 48 kilometres an hour" in weather.spoken_weather(f, "now")
    calm, _ = fc()
    assert "wind" not in weather.spoken_weather(calm, "now")


def test_an_error_reply_from_the_service_is_a_sentence() -> None:
    with pytest.raises(GeoUnavailable, match="Latitude must be in range"):
        fc(body={"error": True, "reason": "Latitude must be in range of -90 to 90°."})


def test_an_unknown_weather_code_is_admitted_not_guessed() -> None:
    assert weather.describe_code(42) == "weather code 42"
    assert weather.describe_code(None) == "unknown conditions"


def test_every_code_reads_after_its() -> None:
    """They are spoken after "it's", so none may start with a capital or "the"."""
    for code in (0, 1, 2, 3, 45, 61, 65, 71, 95, 99):
        phrase = weather.describe_code(code)
        assert phrase == phrase.lower() and not phrase.startswith("the ")
