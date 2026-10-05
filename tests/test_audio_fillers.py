"""Hesitation-only transcripts: what Gemini writes down for a breath or an "mm".

The VAD cannot drop a voiced "uh": it is a voice. So the words are checked
after the fact, and this pins which ones count. The bias is towards keeping:
dropping a real word costs a requirement in a read-back, keeping a filler
costs a stray "huh".
"""

from __future__ import annotations

import pytest

from jarvis.audio.fillers import is_filler, without_fillers


@pytest.mark.parametrize(
    "text",
    [
        "huh", "Huh?", "HUH.", "hmm", "Hmm.", "hmmm", "hm", "uh", "Uh...", "uhh", "um", "umm",
        "uhm", "mm", "Mmm.", "ah", "Ahh!", "eh", "Eh?", "er", "erm", "oh", "Oh.", "ooh", "ha",
        "Ha!", "haha", "heh", "que", "Que?", "Qué", "QUE.",
        # any repetition, any separator
        "hmm hmm", "Hmm, hmm.", "uh, um", "Uh... um... er", "huh huh huh", "oh, ah",
        # nothing said at all
        "...", "…", "?", "!?", "", "   ", "-", "—",
        # other languages' hesitations
        "ııı", "eee", "Eee...", "ähm", "Äh", "öh", "euh", "эм", "嗯", "呃",
        # a recogniser's non-speech tags
        "[noise]", "<breath>", "(inaudible)", "[laughs] haha",
    ],
)  # fmt: skip
def test_a_hesitation_is_a_filler(text: str) -> None:
    assert is_filler(text) is True, text


@pytest.mark.parametrize(
    "text",
    [
        "yes", "no", "stop", "evet", "hayır", "ok", "open notepad", "hey Jarvis",
        "hmm, open notepad", "uh what's the weather", "huh? say that again",
        # single letters are words, or parts of words
        "a", "A", "o", "e", "I", "m",
        # a word that merely starts like a filler
        "home", "hum", "aha moment", "her", "he", "hello", "umbrella", "queue", "query", "ohm",
        # answers: these mean yes, or no
        "uh-huh", "Uh-huh.", "uh huh", "mm-hmm", "Mm-hmm!", "mhm", "uh-uh", "hı hı",
        # numbers are content
        "8080", "hmm 3",
        "[noise] open notepad",
    ],
)  # fmt: skip
def test_a_word_is_never_a_filler(text: str) -> None:
    assert is_filler(text) is False, text


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        ("Huh? que Hmm.", ""),
        ("huh que open notepad", "open notepad"),
        ("I want, um, a web app", "I want, a web app"),
        ("uh open the uh browser", "open the browser"),
        ("Uh huh, do it", "Uh huh, do it"),
        ("uh-huh", "uh-huh"),
        ("mm hmm yes", "mm hmm yes"),
        ("uh uh", ""),
        ("  Hey   Jarvis,  hmm  what's the weather ", "Hey Jarvis, what's the weather"),
        ("[noise] open notepad", "open notepad"),
        ("Ahmet", "Ahmet"),
        ("", ""),
    ],
)
def test_without_fillers_drops_whole_hesitation_words_only(text: str, kept: str) -> None:
    assert without_fillers(text) == kept


def test_without_fillers_never_touches_a_split_word() -> None:
    """Fragments can split a word; joined first, " Ah" + "met" is Ahmet, not "met"."""
    fragments = [" Hey", " Ah", "met,", " huh", " open", " note", "pad"]
    assert without_fillers("".join(fragments)) == "Hey Ahmet, open notepad"


def test_turkish_capitals_fold_safely() -> None:
    assert is_filler("IIİ") is False, "a dotted capital I is not a Turkish hesitation"
    assert is_filler("İPTAL") is False
    assert is_filler("III") is False


def test_it_is_pure_and_stdlib_only() -> None:
    """Any layer may call it, including the transcript window in jarvis/voice."""
    import ast
    from pathlib import Path

    import jarvis.audio.fillers as mod

    tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
    roots = {
        (alias.name if isinstance(node, ast.Import) else node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    }
    assert roots <= {"__future__", "re", "unicodedata"}
