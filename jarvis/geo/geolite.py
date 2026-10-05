"""Download and install MaxMind's GeoLite2 City database.

GeoLite2 is free but licensed: it needs a MaxMind account ID and licence key
(https://www.maxmind.com/en/geolite2/signup), and MaxMind's terms ask that it be
refreshed rather than used stale — the database is rebuilt twice a week and IP
blocks move between cities. ``python -m jarvis geo update`` is that refresh.

THE LICENCE KEY GOES TO ONE HOST AND NO OTHER. MaxMind answers the download with
a redirect to a presigned URL on a third-party storage host. Python's
``urllib`` copies request headers onto a redirected request — Authorization
included — so letting it follow the redirect would hand the licence key to that
host. The redirect is therefore followed BY HAND, without credentials, and the
key never appears in a URL, a log line or an exception message.

THE FILE IS VERIFIED BEFORE IT REPLACES ANYTHING. The archive's SHA-256 is
fetched from MaxMind and checked; the extracted database is opened with the
official reader and must say it is a City database; and only then is it moved
over the old one, atomically. A half-written or wrong file would otherwise make
"where am I?" fail in a way that looks like the network.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import tarfile
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from jarvis.geo.http import USER_AGENT, GeoUnavailable
from jarvis.geo.locate import DB_FILENAME

__all__ = ["DOWNLOAD_URL", "Response", "Send", "UpdateFailed", "update"]

DOWNLOAD_URL = "https://download.maxmind.com/geoip/databases/GeoLite2-City/download"
_MAXMIND_HOST = "download.maxmind.com"


class UpdateFailed(GeoUnavailable):
    """The database could not be refreshed. The old one, if any, is untouched."""


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    headers: Mapping[str, str]
    body: bytes


#: ``(url, headers) -> Response``. NEVER follows redirects: see the module docstring.
Send = Callable[[str, Mapping[str, str]], Response]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:  # type: ignore[override]
        return None


def _urllib_send(url: str, headers: Mapping[str, str]) -> Response:
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **headers})
    try:
        with opener.open(req, timeout=60) as resp:  # noqa: S310 - https only
            return Response(resp.status, dict(resp.headers), resp.read())
    except urllib.error.HTTPError as exc:
        # A 3xx lands here because the redirect handler declined it — that is
        # the point, not a failure.
        return Response(exc.code, dict(exc.headers or {}), exc.read() or b"")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        host = url.split("//", 1)[-1].split("/", 1)[0]
        raise UpdateFailed(f"I couldn't reach {host} to download the location database") from exc


def _get(send: Send, url: str, auth: str | None) -> bytes:
    """GET with auth sent ONLY to MaxMind; follow at most a few redirects by hand."""
    for _ in range(5):
        host = url.split("//", 1)[-1].split("/", 1)[0]
        headers = {"Authorization": auth} if auth and host == _MAXMIND_HOST else {}
        resp = send(url, headers)
        if resp.status in (301, 302, 303, 307, 308):
            location = _header(resp.headers, "Location")
            if not location or not location.startswith("https://"):
                raise UpdateFailed("MaxMind redirected somewhere that isn't https")
            url = location
            continue
        if resp.status == 401:
            raise UpdateFailed(
                "MaxMind refused the account ID or licence key. Check `[location] "
                "maxmind_account_id` in config.toml and `python -m jarvis secrets set "
                "maxmind_license_key`."
            )
        if resp.status == 429:
            raise UpdateFailed("MaxMind's daily download limit is used up; try again tomorrow")
        if resp.status != 200:
            raise UpdateFailed(f"MaxMind answered {resp.status}")
        return resp.body
    raise UpdateFailed("too many redirects downloading the location database")


def _header(headers: Mapping[str, str], name: str) -> str | None:
    for k, v in headers.items():
        if k.lower() == name.lower():
            return v
    return None


def _mmdb_from(archive: bytes) -> bytes:
    """The one ``GeoLite2-City.mmdb`` member, read into memory. Never extracted to disk.

    Reading the member's bytes rather than calling ``extractall`` is what makes a
    hostile archive harmless: there is no path from a member NAME to a file the
    process writes, so ``../../.bashrc`` inside the tarball goes nowhere.
    """
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            for member in tar.getmembers():
                if member.isfile() and member.name.rsplit("/", 1)[-1] == DB_FILENAME:
                    handle = tar.extractfile(member)
                    if handle is not None:
                        return handle.read()
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise UpdateFailed("the downloaded archive is not a readable .tar.gz") from exc
    raise UpdateFailed(f"the downloaded archive has no {DB_FILENAME} in it")


def _validate(path: Path) -> str:
    try:
        import maxminddb  # noqa: PLC0415 - optional extra
    except ImportError as exc:
        raise UpdateFailed("the GeoLite2 reader isn't installed: pip install -e '.[geo]'") from exc
    try:
        with maxminddb.open_database(str(path)) as reader:
            kind = str(reader.metadata().database_type)
    except Exception as exc:  # noqa: BLE001 - every way to fail here means "not a database"
        raise UpdateFailed(f"the downloaded file is not a valid MaxMind database: {exc}") from exc
    if "City" not in kind:
        raise UpdateFailed(f"the downloaded database is {kind}, not a City database")
    return kind


def update(
    *,
    account_id: str,
    license_key: str,
    dest: Path,
    send: Send = _urllib_send,
) -> Path:
    """Download, verify and atomically install GeoLite2 City at ``dest``."""
    if not account_id.strip():
        raise UpdateFailed(
            "set `maxmind_account_id` under [location] in config.toml — it's on "
            "https://www.maxmind.com/en/accounts/current/license-key"
        )
    if not license_key.strip():
        raise UpdateFailed(
            "no MaxMind licence key: python -m jarvis secrets set maxmind_license_key"
        )
    token = base64.b64encode(f"{account_id.strip()}:{license_key.strip()}".encode()).decode()
    auth = f"Basic {token}"

    checksum_text = _get(send, DOWNLOAD_URL + "?suffix=tar.gz.sha256", auth).decode(
        "ascii", "replace"
    )
    expected = checksum_text.strip().split()[0].lower() if checksum_text.strip() else ""
    archive = _get(send, DOWNLOAD_URL + "?suffix=tar.gz", auth)
    actual = hashlib.sha256(archive).hexdigest()
    if not expected or actual != expected:
        raise UpdateFailed("the download did not match MaxMind's checksum, so I kept the old one")

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".mmdb.partial")
    tmp.write_bytes(_mmdb_from(archive))
    try:
        _validate(tmp)
        os.replace(tmp, dest)
    finally:
        if tmp.exists():
            tmp.unlink()
    return dest
