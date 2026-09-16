"""The prompt tidier's mechanical fidelity guarantees.

Everything here runs with no credentials and no network: ``model_call`` is a
protocol, so the model is a one-line fake and every guarantee is a pure function
over strings. That seam is the whole design — a fidelity check you cannot run in
CI is a fidelity check nobody runs.

The bias of this file is failure paths. A tidier that works on a clean
transcript is not interesting; what matters is what happens when the model
invents a requirement, drops "no auth", returns fenced JSON, returns an empty
quote (which is a substring of *everything*), or when two channels edit the same
numbered list at once.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest

from jarvis import spec as S

# ───────────────────────────── fixtures ─────────────────────────────

TRANSCRIPT = (
    "Hey Jarvis, let's build a thing that watches my YouTube comments and pings me "
    "on Telegram. Use Postgres, no auth, and it should listen on port 8080. "
    "Put it in ~/projects/yt-watch and use Python 3.11. "
    "Use Opus 5 with high effort."
)

TURKISH = (
    "İstanbul trafiği için bir uygulama yapalım. Veritabanı Postgres olsun, "
    "auth yok, 8080 portunda çalışsın."
)


def fake_model(payload: Any, *, seen: list[str] | None = None) -> S.ModelCall:
    """A ``model_call`` that returns ``payload`` and records the prompt it saw."""

    def call(prompt: str) -> Any:
        if seen is not None:
            seen.append(prompt)
        if isinstance(payload, Exception):
            raise payload
        return payload

    return call


def reqs(*pairs: tuple[str, str]) -> str:
    return json.dumps(
        {
            "repo_name": "yt-watch",
            "requirements": [{"text": t, "quote": q} for t, q in pairs],
        }
    )


def tidied(*pairs: tuple[str, str], transcript: str = TRANSCRIPT) -> S.Spec:
    return S.tidy(transcript, fake_model(reqs(*pairs)))


# ───────────────────────── 1. normalisation, and Turkish ─────────────────────


def test_the_turkish_casefold_trap_is_real():
    """Documents WHY normalise() cannot just call casefold().

    If this ever starts passing as a naive equality, the fold below is dead
    weight and someone should delete it knowingly rather than by accident.
    """
    assert "I".casefold() == "i"
    assert "İ".casefold() == "i\u0307"  # two characters, not one
    assert "İ".casefold() != "i"
    assert "ı".casefold() == "ı"  # dotless I does not fold to i at all


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("İSTANBUL", "istanbul"),
        ("İstanbul", "istanbul"),
        ("IŞIK", "ışık"),
        ("ILIK", "ılık"),
        ("I\u0307stanbul", "istanbul"),  # decomposed dotted I from a sloppy ASR
        ("ﬁle", "file"),  # NFKC ligature
        ("ｐｏｒｔ", "port"),  # NFKC fullwidth
        ("port   8080", "port 8080"),
        ("port\n\t8080", "port 8080"),
        ("  padded  ", "padded"),
    ],
)
def test_normalise_folds_equal_things_together(a, b):
    assert S.normalise(a) == S.normalise(b)


def test_normalise_does_not_fold_everything_together():
    assert S.normalise("Postgres") != S.normalise("MySQL")
    assert S.normalise("8080") != S.normalise("8000")


@pytest.mark.parametrize("quote", ["", "   ", "\n\t", "\u00a0"])
def test_empty_quote_is_never_contained(quote):
    """The single easiest way to turn the whole guarantee into a no-op.

    "" is a substring of every string, so an empty span must be False or an
    invented requirement passes containment trivially.
    """
    assert S.contains_span(TRANSCRIPT, quote) is False


@pytest.mark.parametrize(
    "quote",
    [
        "Use Postgres",
        "USE POSTGRES",
        "use   postgres",
        "no auth",
        "~/projects/yt-watch",
    ],
)
def test_contains_span_accepts_real_spans_however_cased(quote):
    assert S.contains_span(TRANSCRIPT, quote) is True


@pytest.mark.parametrize(
    "quote",
    [
        "use MySQL",
        "watches my Twitter comments",
        "Postgres watches",  # real words, wrong order: not a contiguous span
        "port 9090",
    ],
)
def test_contains_span_rejects_anything_not_contiguous(quote):
    assert S.contains_span(TRANSCRIPT, quote) is False


def test_turkish_span_survives_the_case_fold():
    assert S.contains_span(TURKISH, "VERİTABANI POSTGRES OLSUN")
    assert S.contains_span(TURKISH, "İSTANBUL TRAFİĞİ")


# ───────────────────── 2. source-span containment in tidy ────────────────────


def test_invented_requirement_is_hard_rejected():
    spec = tidied(
        ("Use Postgres", "Use Postgres"),
        ("Add rate limiting", "sensible rate limits"),
    )
    assert [r.text for r in spec.requirements] == ["Use Postgres"]
    assert [r.text for r in spec.rejected] == ["Add rate limiting"]
    assert spec.rejected[0].reason == "span not in the transcript"


@pytest.mark.parametrize(
    ("entry", "reason"),
    [
        ({"text": "Use Postgres", "quote": ""}, "no source span"),
        ({"text": "Use Postgres", "quote": "   "}, "no source span"),
        ({"text": "Use Postgres"}, "no source span"),
        ({"text": "Use Postgres", "quote": None}, "no source span"),
        ({"text": "", "quote": "Use Postgres"}, "empty text"),
        ({"quote": "Use Postgres"}, "empty text"),
        (
            {"text": f"{S.APPENDIX_HEADER} lol", "quote": "Use Postgres"},
            "impersonates a prompt header",
        ),
        ("just a string", "not an object"),
    ],
)
def test_malformed_requirements_are_rejected_with_a_named_reason(entry, reason):
    payload = json.dumps({"requirements": [entry]})
    spec = S.tidy(TRANSCRIPT, fake_model(payload))
    assert spec.requirements == ()
    assert spec.rejected[0].reason == reason


def test_every_requirement_rejected_is_not_an_exception_but_is_shouted_about():
    """A tidier that drops everything must not look like a tidier that found
    nothing to say. The list is empty and the coverage sentence says why."""
    spec = tidied(("Invented one", "nope"), ("Invented two", "also nope"))
    assert spec.requirements == ()
    sentence = S.coverage_sentence(S.audit(TRANSCRIPT, spec))
    assert sentence is not None
    assert "Invented one" in sentence and "Invented two" in sentence


def test_model_supplied_ids_are_ignored():
    """Ids are ours. A model id can repeat, collide or be a string, and every
    one of those silently breaks "drop three"."""
    payload = json.dumps(
        {
            "requirements": [
                {"id": 99, "text": "Use Postgres", "quote": "Use Postgres"},
                {"id": 99, "text": "No auth", "quote": "no auth"},
            ]
        }
    )
    spec = S.tidy(TRANSCRIPT, fake_model(payload))
    assert [r.id for r in spec.requirements] == [1, 2]


@pytest.mark.parametrize(
    "payload",
    [
        "not json at all",
        "",
        "[]",  # a top-level array
        '"a string"',
        json.dumps({"repo_name": "x"}),  # no requirements key
        json.dumps({"requirements": "Use Postgres"}),  # not an array
    ],
)
def test_unusable_model_replies_raise_tidy_failed(payload):
    with pytest.raises(S.TidyFailed):
        S.tidy(TRANSCRIPT, fake_model(payload))


@pytest.mark.parametrize(
    "payload",
    [
        "```json\n" + reqs(("Use Postgres", "Use Postgres")) + "\n```",
        "```\n" + reqs(("Use Postgres", "Use Postgres")) + "\n```",
        reqs(("Use Postgres", "Use Postgres")).encode(),
        json.loads(reqs(("Use Postgres", "Use Postgres"))),
    ],
)
def test_every_shape_a_real_client_returns_is_accepted(payload):
    spec = S.tidy(TRANSCRIPT, fake_model(payload))
    assert [r.text for r in spec.requirements] == ["Use Postgres"]


@pytest.mark.parametrize("transcript", ["", "   ", "\n"])
def test_empty_transcript_is_refused(transcript):
    with pytest.raises(S.TidyFailed):
        S.tidy(transcript, fake_model(reqs()))


@pytest.mark.parametrize("header", [S.CONFIRMED_HEADER, S.APPENDIX_HEADER])
def test_retidy_is_refused(header):
    with pytest.raises(S.ReTidyRefused):
        S.tidy(f"blah\n{header}\n1. Use Postgres", fake_model(reqs()))


def test_an_assembled_prompt_cannot_be_fed_back_in():
    """The exact wrong move: tidy the thing you already tidied. Every span in
    the second pass would resolve against the first pass's prose."""
    spec = tidied(("Use Postgres", "Use Postgres"))
    assembled = S.assemble_prompt(spec, TRANSCRIPT)
    with pytest.raises(S.ReTidyRefused):
        S.tidy(assembled, fake_model(reqs()))


