"""The deterministic answer matcher, in miniature.

These are the rules that make a wrong build structurally impossible rather than
prompt-hoped: a spoken answer becomes option INDICES, and indices become labels
by local lookup against the frozen options array. No model is consulted, so a
label cannot be reordered, translated, merged or invented on the way back.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "spikes" / "s0_crossproc"))
from answerer import parse_picks, shape_answer  # noqa: E402

SINGLE = {
    "question": "How should todos be stored?",
    "options": [
        {"label": "SQLite", "description": "a local database file"},
        {"label": "JSON file", "description": "a single JSON file"},
        {"label": "Plain text", "description": "one per line"},
    ],
    "multiSelect": False,
}
MULTI = {
    "question": "Which features?",
    "options": [
        {"label": "Due dates"}, {"label": "Tags"},
        {"label": "Priorities"}, {"label": "Recurring"},
    ],
    "multiSelect": True,
}


@pytest.mark.parametrize(
    ("said", "n", "expected"),
    [
        ("1", 3, [1]),
        ("one", 3, [1]),
        ("first", 3, [1]),
        ("one and three", 4, [1, 3]),
        ("1,3", 4, [1, 3]),
        ("1, 3", 4, [1, 3]),
        ("three and one", 4, [3, 1]),          # order preserved as spoken
        ("two.", 3, [2]),                       # trailing punctuation from ASR
        ("ONE", 3, [1]),                        # casing from ASR
        ("bir", 3, [1]),                        # Turkish ordinals, parsed locally
        ("üç", 3, [3]),
        ("uc", 3, [3]),                         # ASR drops the diacritic
        ("bir ve üç", 4, [1, 3]),
        ("1 and 1", 3, [1]),                    # duplicates collapse
        ("", 3, []),
        ("Postgres, actually", 3, []),          # free text yields no picks
        ("9", 3, []),                           # out of range is not a pick
        ("0", 3, []),                           # 1-based: 0 is not an option
    ],
)
def test_parse_picks(said: str, n: int, expected: list[int]) -> None:
    assert parse_picks(said, n) == expected


def test_single_select_returns_one_label_not_a_list() -> None:
    assert shape_answer(SINGLE, [1], None) == "SQLite"


def test_multiselect_returns_a_list_even_for_one_pick() -> None:
    assert shape_answer(MULTI, [2], None) == ["Tags"]


def test_multiselect_preserves_spoken_order() -> None:
    assert shape_answer(MULTI, [3, 1], None) == ["Priorities", "Due dates"]


def test_free_text_is_the_users_own_words_not_a_label() -> None:
    # "None of these" must never become the word "Other" and never a label.
    out = shape_answer(SINGLE, [], "Postgres, actually")
    assert out == "Postgres, actually"
    assert out not in [o["label"] for o in SINGLE["options"]]


def test_labels_come_from_the_options_array_verbatim() -> None:
    # The whole guarantee: every emitted string is an exact member of options[].
    labels = {o["label"] for o in MULTI["options"]}
    assert set(shape_answer(MULTI, [1, 2, 3, 4], None)) <= labels


def test_no_valid_pick_is_an_error_not_a_silent_first_option() -> None:
    # Guessing here would silently build the wrong thing.
    with pytest.raises(SystemExit):
        shape_answer(SINGLE, [], None)


def test_index_is_one_based_against_the_frozen_array() -> None:
    # Off-by-one here maps "one" to "JSON file" and nobody ever notices.
    assert shape_answer(SINGLE, [1], None) == SINGLE["options"][0]["label"]
    assert shape_answer(SINGLE, [3], None) == SINGLE["options"][2]["label"]
