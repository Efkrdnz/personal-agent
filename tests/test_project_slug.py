"""Slugging spoken Turkish, and the invisible character that breaks it.

The trap is asserted twice over: once on Python itself, so the test says WHY the
pipeline is shaped the way it is and fails if a future interpreter changes the
behaviour, and once on our output, so a naive rewrite of :func:`slugify` fails
here rather than on somebody's GitHub account.
"""

from __future__ import annotations

import unicodedata

import pytest

from jarvis.project import slug as sg

COMBINING_DOT = "\u0307"


# ───────────────────────────── the trap ─────────────────────────────


def test_python_really_does_leave_a_combining_dot_behind() -> None:
    """The measured fact this module exists for. If this ever fails, read it twice.

    ``"İ".lower()`` is two characters, not one, and ``casefold()`` agrees. A
    ``lower()``-then-strip pipeline therefore produces a name with an invisible
    mark in it — which GitHub either rejects or, far worse, accepts.
    """
    assert "İ".lower() == "i" + COMBINING_DOT
    assert "İ".casefold() == "i" + COMBINING_DOT
    assert len("İ".lower()) == 2
    # And the dotless ı does not fold to anything at all, so NFKD cannot save it.
    assert "ı".casefold() == "ı"
    assert unicodedata.normalize("NFKD", "ı") == "ı"


@pytest.mark.parametrize(
    ("spoken", "expected"),
    [
        ("comment watcher", "comment-watcher"),
        ("Comment Watcher", "comment-watcher"),
        ("İstanbul takip", "istanbul-takip"),
        ("İSTANBUL TAKİP", "istanbul-takip"),
        ("Yorum İzleyici", "yorum-izleyici"),
        ("ışık ölçer", "isik-olcer"),
        ("Şirket Günlüğü", "sirket-gunlugu"),
        ("çöp toplayıcı", "cop-toplayici"),
        ("ğşıöçü", "gsiocu"),
        ("todo app 2", "todo-app-2"),
        ("  spaced   out  ", "spaced-out"),
        ("my app's notes", "my-app-s-notes"),
        ("emoji 🚀 rocket", "emoji-rocket"),
        ("über café", "uber-cafe"),
    ],
)
def test_the_slug_is_what_the_words_should_produce(spoken: str, expected: str) -> None:
    assert sg.slugify(spoken) == expected


@pytest.mark.parametrize(
    "spoken",
    ["İstanbul takip", "İSTANBUL", "İzmir Ölçüm", "İİİ", "Iğdır"],
)
def test_no_slug_ever_contains_an_invisible_mark(spoken: str) -> None:
    """The property, not the examples: pure ASCII, and no combining characters."""
    out = sg.slugify(spoken)
    assert out.isascii(), out
    assert COMBINING_DOT not in out
    assert not any(unicodedata.combining(c) for c in out)
    assert out == unicodedata.normalize("NFC", out)


def test_the_naive_pipeline_would_have_failed_this_very_test() -> None:
    """Proof that the assertions above have teeth rather than merely passing."""
    naive = "-".join("İstanbul takip".lower().split())
    assert COMBINING_DOT in naive
    assert not naive.isascii()
    assert naive != sg.slugify("İstanbul takip")


# ───────────────────────────── the shape ─────────────────────────────


@pytest.mark.parametrize(
    "spoken",
    ["comment watcher", "İstanbul takip", "a", "x9", "--hello--", "a...b", "  A  B  "],
)
def test_every_slug_has_the_shape_github_and_a_human_can_both_use(spoken: str) -> None:
    out = sg.slugify(spoken)
    assert out
    assert out == out.lower()
    assert set(out) <= set("abcdefghijklmnopqrstuvwxyz0123456789-")
    assert not out.startswith("-") and not out.endswith("-")
    assert "--" not in out
    assert len(out) <= sg.MAX_NEW_REPO_NAME


