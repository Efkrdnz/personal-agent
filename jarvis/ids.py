"""Identifiers, timestamps and canonical hashing.

Every durable row in Jarvis carries an id from here and a timestamp from here.
Two rules the rest of the system depends on:

  * Time is stored in UTC, always, as RFC3339 with millisecond precision and a
    literal ``Z``. Europe/Istanbul exists only at the moment Jarvis speaks
    (see :mod:`jarvis.clock`). A daemon that may run on a cloud box in another
    zone cannot afford local time in the database.
  * ``dedupe_key`` is a hash of *canonical* JSON, so the same question asked
    twice produces the same key regardless of key order or float formatting.
    That key is what stops a re-fired question after a resume from being
    mistaken for a genuine second ask.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

__all__ = ["nid", "now", "canon", "dedupe_key", "parse_ts"]


def nid(prefix: str) -> str:
    """A short, sortable-enough, readable id: ``req_3f9a1c2b4d5e``.

    Readability matters more than entropy here — these ids end up in log lines a
    human reads at 1am. 48 bits of randomness is ample for one person's home
    system, and collisions are caught by the PRIMARY KEY regardless.
    """
    if not prefix or not prefix.isidentifier():
        raise ValueError(f"id prefix must be an identifier, got {prefix!r}")
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now() -> str:
    """The current instant, UTC, RFC3339 with milliseconds and a literal Z.

    Millisecond precision (not microsecond) because these strings are compared
    lexicographically in SQL and a fixed width keeps that honest.
    """
    t = datetime.now(UTC)
    return f"{t.strftime('%Y-%m-%dT%H:%M:%S')}.{t.microsecond // 1000:03d}Z"


def parse_ts(ts: str) -> datetime:
    """Parse a timestamp written by :func:`now` back to an aware datetime."""
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


def canon(obj: Any) -> str:
    """Canonical JSON: sorted keys, no insignificant whitespace, UTF-8 preserved.

    ``ensure_ascii=False`` is deliberate. Turkish text is everywhere in this
    project, and escaping it would make two identical payloads hash differently
    depending on how they were parsed.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def dedupe_key(*parts: Any) -> str:
    """A stable sha256 over canonical JSON of the parts.

    Used as ``dedupe_key(job_id, tool_name, tool_input)``. Spike S1 measured that
    ``tool_use_id`` is stable across defer and resume, so this is a convenience
    for rows that have no tool_use_id rather than a correctness mechanism — but
    it must still be stable, because a genuine re-ask increments ``attempt`` and
    the UNIQUE(job_id, dedupe_key, attempt) constraint is what tells the two
    apart.
    """
    return hashlib.sha256(canon(list(parts)).encode("utf-8")).hexdigest()
