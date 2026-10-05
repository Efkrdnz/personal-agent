"""The weather, where you are, and what time it is somewhere.

The everyday half of a general assistant. Every answer names the place it is
about, and an answer built from an IP lookup says it is approximate — the user
asking "do I need a coat" should not find out it was Frankfurt's weather because
their VPN exits there.

THE LOCATOR IS HANDED IN, never built from configuration here. The composition
root reads ``[location]`` and passes a :class:`jarvis.geo.Locator` through
``ctx.extra``; a tool that read config.toml itself would behave differently
depending on which process happened to run it.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jarvis.clock import local_tz
from jarvis.geo import GeoUnavailable, Locator, forecast, spoken_weather
from jarvis.geo.place import Place
from jarvis.tools.ctx import ToolCtx
from jarvis.tools.registry import ALL_CHANNELS, Tool, ToolError

__all__ = ["LOCATOR", "UNITS", "TOOLS", "local_time", "weather", "where_am_i"]

#: Keys in :attr:`ToolCtx.extra`, set by the composition root.
LOCATOR = "locator"
UNITS = "units"


def _locator(ctx: ToolCtx) -> Locator:
    found = ctx.extra.get(LOCATOR)
    return found if isinstance(found, Locator) else Locator()


def _resolve(ctx: ToolCtx, place: str) -> Place:
    try:
        return _locator(ctx).find(place)
    except GeoUnavailable as exc:
        # Every GeoUnavailable is already a sentence; the registry would turn a
        # bare exception into "Sorry — weather failed", which says nothing.
        raise ToolError(str(exc)) from exc


def weather(ctx: ToolCtx, place: str = "", when: str = "now") -> str:
    """Current conditions, today, tomorrow or the week — for here or a named place."""
    window = when.strip().lower() or "now"
    if window not in ("now", "today", "tomorrow", "week"):
        raise ToolError(f"I can do the weather now, today, tomorrow or this week — not {when}.")
    where = _resolve(ctx, place)
    units = str(ctx.extra.get(UNITS) or "metric")
    try:
        f = forecast(
            where, _locator(ctx).fetch, units="imperial" if units == "imperial" else "metric"
        )
        return spoken_weather(f, window)  # type: ignore[arg-type]
    except GeoUnavailable as exc:
        raise ToolError(f"I couldn't get the weather for {where.label}: {exc}") from exc


def where_am_i(ctx: ToolCtx) -> str:
    """Where Jarvis believes you are, how it knows, and how sure it is."""
    here = _resolve(ctx, "")
    if here.source == "config":
        return f"You've told me you're in {here.label}."
    radius = f", give or take {here.accuracy_km:.0f} kilometres" if here.accuracy_km else ""
    region = f" in {here.region}" if here.region and here.region != here.name else ""
    return (
        f"Going by your internet address, you're in or near {here.name}{region}, "
        f"{here.country or 'somewhere'}{radius}. A VPN or a mobile connection can move "
        "that a long way — set your city in config.toml if it's wrong."
    )


def _zone(p: Place) -> ZoneInfo:
    if p.timezone:
        try:
            return ZoneInfo(p.timezone)
        except ZoneInfoNotFoundError:
            pass
    return local_tz()


def local_time(ctx: ToolCtx, place: str = "") -> str:
    """The time and date here, or in a named place."""
    if not place.strip():
        now = datetime.now(local_tz())
        return f"It's {now:%H:%M} on {now:%A} the {now.day}{_ordinal(now.day)}."
    where = _resolve(ctx, place)
    if not where.timezone:
        raise ToolError(f"I found {where.label} but not its time zone.")
    there = datetime.now(_zone(where))
    here = datetime.now(local_tz())
    offset = there.utcoffset() - here.utcoffset()  # type: ignore[operator]
    hours = offset.total_seconds() / 3600
    if abs(hours) < 0.01:
        relation = "the same as here"
    else:
        h = f"{abs(hours):g} hour{'' if abs(hours) == 1 else 's'}"
        relation = f"{h} {'ahead of' if hours > 0 else 'behind'} you"
    return f"It's {there:%H:%M} on {there:%A} in {where.label} — {relation}."


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="weather",
        description=(
            "The weather: current conditions, today, tomorrow or the week ahead, for where the "
            "user is or for a place they name. Use for 'what's the weather', 'will it rain "
            "tomorrow', 'do I need a coat', 'weather in Paris this week'. Read the answer as "
            "given — it names the place and says when the location is only approximate."
        ),
        handler=weather,
        parameters={
            "type": "OBJECT",
            "properties": {
                "place": {
                    "type": "STRING",
                    "description": "A city or place name. Empty for where the user is.",
                },
                "when": {
                    "type": "STRING",
                    "enum": ["now", "today", "tomorrow", "week"],
                    "description": "Defaults to now.",
                },
            },
        },
        channels=ALL_CHANNELS,
    ),
    Tool(
        name="where_am_i",
        description=(
            "Where the user is, as best Jarvis can tell, and how it knows. Use for 'where am "
            "I', 'what city do you think I'm in', 'what's my location'."
        ),
        handler=where_am_i,
        parameters={"type": "OBJECT", "properties": {}},
        channels=ALL_CHANNELS,
    ),
    Tool(
        name="local_time",
        description=(
            "The current time and date, here or in a named place, and the difference from "
            "here. Use for 'what time is it', 'what time is it in Tokyo', 'what day is it'."
        ),
        handler=local_time,
        parameters={
            "type": "OBJECT",
            "properties": {
                "place": {
                    "type": "STRING",
                    "description": "A city or place. Empty for here.",
                }
            },
        },
        channels=ALL_CHANNELS,
    ),
)
