"""The ``AskUserQuestion`` payload, and the index-to-label resolution. PURE, both ways.

This module is the fidelity guarantee in code. Two properties, and everything
else here exists to serve them:

*THE NUMBERING IS OURS.* It is generated locally from ``input["questions"]`` in
payload order, never asked of a model and never parsed back out of speech. What
was spoken is therefore reproducible from the stored row, which is what makes
``jarvis log`` able to show what the user actually heard.

*THE MODEL MAY EMIT AN INDEX, NEVER A LABEL.* Labels are carried through
verbatim — not stripped, not title-cased, not translated, not reordered — and
they come back by LOCAL LOOKUP against the frozen options array. A hallucinated,
merged or translated label cannot reach ``updated_input`` because no code path
here ever accepts one.

Everything is a pure function over plain data. No connection, no clock, no I/O:
the numbering a user heard at 9am is the numbering a test can reproduce at 4pm.

THE NUMBERING IS GLOBAL ACROSS THE BATCH. ``AskUserQuestion`` may carry up to
four questions, and one tool call is one row in ``requests`` (the unique index on
``tool_use_id`` sees to that), so one row's :class:`~jarvis.requests.Presentation`
has to hold every option in the batch. Restarting at 1 for each question would
make "three" ambiguous the moment the user answers out of order, and it would put
two options numbered 1 in one frozen array. For the overwhelmingly common
single-question call the two schemes are identical.

THIS LIVES IN THE SPINE BECAUSE EVERY CHANNEL NEEDS IT AND NONE OF THEM IS THE
DESK. The shape of a question and the shape of its answer are what the Claude
Code driver, the Telegram bot and the phone leg all have to agree on; putting
them inside :mod:`jarvis.cc` made two layers depend backwards on a driver they
never talk to. Speaking a question is a different concern and lives in
:mod:`jarvis.voice.script`, which reads the numbering from here rather than
inventing a second one.

Measured facts from spike S1 that this module encodes and must never contradict:

  * ``answers`` is keyed by the EXACT question string.
  * single-select takes ONE label string; ``multiSelect`` takes a LIST of them.
  * "none of these" is the user's OWN WORDS — never the word "Other" and never
    a label.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from jarvis.requests import Answer, Presentation, make_presentation

__all__ = [
    "FREE_TEXT_PROMPT",
    "MAX_ANSWER_CHARS",
    "AnswerShapeError",
    "MalformedQuestions",
    "Slot",
    "answer",
    "answers_from_indices",
    "presentation",
    "questions_of",
    "short_label",
    "slots",
    "validate_answers",
]

#: Spoken after the options, every time. It is a PROMPT and not an option: the
#: measured shape of "none of these" is the user's own words, so there is no
#: label for it and it deliberately never gets a number of its own.
FREE_TEXT_PROMPT = "Or say your own answer."

#: The CLI's own validator refuses an answer value longer than this. Catching it
#: here turns a rejected tool call deep inside Claude Code into a ValueError at
#: the channel that built the answer.
MAX_ANSWER_CHARS = 8192


class MalformedQuestions(ValueError):
    """An ``AskUserQuestion`` payload this module refuses to read.

    Its own type because the caller's response differs: a malformed payload is
    denied back to Claude Code, while a bad ANSWER is the channel's bug.
    """


class AnswerShapeError(ValueError):
    """An answer whose SHAPE would be silently misread by the CLI.

    A list where a string belongs, a label where free text belongs, a question
    nobody asked. All four of these serialise fine and are only wrong once they
    reach Claude Code, which is far too late to say so out loud.
    """


@dataclass(frozen=True, slots=True)
class Slot:
    """One numbered option: our index, bound to the exact label that earned it.

    ``question`` is carried on every slot because it is the key the answer goes
    under, and re-deriving it from a position later is exactly the class of bug
    this dataclass exists to make unrepresentable.
    """

    index: int
    question: str
    question_index: int
    label: str
    description: str
    multi: bool


# ───────────────────────────── reading the payload ─────────────────────────────


def questions_of(input_data: Mapping[str, Any] | Sequence[Any]) -> list[dict[str, Any]]:
    """The validated questions array, from a tool input or from the array itself.

    Accepts both because the payload arrives two ways that must produce identical
    numbering: ``can_use_tool`` gets the whole ``input_data``, while the defer
    path only ever sees ``ResultMessage.deferred_tool_use.input["questions"]``.
    """
    raw: Any = input_data
    if isinstance(input_data, Mapping):
        if "questions" not in input_data:
            raise MalformedQuestions("AskUserQuestion input has no 'questions' array")
        raw = input_data["questions"]
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or not raw:
        raise MalformedQuestions("'questions' must be a non-empty array")

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, q in enumerate(raw, start=1):
        if not isinstance(q, Mapping):
            raise MalformedQuestions(f"question {i} is not an object")
        text = q.get("question")
        if not isinstance(text, str) or not text.strip():
            raise MalformedQuestions(f"question {i} has no question text")
        if text in seen:
            # answers is keyed by the question STRING, so two identical questions
            # in one batch cannot both be answered: the second would overwrite
            # the first and one of the user's decisions would vanish silently.
            raise MalformedQuestions(f"question {i} repeats an earlier question verbatim")
        seen.add(text)
        options = q.get("options")
        if not isinstance(options, Sequence) or isinstance(options, (str, bytes)) or not options:
            raise MalformedQuestions(f"question {i} has no options")
        for j, opt in enumerate(options, start=1):
            if not isinstance(opt, Mapping) or not isinstance(opt.get("label"), str):
                raise MalformedQuestions(f"question {i}, option {j} has no string label")
            if not opt["label"].strip():
                raise MalformedQuestions(f"question {i}, option {j} has an empty label")
        out.append(dict(q))
    return out


def slots(questions: Mapping[str, Any] | Sequence[Any]) -> tuple[Slot, ...]:
    """Every option in the batch, numbered 1..N in payload order. The frozen array."""
    qs = questions_of(questions)
    made: list[Slot] = []
    n = 0
    for qi, q in enumerate(qs, start=1):
        multi = bool(q.get("multiSelect"))
        for opt in q["options"]:
            n += 1
            made.append(
                Slot(
                    index=n,
                    question=str(q["question"]),
                    question_index=qi,
                    label=str(opt["label"]),
                    description=str(opt.get("description") or ""),
                    multi=multi,
                )
            )
    return tuple(made)


def presentation(questions: Mapping[str, Any] | Sequence[Any]) -> Presentation:
    """Build the spine's :class:`~jarvis.requests.Presentation` for this batch.

    Delegates the numbering to :func:`jarvis.requests.make_presentation` rather
    than repeating it, so there is exactly one place in the tree where an option
    gets a number and exactly one place a test has to pin.
    """
    qs = questions_of(questions)
    sl = slots(qs)
    if len(qs) == 1:
        intro = str(qs[0]["question"])
    else:
        intro = " ".join(
            f"Question {i} of {len(qs)}: {q['question']}" for i, q in enumerate(qs, start=1)
        )
    return make_presentation(
        intro=intro,
        options=[{"label": s.label, "description": s.description} for s in sl],
        verbatim=True,
        multi=any(s.multi for s in sl),
        allows_free_text=True,
        free_text_prompt=FREE_TEXT_PROMPT,
        # Only when every option fits on the keypad. A partial map would let a
        # phone answer reach some options and silently not others.
        dtmf_map={str(s.index): s.index for s in sl} if len(sl) <= 9 else None,
        question=str(qs[0]["question"]) if len(qs) == 1 else None,
    )


def short_label(questions: Mapping[str, Any] | Sequence[Any]) -> str:
    """Three words at most, because this string is SPOKEN in the briefing."""
    qs = questions_of(questions)
    if len(qs) > 1:
        return f"{len(qs)} questions"
    header = str(qs[0].get("header") or "").strip()
    if header:
        return header.lower()
    return " ".join(str(qs[0]["question"]).split()[:3]).rstrip("?,.").lower()


# ───────────────────────────── indices back to labels ─────────────────────────────


def _norm(text: str) -> str:
    """NFKC + casefold + collapsed whitespace, for COMPARISON only.

    Never used to produce an answer. It exists so "postgres" typed by a channel
    can be recognised as a label that should have been picked by number, and for
    nothing else.
    """
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split()).strip(" .!?,;:")


def _picks_by_question(
    sl: Sequence[Slot],
    picks: Sequence[int] | Mapping[str, Sequence[int] | int],
) -> dict[str, list[int]]:
    """Normalise both accepted pick shapes into {question: [global indices]}."""
    by_index = {s.index: s for s in sl}
    grouped: dict[str, list[int]] = {}

    def add(index: Any, expect_question: str | None) -> None:
        if not isinstance(index, int) or isinstance(index, bool):
            raise AnswerShapeError(f"option index must be an int, got {index!r}")
        slot = by_index.get(index)
        if slot is None:
            # Never clamp, never fall back to the first option: a wrong index is
            # a wrong build, and the only safe thing a pure function can do with
            # one is refuse it where it was produced.
            raise AnswerShapeError(f"option {index} does not exist; there are {len(by_index)}")
        if expect_question is not None and slot.question != expect_question:
            raise AnswerShapeError(
                f"option {index} belongs to {slot.question!r}, not {expect_question!r}; "
                "indices are numbered across the whole batch, not per question"
            )
        bucket = grouped.setdefault(slot.question, [])
        if index in bucket:
            raise AnswerShapeError(f"option {index} was picked twice")
        bucket.append(index)

    if isinstance(picks, Mapping):
        for question, value in picks.items():
            if not any(s.question == question for s in sl):
                raise AnswerShapeError(f"nobody asked {question!r}")
            grouped.setdefault(question, [])
            # Same refusal as the flat branch below, and for the same reason. It
            # was missing here, and bytes are the case that bites: they ARE a
            # Sequence, so b"\x01" iterated as [1] and silently picked option
            # one. A wrong option chosen in silence is the exact failure this
            # module exists to make impossible, so text is refused rather than
            # coerced — on both shapes, with one message.
            if isinstance(value, (str, bytes)):
                raise AnswerShapeError("picks are option indices, not text")
            many = isinstance(value, Sequence)
            values = value if many else [value]
            for index in values:
                add(index, question)
    else:
        if isinstance(picks, (str, bytes)):
            raise AnswerShapeError("picks are option indices, not text")
        for index in picks:
            add(index, None)
    return grouped


def answers_from_indices(
    questions: Mapping[str, Any] | Sequence[Any],
    picks: Sequence[int] | Mapping[str, Sequence[int] | int] = (),
    free_text: Mapping[str, str] | str | None = None,
) -> dict[str, str | list[str]]:
    """Indices in, the exact ``answers`` dict for ``updated_input`` out.

    ``picks`` are the numbers from :func:`jarvis.voice.script.script` — a flat
    sequence, or a mapping from the exact question text to its numbers.
    ``free_text`` is the "none of these" case and is the user's OWN WORDS, keyed
    the same way.

    Every question in the batch must get exactly one of the two. A partially
    answered batch is refused rather than sent, because the CLI would proceed on
    the questions that were answered and the user would never learn that the rest
    were decided by nobody.
    """
    qs = questions_of(questions)
    sl = slots(qs)
    grouped = _picks_by_question(sl, picks)

    if isinstance(free_text, str):
        if len(qs) != 1:
            raise AnswerShapeError("bare free text needs exactly one question; key it by question")
        free: dict[str, str] = {str(qs[0]["question"]): free_text}
    else:
        free = {str(k): str(v) for k, v in (free_text or {}).items()}
        asked = {str(q["question"]) for q in qs}
        stray = sorted(set(free) - asked)
        if stray:
            # Symmetric with picks, which already refuse an unknown question. A
            # dropped free_text key is worse than a dropped index: the user SAID
            # something, and the answer that reaches Claude would be whatever the
            # picks happened to cover, with nobody told their words went nowhere.
            raise AnswerShapeError(f"free text names questions nobody asked: {stray}")

    labels_by_question: dict[str, set[str]] = {}
    for s in sl:
        labels_by_question.setdefault(s.question, set()).add(_norm(s.label))

    out: dict[str, str | list[str]] = {}
    for q in qs:
        question = str(q["question"])
        multi = bool(q.get("multiSelect"))
        chosen = grouped.get(question) or []
        words = free.get(question)

        if words is not None and chosen:
            raise AnswerShapeError(f"{question!r} has both picked options and free text")
        if words is not None:
            if not words.strip():
                raise AnswerShapeError(f"free text for {question!r} is empty")
            if _norm(words) in labels_by_question[question]:
                # A genuine "none of these" never looks like an option. Letting
                # this through would put a label into the answer by a path that
                # bypasses the index lookup — the one thing this module exists
                # to make impossible.
                raise AnswerShapeError(
                    f"free text for {question!r} is an existing option; pick it by number"
                )
            out[question] = words
            continue
        if not chosen:
            raise AnswerShapeError(f"no answer for {question!r}")

        picked_labels = [next(s.label for s in sl if s.index == i) for i in chosen]
        if multi:
            out[question] = picked_labels
        else:
            if len(picked_labels) != 1:
                raise AnswerShapeError(
                    f"{question!r} is single-select; {len(picked_labels)} options were picked"
                )
            out[question] = picked_labels[0]

    validate_answers(qs, out)
    return out


def answer(
    questions: Mapping[str, Any] | Sequence[Any],
    picks: Sequence[int] | Mapping[str, Sequence[int] | int] = (),
    free_text: Mapping[str, str] | str | None = None,
) -> Answer:
    """The spine's :class:`~jarvis.requests.Answer` for this batch.

    ``sources`` records option-versus-free-text per question, so the read-back
    can say "you said Postgres, in your own words" rather than presenting a
    typed sentence as though it had been one of the offered choices.
    """
    answers = answers_from_indices(questions, picks, free_text)
    offered: dict[str, set[str]] = {}
    for s in slots(questions):
        offered.setdefault(s.question, set()).add(s.label)
    sources = {
        question: "option" if isinstance(value, list) or value in offered[question] else "free_text"
        for question, value in answers.items()
    }
    return {"answers": answers, "sources": sources}


def validate_answers(
    questions: Mapping[str, Any] | Sequence[Any],
    answers: Mapping[str, Any],
) -> None:
    """Refuse an answers dict the CLI would misread. Raises; returns nothing.

    Called on answers built HERE and on answers that arrived from another process
    hours later, because the second case is the one that cannot be trusted: the
    Telegram bot, the phone worker and a hand-written repair script all write the
    same column and none of them imports this module.
    """
    qs = questions_of(questions)
    by_question = {str(q["question"]): q for q in qs}
    if not isinstance(answers, Mapping) or not answers:
        raise AnswerShapeError("answers must be a non-empty mapping")

    unknown = sorted(set(answers) - set(by_question))
    if unknown:
        raise AnswerShapeError(f"answers name questions nobody asked: {unknown}")
    missing = sorted(set(by_question) - set(answers))
    if missing:
        raise AnswerShapeError(f"unanswered questions: {missing}")

    for question, value in answers.items():
        q = by_question[question]
        multi = bool(q.get("multiSelect"))
        labels = [str(o["label"]) for o in q["options"]]
        if isinstance(value, list):
            if not multi:
                raise AnswerShapeError(
                    f"{question!r} is single-select: it takes ONE label string, not a list"
                )
            if not value:
                raise AnswerShapeError(f"{question!r} has an empty list of labels")
            if len(value) > len(labels) + 1:
                raise AnswerShapeError(f"{question!r} has more answers than it has options")
            if len(set(value)) != len(value):
                raise AnswerShapeError(f"{question!r} repeats a label")
            for item in value:
                if not isinstance(item, str):
                    raise AnswerShapeError(f"{question!r} has a non-string label {item!r}")
                if item not in labels:
                    # The whole point. A label that is not a byte-for-byte member
                    # of the frozen array was translated, reordered or invented.
                    raise AnswerShapeError(
                        f"{item!r} is not one of the options offered for {question!r}"
                    )
        elif isinstance(value, str):
            if not value.strip():
                raise AnswerShapeError(f"{question!r} has an empty answer")
            if multi and value in labels:
                # A bare string on a multiSelect question is only ever the "none
                # of these" case. A LABEL arriving that way is the single-select
                # shape on a multi question — accepted by the CLI, and silently
                # one choice where the user made several.
                raise AnswerShapeError(
                    f"{question!r} is multiSelect: a chosen label belongs in a LIST"
                )
            if len(value) > MAX_ANSWER_CHARS:
                raise AnswerShapeError(
                    f"{question!r}: the CLI refuses an answer over {MAX_ANSWER_CHARS} characters"
                )
        else:
            raise AnswerShapeError(
                f"{question!r} must be a label string or a list of label strings, "
                f"got {type(value).__name__}"
            )