def test_a_created_name_leaves_room_for_the_name_it_may_be_renamed_to() -> None:
    """The cap is 87 because the compensation promises a 13-character prefix.

    If this ever drifts, the confirmation would promise a rename to a name GitHub
    refuses — for exactly the long names nobody tries by hand.
    """
    assert sg.MAX_REPO_NAME - len(sg.ABANDONED_PREFIX) == sg.MAX_NEW_REPO_NAME
    longest = sg.slugify("a" * sg.MAX_NEW_REPO_NAME)
    assert len(longest) == sg.MAX_NEW_REPO_NAME
    assert len(sg.abandoned_name(longest)) <= sg.MAX_REPO_NAME


def test_the_abandoned_name_is_the_one_the_spoken_line_promises() -> None:
    """Same prefix as jarvis.github.scopes, because the promise and the act must match."""
    from jarvis.github import scopes

    assert sg.abandoned_name("comment-watcher") == scopes.abandoned_name("comment-watcher")
    assert sg.ABANDONED_PREFIX is scopes.ABANDONED_PREFIX


def test_a_slug_is_a_name_the_github_client_accepts() -> None:
    """The layer below validates rather than encodes, so our output must pass it."""
    from jarvis.github import repos

    for spoken in ("comment watcher", "İstanbul takip", "çöp toplayıcı", "x"):
        repos.check_slug(sg.slugify(spoken))


# ───────────────────────────── the refusals ─────────────────────────────


@pytest.mark.parametrize("spoken", ["", "   ", "\n\t "])
def test_silence_is_refused_rather_than_named(spoken: str) -> None:
    with pytest.raises(sg.EmptyName):
        sg.slugify(spoken)


@pytest.mark.parametrize("spoken", ["...", "!!!", "-", "---", "🚀", "£€¥", "()[]{}"])
def test_punctuation_alone_is_refused_rather_than_producing_something_odd(spoken: str) -> None:
    with pytest.raises(sg.EmptyName):
        sg.slugify(spoken)


@pytest.mark.parametrize("spoken", [".", "..", ".git", ".GIT", "  .git  "])
def test_a_reserved_name_raises_instead_of_becoming_a_surprising_one(spoken: str) -> None:
    """ ".git" would slug to "git": legal, and not what anybody meant."""
    with pytest.raises(sg.ReservedName):
        sg.slugify(spoken)


def test_a_name_too_long_to_rename_is_refused_at_the_door() -> None:
    with pytest.raises(sg.NameTooLong) as caught:
        sg.slugify("a" * (sg.MAX_NEW_REPO_NAME + 1))
    assert str(sg.MAX_NEW_REPO_NAME) in caught.value.spoken


def test_every_refusal_carries_a_sentence_a_user_would_understand() -> None:
    """These are all HEARD. A bare exception type is not an answer to a person."""
    for spoken in ("", "...", ".git", "a" * 200):
        with pytest.raises(sg.SlugError) as caught:
            sg.slugify(spoken)
        said = caught.value.spoken
        assert said and said[0].isupper() and said.endswith(("?", ".")), said
        assert "Traceback" not in said


def test_a_name_from_elsewhere_is_checked_by_the_same_rules() -> None:
    """Names arrive typed into Telegram and read off a clone, not only from speech."""
    assert sg.is_repo_name("comment-watcher")
    assert not sg.is_repo_name("Comment-Watcher")
    assert not sg.is_repo_name("comment--watcher")
    assert not sg.is_repo_name("-comment")
    assert not sg.is_repo_name("comment.watcher")
    assert not sg.is_repo_name("istanbul" + COMBINING_DOT)
    assert not sg.is_repo_name("")
    assert not sg.is_repo_name("a" * 101)
    with pytest.raises(sg.SlugError):
        sg.check_repo_name("comment watcher")


def test_abandoning_a_name_that_is_already_too_long_is_refused() -> None:
    """A repository adopted from elsewhere may be 100 characters; then the rename cannot fit."""
    with pytest.raises(sg.NameTooLong):
        sg.abandoned_name("a" * 95)
