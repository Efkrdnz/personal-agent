"""The spoken script: our numbering, said in the order the payload put it in.

These two assertions used to sit beside the answer-shape tests because the
script and the shapes lived in one module. They belong to the voice layer: what
matters here is that the ordinal is adjacent to the label in the TEXT a reader
will speak, and that a label reaches that text without being normalised on the
way. The shapes themselves are pinned in ``tests/test_answers.py``.
"""

from __future__ import annotations

from jarvis.answers import presentation
from jarvis.voice.script import script

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


def test_the_script_numbers_options_in_payload_order_and_never_rewrites_a_label() -> None:
    lines = script(SINGLE)
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
    labels = [ln.label for ln in script(payload) if ln.kind == "option"]
    assert labels == ["SQLite", "Düğüm — üç"]
    # Normalisation exists for COMPARISON only. If it ever leaked into the
    # spoken or stored label, this is where it would show.
    assert presentation(payload)["items"][1]["label"] == "Düğüm — üç"