def test_the_model_sees_the_raw_transcript():
    seen: list[str] = []
    S.tidy(TRANSCRIPT, fake_model(reqs(), seen=seen))
    assert len(seen) == 1
    assert TRANSCRIPT in seen[0]
    assert seen[0] == S.tidy_prompt(TRANSCRIPT)


def test_model_call_errors_are_not_swallowed():
    with pytest.raises(TimeoutError):
        S.tidy(TRANSCRIPT, fake_model(TimeoutError("gemini is down")))


# ───────────────────────── 3. literal-token assertion ────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("it should listen on port 8080", ("8080", "number")),
        ("use Python 3.11", ("3.11", "version")),
        ("use Postgres for storage", ("Postgres", "tech")),
        ("no auth on the endpoint", ("no auth", "negation")),
        ("without Docker please", ("without Docker", "negation")),
        ("auth yok olsun", ("auth yok", "negation")),
        ("put it in ~/projects/yt-watch", ("~/projects/yt-watch", "path")),
        ("read /etc/hosts first", ("/etc/hosts", "path")),
        ("fetch https://example.com/a?b=1", ("https://example.com/a?b=1", "url")),
        ('the path is "/hooks/yt" exactly', ("/hooks/yt", "quoted")),
        ("call it parseTranscript in code", ("parseTranscript", "identifier")),
        ("name it my_module please", ("my_module", "identifier")),
        ("it watches my YouTube comments", ("YouTube", "identifier")),
        ("edit the file app.py directly", ("app.py", "identifier")),
        ("expose a CSV export somewhere", ("CSV", "acronym")),
    ],
)
def test_literal_tokens_catch_the_forms_that_must_not_drift(text, expected):
    found = {(t.text, t.kind) for t in S.literal_tokens(text)}
    assert expected in found


