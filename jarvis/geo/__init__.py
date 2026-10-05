"""Where the user is, and what the sky is doing there.

Three sources of a location, in descending order of trust, and the answer always
says which one it used:

``config``    ``[location]`` in config.toml — a city name or coordinates. Exact,
              because the user said so.
``geocoded``  a place NAME the user asked about ("weather in Paris"), resolved by
              Open-Meteo's free geocoder.
``geoip``     the machine's public IP looked up in a local MaxMind GeoLite2 City
              database. Approximate — GeoLite2 carries an ``accuracy_radius`` and
              a VPN moves you to another country — so every sentence built from
              it says "roughly", with the radius.

NOTHING HERE SPEAKS AND NOTHING HERE KNOWS A CHANNEL. It returns data and the
sentence for it; which voice says the sentence is decided by whoever called. And
every network call goes through an injected ``fetch``, so the whole layer is
testable with no network at all — which is also exactly the situation the user is
in when the Wi-Fi is down, and the errors are written for that.
"""

from __future__ import annotations

from jarvis.geo.http import Fetch, GeoUnavailable, urllib_fetch
from jarvis.geo.locate import Locator, NoDatabase, PlaceNotFound, default_db_path, geocode
from jarvis.geo.place import Place
from jarvis.geo.weather import Forecast, forecast, spoken_weather

__all__ = [
    "Fetch",
    "Forecast",
    "GeoUnavailable",
    "Locator",
    "NoDatabase",
    "Place",
    "PlaceNotFound",
    "default_db_path",
    "forecast",
    "geocode",
    "spoken_weather",
    "urllib_fetch",
]
