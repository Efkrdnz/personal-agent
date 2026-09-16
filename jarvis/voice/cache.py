"""Content-addressed PCM on disk, so "SQLite" is synthesised once, ever.

``~/.cache/jarvis/tts/<sha256(engine|voice|lang|text)>.pcm``. Option labels are
one to five words and repeat constantly — "Yes", "Skip tests", "Postgres" — so
after the first round a whole question is a handful of file reads and "repeat
option two" is instant and free. That is the latency story as much as
pre-synthesis is: the cheapest clip is the one already on disk.

THE KEY IS THE WHOLE IDENTITY. Engine, voice, language and the EXACT text, with
no normalisation of any kind: two strings that differ by a space are different
answer keys and must never share a clip. The separator is forbidden inside the
first three fields rather than escaped, because a collision here does not crash
— it plays confident, fluent, wrong audio.

CORRUPTION IS EXPECTED, NOT EXCEPTIONAL. A machine loses power mid-write; a
backup restores half a file; something else writes into the directory. So every
clip is written atomically and paired with a digest sidecar, and a clip that
does not verify is DELETED and re-synthesised rather than played or raised over.
The sidecar is written after the audio, so the crash window produces an
unverifiable file, which reads as a miss. There is no window in which a
truncated clip looks valid.

Cache failure is never speech failure. A full disk, a read-only home directory
or a vanished cache root degrades to "synthesise every time" — slower, and
audibly identical.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["PcmCache", "cache_key", "default_cache_dir"]

_SEP = "|"
_SIDECAR_SUFFIX = ".sha256"


def default_cache_dir() -> Path:
    """``$XDG_CACHE_HOME/jarvis/tts``, falling back to ``~/.cache/jarvis/tts``."""
    base = os.environ.get("XDG_CACHE_HOME") or ""
    root = Path(base) if base.strip() else Path.home() / ".cache"
    return root / "jarvis" / "tts"


def cache_key(*, engine: str, voice: str, lang: str, text: str) -> str:
    """sha256 over the four fields that decide what the audio sounds like."""
    for name, value in (("engine", engine), ("voice", voice), ("lang", lang)):
        if _SEP in value:
            raise ValueError(f"{name}={value!r} contains {_SEP!r}, which would collide keys")
        if not value.strip():
            raise ValueError(f"{name} must be set; an unattributed clip cannot be invalidated")
    joined = _SEP.join((engine, voice, lang, text))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


@dataclass
class PcmCache:
    """One cache directory. Instantiate per process; there is no global one.

    ``hits``/``misses``/``repaired`` are per-instance counters for the TUI and
    for tests. ``repaired`` counting above zero in production means something is
    eating the disk, which is worth seeing before it eats the database.
    """

    root: Path = field(default_factory=default_cache_dir)
    enabled: bool = True
    hits: int = 0
    misses: int = 0
    repaired: int = 0
    writes: int = 0
    write_failures: int = 0

    def key(self, *, engine: str, voice: str, lang: str, text: str) -> str:
        return cache_key(engine=engine, voice=voice, lang=lang, text=text)

    def path(self, key: str) -> Path:
        return self.root / f"{key}.pcm"

    def get(self, key: str) -> bytes | None:
        """The clip, or ``None`` for miss, corrupt, truncated or unreadable."""
        if not self.enabled:
            self.misses += 1
            return None
        path = self.path(key)
        sidecar = path.with_name(path.name + _SIDECAR_SUFFIX)
        try:
            data = path.read_bytes()
            expected = sidecar.read_text(encoding="utf-8").split()
        except (OSError, UnicodeDecodeError):
            self.misses += 1
            return None
        if not self._verifies(data, expected):
            self.repaired += 1
            self.misses += 1
            self._discard(path, sidecar)
            return None
        self.hits += 1
        return data

    def put(self, key: str, pcm: bytes) -> Path | None:
        """Store atomically. Returns the path, or ``None`` if the disk said no."""
        if not self.enabled or not pcm:
            return None
        path = self.path(key)
        sidecar = path.with_name(path.name + _SIDECAR_SUFFIX)
        digest = hashlib.sha256(pcm).hexdigest()
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self._atomic_write(path, pcm)
            self._atomic_write(sidecar, f"{digest} {len(pcm)}\n".encode())
        except OSError:
            # Speech does not depend on the cache existing. A read-only home or
            # a full disk makes every clip a synth, which is slow and correct.
            self.write_failures += 1
            self._discard(path, sidecar)
            return None
        self.writes += 1
        return path

    def clear(self) -> int:
        """Drop every clip. Returns how many files went. Never raises."""
        gone = 0
        try:
            entries = list(self.root.iterdir())
        except OSError:
            return 0
        for entry in entries:
            if entry.suffix not in (".pcm", _SIDECAR_SUFFIX):
                continue
            try:
                entry.unlink()
            except OSError:
                continue
            gone += 1
        return gone

    @staticmethod
    def _verifies(data: bytes, expected: list[str]) -> bool:
        if len(expected) != 2:
            return False
        digest, size = expected
        try:
            if int(size) != len(data):
                return False
        except ValueError:
            return False
        return hashlib.sha256(data).hexdigest() == digest

    def _atomic_write(self, path: Path, data: bytes) -> None:
        # Same directory, so os.replace is a rename within one filesystem and
        # therefore atomic: a reader sees the old file or the new one, never a
        # half-written one. A tmp file per pid keeps two processes synthesising
        # the same label from stepping on each other's partial write.
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, path)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise

    @staticmethod
    def _discard(*paths: Path) -> None:
        for path in paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue
