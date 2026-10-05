"""Where "here" is: configured, then GeoLite2, and never a silent guess.

GEOLITE2 IS A LOCAL FILE. MaxMind's GeoLite2 City database maps an IP address to
a city with an accuracy radius, entirely offline: the lookup sends nothing
anywhere. The one network call is finding the machine's PUBLIC address, because
the address the machine knows about itself is almost always a private one
(192.168.x.x) that no database can place. That call goes to a service that sees
only that a request arrived — which it would see anyway — and it can be skipped
entirely by setting ``[location] ip`` or coordinates in config.toml.

``maxminddb`` IS IMPORTED LAZILY, inside :func:`lookup`. The package is an
optional extra (``.[geo]``); the rest of Jarvis must import, and the weather for
a NAMED place must still work, on a machine without it.
"""

from __future__ import annotations

import ipaddress
import os
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jarvis.geo.http import Fetch, GeoUnavailable, get_json, urllib_fetch
from jarvis.geo.place import Place

__all__ = [
    "DB_FILENAME",
    "IP_ECHO_URLS",
    "Locator",
    "NoDatabase",
    "PlaceNotFound",
    "default_db_path",
    "geocode",
    "lookup",
    "public_ip",
]

DB_FILENAME = "GeoLite2-City.mmdb"

#: Services that answer "what is my public address" with the address and nothing
#: else. Two, so one being down is not "I don't know where you are". Plain-text
#: first: there is no JSON to be malformed.
IP_ECHO_URLS: tuple[str, ...] = (
    "https://checkip.amazonaws.com",
    "https://api.ipify.org",
)

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"

#: How long a discovered public IP is trusted. Long enough that asking for the
#: weather three times does not mean three lookups; short enough that a laptop
#: carried from home to a café gets a new answer the same morning.
IP_TTL_S = 15 * 60


class NoDatabase(GeoUnavailable):
    """There is no GeoLite2 file, so an IP cannot be placed. Says how to get one."""

    def __init__(self, path: Path) -> None:
        super().__init__(
            f"I don't have a location database at {path}. Run `python -m jarvis geo update` "
            "with a free MaxMind licence key, or set your city in config.toml under [location]."
        )
        self.path = path


class PlaceNotFound(GeoUnavailable):
    """A name that geocodes to nothing, or an IP the database cannot place."""


def default_db_path() -> Path:
    """``$XDG_DATA_HOME/jarvis/GeoLite2-City.mmdb``. Data, not config and not state."""
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "jarvis" / DB_FILENAME


def public_ip(fetch: Fetch = urllib_fetch) -> str:
    """The machine's address as the internet sees it. Raises GeoUnavailable."""
    last: Exception | None = None
    for url in IP_ECHO_URLS:
        try:
            text = fetch(url).decode("ascii", "replace").strip()
            ip = text.split()[0] if text else ""
            ipaddress.ip_address(ip)  # an HTML error page must not become an address
            return ip
        except (GeoUnavailable, ValueError, IndexError) as exc:
            last = exc
    raise GeoUnavailable(
        "I couldn't find your public internet address, so I can't tell where you are."
    ) from last


def _name(node: dict[str, Any] | None, language: str) -> str | None:
    """MaxMind stores names per language. Fall back to English, never to nothing."""
    names = (node or {}).get("names") or {}
    return names.get(language) or names.get("en") or None


