"""The one door to the network, so every caller can be handed a fake one.

A ``Fetch`` is ``url -> bytes``. That is deliberately all: the geo layer only ever
GETs public, unauthenticated JSON or text, so a richer client would be surface
area with nothing behind it. The one authenticated download (GeoLite2) lives in
:mod:`jarvis.geo.geolite` with its own opener, because it must NOT share this
door: it carries a licence key and has to control exactly which host sees it.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

__all__ = ["Fetch", "GeoUnavailable", "USER_AGENT", "get_json", "urllib_fetch"]

#: Open-Meteo asks for an identifying User-Agent and rate-limits anonymous ones
#: more aggressively; so does every other free service this layer touches.
USER_AGENT = "jarvis-personal-assistant/0.1 (+https://github.com/Efkrdnz/personal-agent)"

Fetch = Callable[[str], bytes]


class GeoUnavailable(RuntimeError):
    """The network, or the service on the other end of it, did not answer.

    The message is a sentence: this is the error the user hears when they ask
    for the weather on a train, and "URLError: [Errno -3]" is not an answer.
    """


def urllib_fetch(url: str, *, timeout: float = 8.0) -> bytes:
    """GET ``url`` with a timeout. Raises :class:`GeoUnavailable`, never a URLError."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - https only, below
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise GeoUnavailable(f"{_host(url)} answered {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise GeoUnavailable(f"I couldn't reach {_host(url)}") from exc


def get_json(fetch: Fetch, url: str) -> Any:
    raw = fetch(url)
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise GeoUnavailable(f"{_host(url)} sent something that is not JSON") from exc


def _host(url: str) -> str:
    return url.split("//", 1)[-1].split("/", 1)[0]
