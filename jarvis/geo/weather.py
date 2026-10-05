"""The weather for a place, from Open-Meteo, as a sentence somebody can hear.

WHY OPEN-METEO. It is free, needs no key, is backed by national weather services
(DWD, NOAA, Météo-France, and others blended by location), and takes coordinates
directly — which is what GeoLite2 and the geocoder both produce. No key means no
secret, no signup, and nothing to expire on the morning the user asks whether to
take an umbrella.

THE SENTENCE IS BUILT HERE, next to the numbers, so the desk, Telegram and the
text chat cannot word the same forecast three ways. It says the place, because
"18 degrees" for the wrong city is worse than no answer — and when the place came
from an IP address, it says that too.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from jarvis.geo.http import Fetch, GeoUnavailable, get_json, urllib_fetch
from jarvis.geo.place import Place

__all__ = [
    "FORECAST_URL",
    "Current",
    "Day",
    "Forecast",
    "Units",
    "Window",
    "describe_code",
    "forecast",
    "spoken_weather",
]

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

Units = Literal["metric", "imperial"]
Window = Literal["now", "today", "tomorrow", "week"]

#: WMO weather interpretation codes, as Open-Meteo documents them. Phrased to be
#: SPOKEN after "it's" — "it's light rain", "it's overcast" — rather than as a
#: table heading.
_WMO: dict[int, str] = {
    0: "clear",
    1: "mostly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "foggy",
    48: "foggy with frost",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    56: "light freezing drizzle",
    57: "freezing drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    66: "light freezing rain",
    67: "freezing rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    77: "snow grains",
    80: "light showers",
    81: "showers",
    82: "violent showers",
    85: "light snow showers",
    86: "heavy snow showers",
    95: "thunderstorms",
    96: "thunderstorms with hail",
    99: "thunderstorms with heavy hail",
}


def describe_code(code: int | None) -> str:
    """A spoken phrase for a WMO code. An unknown code says so rather than guessing."""
    if code is None:
        return "unknown conditions"
    return _WMO.get(int(code), f"weather code {code}")


@dataclass(frozen=True, slots=True)
class Current:
    temperature: float
    feels_like: float | None
    code: int | None
    wind: float | None
    humidity: float | None
    is_day: bool = True


@dataclass(frozen=True, slots=True)
class Day:
    date: date
    code: int | None
    high: float
    low: float
    rain_chance: float | None
    sunrise: str | None = None
    sunset: str | None = None


@dataclass(frozen=True, slots=True)
class Forecast:
    place: Place
    units: Units
    current: Current
    days: tuple[Day, ...] = field(default=())
    timezone: str | None = None

    @property
    def degree(self) -> str:
        return "degrees" if self.units == "metric" else "degrees Fahrenheit"

    @property
    def speed(self) -> str:
        return "kilometres an hour" if self.units == "metric" else "miles an hour"


def forecast(place: Place, fetch: Fetch = urllib_fetch, *, units: Units = "metric") -> Forecast:
    """Current conditions and a three-day outlook for one place."""
    params = {
        "latitude": f"{place.latitude:.4f}",
        "longitude": f"{place.longitude:.4f}",
        "current": ",".join(
            (
                "temperature_2m",
                "apparent_temperature",
                "relative_humidity_2m",
                "weather_code",
                "wind_speed_10m",
                "is_day",
            )
        ),
        "daily": ",".join(
            (
                "weather_code",
                "temperature_2m_max",
                "temperature_2m_min",
                "precipitation_probability_max",
                "sunrise",
                "sunset",
            )
        ),
        # 'auto' makes every time in the reply LOCAL TO THE PLACE, which is what
        # "sunset at seven" has to mean when the user asked about Tokyo.
        "timezone": "auto",
        "forecast_days": 7,
    }
    if units == "imperial":
        params |= {"temperature_unit": "fahrenheit", "wind_speed_unit": "mph"}
    data = get_json(fetch, FORECAST_URL + "?" + urllib.parse.urlencode(params))
    return _parse(data, place, units)


def _parse(data: Any, place: Place, units: Units) -> Forecast:
    if not isinstance(data, dict) or "current" not in data:
        reason = (data or {}).get("reason") if isinstance(data, dict) else None
        raise GeoUnavailable(
            f"the weather service didn't send a forecast{f': {reason}' if reason else ''}"
        )
    cur = data["current"]
    current = Current(
        temperature=float(cur["temperature_2m"]),
        feels_like=_num(cur.get("apparent_temperature")),
        code=_int(cur.get("weather_code")),
        wind=_num(cur.get("wind_speed_10m")),
        humidity=_num(cur.get("relative_humidity_2m")),
        is_day=bool(cur.get("is_day", 1)),
    )
    daily = data.get("daily") or {}
    days: list[Day] = []
    for i, d in enumerate(daily.get("time") or ()):
        days.append(
            Day(
                date=date.fromisoformat(d),
                code=_int(_at(daily, "weather_code", i)),
                high=float(_at(daily, "temperature_2m_max", i)),
                low=float(_at(daily, "temperature_2m_min", i)),
                rain_chance=_num(_at(daily, "precipitation_probability_max", i)),
                sunrise=_at(daily, "sunrise", i),
                sunset=_at(daily, "sunset", i),
            )
        )
    tz = data.get("timezone")
    if place.timezone is None and tz:
        # The weather service just told us the zone. Keep it, so "what time is
        # it there" can be answered for a place GeoLite2 left without one.
        place = Place(
            **{f: getattr(place, f) for f in place.__dataclass_fields__} | {"timezone": tz}
        )
    return Forecast(place=place, units=units, current=current, days=tuple(days), timezone=tz)


def _at(daily: dict[str, Any], key: str, i: int) -> Any:
    seq = daily.get(key) or ()
    return seq[i] if i < len(seq) else None


def _num(v: Any) -> float | None:
    return None if v is None else float(v)


def _int(v: Any) -> int | None:
    return None if v is None else int(v)


def _deg(v: float) -> str:
    """Round to a whole degree. "18.4 degrees" is not how anybody says it."""
    n = round(v)
    return "0" if n == 0 else str(n)


def _clock(iso: str | None) -> str | None:
    """ "2026-10-05T19:12" -> "19:12". Local to the place (timezone=auto)."""
    if not iso or "T" not in iso:
        return None
    return iso.split("T", 1)[1][:5]


def _day_line(f: Forecast, day: Day, label: str) -> str:
    rain = ""
    if day.rain_chance is not None and day.rain_chance >= 10:
        rain = f", {round(day.rain_chance)} percent chance of rain"
    return (
        f"{label}: {describe_code(day.code)}, high of {_deg(day.high)}, "
        f"low of {_deg(day.low)}{rain}."
    )


def spoken_weather(f: Forecast, when: Window = "now") -> str:
    """One answer, said the way a person would say it, with the place in it."""
    where = f.place.label
    parts: list[str] = []
    if when in ("now", "today"):
        c = f.current
        line = f"In {where} it's {_deg(c.temperature)} {f.degree} and {describe_code(c.code)}"
        if c.feels_like is not None and abs(c.feels_like - c.temperature) >= 2:
            line += f", feels like {_deg(c.feels_like)}"
        if c.wind is not None and c.wind >= (30 if f.units == "metric" else 20):
            # Only when it matters: wind is noise on a calm day and the whole
            # story on a stormy one.
            line += f", with wind at {round(c.wind)} {f.speed}"
        parts.append(line + ".")
        if f.days:
            parts.append(_day_line(f, f.days[0], "Today"))
            if when == "today" and (sunset := _clock(f.days[0].sunset)):
                parts.append(f"Sunset at {sunset}.")
    elif when == "tomorrow":
        if len(f.days) < 2:
            raise GeoUnavailable("the forecast didn't include tomorrow")
        parts.append(_day_line(f, f.days[1], f"Tomorrow in {where}"))
    else:
        parts.append(f"The week in {where}.")
        for day in f.days[:7]:
            parts.append(_day_line(f, day, day.date.strftime("%A")))
    if hedge := f.place.hedge():
        parts.append(hedge)
    return " ".join(parts)