def test_control_phrases_are_masked_out_of_the_literal_check():
    """ "Use Opus 5 with high effort" is a session option, not a requirement.

    Without masking, the "5" of "Opus 5" is a missing literal on every single
    build — a permanent false alarm that trains the user to ignore the real ones.
    """
    tokens = [t.text for t in S.literal_tokens("Use Opus 5 with high effort and port 8080")]
    assert "8080" in tokens
    assert "5" not in tokens


def test_apostrophes_do_not_open_a_quoted_span():
    """Turkish attaches suffixes with an apostrophe and English contracts with
    one; a single-quote rule swallows the rest of the sentence."""
    text = "Postgres'i kullan, don't use MySQL"
    quoted = [t.text for t in S.literal_tokens(text) if t.kind == "quoted"]
    assert quoted == []


@pytest.mark.parametrize(
    ("drifted", "flagged"),
    [
        ("Use MySQL for storage", "Postgres"),
        ("Listen on port 8000", "8080"),
        ("Use Python 3.12", "3.11"),
        ("Store it somewhere sensible", "~/projects/yt-watch"),
        ("Watch Youtube comments", "YouTube"),  # capitalisation drift
    ],
)
def test_missing_literals_catches_the_highest_damage_drift(drifted, flagged):
    missing = [t.text for t in S.missing_literals(TRANSCRIPT, [drifted])]
    assert flagged in missing


def test_dropping_a_negation_is_caught_even_when_the_noun_survives():
    """The worst single failure in this system: "no Docker" tidied to "Docker".

    Containment passes (the span exists), the noun is present, and the build
    ships the exact opposite of what was asked for. Only the negation phrase
    catches it.
    """
    transcript = "Build the scraper, no Docker, and ship it."
    missing = [t.text for t in S.missing_literals(transcript, ["Use Docker"])]
    assert "no Docker" in missing


def test_negation_case_may_change_because_a_bullet_starts_with_a_capital():
    transcript = "Build the scraper, no auth, and ship it."
    assert S.missing_literals(transcript, ["No auth on any endpoint"]) == []


def test_identifier_case_may_not_change_because_case_is_its_identity():
    transcript = "call it parseTranscript in the code"
    missing = [t.text for t in S.missing_literals(transcript, ["call it parsetranscript"])]
    assert "parseTranscript" in missing


