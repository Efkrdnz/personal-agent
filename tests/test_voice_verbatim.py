"""The ladder, the cache in front of it, and the refusal at the end.

The refusal is the test that matters. Everything else here is plumbing that can
be re-derived; "when no engine can speak, the system says so instead of letting
Gemini paraphrase the answer key" is the property the whole design was built to
hold, so it is asserted from three directions: the exception, its contents, and
the absence of any path from here to the conversational voice.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from jarvis.voice.cache import PcmCache
from jarvis.voice.engines import RATE, EngineFailed, FakeEngine, KokoroEngine, silence
from jarvis.voice.router import NoReader, TrackSink, Utterance
from jarvis.voice.verbatim import (
    EARCON_MS,
    NoVerbatimEngine,
    VerbatimSpeaker,
    duration_s,
    earcon_pcm,
)


class CollectSink:
    """Stands in for an AudioBus track: bytes in, nothing opened."""

    def __init__(self) -> None:
        self.chunks: list[bytes] = []
        self.tiers: list[str] = []

    async def write(self, pcm: bytes, *, tier: str = "free") -> None:
        self.chunks.append(pcm)
        self.tiers.append(tier)

    @property
    def pcm(self) -> bytes:
        return b"".join(self.chunks)


class BrokenSink:
    """The call dropped between two clips of one episode."""

    def __init__(self, ok_writes: int) -> None:
        self.left = ok_writes

    async def write(self, pcm: bytes, *, tier: str = "free") -> None:
        if self.left <= 0:
            raise ConnectionResetError("track closed")
        self.left -= 1


@pytest.fixture
def cache(tmp_path: Path) -> PcmCache:
    return PcmCache(root=tmp_path / "tts")


# ───────────────────────────── the ladder ─────────────────────────────


async def test_an_english_only_engine_declines_turkish_and_the_next_rung_speaks() -> None:
    local = KokoroEngine(pipeline=lambda text, voice: [[0.0] * 240])
    cloud = FakeEngine(name="edge", voice="tr-TR-AhmetNeural")
    speaker = VerbatimSpeaker(engines=(local, cloud))

    pcm = await speaker.pcm_for("Postgres", "tr")
    assert len(pcm) == cloud.nbytes("Postgres")
    assert cloud.calls == [("Postgres", "tr")]


async def test_a_failing_engine_is_reported_and_the_next_one_is_tried() -> None:
    seen: list[tuple[str, str, str]] = []
    first = FakeEngine(name="first", fail_on=("SQLite",))
    second = FakeEngine(name="second")
    speaker = VerbatimSpeaker(
        engines=(first, second),
        on_engine_error=lambda engine, text, exc: seen.append((engine, text, type(exc).__name__)),
    )

    pcm = await speaker.pcm_for("SQLite", "en")
    assert len(pcm) == second.nbytes("SQLite")
    assert seen == [("first", "SQLite", "EngineFailed")]


async def test_an_engine_returning_junk_bytes_counts_as_a_failure() -> None:
    class Junk:
        name, deterministic, verified = "junk", True, True

        def voice_for(self, lang: str) -> str:
            return "junk-voice"

        def synth(self, text: str, lang: str) -> bytes:
            return b"\x01\x02\x03"  # not whole int16 frames

    good = FakeEngine(name="good")
    speaker = VerbatimSpeaker(engines=(Junk(), good))
    assert len(await speaker.pcm_for("SQLite", "en")) == good.nbytes("SQLite")


async def test_when_the_ladder_is_exhausted_the_reader_REFUSES() -> None:
    speaker = VerbatimSpeaker(
        engines=(
            KokoroEngine(pipeline=lambda text, voice: []),
            FakeEngine(name="edge", fail_on=("1. SQLite",)),
        )
    )
    with pytest.raises(NoVerbatimEngine) as excinfo:
        await speaker.pcm_for("1. SQLite", "en", exact=True)

    err = excinfo.value
    assert err.text == "1. SQLite"
    assert [a.engine for a in err.attempts] == ["kokoro", "edge"]
    assert "no audio" in str(err)
    # Nothing in this module can reach the paraphrase channel: the only exit
    # from a failed ladder is this exception.
    assert not hasattr(speaker, "live")


async def test_a_speaker_with_no_engines_refuses_and_says_so_plainly() -> None:
    with pytest.raises(NoVerbatimEngine, match="no engines configured"):
        await VerbatimSpeaker().pcm_for("SQLite", "en")


async def test_empty_text_is_a_caller_bug_not_silence() -> None:
    with pytest.raises(ValueError, match="empty text"):
        await VerbatimSpeaker(engines=(FakeEngine(),)).pcm_for("   ")


# ───────────────────────────── exact-tier engine policy ─────────────────────


async def test_an_unverified_generative_engine_may_not_read_an_answer_key() -> None:
    llm = FakeEngine(name="gemini-tts", deterministic=False, verified=False)
    local = FakeEngine(name="kokoro")
    speaker = VerbatimSpeaker(engines=(llm, local))

    await speaker.pcm_for("a plan body", "en", exact=False)
    assert llm.calls, "faithful-tier prose may go through the nicer voice"

    llm.calls.clear()
    await speaker.pcm_for("1. SQLite", "en", exact=True)
    assert llm.calls == []
    assert local.calls == [("1. SQLite", "en")]


async def test_a_generative_engine_that_cleared_the_probe_is_allowed() -> None:
    llm = FakeEngine(name="gemini-tts", deterministic=False, verified=True)
    speaker = VerbatimSpeaker(engines=(llm,))
    await speaker.pcm_for("1. SQLite", "en", exact=True)
    assert llm.calls == [("1. SQLite", "en")]


# ───────────────────────────── the cache in front ─────────────────────────────


async def test_the_second_reading_of_a_label_never_reaches_an_engine(cache: PcmCache) -> None:
    engine = FakeEngine()
    speaker = VerbatimSpeaker(engines=(engine,), cache=cache)

    first = await speaker.pcm_for("SQLite", "en")
    assert speaker.cached("SQLite", "en")
    second = await speaker.pcm_for("SQLite", "en")

    assert first == second
    assert engine.calls == [("SQLite", "en")]  # "repeat option two" is a file read
    assert cache.hits == 2  # the pcm_for above, plus cached() which is itself a read


async def test_a_corrupt_cached_clip_is_re_synthesised_rather_than_played(
    cache: PcmCache,
) -> None:
    engine = FakeEngine()
    speaker = VerbatimSpeaker(engines=(engine,), cache=cache)
    await speaker.pcm_for("SQLite", "en")
    path = cache.path(cache.key(engine="fake", voice="fake-voice", lang="en", text="SQLite"))
    path.write_bytes(b"\x00")  # truncated to one byte by a bad shutdown

    pcm = await speaker.pcm_for("SQLite", "en")
    assert len(pcm) == engine.nbytes("SQLite")
    assert engine.calls == [("SQLite", "en"), ("SQLite", "en")]
    assert cache.repaired == 1


async def test_two_engines_do_not_share_a_cache_entry(cache: PcmCache) -> None:
    a = FakeEngine(name="a", ms_per_char=10.0)
    b = FakeEngine(name="b", ms_per_char=20.0)
    speaker_a = VerbatimSpeaker(engines=(a,), cache=cache)
    speaker_b = VerbatimSpeaker(engines=(b,), cache=cache)
    assert len(await speaker_a.pcm_for("SQLite")) != len(await speaker_b.pcm_for("SQLite"))


# ───────────────────────────── pre-synthesis ─────────────────────────────


async def test_prefetch_warms_every_clip_and_deduplicates(cache: PcmCache) -> None:
    engine = FakeEngine()
    speaker = VerbatimSpeaker(engines=(engine,), cache=cache)
    ready = await speaker.prefetch(["1. SQLite", "2. Postgres", "1. SQLite", "  "])
    assert ready == 2
    assert [t for t, _ in engine.calls] == ["1. SQLite", "2. Postgres"]
    assert speaker.cached("2. Postgres")


async def test_prefetch_swallows_failures_so_a_slow_question_is_not_a_lost_one() -> None:
    engine = FakeEngine(fail_on=("2. Postgres",))
    speaker = VerbatimSpeaker(engines=(engine,))
    assert await speaker.prefetch(["1. SQLite", "2. Postgres"]) == 1


async def test_prefetching_nothing_is_not_an_error() -> None:
    assert await VerbatimSpeaker().prefetch([]) == 0


# ───────────────────────────── playback ─────────────────────────────


async def test_say_brackets_the_episode_with_the_earcon() -> None:
    sink = CollectSink()
    engine = FakeEngine()
    speaker = VerbatimSpeaker(engines=(engine,))
    earcon = earcon_pcm()

    written = await speaker.say(sink, "1. SQLite", earcon="open")
    assert sink.chunks == [earcon, silence(engine.duration_ms("1. SQLite"))]
    assert written == len(earcon) + engine.nbytes("1. SQLite")

    sink2 = CollectSink()
    await speaker.say(sink2, "3. Plain text", earcon="close")
    assert sink2.chunks[-1] == earcon


async def test_the_earcon_is_150_ms_and_identical_every_time() -> None:
    speaker = VerbatimSpeaker(engines=(FakeEngine(),))
    assert speaker.earcon() is speaker.earcon()
    assert duration_s(speaker.earcon()) == pytest.approx(EARCON_MS / 1000.0)
    samples = np.frombuffer(speaker.earcon(), dtype="<i2")
    assert samples.size == int(RATE * EARCON_MS / 1000)


async def test_speak_carries_the_tier_so_nobody_has_to_remember_to_pass_it() -> None:
    llm = FakeEngine(name="gemini-tts", deterministic=False, verified=False)
    local = FakeEngine(name="kokoro")
    sink = CollectSink()
    speaker = VerbatimSpeaker(engines=(llm, local), sink=sink)

    await speaker.speak(Utterance(text="1. SQLite", fidelity="exact", index=1, label="SQLite"))
    assert llm.calls == []
    assert local.calls == [("1. SQLite", "en")]


async def test_a_speaker_with_no_sink_is_a_wiring_bug_with_its_own_name() -> None:
    speaker = VerbatimSpeaker(engines=(FakeEngine(),))
    with pytest.raises(NoReader, match="no sink"):
        await speaker.speak(Utterance(text="hello"))


async def test_a_track_that_closes_mid_episode_surfaces_to_the_caller() -> None:
    speaker = VerbatimSpeaker(engines=(FakeEngine(),), sink=BrokenSink(ok_writes=1))
    with pytest.raises(ConnectionResetError):
        await speaker.speak(
            Utterance(text="1. SQLite", fidelity="exact", index=1, label="SQLite", earcon="open")
        )


async def test_synthesis_failure_reaches_the_caller_as_a_refusal_not_as_silence() -> None:
    sink = CollectSink()
    speaker = VerbatimSpeaker(engines=(FakeEngine(fail_on=("1. SQLite",)),), sink=sink)
    with pytest.raises(NoVerbatimEngine):
        await speaker.speak(Utterance(text="1. SQLite", fidelity="exact", index=1, label="SQLite"))
    assert sink.chunks == []  # not even the earcon: nothing half-spoken


def test_engine_failures_are_two_different_words_on_purpose() -> None:
    engine = FakeEngine(fail_on=("x",))
    with pytest.raises(EngineFailed):
        engine.synth("x", "en")


# ───────────────────────────── the second check, at the bus ─────────────────


async def test_the_tier_travels_with_the_bytes_so_the_mixer_can_check_it_too() -> None:
    sink = CollectSink()
    speaker = VerbatimSpeaker(engines=(FakeEngine(),), sink=sink)
    await speaker.speak(
        Utterance(text="1. SQLite", fidelity="exact", index=1, label="SQLite", earcon="open")
    )
    # The tone carries no words and is free on any track; the clip is not.
    assert sink.tiers == ["free", "exact"]


async def test_the_mixer_refuses_exact_audio_on_the_paraphrase_track() -> None:
    """Two layers, one rule. The router refuses to ROUTE it; the mixer refuses
    to ACCEPT it. This test bypasses the router entirely — as a future caller
    with a mixer track in hand could — and the audio layer still says no."""
    mixer = pytest.importorskip("jarvis.audio.mixer")
    bus = mixer.PlaybackMixer()
    live = TrackSink(bus.track("live", mixer.Prio.LIVE))
    reader = TrackSink(bus.track("verbatim", mixer.Prio.VERBATIM))
    speaker = VerbatimSpeaker(engines=(FakeEngine(),))

    utt = Utterance(text="1. SQLite", fidelity="exact", index=1, label="SQLite")
    with pytest.raises(mixer.FidelityViolation):
        await speaker.say(live, utt.text, exact=True, tier=utt.fidelity)

    written = await speaker.say(reader, utt.text, exact=True, tier=utt.fidelity)
    assert written > 0
