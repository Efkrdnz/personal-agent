"""Hearing "quote" when the recogniser wrote "coat" — and leaving the coat alone.

The failure this file exists for is a mishearing that becomes a contract: "add a
stock coat widget" filed, tidied and read back with "coat" in it. The failure it
must not trade that for is worse: an assistant that edits what the user said
when they said it right. So every correction here is tested from both sides.
"""

from __future__ import annotations

import ast
import json
import os
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from jarvis import __main__ as cli
from jarvis import hearing
from jarvis.config import Config, Voice
from jarvis.db import connect, migrate
from jarvis.live import persona as manner
from jarvis.live.profiles import DESK, PHONE_USER, live_connect_config
from jarvis.live.session import ToolCall
from jarvis.live.text import GeminiArbiter, TextCallFailed
from jarvis.tools.builtin import hearing as hearing_tools
from jarvis.tools.ctx import ToolCtx
from jarvis.tools.default import registry
from jarvis.tools.registry import Registry, Tool
from jarvis.voice.tools import LiveTools, Transcript


@pytest.fixture
def dbpath(tmp_path: Path) -> Iterator[Path]:
    p = tmp_path / "j.db"
    c = connect(p)
    migrate(c)
    c.close()
    yield p


@pytest.fixture
def con(dbpath: Path) -> Iterator[sqlite3.Connection]:
    c = connect(dbpath)
    yield c
    c.close()


def fix(text: str, con: sqlite3.Connection | None = None, **kw) -> str:
    lex = hearing.lexicon(con) if con is not None else hearing.Lexicon(entries=hearing.SEED)
    return hearing.correct(text, lex, **kw).text


# ───────────────────────────── the seeds, both ways ─────────────────────────────


@pytest.mark.parametrize(
    ("said", "meant"),
    [
        ("get me a stock coat for apple", "get me a stock quote for apple"),
        ("what's the coat of the day", "what's the quote of the day"),
        ("Coat me a price for the roof", "Quote me a price for the roof"),
        ("put single coats around the string", "put single quotes around the string"),
        ("add a coat widget to the app", "add a quote widget to the app"),
        ("I got a court for the insurance", "I got a quote for the insurance"),
        ("a famous coat by Einstein", "a famous quote by Einstein"),
        ("ask cloud code to fix the tests", "ask Claude code to fix the tests"),
    ],
)
def test_the_word_meant_is_restored_when_the_context_says_so(said: str, meant: str) -> None:
    assert fix(said) == meant


@pytest.mark.parametrize(
    "said",
    [
        "where is my winter coat",
        "it needs a fresh coat of paint",
        "the court case was dismissed",
        "we played on the tennis court",
        "I need a coat",
        "is my data in the cloud",
        "the coat of arms on the gate",
    ],
)
def test_a_word_that_was_heard_right_is_left_alone(said: str) -> None:
    assert fix(said) == said


def test_two_readings_in_one_sentence_are_decided_separately() -> None:
    # "winter coat" at the end must not argue about the "stock coat" at the start.
    out = hearing.correct(
        "get me a stock coat for apple and put on my winter coat",
        hearing.Lexicon(entries=hearing.SEED),
    )
    assert out.text == "get me a stock quote for apple and put on my winter coat"
    assert [f.applied for f in out.fixes] == [True, False]


def test_a_cue_from_another_thought_does_not_count() -> None:
    far = "the price was fine " + "and then we talked about other things for ages " * 3
    assert fix(far + "and I hung up my coat") == far + "and I hung up my coat"


def test_capitals_and_names_are_kept() -> None:
    assert fix("STOCK COAT please") == "STOCK QUOTE please"
    assert "Claude" in fix("tell cloud to plan it")


def test_the_raw_words_survive_every_correction() -> None:
    out = hearing.correct("stock coat", hearing.Lexicon(entries=hearing.SEED))
    assert out.raw == "stock coat" and out.text == "stock quote" and out.changed
    (f,) = out.applied
    assert (f.heard, f.meant) == ("coat", "quote") and "stock" in f.why


# ───────────────────────────── learning ─────────────────────────────


def test_teaching_a_pair_makes_a_borderline_word_fire(con: sqlite3.Connection) -> None:
    # One weak cue: not enough on its own...
    assert fix("send me that coat later, it's for the deck", con) == (
        "send me that coat later, it's for the deck"
    )
    hearing.teach(con, "coat", "quote", context="the coat for the deck")
    hearing.teach(con, "coat", "quote", context="the coat for the deck again")
    # ...the user's own history tips it, and 'deck' became a learned cue.
    assert fix("send me that coat later, it's for the deck", con) == (
        "send me that quote later, it's for the deck"
    )
    assert "deck" in hearing.lexicon(con).learned["quote"]