def test_a_faithful_list_flags_nothing():
    faithful = [
        "Watch my YouTube comments",
        "Ping me on Telegram",
        "Use Postgres",
        "no auth",
        "Listen on port 8080",
        "Put it in ~/projects/yt-watch",
        "Use Python 3.11",
    ]
    assert S.missing_literals(TRANSCRIPT, faithful) == []


def test_literal_tokens_are_reported_in_spoken_order_without_duplicates():
    text = "use Postgres, and again Postgres, then port 8080"
    tokens = [t.text for t in S.literal_tokens(text)]
    assert tokens == ["Postgres", "8080"]


# ─────────────────── 4. edits are local list mutations ───────────────────────


def three() -> S.Spec:
    return tidied(
        ("Watch YouTube comments", "watches my YouTube comments"),
        ("Ping me on Telegram", "pings me on Telegram"),
        ("Use Postgres", "Use Postgres"),
    )


def test_drop_removes_by_position_and_leaves_ids_alone():
    spec = S.drop(three(), 2)
    assert [r.text for r in spec.requirements] == ["Watch YouTube comments", "Use Postgres"]
    assert [r.id for r in spec.requirements] == [1, 3]
    # The read-back renumbers; the ids in the activity log do not.
    assert S.readback_items(spec) == ((1, "Watch YouTube comments"), (2, "Use Postgres"))


@pytest.mark.parametrize("n", [0, -1, 4, 99])
def test_a_position_that_is_not_in_the_list_is_an_error_not_a_guess(n):
    with pytest.raises(S.EditIndexError):
        S.drop(three(), n)


def test_a_bool_is_not_a_position():
    """True == 1 in Python, so "drop True" would silently drop item one."""
    with pytest.raises(S.EditIndexError):
        S.drop(three(), True)


def test_restate_keeps_the_id_and_records_that_the_user_wrote_it():
    spec = S.restate(three(), 3, "Use Postgres, not MySQL")
    assert spec.requirements[2].text == "Use Postgres, not MySQL"
    assert spec.requirements[2].id == 3
    assert spec.requirements[2].origin == "user_edited"
    assert spec.requirements[2].quote == "Use Postgres"  # provenance survives


def test_added_requirements_carry_no_span_because_the_user_is_the_source():
    spec = S.add(three(), "no Docker")
    assert spec.requirements[-1].text == "no Docker"
    assert spec.requirements[-1].quote is None
    assert spec.requirements[-1].origin == "user_added"


def test_ids_are_never_reused_after_a_drop():
    """Otherwise two different requirements share an id in the activity log and
    "which one did I drop" becomes unanswerable after the fact."""
    spec = S.add(S.drop(three(), 3), "no Docker")
    assert [r.id for r in spec.requirements] == [1, 2, 4]


def test_add_can_insert_at_a_position():
    spec = S.add(three(), "no Docker", at=1)
    assert spec.requirements[0].text == "no Docker"
    assert len(spec.requirements) == 4


@pytest.mark.parametrize("text", ["", "   ", "\n"])
def test_an_empty_edit_is_refused(text):
    with pytest.raises(ValueError):
        S.add(three(), text)
    with pytest.raises(ValueError):
        S.restate(three(), 1, text)


@pytest.mark.parametrize("header", [S.CONFIRMED_HEADER, S.APPENDIX_HEADER])
def test_a_requirement_may_not_impersonate_a_prompt_header(header):
    with pytest.raises(ValueError):
        S.add(three(), f"and then {header}")


def test_the_spec_is_frozen_so_an_in_place_edit_is_impossible():
    spec = three()
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.requirements = ()  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.requirements[0].text = "something else"  # type: ignore[misc]


def test_two_channels_editing_the_same_read_back_is_a_detected_race():
    """The real race: the numbered list is spoken at the desk AND shown on
    Telegram. Both users are looking at revision 0. The second "drop three"
    refers to an item that is now at position two — or gone.
    """
    spoken = three()
    desk = S.drop(spoken, 1, expect_revision=spoken.revision)
    assert desk.revision == spoken.revision + 1
    with pytest.raises(S.StaleSpec):
        S.drop(desk, 3, expect_revision=spoken.revision)


def test_the_revision_check_is_opt_in_so_a_single_channel_stays_simple():
    spec = S.drop(three(), 1)
    assert S.drop(spec, 1).revision == 2


