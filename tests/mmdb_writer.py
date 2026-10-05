"""A minimal MaxMind DB WRITER, for building GeoLite2-shaped test fixtures.

Test-only. Production reads with the official ``maxminddb`` package, and that is
the point: the official reader is the validator for this writer. If this file
encodes something wrong, ``maxminddb.open_database`` refuses it or returns the
wrong record, and the fixture tests fail before any code under test runs.

Why write one rather than ship a fixture: MaxMind's own test databases are
CC BY-SA, and this tree is MIT and deliberately careful about licences (see
ADR 0001). A real GeoLite2 file cannot be committed at all.

Only what the fixtures need: an IPv4 tree with 24-bit records, and the types a
GeoIP2 City record uses (map, utf8 string, double, uint16/32/64, array).
"""

from __future__ import annotations

import ipaddress
import struct
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

_METADATA_MARKER = b"\xab\xcd\xefMaxMind.com"


def _control(type_: int, size: int) -> bytes:
    if size < 29:
        bits, extra = size, b""
    elif size < 29 + 256:
        bits, extra = 29, bytes([size - 29])
    elif size < 285 + 65536:
        bits, extra = 30, (size - 285).to_bytes(2, "big")
    else:
        bits, extra = 31, (size - 65821).to_bytes(3, "big")
    if type_ <= 7:
        return bytes([(type_ << 5) | bits]) + extra
    # Extended type: type bits are zero, and the real type follows the control byte.
    return bytes([bits, type_ - 7]) + extra


def _uint(type_: int, value: int) -> bytes:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big") if value else b""
    return _control(type_, len(raw)) + raw


class U32(int):
    """Encode as uint32 regardless of size. libmaxminddb checks metadata TYPES."""


class U64(int):
    """Encode as uint64 regardless of size."""


def encode(value: Any) -> bytes:
    # The C reader is strict where the Python one is not: `node_count` must be a
    # uint32 and `build_epoch` a uint64 even when the number would fit in fewer
    # bytes. Encoding the smallest type that fits is accepted by the pure-Python
    # reader and REJECTED by libmaxminddb — which is what real installs use.
    if isinstance(value, U32):
        return _uint(6, int(value))
    if isinstance(value, U64):
        return _uint(9, int(value))
    if isinstance(value, bool):
        return _control(14, int(value))
    if isinstance(value, str):
        raw = value.encode("utf-8")
        return _control(2, len(raw)) + raw
    if isinstance(value, float):
        return _control(3, 8) + struct.pack(">d", value)
    if isinstance(value, int):
        if value < 0:
            raise ValueError("the fixtures never need a negative integer")
        if value < 1 << 16:
            return _uint(5, value)
        if value < 1 << 32:
            return _uint(6, value)
        return _uint(9, value)
    if isinstance(value, Mapping):
        out = _control(7, len(value))
        for k, v in value.items():
            out += encode(str(k)) + encode(v)
        return out
    if isinstance(value, Sequence):
        out = _control(11, len(value))
        for v in value:
            out += encode(v)
        return out
    raise TypeError(f"cannot encode {type(value).__name__}")


class _Node:
    __slots__ = ("children",)

    def __init__(self) -> None:
        self.children: list[Any] = [None, None]


def write(
    path: Path,
    records: Mapping[str, Mapping[str, Any]],
    *,
    database_type: str = "GeoLite2-City",
    build_epoch: int = 1_700_000_000,
) -> Path:
    """Write ``{"81.215.0.0/16": {...geoip2 city record...}}`` as an IPv4 .mmdb."""
    data = b""
    offsets: dict[str, int] = {}
    for net in records:
        offsets[net] = len(data)
        data += encode(records[net])

    root = _Node()
    for net, offset in offsets.items():
        network = ipaddress.IPv4Network(net)
        if network.prefixlen == 0:
            raise ValueError("a /0 has no bits to walk")
        bits = int(network.network_address)
        node = root
        for i in range(network.prefixlen):
            bit = (bits >> (31 - i)) & 1
            if i == network.prefixlen - 1:
                node.children[bit] = ("data", offset)
            else:
                nxt = node.children[bit]
                if not isinstance(nxt, _Node):
                    nxt = _Node()
                    node.children[bit] = nxt
                node = nxt

    order: list[_Node] = []
    queue = [root]
    while queue:
        node = queue.pop(0)
        order.append(node)
        queue.extend(c for c in node.children if isinstance(c, _Node))
    index = {id(n): i for i, n in enumerate(order)}
    node_count = len(order)

    tree = bytearray()
    for node in order:
        for child in node.children:
            if child is None:
                value = node_count
            elif isinstance(child, _Node):
                value = index[id(child)]
            else:
                value = child[1] + node_count + 16
            tree += value.to_bytes(3, "big")

    metadata = {
        "node_count": U32(node_count),
        "record_size": 24,
        "ip_version": 4,
        "database_type": database_type,
        "languages": ["en", "tr"],
        "binary_format_major_version": 2,
        "binary_format_minor_version": 0,
        "build_epoch": U64(build_epoch),
        "description": {"en": "Jarvis test fixture, not MaxMind data"},
    }
    path.write_bytes(bytes(tree) + b"\x00" * 16 + data + _METADATA_MARKER + encode(metadata))
    return path


def city_record(
    *,
    city: str,
    region: str,
    country: str,
    iso: str,
    lat: float,
    lon: float,
    tz: str,
    accuracy_km: int,
    city_tr: str | None = None,
) -> dict[str, Any]:
    """The subset of a GeoIP2 City record the locator reads, in MaxMind's own shape."""
    return {
        "city": {"names": {"en": city, **({"tr": city_tr} if city_tr else {})}},
        "subdivisions": [{"names": {"en": region}}],
        "country": {"iso_code": iso, "names": {"en": country}},
        "location": {
            "latitude": lat,
            "longitude": lon,
            "time_zone": tz,
            "accuracy_radius": accuracy_km,
        },
    }
