"""The numbering and the answer shapes, pinned against the RECORDED spike facts.

Spike S1 ran these four payloads against the live CLI v2.1.273 and wrote down
what came back. These tests assert the same bytes without a network call, which
is the only way the shapes can be checked on every commit:

    single-select  -> {"How should todos be stored?": "SQLite"}
    multiSelect    -> {"Which features...?": ["Due dates", "Priorities"]}
    none of these  -> {"Which database...?": "Postgres, actually"}
    'response'     -> never, ever, alongside 'answers'

Everything else in this file is a failure path, because the happy path is one
line and the ways to be silently wrong are many.
"""

from __future__ import annotations

import pytest

from jarvis.cc import narrate
from jarvis.requests import _check_presentation, label_for_index

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
MULTI = {
    "questions": [
        {
            "header": "Features",
            "question": "Which features should be included in the tiny todo CLI?",
            "options": [
                {"label": "Due dates"},
                {"label": "Tags"},
                {"label": "Priorities"},
                {"label": "Recurring"},
            ],
            "multiSelect": True,
        }
    ]
}
BATCH = {
    "questions": [
        SINGLE["questions"][0],
        MULTI["questions"][0],
    ]
}

Q1 = "How should todos be stored?"
Q2 = "Which features should be included in the tiny todo CLI?"


# ───────────────────────────── the numbering is ours ─────────────────────────────


def test_the_script_numbers_options_in_payload_order_and_never_rewrites_a_label() -> None:
    lines = narrate.script(SINGLE)
    options = [ln for ln in lines if ln.kind == "option"]
    assert [(ln.index, ln.label) for ln in options] == [
        (1, "SQLite"),
        (2, "JSON file"),
        (3, "Plain text"),
    ]
    # The ordinal is immediately adjacent to the label, with nothing between
    # them: tools/fidelity_probe.py scores exactly that adjacency.
    assert [ln.text for ln in options] == ["1. SQLite", "2. JSON file", "3. Plain text"]
    assert all(ln.fidelity == "exact" for ln in options)


def test_option_labels_survive_narration_byte_for_byte_including_turkish() -> None:
    payload = {
        "questions": [
            {
                "question": "Hangi veritabanı?",
                "options": [{"label": "SQLite"}, {"label": "Düğüm — üç"}],
            }
        ]
    }
    labels = [ln.label for ln in narrate.script(payload) if ln.kind == "option"]
    assert labels == ["SQLite", "Düğüm — üç"]
    # Normalisation exists for COMPARISON only. If it ever leaked into the
    # spoken or stored label, this is where it would show.
    assert narrate.presentation(payload)["items"][1]["label"] == "Düğüm — üç"


def test_a_batch_is_numbered_once_across_every_question() -> None:
    sl = narrate.slots(BATCH)
    assert [s.index for s in sl] == [1, 2, 3, 4, 5, 6, 7]
    assert [s.question_index for s in sl] == [1, 1, 1, 2, 2, 2, 2]
    assert sl[3].label == "Due dates"
    # One frozen array, one numbering: the Presentation the phone renders and the
    # script the desk speaks must agree on what "four" means.
    pres = narrate.presentation(BATCH)
    assert label_for_index(pres, 4) == "Due dates"


def test_the_presentation_is_the_spines_own_and_passes_its_own_validator() -> None:
    pres = narrate.presentation(SINGLE)
    _check_presentation(pres)  # raises if the shape is not what requests.py accepts
    assert pres["verbatim"] is True
    assert pres["multi"] is False
    assert pres["dtmf_map"] == {"1": 1, "2": 2, "3": 3}
    assert pres["question"] == Q1


def test_a_batch_too_big_for_the_keypad_gets_no_dtmf_map_at_all() -> None:
    big = {
        "questions": [
            {"question": f"q{i}", "options": [{"label": f"l{i}{j}"} for j in range(4)]}
            for i in range(3)
        ]
    }
    # A PARTIAL map would let a phone answer reach options 1-9 and silently not
    # 10-12, which looks like the keypad being broken rather than a limit.
    assert narrate.presentation(big)["dtmf_map"] is None


# ───────────────────────────── the recorded answer shapes ─────────────────────────────


def test_single_select_is_one_label_string() -> None:
    assert narrate.answers_from_indices(SINGLE, [1]) == {Q1: "SQLite"}


def test_multiselect_is_a_list_of_labels_in_the_order_they_were_said() -> None:
    assert narrate.answers_from_indices(MULTI, [3, 1]) == {Q2: ["Priorities", "Due dates"]}


def test_none_of_these_is_the_users_own_words_and_never_the_word_other() -> None:
    answers = narrate.answers_from_indices(SINGLE, [], "Postgres, actually")
    assert answers == {Q1: "Postgres, actually"}
    assert "Other" not in str(answers)


def test_getting_multi_and_single_backwards_is_refused_in_both_directions() -> None:
    # Silent if it ever got through: the CLI accepts the JSON either way and the
    # damage only appears as Claude building the wrong thing.
    with pytest.raises(narrate.AnswerShapeError):
        narrate.validate_answers(SINGLE, {Q1: ["SQLite"]})
    with pytest.raises(narrate.AnswerShapeError):
        narrate.validate_answers(MULTI, {Q2: "Due dates"})


def test_a_single_select_question_cannot_take_two_picks() -> None:
    with pytest.raises(narrate.AnswerShapeError, match="single-select"):
        narrate.answers_from_indices(SINGLE, [1, 2])


# ───────────────────────────── the ways to be wrong ─────────────────────────────