@pytest.mark.parametrize(
    ("said", "expected"),
    [
        ("drop three", S.Edit(kind="drop", n=3)),
        ("Drop 3.", S.Edit(kind="drop", n=3)),
        ("delete number two", S.Edit(kind="drop", n=2)),
        ("remove first", S.Edit(kind="drop", n=1)),
        ("üçüncüyü sil", S.Edit(kind="drop", n=3)),
        ("ikinciyi çıkar", S.Edit(kind="drop", n=2)),
        (
            "two should say Postgres not MySQL",
            S.Edit(kind="restate", n=2, text="Postgres not MySQL"),
        ),
        ("change 4 to no Docker", S.Edit(kind="restate", n=4, text="no Docker")),
        ("add: no Docker", S.Edit(kind="add", text="no Docker")),
        ("add no Docker", S.Edit(kind="add", text="no Docker")),
        ("ekle: Docker olmasın", S.Edit(kind="add", text="Docker olmasın")),
    ],
)
def test_spoken_edits_are_parsed_locally_with_no_model(said, expected):
    assert S.parse_edit(said) == expected


@pytest.mark.parametrize(
    "said",
    [
        "",
        "   ",
        "hmm",
        "actually can you redo the whole thing",
        "make it better",
        "drop the ball",  # "ball" is not an ordinal
        "yes that's right",
    ],
)
def test_an_unparsed_phrase_returns_none_and_never_re_tidies(said):
    """None means "ask again". There is deliberately no fall-through to a fresh
    tidy: a mumble must not trigger a silent full-list rewrite."""
    assert S.parse_edit(said) is None


def test_apply_edit_round_trips_the_three_spoken_forms():
    spec = three()
    spec = S.apply_edit(spec, S.parse_edit("drop three"))
    spec = S.apply_edit(spec, S.parse_edit("two should say Ping me on Telegram only"))
    spec = S.apply_edit(spec, S.parse_edit("add: no Docker"))
    assert [r.text for r in spec.requirements] == [
        "Watch YouTube comments",
        "Ping me on Telegram only",
        "no Docker",
    ]
    assert spec.revision == 3


def test_there_is_no_re_tidy_entry_point():
    """Guards the API shape itself: the only way to change the list is a local
    mutation. If a re-tidy helper is ever added it must be a deliberate act,
    not something that appears because it was convenient."""
    exported = set(S.__all__)
    assert {"drop", "restate", "add", "apply_edit"} <= exported
    assert not {n for n in exported if "retidy" in n.lower().replace("_", "")} - {"ReTidyRefused"}


# ───────────────────────── 5. assemble_prompt ────────────────────────────────


def test_every_confirmed_requirement_appears_verbatim_in_the_prompt():
    spec = S.add(S.restate(three(), 1, "Watch YouTube comments — all of them"), "no Docker")
    out = S.assemble_prompt(spec, TRANSCRIPT)
    for req in spec.requirements:
        assert req.text in out


def test_the_raw_transcript_is_appended_unedited():
    """Net 4. Even total prose drift in the bullets cannot lose a constraint,
    because Claude still receives the user's own words."""
    out = S.assemble_prompt(three(), TRANSCRIPT)
    assert TRANSCRIPT in out


def test_the_sections_appear_in_the_documented_order():
    out = S.assemble_prompt(three(), TRANSCRIPT)
    assert out.startswith(S.PREAMBLE)
    assert out.index(S.PREAMBLE) < out.index(S.CONFIRMED_HEADER) < out.index(S.APPENDIX_HEADER)
    assert out.index(S.APPENDIX_HEADER) < out.index(TRANSCRIPT)


def test_the_bullets_are_numbered_by_position_not_by_id():
    spec = S.drop(three(), 1)
    out = S.assemble_prompt(spec, TRANSCRIPT)
    assert "1. Ping me on Telegram" in out
    assert "2. Use Postgres" in out


def test_an_empty_list_still_sends_the_users_words():
    spec = tidied()
    out = S.assemble_prompt(spec, TRANSCRIPT)
    assert TRANSCRIPT in out
    assert "(none confirmed" in out


def test_a_transcript_with_odd_whitespace_is_not_reflowed():
    weird = "build a thing\n\n  with two   spaces\nand a newline"
    out = S.assemble_prompt(tidied(transcript=weird), weird)
    assert weird in out


# ───────────────────────── 6. the coverage sentence ──────────────────────────


def test_nothing_flagged_says_nothing_at_all():
    assert S.coverage_sentence(S.Coverage()) is None
    assert S.Coverage().clean is True


