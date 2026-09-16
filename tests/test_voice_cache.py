"""The clip cache: identity, corruption, and never breaking speech.

Two properties matter. The KEY must be the whole identity of the audio — two
strings that differ by a space are different answer keys and must never share a
file. And a BAD FILE must be re-synthesised rather than played or raised over,
because the alternative is a half-written clip going to the speakers or an
exception taking down a question the user is waiting on.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from jarvis.voice.cache import PcmCache, cache_key, default_cache_dir
from jarvis.voice.engines import silence

CLIP = silence(20)


@pytest.fixture
def cache(tmp_path: Path) -> PcmCache:
    return PcmCache(root=tmp_path / "tts")


def key_of(cache: PcmCache, text: str = "SQLite") -> str:
    return cache.key(engine="fake", voice="v1", lang="en", text=text)


# ───────────────────────────── identity ─────────────────────────────


def test_the_key_is_sha256_of_the_four_fields_joined_by_pipes() -> None:
    expected = hashlib.sha256(b"kokoro|af_heart|en|SQLite").hexdigest()
    assert cache_key(engine="kokoro", voice="af_heart", lang="en", text="SQLite") == expected


@pytest.mark.parametrize(
    "changed",
    [
        {"engine": "edge"},
        {"voice": "tr-TR-AhmetNeural"},
        {"lang": "tr"},
        {"text": "SQLite "},
        {"text": "sqlite"},
    ],
)
def test_every_field_changes_the_key(changed: dict[str, str]) -> None:
    base = {"engine": "kokoro", "voice": "af_heart", "lang": "en", "text": "SQLite"}
    assert cache_key(**base) != cache_key(**{**base, **changed})


def test_a_separator_in_a_field_is_refused_rather_than_escaped() -> None:
    with pytest.raises(ValueError, match="collide"):
        cache_key(engine="ed|ge", voice="v", lang="en", text="SQLite")
    with pytest.raises(ValueError, match="must be set"):
        cache_key(engine="edge", voice="", lang="en", text="SQLite")


def test_default_dir_follows_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert default_cache_dir() == tmp_path / "jarvis" / "tts"
    monkeypatch.setenv("XDG_CACHE_HOME", "")
    assert default_cache_dir() == Path.home() / ".cache" / "jarvis" / "tts"


# ───────────────────────────── round trip ─────────────────────────────


def test_a_stored_clip_comes_back_and_counts_as_a_hit(cache: PcmCache) -> None:
    key = key_of(cache)
    assert cache.get(key) is None
    assert cache.misses == 1

    path = cache.put(key, CLIP)
    assert path is not None and path.exists()
    assert cache.get(key) == CLIP
    assert cache.hits == 1
    # No temporary file survives a successful write.
    assert not list(cache.root.glob(".*tmp"))


def test_a_second_process_reads_what_the_first_one_wrote(tmp_path: Path) -> None:
    writer = PcmCache(root=tmp_path / "tts")
    reader = PcmCache(root=tmp_path / "tts")
    key = key_of(writer)
    writer.put(key, CLIP)
    assert reader.get(key) == CLIP


def test_disabling_the_cache_makes_every_lookup_a_miss(cache: PcmCache) -> None:
    key = key_of(cache)
    cache.put(key, CLIP)
    cache.enabled = False
    assert cache.get(key) is None
    assert cache.put(key, CLIP) is None


# ───────────────────────────── corruption ─────────────────────────────


def test_a_truncated_clip_is_discarded_and_re_synthesised(cache: PcmCache) -> None:
    key = key_of(cache)
    path = cache.put(key, CLIP)
    assert path is not None
    path.write_bytes(CLIP[: len(CLIP) // 2])

    assert cache.get(key) is None  # a miss, not an exception and not half a clip
    assert cache.repaired == 1
    assert not path.exists()
    assert not path.with_name(path.name + ".sha256").exists()


def test_a_clip_whose_bytes_changed_under_us_is_discarded(cache: PcmCache) -> None:
    key = key_of(cache)
    path = cache.put(key, CLIP)
    assert path is not None
    path.write_bytes(b"\x7f" * len(CLIP))  # same length, different audio
    assert cache.get(key) is None
    assert cache.repaired == 1


@pytest.mark.parametrize("sidecar_text", ["", "garbage", "deadbeef", "deadbeef notanumber"])
def test_an_unreadable_sidecar_is_a_miss(cache: PcmCache, sidecar_text: str) -> None:
    key = key_of(cache)
    path = cache.put(key, CLIP)
    assert path is not None
    path.with_name(path.name + ".sha256").write_text(sidecar_text)
    assert cache.get(key) is None


def test_a_clip_with_no_sidecar_at_all_is_a_miss(cache: PcmCache) -> None:
    """The crash window: audio written, digest not. It must read as absent."""
    key = key_of(cache)
    cache.root.mkdir(parents=True, exist_ok=True)
    cache.path(key).write_bytes(CLIP)
    assert cache.get(key) is None


def test_an_unwritable_cache_degrades_to_synthesising_every_time(tmp_path: Path) -> None:
    """A cache root that cannot exist must cost latency, never speech.

    A regular file where the directory belongs reproduces the class of failure —
    a full disk, a read-only home, a stale mount — in a way that also fails for
    root, which is who CI runs as.
    """
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("something else owns this path")
    cache = PcmCache(root=blocker / "tts")

    assert cache.put(key_of(cache), CLIP) is None
    assert cache.write_failures == 1
    assert cache.get(key_of(cache)) is None
    assert cache.clear() == 0


def test_clear_drops_clips_and_survives_a_missing_directory(cache: PcmCache) -> None:
    assert cache.clear() == 0
    cache.put(key_of(cache, "SQLite"), CLIP)
    cache.put(key_of(cache, "Postgres"), CLIP)
    assert cache.clear() == 4  # two clips, two sidecars
    assert cache.get(key_of(cache, "SQLite")) is None