def test_a_learned_cue_needs_two_sentences_and_is_never_the_misheard_word(
    con: sqlite3.Connection,
) -> None:
    hearing.teach(con, "coat", "quote", context="the coat for the boiler")
    assert "boiler" not in hearing.lexicon(con).learned.get("quote", ())
    hearing.teach(con, "coat", "quote", context="another coat for the boiler")
    learned = hearing.lexicon(con).learned["quote"]
    assert "boiler" in learned and "coat" not in learned


def test_a_rejection_outweighs_a_confirmation(con: sqlite3.Connection) -> None:
    hearing.teach(con, "coat", "quote")
    hearing.reject(con, "coat", "quote", context="no I really said coat")
    assert hearing.lexicon(con).prior[("coat", "quote")] < 0


def test_teaching_a_new_word_creates_its_entry(con: sqlite3.Connection) -> None:
    hearing.teach(con, "jason", "json", cues=["file", "parse"])
    assert fix("parse the jason file", con) == "parse the json file"


def test_the_database_refuses_a_pair_that_is_not_a_correction(con: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="nothing to teach"):
        hearing.teach(con, "Quote", "quote")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO hearing_fixes(heard, meant, verdict, actor, at) "
            "VALUES ('coat','coat','yes','t','x')"
        )


def test_forgetting_drops_what_was_taught_but_not_the_seed(con: sqlite3.Connection) -> None:
    hearing.teach(con, "coach", "quote")
    assert hearing.forget(con, "quote")
    entry = hearing.lexicon(con).entry("quote")
    assert entry is not None and "coach" not in entry.heard_as and "coat" in entry.heard_as


def test_config_words_bias_the_ear_but_never_rewrite_anything(con: sqlite3.Connection) -> None:
    lex = hearing.lexicon(con, extra_terms=["Kubernetes"])
    assert "kubernetes" in hearing.vocabulary(lex)
    assert hearing.correct("cooper nettie's cluster", lex).text == "cooper nettie's cluster"