FLAG_MARKERS = {
    "invented": "INVENTED-MARKER",
    "literals_missing": "LITERAL-MARKER",
    "uncovered": "UNCOVERED-MARKER",
}


@pytest.mark.parametrize("field", [f.name for f in dataclasses.fields(S.Coverage)])
def test_every_flag_field_is_actually_spoken(field):
    """The guard against silent under-reporting.

    This iterates the dataclass fields rather than a hand-written list, so a
    future change that adds a flag — or stops rendering an existing one — fails
    here. A flag the user never hears is worse than no flag: it makes the
    read-back look complete when it is not.
    """
    marker = FLAG_MARKERS.get(field)
    assert marker is not None, f"Coverage.{field} has no marker: is it rendered aloud?"
    cov = S.Coverage(**{field: (marker,)})
    sentence = S.coverage_sentence(cov)
    assert sentence is not None
    assert marker in sentence


def test_the_sentence_opens_with_the_words_jarvis_promised():
    cov = S.Coverage(literals_missing=("8080",))
    assert S.coverage_sentence(cov).startswith("I may have missed")


def test_all_three_nets_are_reported_in_one_sentence():
    cov = S.Coverage(
        invented=("Add rate limiting",),
        literals_missing=("no auth", "8080"),
        uncovered=("and email me a weekly digest",),
    )
    sentence = S.coverage_sentence(cov)
    for item in ("Add rate limiting", "no auth", "8080", "and email me a weekly digest"):
        assert item in sentence


def test_audit_reports_the_invented_the_missing_and_the_uncovered_together():
    spec = tidied(
        ("Watch YouTube comments", "watches my YouTube comments"),
        ("Add rate limiting", "sensible rate limits"),
    )
    cov = S.audit(TRANSCRIPT, spec)
    assert cov.invented == ("Add rate limiting",)
    assert "Postgres" in cov.literals_missing
    assert "no auth" in cov.literals_missing
    assert "8080" in cov.literals_missing
    assert any("Postgres" in s for s in cov.uncovered)
    assert cov.clean is False


def test_a_sentence_covered_by_a_span_is_not_flagged_as_uncovered():
    spec = tidied(
        (
            "Watch YouTube comments and ping Telegram",
            "watches my YouTube comments and pings me on Telegram",
        ),
    )
    assert not any("YouTube" in s for s in S.uncovered_sentences(TRANSCRIPT, spec))


def test_filler_only_sentences_are_not_flagged():
    transcript = "Um, okay, so. Build a scraper that writes to Postgres."
    spec = tidied(
        ("Build a scraper that writes to Postgres", "Build a scraper that writes to Postgres"),
        transcript=transcript,
    )
    assert S.uncovered_sentences(transcript, spec) == []


# ───────────────────────── 7. the spoken control phrases ─────────────────────


@pytest.mark.parametrize(
    ("said", "alias", "phrase"),
    [
        ("use Opus 5 with high effort", "opus", "Opus 5"),
        ("run it on Sonnet", "sonnet", "Sonnet"),
        ("use haiku", "haiku", "Haiku"),
        ("use Opus 4.1 for this", "opus", "Opus 4.1"),
        ("no, use Sonnet instead of Opus 5... actually Opus", "opus", "Opus"),
        ("build me a todo app", None, None),
    ],
)
def test_parse_model(said, alias, phrase):
    ask = S.parse_model(said)
    assert (ask.alias, ask.phrase) == (alias, phrase)


def test_a_spoken_model_version_is_never_turned_into_an_invented_model_id():
    """ "Opus 4.1" must not become "claude-opus-4-1". A model id we have not
    verified starts the wrong model, or none, and the failure surfaces minutes
    later as a dead job."""
    ask = S.parse_model("use Opus 4.1")
    assert ask.alias == "opus"
    assert "4.1" in ask.phrase


@pytest.mark.parametrize(
    ("said", "level"),
    [
        ("with low effort", "low"),
        ("medium effort please", "medium"),
        ("with high effort", "high"),
        ("with extra-high effort", "xhigh"),
        ("with extra high effort", "xhigh"),
        ("xhigh effort", "xhigh"),
        ("very high effort", "xhigh"),
        ("max effort", "max"),
        ("maximum effort", "max"),
        ("çok yüksek efor", "xhigh"),
        ("düşük efor kullan", "low"),
        ("this is a high priority app", None),  # "high" without "effort" is prose
        ("build me a todo app", None),
    ],
)
def test_parse_effort(said, level):
    assert S.parse_effort(said).level == level


