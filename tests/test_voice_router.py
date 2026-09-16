"""The routing invariant, and what happens when the reader dies.

Every test here is about the one bug this layer exists to prevent: exact text
reaching the voice that paraphrases. The happy path ("free text goes to Gemini")
is two lines; the rest is refusal, mis-wiring and a session that drops halfway
through reading four options aloud.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from jarvis import bus, db
from jarvis.voice.router import (
    LIVE_TRACK,
    VERBATIM_TRACK,
    FidelityViolation,
    NoReader,
    OutputRouter,
    Refused,
    Spoken,
    Track,
    Utterance,
    publish_said,
    refusal_line,
    track_for,
)
from jarvis.voice.verbatim import NoVerbatimEngine


class CollectingReader:
    """A verbatim sink that records what it was asked to say."""

    def __init__(self, nbytes: int = 480) -> None:
        self.said: list[Utterance] = []
        self._nbytes = nbytes

    async def speak(self, utt: Utterance) -> int:
        self.said.append(utt)
        return self._nbytes


class DeadReader:
    """Every engine in the ladder failed. The only honest answer is a refusal."""

    async def speak(self, utt: Utterance) -> int:
        raise NoVerbatimEngine(utt.text, utt.lang, [])


class DroppingReader:
    """Speaks ``ok_for`` clips, then the session drops mid-episode."""

    def __init__(self, ok_for: int) -> None:
        self.said: list[Utterance] = []
        self._left = ok_for

    async def speak(self, utt: Utterance) -> int:
        if self._left <= 0:
            raise ConnectionResetError("reader stream closed mid-turn")
        self._left -= 1
        self.said.append(utt)
        return 480


class CollectingLive:
    def __init__(self) -> None:
        self.said: list[Utterance] = []

    async def say(self, utt: Utterance) -> None:
        self.said.append(utt)


class FailingLive:
    async def say(self, utt: Utterance) -> None:
        raise ConnectionResetError("live session closed")


@pytest.fixture
def mirrored() -> tuple[list[tuple[Utterance, Track | None]], OutputRouter]:
    seen: list[tuple[Utterance, Track | None]] = []
    router = OutputRouter(
        verbatim=CollectingReader(),
        live=CollectingLive(),
        mirror=lambda utt, track: seen.append((utt, track)),
        has_text_channel=True,
    )
    return seen, router


# ───────────────────────────── the type is the rule ─────────────────────────────


def test_exact_never_routes_to_the_paraphrase_track() -> None:
    assert track_for("exact") == VERBATIM_TRACK
    assert track_for("exact", paraphrase_ok=True) == VERBATIM_TRACK
    assert track_for("faithful") == VERBATIM_TRACK
    assert track_for("faithful", paraphrase_ok=True) == LIVE_TRACK
    assert track_for("free") == LIVE_TRACK


def test_pinning_exact_text_to_the_live_track_raises_at_the_call_site() -> None:
    router = OutputRouter(verbatim=CollectingReader(), live=CollectingLive())
    with pytest.raises(FidelityViolation, match="paraphrases"):
        router.enqueue(Utterance(text="2. Postgres", fidelity="exact", track=LIVE_TRACK))
    assert router.pending == 0


def test_paraphrase_faithful_never_reaches_exact() -> None:
    reader, live = CollectingReader(), CollectingLive()
    router = OutputRouter(verbatim=reader, live=live, paraphrase_faithful=True)
    router.enqueue(Utterance(text="How should todos be stored?", fidelity="faithful"))
    router.enqueue(Utterance(text="1. SQLite", fidelity="exact", label="SQLite", index=1))
    assert [u.text for u in router._queue] == [
        "How should todos be stored?",
        "1. SQLite",
    ]


async def test_drain_sends_each_tier_to_its_own_voice(
    mirrored: tuple[list[tuple[Utterance, Track | None]], OutputRouter],
) -> None:
    seen, router = mirrored
    router.enqueue(Utterance(text="Claude Code has a question about storage."))
    router.enqueue(
        Utterance(text="1. SQLite", fidelity="exact", index=1, label="SQLite", earcon="open")
    )
    out = await router.drain()

    assert [type(d) for d in out] == [Spoken, Spoken]
    assert [d.track for d in out] == [LIVE_TRACK, VERBATIM_TRACK]  # type: ignore[union-attr]
    reader = router.verbatim
    assert isinstance(reader, CollectingReader)
    assert [u.text for u in reader.said] == ["1. SQLite"]
    assert [track for _, track in seen] == [LIVE_TRACK, VERBATIM_TRACK]


# ───────────────────────────── the utterance validates itself ─────────────────


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"text": "   "}, "no text"),
        ({"text": "hi", "fidelity": "verbatim"}, "unknown fidelity"),
        ({"text": "hi", "lang": " "}, "lang must be set"),
        ({"text": "hi", "earcon": "chime"}, "unknown earcon"),
        ({"text": "hi", "track": "telegram"}, "unknown track"),
        ({"text": "hi", "index": 0}, "1-based"),
        ({"text": "1. SQLite", "label": "Postgres"}, "not a substring"),
        ({"text": "1. SQLite", "label": " "}, "empty label"),
        ({"text": "1. SQLite", "fidelity": "exact", "mirror": False}, "always mirrored"),
    ],
)
def test_malformed_utterances_are_refused_where_they_are_built(
    kwargs: dict[str, object], match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        Utterance(**kwargs)  # type: ignore[arg-type]


def test_label_must_be_a_substring_so_mirror_probe_and_user_see_one_string() -> None:
    utt = Utterance(text="2. Postgres", fidelity="exact", index=2, label="Postgres")
    assert utt.label is not None
    assert utt.label in utt.text
    assert utt.load_bearing


# ───────────────────────────── refusing, not downgrading ─────────────────────


async def test_dead_reader_refuses_and_announces_in_the_live_voice() -> None:
    live = CollectingLive()
    mirrored: list[tuple[Utterance, Track | None]] = []
    router = OutputRouter(
        verbatim=DeadReader(),
        live=live,
        mirror=lambda u, t: mirrored.append((u, t)),
        has_text_channel=True,
    )
    router.enqueue_all(
        Utterance(text=f"{i}. {label}", fidelity="exact", index=i, label=label, tag="options")
        for i, label in enumerate(("SQLite", "Postgres", "Plain text"), start=1)
    )
    out = await router.drain()

    refusals = [d for d in out if isinstance(d, Refused)]
    assert len(refusals) == 3
    assert {d.action for d in refusals} == {"announced"}
    # The apology is spoken ONCE per episode, in the conversational voice, and
    # it is the only thing that voice ever said about this question.
    assert [u.text for u in live.said] == [refusal_line("en")]
    assert [u.fidelity for u in live.said] == ["free"]
    # The exact text still reached the screen; track None means "not spoken".
    assert [(u.text, t) for u, t in mirrored if u.fidelity == "exact"] == [
        ("1. SQLite", None),
        ("2. Postgres", None),
        ("3. Plain text", None),
    ]


async def test_a_phone_leg_with_no_text_channel_defers_instead() -> None:
    deferred: list[tuple[str, str]] = []
    live = CollectingLive()
    router = OutputRouter(
        verbatim=DeadReader(),
        live=live,
        has_text_channel=False,
        on_defer=lambda utt, reason: deferred.append((utt.text, reason)),
    )
    router.enqueue(Utterance(text="1. SQLite", fidelity="exact", index=1, label="SQLite"))
    out = await router.drain()

    assert isinstance(out[0], Refused)
    assert out[0].action == "deferred"
    assert deferred and deferred[0][0] == "1. SQLite"
    assert "NoVerbatimEngine" in deferred[0][1]
    # Nothing load-bearing was handed to the paraphrase voice. Not one clip.
    assert all(u.fidelity == "free" for u in live.said)
    assert "SQLite" not in " ".join(u.text for u in live.said)


async def test_the_phone_leg_is_not_told_to_look_at_a_screen_it_does_not_have() -> None:
    """The announced line points at the TUI and Telegram. Saying that down a
    phone leg with no text channel is a false statement in the one layer whose
    whole job is that the user can trust what they hear: it sounds like a
    recovery and leaves them waiting for options nobody sent anywhere."""
    live = CollectingLive()
    router = OutputRouter(
        verbatim=DeadReader(),
        live=live,
        has_text_channel=False,
        on_defer=lambda utt, reason: None,
    )
    router.enqueue(Utterance(text="1. SQLite", fidelity="exact", index=1, label="SQLite"))
    await router.drain()

    (spoken,) = [u.text for u in live.said]
    assert spoken == refusal_line("en", has_text_channel=False)
    assert "on screen" not in spoken
    assert "Telegram" not in spoken
    assert spoken != refusal_line("en")


def test_the_deferral_line_also_speaks_turkish() -> None:
    tr = refusal_line("tr-TR", has_text_channel=False)
    assert "ekran yok" in tr
    assert tr != refusal_line("tr-TR")


async def test_a_failing_apology_cannot_abort_the_refusal_of_the_clips_behind_it() -> None:
    """Both voices down is the WORST case, not an exotic one — a dropped network
    takes the cloud reader and the Live session together. The apology is
    manufactured by the refusal path itself, so if it could raise, the
    load-bearing clips still queued behind it would vanish unmirrored and
    undeferred: precisely the silent loss the refusal exists to prevent."""
    mirrored: list[tuple[str, Track | None]] = []
    deferred: list[str] = []
    router = OutputRouter(
        verbatim=DeadReader(),
        live=FailingLive(),
        mirror=lambda u, t: mirrored.append((u.text, t)),
        has_text_channel=False,
        on_defer=lambda utt, reason: deferred.append(utt.text),
    )
    router.enqueue_all(
        Utterance(text=f"{i}. {label}", fidelity="exact", index=i, label=label, tag="options")
        for i, label in enumerate(("SQLite", "Postgres", "Plain text"), start=1)
    )
    out = await router.drain()

    assert router.pending == 0
    assert [d.utterance.text for d in out if isinstance(d, Refused)] == [
        "1. SQLite",
        refusal_line("en", has_text_channel=False),
        "2. Postgres",
        "3. Plain text",
    ]
    # Every answer key reached the screen and the gate, none reached a voice.
    assert [txt for txt, t in mirrored] == ["1. SQLite", "2. Postgres", "3. Plain text"]
    assert {t for _, t in mirrored} == {None}
    # The QUESTION defers. Jarvis's own failed apology is not a question, and
    # handing it to the gate is how a channel ends up holding a request nobody
    # asked. A refusal that apologised for its own apology would also never
    # terminate: each failure carries a new tag, so the per-tag dedupe misses.
    assert deferred == ["1. SQLite", "2. Postgres", "3. Plain text"]


async def test_ordinary_narration_the_live_session_drops_still_reaches_the_caller() -> None:
    """The containment above is scoped to the apology. A narration clip that the
    session dropped is a session problem, and swallowing it would hide it."""
    router = OutputRouter(verbatim=CollectingReader(), live=FailingLive(), has_text_channel=True)
    router.enqueue(Utterance(text="Tests are still running."))
    with pytest.raises(ConnectionResetError):
        await router.drain()


async def test_a_router_with_no_reader_attached_refuses_rather_than_paraphrasing() -> None:
    live = CollectingLive()
    router = OutputRouter(verbatim=None, live=live, has_text_channel=True)
    router.enqueue(Utterance(text="1. SQLite", fidelity="exact", index=1, label="SQLite"))
    out = await router.drain()
    assert isinstance(out[0], Refused)
    assert NoReader.__name__ in out[0].reason
    assert "SQLite" not in " ".join(u.text for u in live.said)


async def test_a_session_that_drops_mid_episode_refuses_only_the_rest() -> None:
    reader = DroppingReader(ok_for=2)
    live = CollectingLive()
    router = OutputRouter(verbatim=reader, live=live, has_text_channel=True)
    router.enqueue_all(
        Utterance(text=f"{i}. {label}", fidelity="exact", index=i, label=label, tag="options")
        for i, label in enumerate(("SQLite", "Postgres", "Plain text", "CSV"), start=1)
    )
    out = await router.drain()

    assert [isinstance(d, Spoken) for d in out[:2]] == [True, True]
    tail = [d for d in out if isinstance(d, Refused)]
    assert [d.utterance.text for d in tail] == ["3. Plain text", "4. CSV"]
    assert [u.text for u in live.said] == [refusal_line("en")]


async def test_free_text_failing_on_the_live_track_is_not_a_fidelity_problem() -> None:
    router = OutputRouter(verbatim=CollectingReader(), live=FailingLive())
    router.enqueue(Utterance(text="Claude Code has a question."))
    with pytest.raises(ConnectionResetError):
        await router.drain()


def test_refusal_line_speaks_the_user_s_language() -> None:
    assert "reader voice" in refusal_line("en")
    assert "Okuyucu sesim" in refusal_line("tr-TR")
    assert refusal_line("de") == refusal_line("en")


# ───────────────────────────── two legs, two routers ─────────────────────────


async def test_routers_are_multi_instantiable_and_share_nothing() -> None:
    desk_reader, phone_reader = CollectingReader(), CollectingReader()
    desk = OutputRouter(verbatim=desk_reader, live=CollectingLive())
    phone = OutputRouter(verbatim=phone_reader, live=CollectingLive())
    desk.enqueue(Utterance(text="1. SQLite", fidelity="exact", index=1, label="SQLite"))
    phone.enqueue(Utterance(text="1. Postgres", fidelity="exact", index=1, label="Postgres"))
    await desk.drain()
    await phone.drain()
    assert [u.text for u in desk_reader.said] == ["1. SQLite"]
    assert [u.text for u in phone_reader.said] == ["1. Postgres"]
    assert desk.pending == phone.pending == 0


# ───────────────────────────── mirroring into the log ────────────────────────


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("JARVIS_DB", str(tmp_path / "jarvis.db"))
    monkeypatch.setenv("JARVIS_POKE_DIR", str(tmp_path / "pk"))
    path = tmp_path / "jarvis.db"
    con = db.connect(path)
    db.migrate(con)
    con.close()
    return path


@pytest.fixture
def con(db_path: Path) -> Iterator[sqlite3.Connection]:
    c = db.connect(db_path)
    yield c
    c.close()


def test_publish_said_records_the_exact_string_and_the_track(con: sqlite3.Connection) -> None:
    utt = Utterance(
        text="2. Postgres",
        fidelity="exact",
        tag="options",
        index=2,
        label="Postgres",
        request_id="req_1",
    )
    ev_id = publish_said(con, "voice", utt, VERBATIM_TRACK, idem_key="said:req_1:2")
    (event,) = bus.read_since(con, 0)
    assert event.id == ev_id
    assert event.kind == "speech.said"
    assert event.request_id == "req_1"
    assert event.payload["text"] == "2. Postgres"
    assert event.payload["label"] == "Postgres"
    assert event.payload["fidelity"] == "exact"
    assert event.payload["track"] == VERBATIM_TRACK
    assert event.payload["spoken"] is True


def test_a_refused_clip_is_mirrored_as_not_spoken(con: sqlite3.Connection) -> None:
    utt = Utterance(text="1. SQLite", fidelity="exact", index=1, label="SQLite")
    publish_said(con, "voice", utt, None)
    (event,) = bus.read_since(con, 0)
    assert event.payload["spoken"] is False
    assert event.payload["track"] is None


def test_mirroring_the_same_clip_twice_makes_one_row(con: sqlite3.Connection) -> None:
    utt = Utterance(text="1. SQLite", fidelity="exact", index=1, label="SQLite")
    first = publish_said(con, "voice", utt, VERBATIM_TRACK, idem_key="said:req_9:1")
    second = publish_said(con, "voice", utt, VERBATIM_TRACK, idem_key="said:req_9:1")
    assert first == second
    assert len(bus.read_since(con, 0)) == 1
