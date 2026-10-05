"""A place, and how sure we are about it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = ["Place", "Source"]

Source = Literal["config", "geocoded", "geoip"]


@dataclass(frozen=True, slots=True)
class Place:
    """Somewhere with coordinates, and a record of how we know.

    ``accuracy_km`` is GeoLite2's ``accuracy_radius`` and is None for a place the
    user named or configured. It is carried rather than dropped because it is
    the difference between "you're in Istanbul" and "you're somewhere within a
    thousand kilometres of Mountain View", which is what GeoLite2 says for a lot
    of mobile and VPN addresses.
    """

    name: str
    latitude: float
    longitude: float
    source: Source
    region: str | None = None
    country: str | None = None
    country_code: str | None = None
    timezone: str | None = None
    accuracy_km: float | None = None
    ip: str | None = None

    @property
    def label(self) -> str:
        """ "Paris, France" — enough to tell which Paris without reading coordinates."""
        parts = [self.name]
        if self.country and self.country != self.name:
            parts.append(self.country)
        return ", ".join(p for p in parts if p)

    @property
    def approximate(self) -> bool:
        return self.source == "geoip"

    def hedge(self) -> str:
        """The caveat to say after anything built from an approximate location.

        Empty for a place the user named or configured: hedging a city they
        chose themselves would be noise. For an IP lookup it is not optional —
        a VPN, a mobile carrier or an office network can put the "here" in a
        different city, and the user should hear that before they dress for it.
        """
        if not self.approximate:
            return ""
        radius = f", roughly within {self.accuracy_km:.0f} kilometres" if self.accuracy_km else ""
        return f"That's for {self.label}, worked out from your internet address{radius}."