def lookup(db_path: Path, ip: str, *, language: str = "en") -> Place:
    """Place one IP with the local GeoLite2 file. No network."""
    addr = ipaddress.ip_address(ip)
    if addr.is_private or addr.is_loopback or addr.is_link_local:
        # Said in words, because "not found" for 192.168.1.10 sends people
        # looking for a broken database when the database is fine.
        raise PlaceNotFound(f"{ip} is a private address; no database can place it")
    if not db_path.exists():
        raise NoDatabase(db_path)
    try:
        import maxminddb  # noqa: PLC0415 - optional extra, see the module docstring
    except ImportError as exc:
        raise GeoUnavailable(
            'the GeoLite2 reader isn\'t installed: pip install -e ".[geo]"'
        ) from exc

    try:
        with maxminddb.open_database(str(db_path)) as reader:
            record = reader.get(ip)
    except (ValueError, OSError, maxminddb.InvalidDatabaseError) as exc:
        raise GeoUnavailable(f"the location database at {db_path} won't open: {exc}") from exc
    if not record:
        raise PlaceNotFound(f"the location database has no entry for {ip}")

    loc = record.get("location") or {}
    if loc.get("latitude") is None or loc.get("longitude") is None:
        raise PlaceNotFound(f"the location database knows {ip}'s country but not where in it")
    subdivisions = record.get("subdivisions") or [{}]
    country = record.get("country") or record.get("registered_country") or {}
    name = (
        _name(record.get("city"), language)
        or _name(subdivisions[0], language)
        or _name(country, language)
        or "somewhere"
    )
    return Place(
        name=name,
        latitude=float(loc["latitude"]),
        longitude=float(loc["longitude"]),
        source="geoip",
        region=_name(subdivisions[0], language),
        country=_name(country, language),
        country_code=country.get("iso_code"),
        timezone=loc.get("time_zone"),
        accuracy_km=float(loc["accuracy_radius"]) if loc.get("accuracy_radius") else None,
        ip=ip,
    )


def geocode(name: str, fetch: Fetch = urllib_fetch, *, language: str = "en") -> Place:
    """A place name to coordinates, via Open-Meteo's free geocoder.

    Takes the first result, which Open-Meteo ranks by population — so "Paris" is
    Paris, France and not Paris, Texas. The answer always carries the country so
    the user hears WHICH Paris and can correct it.
    """
    query = " ".join(name.split())
    if not query:
        raise PlaceNotFound("which place?")
    url = (
        GEOCODE_URL
        + "?"
        + urllib.parse.urlencode(
            {"name": query, "count": 1, "language": language, "format": "json"}
        )
    )
    data = get_json(fetch, url)
    results = (data or {}).get("results") or []
    if not results:
        raise PlaceNotFound(f"I couldn't find a place called {query}.")
    hit = results[0]
    return Place(
        name=str(hit.get("name") or query),
        latitude=float(hit["latitude"]),
        longitude=float(hit["longitude"]),
        source="geocoded",
        region=hit.get("admin1"),
        country=hit.get("country"),
        country_code=hit.get("country_code"),
        timezone=hit.get("timezone"),
    )


@dataclass
class Locator:
    """Resolves "here" for one process. Holds a short-lived cache and nothing else.

    Built by the composition root from ``[location]`` in config.toml and handed
    to the tools through ``ctx.extra``, so a tool never reads configuration and
    a test never needs a network.
    """

    city: str = ""
    latitude: float | None = None
    longitude: float | None = None
    ip: str = ""
    db_path: Path = field(default_factory=default_db_path)
    language: str = "en"
    fetch: Fetch = urllib_fetch
    clock: Callable[[], float] = time.monotonic
    _ip_cache: tuple[float, str] | None = field(default=None, repr=False)

    def here(self) -> Place:
        """Where the user is, by the most trustworthy source available."""
        if self.latitude is not None and self.longitude is not None:
            return Place(
                name=self.city or "your configured location",
                latitude=float(self.latitude),
                longitude=float(self.longitude),
                source="config",
            )
        if self.city:
            found = geocode(self.city, self.fetch, language=self.language)
            # The user configured it, so it is not approximate even though a
            # geocoder resolved it: the hedge is about IP guesswork, not this.
            return Place(**{**_fields(found), "source": "config"})
        return lookup(self.db_path, self._public_ip(), language=self.language)

    def find(self, name: str) -> Place:
        """A named place, or "here" when the name is empty or means here."""
        if not name or name.strip().lower() in {"here", "home", "my location", "where i am"}:
            return self.here()
        return geocode(name, self.fetch, language=self.language)

    def _public_ip(self) -> str:
        if self.ip:
            return self.ip
        now = self.clock()
        if self._ip_cache is not None and now - self._ip_cache[0] < IP_TTL_S:
            return self._ip_cache[1]
        ip = public_ip(self.fetch)
        self._ip_cache = (now, ip)
        return ip


def _fields(p: Place) -> dict[str, Any]:
    return {f: getattr(p, f) for f in p.__dataclass_fields__}
