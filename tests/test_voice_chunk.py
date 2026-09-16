"""One clip per option, and a splitter that survives a stream.

The clip tests pin the shape the latency trick needs: N+2 short clips per
question, each one independently cacheable, with the ordinal glued to its label.
The splitter tests are all about the STREAM — a terminator at the end of a chunk
may be the end of a sentence or the middle of "Dr.", and only the next chunk
knows which.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis.voice.cache import PcmCache
from jarvis.voice.chunk import (
    MAX_RUN_CHARS,
    PrefetchItem,
    SentenceSplitter,
    disclosure_clip,
    narration_clips,
    option_clips,
    presynthesise,
    readback_clips,
    sentences,
    texts_to_prefetch,
)
from jarvis.voice.engines import FakeEngine
from jarvis.voice.router import LIVE_TRACK, VERBATIM_TRACK, OutputRouter, Spoken, Utterance
from jarvis.voice.verbatim import VerbatimSpeaker

SINGLE = {
    "questions": [
        {
            "header": "Storage",
            "question": "How should todos be stored?",
            "options": [
                {"label": "SQLite", "description": "a local database file"},
                {"label": "JSON file", "description": "a single JSON file"},
                {"label": "Plain text", "description": "one per line"},
            ],
            "multiSelect": False,
        }
    ]
}
BATCH = {
    "questions": [
        SINGLE["questions"][0],
        {
            "question": "Which features should be included?",
            "options": [{"label": "Due dates"}, {"label": "Tags"}],
            "multiSelect": True,
        },
    ]
}


# ───────────────────────────── one clip per option ─────────────────────────────


def test_a_question_renders_to_n_plus_two_clips() -> None:
    clips = option_clips(SINGLE)
    assert len(clips) == 3 + 2  # the question, three options, the free-text tail
    assert [c.fidelity for c in clips] == ["faithful", "exact", "exact", "exact", "free"]
    assert [c.text for c in clips[1:4]] == ["1. SQLite", "2. JSON file", "3. Plain text"]
    assert [c.label for c in clips[1:4]] == ["SQLite", "JSON file", "Plain text"]
    assert [c.index for c in clips[1:4]] == [1, 2, 3]


def test_the_ordinal_is_glued_to_its_label_with_nothing_in_between() -> None:
    """That adjacency is what tools/fidelity_probe.py scores, and what makes
    "the second one" resolve against something."""
    for clip in option_clips(SINGLE):
        if clip.fidelity != "exact":
            continue
        assert clip.label is not None
        assert clip.text == f"{clip.index}. {clip.label}"


def test_the_whole_episode_is_pinned_to_the_reader_even_the_free_lines() -> None:
    clips = option_clips(SINGLE)
    assert {c.track for c in clips} == {VERBATIM_TRACK}


def test_the_earcon_brackets_the_episode_not_every_clip() -> None:
    clips = option_clips(SINGLE)
    assert clips[0].earcon == "open"
    assert clips[-1].earcon == "close"
    assert {c.earcon for c in clips[1:-1]} == {"none"}


def test_numbering_runs_across_a_whole_batch() -> None:
    clips = [c for c in option_clips(BATCH) if c.fidelity == "exact"]
    assert [(c.index, c.label) for c in clips] == [
        (1, "SQLite"),
        (2, "JSON file"),
        (3, "Plain text"),
        (4, "Due dates"),
        (5, "Tags"),
    ]


def test_the_request_id_travels_with_every_clip() -> None:
    clips = option_clips(SINGLE, request_id="req_7", lang="tr")
    assert {c.request_id for c in clips} == {"req_7"}
    assert {c.lang for c in clips} == {"tr"}


# ───────────────────────────── read-back and disclosure ─────────────────────


def test_the_confirmed_requirement_list_is_exact_and_numbered() -> None:
    clips = readback_clips(["Use Postgres, not MySQL", "  ", "No Docker"])
    assert [c.text for c in clips] == ["1. Use Postgres, not MySQL", "2. No Docker"]
    assert {c.fidelity for c in clips} == {"exact"}
    assert [c.earcon for c in clips] == ["open", "close"]


def test_a_one_item_readback_still_gets_both_brackets() -> None:
    (clip,) = readback_clips(["Ship it"])
    assert clip.earcon == "both"


def test_the_disclosure_line_is_contract_text() -> None:
    clip = disclosure_clip("This call is being handled by an AI assistant.")
    assert clip.fidelity == "exact"
    assert clip.track == VERBATIM_TRACK
    assert clip.earcon == "both"


def test_narration_is_free_tier_and_goes_to_the_conversational_voice() -> None:
    clips = narration_clips("Tests are running. Two files changed so far.")
    assert [c.text for c in clips] == ["Tests are running.", "Two files changed so far."]
    assert {c.fidelity for c in clips} == {"free"}
    assert {c.track for c in clips} == {None}


# ───────────────────────────── pre-synthesis ─────────────────────────────


def test_only_clips_the_reader_will_speak_are_queued_for_synthesis() -> None:
    wanted = texts_to_prefetch([*option_clips(SINGLE), *narration_clips("Free prose here.")])
    assert PrefetchItem("1. SQLite", "en", True) in wanted
    assert "Free prose here." not in [item.text for item in wanted]


def test_duplicate_clips_are_synthesised_once() -> None:
    utt = Utterance(text="Yes", fidelity="exact", track=VERBATIM_TRACK)
    assert texts_to_prefetch([utt, utt]) == (PrefetchItem("Yes", "en", True),)


def test_the_tier_survives_into_the_prefetch_because_it_picks_the_engine() -> None:
    wanted = texts_to_prefetch(option_clips(SINGLE))
    by_text = {item.text: item.exact for item in wanted}
    # The question is faithful and the tail is free; only the answer keys are exact.
    assert by_text["1. SQLite"] is True
    assert by_text["How should todos be stored?"] is False


class _NullSink:
    async def write(self, pcm: bytes, *, tier: str = "free") -> None:
        return None


async def test_presynthesis_puts_the_whole_question_on_disk_before_it_is_read(
    tmp_path: Path,
) -> None:
    cache = PcmCache(root=tmp_path / "tts")
    engine = FakeEngine()
    speaker = VerbatimSpeaker(engines=(engine,), cache=cache)
    clips = option_clips(SINGLE)

    ready = await presynthesise(speaker, clips)
    assert ready == len(clips)
    calls_after_prefetch = len(engine.calls)

    speaker.sink = _NullSink()
    router = OutputRouter(verbatim=speaker, live=None)
    router.enqueue_all(clips)
    out = await router.drain()

    assert all(isinstance(d, Spoken) for d in out)
    # Reading the question touched no engine at all: every clip was a file read.
    assert len(engine.calls) == calls_after_prefetch


async def test_prefetching_past_an_engine_barred_from_answer_keys_still_warms_the_right_one(
    tmp_path: Path,
) -> None:
    """The ladder the architecture actually describes: a generative voice on top
    for FAITHFUL prose, a deterministic one under it for the labels. Pre-synthesis
    must warm each clip against the engine that will really speak it, or the
    answer keys are synthesised from scratch at the one moment this whole
    mechanic exists to keep quiet."""
    cache = PcmCache(root=tmp_path / "tts")
    generative = FakeEngine(name="gemini-tts", voice="charon", deterministic=False, verified=False)
    deterministic = FakeEngine(name="kokoro", voice="af_heart")
    speaker = VerbatimSpeaker(engines=(generative, deterministic), cache=cache)
    clips = option_clips(SINGLE)

    await presynthesise(speaker, clips)
    calls_after_prefetch = (len(generative.calls), len(deterministic.calls))
    # The answer keys were warmed against kokoro, not against the barred engine.
    assert calls_after_prefetch[1] == sum(1 for c in clips if c.fidelity == "exact")

    speaker.sink = _NullSink()
    router = OutputRouter(verbatim=speaker, live=None)
    router.enqueue_all(clips)
    out = await router.drain()

    assert all(isinstance(d, Spoken) for d in out)
    assert (len(generative.calls), len(deterministic.calls)) == calls_after_prefetch


async def test_a_mixed_batch_prefetches_each_language_separately(tmp_path: Path) -> None:
    speaker = VerbatimSpeaker(
        engines=(FakeEngine(),),
        cache=PcmCache(root=tmp_path / "tts"),
    )
    clips = (
        *option_clips(SINGLE, lang="en"),
        *option_clips(SINGLE, lang="tr"),
    )
    assert await presynthesise(speaker, clips) == len(clips)
    assert speaker.cached("1. SQLite", "tr")


# ───────────────────────────── the sentence splitter ─────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("One. Two. Three.", ["One.", "Two.", "Three."]),
        ("Ran 3.14 seconds, fine.", ["Ran 3.14 seconds, fine."]),
        ("See jarvis/voice/router.py for it.", ["See jarvis/voice/router.py for it."]),
        ("CLI v2.1.273 is pinned.", ["CLI v2.1.273 is pinned."]),
        ("Use e.g. SQLite here. Then stop.", ["Use e.g. SQLite here.", "Then stop."]),
        ("Dr. Who called. He waited.", ["Dr. Who called.", "He waited."]),
        ("1. SQLite 2. Postgres", ["1. SQLite 2. Postgres"]),
        ("Bitti mi? Evet, örn. şimdi.", ["Bitti mi?", "Evet, örn. şimdi."]),
        ("Wait... it finished.", ["Wait...", "it finished."]),
        ('He said "go." Then left.', ['He said "go."', "Then left."]),
        ("no punctuation at all", ["no punctuation at all"]),
        ("", []),
    ],
)
def test_sentence_boundaries(text: str, expected: list[str]) -> None:
    assert sentences(text) == expected


def test_the_splitter_waits_for_the_chunk_that_disambiguates() -> None:
    splitter = SentenceSplitter()
    assert splitter.feed("The file is router") == []
    # A terminator at the end of the buffer may be a boundary or an abbreviation;
    # nothing is emitted until whitespace proves it.
    assert splitter.feed(".") == []
    assert splitter.feed("py and it works. Next") == ["The file is router.py and it works."]
    assert splitter.flush() == ["Next"]


def test_flush_ends_the_stream_and_leaves_nothing_behind() -> None:
    splitter = SentenceSplitter()
    splitter.feed("One. Two")
    assert splitter.flush() == ["Two"]
    assert splitter.flush() == []


def test_a_long_run_with_no_punctuation_is_still_spoken() -> None:
    run = "word " * 200
    out = sentences(run)
    assert len(out) > 1
    assert all(len(s) <= MAX_RUN_CHARS for s in out)
    assert "".join(out).replace(" ", "") == run.replace(" ", "")


class _CollectLive:
    def __init__(self) -> None:
        self.said: list[Utterance] = []

    async def say(self, utt: Utterance) -> None:
        self.said.append(utt)


async def test_free_narration_actually_reaches_the_live_track() -> None:
    live = _CollectLive()
    router = OutputRouter(verbatim=None, live=live)
    router.enqueue_all(narration_clips("Tests pass. Nothing else to report."))
    out = await router.drain()
    assert [d.track for d in out] == [LIVE_TRACK, LIVE_TRACK]  # type: ignore[union-attr]
    assert [u.text for u in live.said] == ["Tests pass.", "Nothing else to report."]