def test_an_unknown_effort_word_is_admitted_rather_than_silently_defaulted():
    ask = S.parse_effort("with aggressive effort")
    assert ask.level is None
    assert ask.phrase == "with aggressive effort"
    spec = tidied(transcript="Build a thing with aggressive effort")
    note = S.effort_note(spec)
    assert "aggressive effort" in note
    assert "low, medium, extra-high and max" in note


def test_high_effort_is_honestly_reported_as_a_no_op():
    """ "high" is Claude Code's default, so "use Opus 5 with high effort" changes
    nothing. A demo where the user asks for more thinking and gets exactly the
    default is a demo that feels fake."""
    spec = tidied(transcript="Build a thing. Use Opus 5 with high effort.")
    assert spec.effort == "high"
    assert spec.effort_ask.changes_behaviour is False
    note = S.effort_note(spec)
    assert "Opus 5" in note
    assert "default" in note
    assert "max" in note


def test_the_levels_that_change_behaviour_are_named_correctly():
    assert S.EFFORT_THAT_IS_ALREADY_THE_DEFAULT == "high"
    assert {"low", "medium", "xhigh", "max"} == S.EFFORTS_THAT_CHANGE_BEHAVIOUR
    assert set(S.EFFORT_LEVELS) == S.EFFORTS_THAT_CHANGE_BEHAVIOUR | {"high"}


def test_a_level_that_does_something_says_so_without_the_default_caveat():
    spec = tidied(transcript="Build a thing. Use Opus 5 with max effort.")
    assert spec.effort_ask.changes_behaviour is True
    assert "default" not in S.effort_note(spec)


def test_no_effort_asked_says_nothing():
    assert S.effort_note(tidied(transcript="Build me a todo app")) is None


@pytest.mark.parametrize(
    ("said", "asked"),
    [
        ("run it in the cloud", "cloud"),
        ("do it on the cloud so nothing touches local disk", "cloud"),
        ("bulutta çalıştır", "cloud"),
        ("cloud mode please", "cloud"),
        ("just run it locally", "local"),
        ("do it on my machine", "local"),
        ("bu makinede çalıştır", "local"),
        ("run it in the cloud, or actually locally", "unclear"),
        ("build me a todo app", "unspecified"),
    ],
)
def test_parse_mode(said, asked):
    assert S.parse_mode(said).asked == asked


def test_cloud_is_cut_but_the_phrasing_is_recorded():
    """ADR 0009: cloud mode is cut from v1 because a deferred permission is a
    hard DENY for cloud sessions. The classifier still ships and still records
    what was asked, so the six months of phrasing data the ADR wants exists."""
    spec = tidied(transcript="Build a thing and run it in the cloud")
    assert spec.mode == "local"
    assert spec.mode_ask.asked == "cloud"
    assert spec.mode_ask.phrase == "in the cloud"
    note = S.mode_note(spec)
    assert "cloud" in note and "locally" in note


def test_ambiguous_phrasing_is_admitted_rather_than_guessed():
    spec = tidied(transcript="Run it in the cloud, or actually locally, whatever")
    assert spec.mode == "local"
    assert spec.mode_ask.asked == "unclear"
    assert "couldn't tell" in S.mode_note(spec)


def test_saying_nothing_about_where_it_runs_says_nothing_back():
    assert S.mode_note(tidied(transcript="Build me a todo app")) is None


# ───────────────────────── 8. the repo name ──────────────────────────────────


@pytest.mark.parametrize(
    ("proposed", "expected"),
    [
        ("yt-watch", "yt-watch"),
        ("YT Watch", "yt-watch"),
        ("İstanbul Trafiği", "istanbul-trafigi"),
        ("şık_ğüzel", "sik-guzel"),
        ("   ", "build-me-a-todo"),
        ("!!!", "build-me-a-todo"),
    ],
)
def test_repo_slug_transliterates_rather_than_stripping(proposed, expected):
    """NFKD-stripping gives "s" for "ş" but leaves "ı" untouched — it has no
    combining form — so a Turkish name would keep a non-ASCII byte in a path."""
    assert S.repo_slug(proposed, "build me a todo app") == expected


def test_repo_slug_has_a_last_resort():
    assert S.repo_slug("", "") == "new-project"
    assert S.repo_slug("!!!", "!!!") == "new-project"


# ──────────────── 9. regressions found in review (each one was live) ─────────


