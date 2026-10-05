"""Refreshing GeoLite2: the licence key reaches MaxMind and nobody else.

MaxMind answers the download with a redirect to a presigned URL on third-party
storage, and urllib copies the Authorization header onto a redirected request.
These tests drive the real ``update`` against a fake ``send`` that records every
request, so "the key never left download.maxmind.com" is asserted on the wire.
"""

from __future__ import annotations

import hashlib
import io
import sys
import tarfile
from collections.abc import Mapping
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import mmdb_writer  # noqa: E402

from jarvis.geo import geolite  # noqa: E402

pytest.importorskip("maxminddb")

KEY = "sk_licence_DO_NOT_LEAK"
ACCOUNT = "123456"
STORAGE = "https://storage.example-r2.net/geolite/GeoLite2-City.tar.gz?sig=abc"


def a_city_db(tmp_path: Path) -> bytes:
    path = mmdb_writer.write(
        tmp_path / "src.mmdb",
        {
            "81.215.0.0/16": mmdb_writer.city_record(
                city="Istanbul",
                region="Istanbul",
                country="Türkiye",
                iso="TR",
                lat=41.0,
                lon=29.0,
                tz="Europe/Istanbul",
                accuracy_km=20,
            )
        },
    )
    return path.read_bytes()


def tarball(members: Mapping[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class Wire:
    """MaxMind as it really behaves: checksum direct, archive via redirect."""

    def __init__(self, archive: bytes, *, checksum: str | None = None, status: int = 200) -> None:
        self.archive = archive
        self.checksum = checksum or hashlib.sha256(archive).hexdigest()
        self.status = status
        self.seen: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, headers: Mapping[str, str]) -> geolite.Response:
        self.seen.append((url, dict(headers)))
        if self.status != 200:
            return geolite.Response(self.status, {}, b"")
        if url.endswith("suffix=tar.gz.sha256"):
            return geolite.Response(
                200, {}, f"{self.checksum}  GeoLite2-City_20261003.tar.gz\n".encode()
            )
        if url.endswith("suffix=tar.gz"):
            return geolite.Response(302, {"Location": STORAGE}, b"")
        if url == STORAGE:
            return geolite.Response(200, {}, self.archive)
        return geolite.Response(404, {}, b"")


def test_a_good_download_is_installed(tmp_path: Path) -> None:
    db = a_city_db(tmp_path)
    wire = Wire(tarball({"GeoLite2-City_20261003/GeoLite2-City.mmdb": db}))
    dest = tmp_path / "data" / "GeoLite2-City.mmdb"
    geolite.update(account_id=ACCOUNT, license_key=KEY, dest=dest, send=wire)
    assert dest.read_bytes() == db


def test_the_licence_key_never_reaches_the_storage_host(tmp_path: Path) -> None:
    """THE reason the redirect is followed by hand."""
    wire = Wire(tarball({"x/GeoLite2-City.mmdb": a_city_db(tmp_path)}))
    geolite.update(account_id=ACCOUNT, license_key=KEY, dest=tmp_path / "g.mmdb", send=wire)

    for url, headers in wire.seen:
        sent = " ".join(headers.values())
        if "download.maxmind.com" in url:
            assert sent.startswith("Basic "), "MaxMind itself must get the credential"
        else:
            assert "Authorization" not in headers, f"credential sent to {url}"
        assert KEY not in url, "the key must never be in a URL"
    assert any(url == STORAGE for url, _ in wire.seen), "the redirect was actually followed"


def test_a_checksum_mismatch_keeps_the_old_database(tmp_path: Path) -> None:
    dest = tmp_path / "GeoLite2-City.mmdb"
    dest.write_bytes(b"the old, working database")
    wire = Wire(tarball({"x/GeoLite2-City.mmdb": a_city_db(tmp_path)}), checksum="0" * 64)
    with pytest.raises(geolite.UpdateFailed, match="checksum"):
        geolite.update(account_id=ACCOUNT, license_key=KEY, dest=dest, send=wire)
    assert dest.read_bytes() == b"the old, working database"


def test_a_file_that_is_not_a_database_keeps_the_old_one(tmp_path: Path) -> None:
    dest = tmp_path / "GeoLite2-City.mmdb"
    dest.write_bytes(b"old")
    wire = Wire(tarball({"x/GeoLite2-City.mmdb": b"not a maxmind database"}))
    with pytest.raises(geolite.UpdateFailed, match="not a valid MaxMind database"):
        geolite.update(account_id=ACCOUNT, license_key=KEY, dest=dest, send=wire)
    assert dest.read_bytes() == b"old"
    assert not dest.with_suffix(".mmdb.partial").exists(), "no half-written file left behind"


def test_a_hostile_member_name_writes_nothing_outside(tmp_path: Path) -> None:
    """Members are READ, never extracted: a path in a tarball goes nowhere."""
    db = a_city_db(tmp_path)
    wire = Wire(tarball({"../../../evil.sh": b"rm -rf ~", "x/GeoLite2-City.mmdb": db}))
    dest = tmp_path / "deep" / "GeoLite2-City.mmdb"
    geolite.update(account_id=ACCOUNT, license_key=KEY, dest=dest, send=wire)
    assert not (tmp_path / "evil.sh").exists()
    assert not any(p.name == "evil.sh" for p in tmp_path.rglob("*"))


def test_an_archive_without_the_database_is_refused(tmp_path: Path) -> None:
    wire = Wire(tarball({"x/README.txt": b"hello"}))
    with pytest.raises(geolite.UpdateFailed, match="no GeoLite2-City.mmdb"):
        geolite.update(account_id=ACCOUNT, license_key=KEY, dest=tmp_path / "g.mmdb", send=wire)


def test_a_refused_key_says_where_to_fix_it_and_never_echoes_it(tmp_path: Path) -> None:
    wire = Wire(b"", status=401)
    with pytest.raises(geolite.UpdateFailed) as exc:
        geolite.update(account_id=ACCOUNT, license_key=KEY, dest=tmp_path / "g.mmdb", send=wire)
    assert "maxmind_license_key" in str(exc.value)
    assert KEY not in str(exc.value)


def test_missing_credentials_are_named_before_any_request(tmp_path: Path) -> None:
    wire = Wire(b"")
    with pytest.raises(geolite.UpdateFailed, match="maxmind_account_id"):
        geolite.update(account_id="", license_key=KEY, dest=tmp_path / "g.mmdb", send=wire)
    with pytest.raises(geolite.UpdateFailed, match="secrets set maxmind_license_key"):
        geolite.update(account_id=ACCOUNT, license_key="", dest=tmp_path / "g.mmdb", send=wire)
    assert wire.seen == []


def test_a_redirect_to_plain_http_is_refused(tmp_path: Path) -> None:
    class Downgrade(Wire):
        def __call__(self, url: str, headers: Mapping[str, str]) -> geolite.Response:
            if url.endswith("suffix=tar.gz"):
                return geolite.Response(302, {"location": "http://evil.example/x"}, b"")
            return super().__call__(url, headers)

    wire = Downgrade(tarball({"x/GeoLite2-City.mmdb": a_city_db(tmp_path)}))
    with pytest.raises(geolite.UpdateFailed, match="https"):
        geolite.update(account_id=ACCOUNT, license_key=KEY, dest=tmp_path / "g.mmdb", send=wire)