def test_an_out_of_range_index_raises_and_never_falls_back_to_the_first_option() -> None:
    with pytest.raises(narrate.AnswerShapeError, match="does not exist"):
        narrate.answers_from_indices(SINGLE, [4])
    with pytest.raises(narrate.AnswerShapeError):
        narrate.answers_from_indices(SINGLE, [0])
    with pytest.raises(narrate.AnswerShapeError):
        narrate.answers_from_indices(SINGLE, [-1])


def test_a_bool_is_not_an_index_even_though_python_says_it_is_an_int() -> None:
    # True == 1 in Python, so a sloppy parse could pick option one by accident.
    with pytest.raises(narrate.AnswerShapeError, match="must be an int"):
        narrate.answers_from_indices(SINGLE, [True])


def test_an_index_from_the_wrong_question_is_refused_rather_than_reassigned() -> None:
    with pytest.raises(narrate.AnswerShapeError, match="numbered across the whole batch"):
        # Option 1 belongs to the storage question; offering it as an answer to
        # the features question is a caller mixing per-question numbering with
        # ours, and quietly honouring it would answer the wrong question.
        narrate.answers_from_indices(BATCH, {Q2: [1], Q1: [1]})


def test_picking_the_same_option_twice_is_refused() -> None:
    with pytest.raises(narrate.AnswerShapeError, match="picked twice"):
        narrate.answers_from_indices(MULTI, [1, 1])


def test_free_text_that_is_really_a_label_must_be_picked_by_number() -> None:
    # A genuine "none of these" never looks like an option. Accepting it would
    # put a label into the answer by a path that skips the index lookup.
    with pytest.raises(narrate.AnswerShapeError, match="pick it by number"):
        narrate.answers_from_indices(SINGLE, [], "sqlite")


def test_a_partially_answered_batch_is_refused_rather_than_half_sent() -> None:
    with pytest.raises(narrate.AnswerShapeError, match="no answer for"):
        narrate.answers_from_indices(BATCH, [1])


def test_an_invented_or_translated_label_cannot_pass_validation() -> None:
    for invented in ("Sqlite", "SQLITE", "Veritabanı", "SQLite "):
        with pytest.raises(narrate.AnswerShapeError):
            narrate.validate_answers(MULTI, {Q2: [invented]})


def test_an_answer_to_a_question_nobody_asked_is_refused() -> None:
    with pytest.raises(narrate.AnswerShapeError, match="nobody asked"):
        narrate.validate_answers(SINGLE, {"Which database?": "SQLite"})


def test_an_answer_over_the_clis_own_limit_is_refused_here_not_there() -> None:
    with pytest.raises(narrate.AnswerShapeError, match="8192"):
        narrate.validate_answers(SINGLE, {Q1: "x" * (narrate.MAX_ANSWER_CHARS + 1)})


def test_two_identical_questions_in_one_batch_are_refused() -> None:
    # answers is keyed by the question STRING: the second answer would overwrite
    # the first and one of the user's decisions would silently vanish.
    duplicated = {"questions": [SINGLE["questions"][0], SINGLE["questions"][0]]}
    with pytest.raises(narrate.MalformedQuestions, match="repeats"):
        narrate.questions_of(duplicated)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"questions": []},
        {"questions": [{"question": "", "options": [{"label": "a"}]}]},
        {"questions": [{"question": "q", "options": []}]},
        {"questions": [{"question": "q", "options": [{"description": "no label"}]}]},
        {"questions": [{"question": "q", "options": [{"label": "  "}]}]},
        {"questions": "not an array"},
    ],
)
def test_a_payload_that_cannot_be_narrated_is_named_as_such(payload: dict) -> None:
    with pytest.raises(narrate.MalformedQuestions):
        narrate.questions_of(payload)


# ───────────────────────────── the spine's Answer ─────────────────────────────


def test_answer_records_whether_each_value_came_from_an_option_or_the_user() -> None:
    picked = narrate.answer(SINGLE, [2])
    assert picked == {"answers": {Q1: "JSON file"}, "sources": {Q1: "option"}}
    spoken = narrate.answer(SINGLE, [], "Postgres, actually")
    assert spoken["sources"] == {Q1: "free_text"}


def test_the_answer_is_accepted_by_the_spines_own_validator() -> None:
    from jarvis.requests import _check_answer

    _check_answer(narrate.answer(MULTI, [1, 2]))
    _check_answer(narrate.answer(SINGLE, [], "Postgres, actually"))


def test_short_label_is_short_enough_to_say_out_loud() -> None:
    assert narrate.short_label(SINGLE) == "storage"
    assert narrate.short_label(BATCH) == "2 questions"
    headerless = {
        "questions": [{"question": "Where should this live?", "options": [{"label": "a"}]}]
    }
    assert narrate.short_label(headerless) == "where should this"


def test_free_text_for_a_question_nobody_asked_is_refused_not_dropped() -> None:
    """The user SAID something. Silently sending the picks instead is the worst case.

    picks already refuse an unknown question key; free text used not to, so a
    channel that mistyped (or re-worded) the question string sent Claude a
    perfectly well-formed answer built entirely from the options, and the words
    the user actually spoke reached nobody.
    """
    with pytest.raises(narrate.AnswerShapeError, match="nobody asked"):
        narrate.answers_from_indices(SINGLE, [1], free_text={"Some other question?": "Postgres"})

    # And the same mistake with no picks at all still reads as the missing answer
    # it is, rather than as an empty batch.
    with pytest.raises(narrate.AnswerShapeError):
        narrate.answers_from_indices(SINGLE, [], free_text={"Some other question?": "Postgres"})