@pytest.mark.parametrize(
    "said",
    [
        # "bir" is a prefix of "birthday"; a bare prefix test made this item ONE.
        "drop birthday",
        "birthday should be optional",
        "change birthday to a reminder",
        # "on" is a prefix of "onboarding" and "online"; these were item TEN.
        "onboarding should say welcome the user",
        "remove online",
        # "beş"/"bes" is a prefix of "beside"; "üç"/"uc" of "uc-berkeley".
        "beside should be a sidebar",
        "drop uncached",
    ],
)
def test_an_english_word_that_merely_starts_like_a_turkish_numeral_is_not_a_position(said):
    """The Turkish numeral stems are matched by PREFIX, and an unguarded prefix
    test fires on ordinary English words.

    This is the exact failure EditIndexError exists to prevent, except worse,
    because it is silent: "birthday should be optional" is a user dictating a
    new requirement, and it used to rewrite the text of item one instead. A
    numeral is only believed when what follows it actually looks like a Turkish
    ordinal or case suffix.
    """
    assert S.parse_edit(said) is None


@pytest.mark.parametrize(
    ("said", "n"),
    [
        ("birinciyi sil", 1),
        ("ikinciyi çıkar", 2),
        ("üçüncüyü sil", 3),
        ("dördüncüyü sil", 4),
        ("beşinciyi kaldır", 5),
        ("altıncıyı sil", 6),
        ("yedinciyi sil", 7),
        ("sekizinciyi sil", 8),
        ("dokuzuncuyu sil", 9),
        ("onuncuyu sil", 10),
    ],
)
def test_every_turkish_ordinal_still_parses_after_the_suffix_guard(said, n):
    """The guard must not be paid for by breaking the language it exists for."""
    assert S.parse_edit(said) == S.Edit(kind="drop", n=n)


@pytest.mark.parametrize(
    "said",
    [
        "build me an app that writes a haiku every morning",
        "a magnum opus of a todo list",
        "the sonnet generator should rhyme",
    ],
)
def test_a_model_name_used_as_an_ordinary_word_does_not_select_a_model(said):
    """ "opus", "sonnet" and "haiku" are English words before they are models.

    Two things went wrong on a bare mention, and the second hid the first:
    Spec.model became "haiku", so the job would have run on the wrong model; and
    control_spans masked the word out of the transcript, so net 2 could never
    flag its disappearance from the list either.
    """
    assert S.parse_model(said).alias is None
    assert S.control_spans(said) == []


def test_a_bare_model_mention_still_counts_once_the_transcript_has_a_real_one():
    """The correction "no, use Sonnet ... actually Opus" ends on a bare mention.

    One explicitly triggered mention promotes the rest, so last-mention-wins
    survives without a bare word being a request on its own.
    """
    assert S.parse_model("no, use Sonnet instead of Opus 5... actually Opus").alias == "opus"
    assert S.parse_model("run it on Sonnet").alias == "sonnet"
    assert S.parse_model("use haiku").alias == "haiku"


def test_a_requirement_containing_the_word_effort_is_not_an_effort_level():
    """ "log the effort in hours" parsed as the effort level "the".

    Jarvis then read out "I heard 'the effort' but I don't know that effort
    level" on a build that never asked for one — and, worse, masked the phrase
    out of the literal check, so a dropped "hours" requirement went unflagged.
    """
    said = "Log the effort in hours for each task"
    assert S.parse_effort(said) == S.EffortAsk(level=None, phrase=None)
    assert S.control_spans(said) == []
    assert S.effort_note(tidied(transcript=said)) is None


def test_a_one_word_span_does_not_certify_a_whole_sentence_as_covered():
    """Net 3 is the weakest net, and this made it weaker than it looks.

    The model is free to quote a single word and still pass net 1, so a
    one-word span used to mark every other clause in the sentence as covered.
    The floor for believing a span is the same floor the net uses to decide a
    sentence is worth checking at all.
    """
    transcript = "Also I want Postgres, a nightly backup to S3, and absolutely no auth."
    spec = tidied(("Use Postgres", "Postgres"), transcript=transcript)
    assert S.uncovered_sentences(transcript, spec) == [transcript.rstrip(".")]
    # A span with real substance still covers it.
    covering = tidied(("Use Postgres", "I want Postgres, a nightly backup"), transcript=transcript)
    assert S.uncovered_sentences(transcript, covering) == []