def test_two_processes_teaching_at_once_both_land(dbpath: Path) -> None:
    errors: list[BaseException] = []

    def worker(heard: str) -> None:
        c = connect(dbpath)
        try:
            for _ in range(10):
                hearing.teach(c, heard, "quote", context=f"a {heard} for the roof")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            c.close()

    threads = [threading.Thread(target=worker, args=(w,)) for w in ("coach", "coast")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    c = connect(dbpath)
    try:
        entry = hearing.lexicon(c).entry("quote")
        assert entry is not None and {"coach", "coast"} <= set(entry.heard_as)
        assert c.execute("SELECT count(*) FROM hearing_fixes").fetchone()[0] == 20
    finally:
        c.close()


# ───────────────────────────── the arbiter ─────────────────────────────


def test_the_arbiter_is_asked_only_when_the_evidence_is_split() -> None:
    asked: list[tuple[str, str, str]] = []

    def arbiter(sentence: str, heard: str, meant: str) -> bool:
        asked.append((sentence, heard, meant))
        return True

    lex = hearing.Lexicon(entries=hearing.SEED, prior={("coat", "quote"): 1.0})
    hearing.correct("stock coat", lex, arbiter=arbiter)  # confident: not asked
    hearing.correct("my winter coat", lex, arbiter=arbiter)  # clearly not: not asked
    assert asked == []
    # History alone is +1: real evidence, not enough to act on. That is the vote.
    out = hearing.correct("I need a coat", lex, arbiter=arbiter)
    assert asked == [("I need a coat", "coat", "quote")]
    assert out.text == "I need a quote" and out.applied[0].by == "arbiter"


def test_a_dead_arbiter_leaves_the_words_as_heard() -> None:
    def broken(*_: str) -> bool:
        raise RuntimeError("network")

    lex = hearing.Lexicon(entries=hearing.SEED, prior={("coat", "quote"): 1.0})
    assert hearing.correct("I need a coat", lex, arbiter=broken).text == "I need a coat"


def test_the_gemini_arbiter_can_only_vote_between_the_two_words() -> None:
    assert GeminiArbiter(lambda p: "Quote.")("stock coat", "coat", "quote") is True
    assert GeminiArbiter(lambda p: "coat")("stock coat", "coat", "quote") is False
    # A third word, or a rewrite, is not a vote for the correction.
    assert GeminiArbiter(lambda p: "quota")("stock coat", "coat", "quote") is False
    assert GeminiArbiter(lambda p: "the user said quote")("x", "coat", "quote") is False

    def down(_: str) -> str:
        raise TextCallFailed("no network")

    assert GeminiArbiter(down)("stock coat", "coat", "quote") is False


# ───────────────────────────── sound ─────────────────────────────


def test_the_accent_confusions_share_a_sound_key() -> None:
    assert hearing.phonetic_key("quote") == hearing.phonetic_key("coat")
    assert hearing.phonetic_key("quote") == hearing.phonetic_key("court")
    assert hearing.phonetic_key("quote") != hearing.phonetic_key("tree")


def test_the_misheard_word_is_found_in_the_sentence() -> None:
    assert hearing.closest("quote", "get the stock coat now") == "coat"
    assert hearing.closest("quote", "turn on the lights") is None


# ───────────────────────────── the other half: the ear and the model ─────────────


def test_the_recogniser_and_the_model_are_both_told() -> None:
    lex = hearing.Lexicon(entries=hearing.SEED)
    vocab = hearing.vocabulary(lex)
    assert "stock quote" in vocab and "quote" in vocab and "Claude" in vocab
    told = hearing.instruction(lex)
    assert "'coat'" in told and "'quote'" in told and "correct_hearing" in told


def test_the_vocabulary_reaches_the_live_config() -> None:
    pytest.importorskip("google.genai")
    from dataclasses import replace

    cfg = live_connect_config(replace(DESK, vocabulary=("stock quote",), language_codes=("en-US",)))
    assert cfg.input_audio_transcription.custom_vocabulary == ["stock quote"]
    assert cfg.input_audio_transcription.language_codes == ["en-US"]
    plain = live_connect_config(DESK)
    assert plain.input_audio_transcription.custom_vocabulary is None


def test_the_composition_root_builds_a_profile_that_knows_the_lexicon(
    con: sqlite3.Connection,
) -> None:
    hearing.teach(con, "jason", "json")
    cfg = Config(voice=Voice(vocabulary=("Kubernetes",), languages=("en-US", "tr-TR")))
    prof = cli.heard_profile(cfg, con, DESK)
    # The desk's own words are rebuilt with what this machine lets it do, so the
    # pin is "the desk's instruction, then the lexicon", not the default text.
    withheld = cli.withheld_tools()
    own = manner.desk_instruction(
        cfg.persona.address,
        cfg.persona.name,
        builds="code_build" not in withheld,
        pc="open_app" not in withheld,
        commands="run_command" not in withheld,
        vision="look_at_screen" not in withheld,
    )
    assert prof.system_instruction.startswith(own)
    assert "jason" in prof.system_instruction
    assert "json" in prof.vocabulary and "kubernetes" in prof.vocabulary
    assert prof.language_codes == ("en-US", "tr-TR")
    off = cli.heard_profile(Config(voice=Voice(asr_vocabulary=False)), con, DESK)
    assert off.vocabulary == ()


def test_the_desk_is_wired_to_hear() -> None:
    """The caller, not the callee: the bug class this repo is prone to.

    `heard_profile` and `hearing_for` both work in isolation; this asserts the
    desk actually calls them, because a corrector nobody hands to LiveTools is
    1,982 green tests and a deaf product.
    """
    src = Path(cli.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    desk = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_build_desk"
    )
    calls = {
        n.func.id
        for n in ast.walk(desk)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert {"heard_profile", "hearing_for", "LiveTools"} <= calls
    live_tools = next(
        n
        for n in ast.walk(desk)
        if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "LiveTools"
    )
    assert "hearing" in {k.arg for k in live_tools.keywords}


# ───────────────────────────── the tools ─────────────────────────────


def ctx(con: sqlite3.Connection, **extra) -> ToolCtx:
    return ToolCtx(con=con, channel="desk", actor="desk", extra=extra)


def test_no_i_said_quote_finds_the_misheard_word_itself(con: sqlite3.Connection) -> None:
    said = registry().dispatch(
        "correct_hearing",
        {"meant": "quote"},
        ctx(con, transcript_raw="get me the coat for the boiler"),
    )
    assert "coat" in said and "quote" in said
    assert hearing.lexicon(con).prior[("coat", "quote")] == 1.0


def test_with_nothing_that_sounds_close_it_asks_instead_of_guessing(
    con: sqlite3.Connection,
) -> None:
    said = registry().dispatch(
        "correct_hearing", {"meant": "quote"}, ctx(con, transcript_raw="turn on the lights")
    )
    assert "can't tell which word" in said
    assert con.execute("SELECT count(*) FROM hearing_fixes").fetchone()[0] == 0


def test_no_i_really_said_coat_backs_off_the_correction_just_made(
    con: sqlite3.Connection,
) -> None:
    heard = hearing.correct("a stock coat", hearing.lexicon(con))
    said = registry().dispatch(
        "wrong_correction", {"said": "coat"}, ctx(con, heard=heard, transcript_raw=heard.raw)
    )
    assert "you said coat" in said
    assert hearing.lexicon(con).prior[("coat", "quote")] < 0


def test_both_tools_are_offered_on_both_voice_legs() -> None:
    names = registry().names("desk")
    for tool in ("correct_hearing", "wrong_correction"):
        assert tool in names and tool in DESK.tools and tool in PHONE_USER.tools


async def test_tools_see_the_corrected_words_and_keep_the_raw_ones(dbpath: Path) -> None:
    seen: dict[str, object] = {}

    def handler(ctx: ToolCtx) -> str:
        seen.update(ctx.extra)
        return "ok"

    reg = Registry([Tool(name="code_build", description="d", handler=handler)])
    t = Transcript()
    t.heard("build a stock coat widget")
    lt = LiveTools(
        registry=reg,
        open_db=lambda: connect(dbpath),
        transcript=t,
        hearing=lambda c, text: hearing.correct(text, hearing.lexicon(c)),
    )
    await lt.dispatch(ToolCall(id="1", name="code_build", args={}))
    assert seen["transcript"] == "build a stock quote widget"
    assert seen[hearing_tools.TRANSCRIPT_RAW] == "build a stock coat widget"


async def test_a_broken_corrector_costs_the_correction_not_the_request(dbpath: Path) -> None:
    seen: list[object] = []

    def handler(ctx: ToolCtx) -> str:
        seen.append(ctx.extra["transcript"])
        return "ok"

    def broken(c: sqlite3.Connection, text: str) -> hearing.Heard:
        raise RuntimeError("lexicon unreadable")

    t = Transcript()
    t.heard("build a stock coat widget")
    lt = LiveTools(
        registry=Registry([Tool(name="code_build", description="d", handler=handler)]),
        open_db=lambda: connect(dbpath),
        transcript=t,
        hearing=broken,
    )
    await lt.dispatch(ToolCall(id="1", name="code_build", args={}))
    assert seen == ["build a stock coat widget"]


def test_a_build_records_which_words_were_corrected(con: sqlite3.Connection) -> None:
    heard = hearing.correct("build a stock coat widget for my portfolio", hearing.lexicon(con))
    registry().dispatch(
        "code_build",
        {"summary": "stock quote widget"},
        ctx(con, transcript=heard.text, transcript_raw=heard.raw, heard=heard),
    )
    (payload,) = con.execute("SELECT payload FROM events WHERE kind='project.requested'").fetchone()
    assert json.loads(payload)["corrected"] == [["coat", "quote"]]


# ───────────────────────────── the CLI and the spine rule ─────────────────────────────


def test_the_cli_teaches_lists_tests_and_forgets(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    db = ["--db", str(tmp_path / "c.db")]
    assert cli.main([*db, "hearing", "test", "get", "me", "a", "stock", "coat"]) == 0
    out = capsys.readouterr().out
    assert "get me a stock quote" in out and "because:" in out
    assert cli.main([*db, "hearing", "teach", "jason", "json"]) == 0
    assert cli.main([*db, "hearing", "list"]) == 0
    out = capsys.readouterr().out
    assert "json  (taught)" in out and "heard as: jason" in out
    assert cli.main([*db, "hearing", "forget", "json"]) == 0
    assert cli.main([*db, "hearing", "teach", "only-one"]) == 2


def test_hearing_is_spine_and_imports_on_a_bare_interpreter() -> None:
    src = Path(__file__).parent.parent
    r = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            f"import sys; sys.path.insert(0, {str(src)!r}); import jarvis.hearing; print('ok')",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONNOUSERSITE": "1", "PYTHONPATH": ""},
    )
    assert r.returncode == 0, r.stderr
