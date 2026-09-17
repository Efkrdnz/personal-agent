"""A SECTION IS A REQUEST, so navigation is its answer. This module owns the pointer.

That sentence is the whole claim of this stage, and it is what this module is
for. Each section is delivered as one row in ``requests`` — the same table a
plan-mode question, a Bash permission and a repo read-back live in — whose
options are "Next", "Repeat that", "Skip to your inbox" and "Stop". So:

* "next" is not a command parser. It is an ANSWER, and it arrives through the
  matcher and the option numbering that already exist
  (:mod:`jarvis.answers`, :mod:`jarvis.requests`), on whichever channel the
  router picked. There is no second navigation mechanism here, and if one ever
  appears in this file the claim was false and that is the finding.
* a dropped channel resumes mid-briefing, because the pointer is
  ``briefings.position`` in SQLite and not state in the process that was
  speaking. :func:`deliver` is the resume path AND the first-delivery path; they
  are the same code because they are the same question: "what does the row say
  comes next?".
* the scheduler's entire coupling to this is one call.

WHAT THIS MODULE MUST NOT KNOW is how the briefing is said. It writes rows; a
channel reads them. There is no import of :mod:`jarvis.voice`,
:mod:`jarvis.telegram` or :mod:`jarvis.cc` here and ``tools/check_layers.py``
fails the build if one appears — a briefing that knew it was being spoken could
not be tapped, and stage 6's phone would be a rewrite rather than a re-wiring.

CURSORS MOVE ON THE ANSWER, NOT ON THE COMPOSITION. An answer is proof that a
human heard the section; composing one proves only that the machine was awake.
So :func:`apply_answer` is where :func:`jarvis.briefing.store.mark_delivered`
fires, and a briefing composed into an empty room is said again tomorrow.
"""

from __future__ import annotations

import sqlite3
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from jarvis import requests as rq
from jarvis.briefing.sections import (
    SECTION_KEYS,
    SectionContent,
    SectionSpec,
    Sources,
    compose,
    spec_for,
)
from jarvis.briefing.store import (
    Briefing,
    SectionRow,
    cursor_of,
    finish,
    get,
    mark_delivered,
    mark_skipped,
    save_content,
    section_at,
    section_for_request,
    sections,
    seen_ids,
    set_position,
    start,
)
from jarvis.bus import publish
from jarvis.ids import dedupe_key, now

__all__ = [
    "BRIEFING_ACTOR",
    "NAV_QUESTION",
    "Command",
    "Delivered",
    "Move",
    "NavOption",
    "UnknownBriefing",
    "begin",
    "command_of",
    "deliver",
    "nav_options",
    "presentation_for",
    "apply_answer",
]

Command = Literal["next", "repeat", "skip", "stop", "unclear"]

#: The key every navigation answer is stored under. One string, because
#: ``answers`` is keyed by the EXACT question text (measured, spike S1) and a
#: per-section question would make every channel derive the key from a position.
NAV_QUESTION = "What next?"

BRIEFING_ACTOR = "briefing"

#: Spoken after the options. Not an option: "none of these" is the user's own
#: words, never a label (spike S1), and "go back to the issues" is exactly the
#: thing a listener says that nobody put a number on.
FREE_TEXT_PROMPT = "Or just tell me what you want."


class UnknownBriefing(KeyError):
    """No such briefing id. A stale id, never a lost race."""


@dataclass(frozen=True, slots=True)
class NavOption:
    """One offered way out of this section, bound to what it means."""

    label: str
    description: str
    command: Command
    target: str | None = None


@dataclass(frozen=True, slots=True)
class Delivered:
    """One section, as a question waiting for an answer.

    ``resumed`` is True when this is the request that was ALREADY pending — a
    second channel attaching mid-briefing gets the same row, not a new one, so
    the user is never asked the same section twice by two devices.
    """

    briefing_id: str
    position: int
    request: rq.Request
    content: SectionContent
    resumed: bool = False


@dataclass(frozen=True, slots=True)
class Move:
    """What one answer did to the pointer."""

    command: Command
    briefing_id: str
    from_position: int
    position: int
    target: str | None = None
    said: str = ""
    finished: bool = False


# ───────────────────────────── starting ─────────────────────────────


def begin(
    con: sqlite3.Connection,
    *,
    actor: str = BRIEFING_ACTOR,
    keys: tuple[str, ...] = SECTION_KEYS,
    run_key: str | None = None,
    now_ts: str | None = None,
) -> Briefing:
    """Start this morning's briefing, or return the one already running.

    THE SCHEDULER'S ENTIRE COUPLING TO THE BRIEFING IS THIS CALL plus
    :func:`deliver`. It does not know what a section is, which sections there
    are, or which channel will say them.
    """
    briefing = start(con, keys, actor=actor, run_key=run_key, now_ts=now_ts)
    publish(
        con,
        "briefing.started",
        actor,
        {"briefing_id": briefing.id, "run_key": briefing.run_key, "sections": list(keys)},
        idem_key=f"briefing:{briefing.id}:started",
    )
    return briefing


# ───────────────────────────── the question ─────────────────────────────


def nav_options(remaining: Sequence[SectionSpec]) -> tuple[NavOption, ...]:
    """The ways out of the section at the pointer, given what is still ahead.

    ``remaining`` is the sections AFTER this one. Everything offered is
    reachable: a "skip to your inbox" on the inbox section would be an option
    that does nothing, and an option that does nothing is how a user learns not
    to trust the list.
    """
    out: list[NavOption] = []
    if remaining:
        out.append(NavOption("Next", f"go on to {remaining[0].title}", "next"))
    else:
        out.append(NavOption("Done", "that was the last section", "next"))
    out.append(NavOption("Repeat that", "say this section again", "repeat"))
    for spec in remaining[1:]:
        out.append(
            NavOption(f"Skip to {spec.title}", f"jump ahead to {spec.title}", "skip", spec.key)
        )
    out.append(NavOption("Stop", "stop the briefing here", "stop"))
    return tuple(out)


def presentation_for(content: SectionContent, options: Sequence[NavOption]) -> rq.Presentation:
    """The section as a :class:`~jarvis.requests.Presentation`.

    ``verbatim=True``: these lines name jobs, senders and issue titles that were
    read out of rows, and a generative reader that "tidied" them would be
    inventing facts about the user's morning.
    """
    return rq.make_presentation(
        intro=content.spoken,
        options=[{"label": o.label, "description": o.description} for o in options],
        verbatim=True,
        multi=False,
        allows_free_text=True,
        free_text_prompt=FREE_TEXT_PROMPT,
        # Every option fits on a keypad, so the phone stage gets navigation for
        # free rather than as a special case. ONE DIGIT EACH, and that is the
        # whole of the cap: the four shipped sections never offer more than five
        # options, but `begin(keys=...)` takes any list, and a tenth option mapped
        # to the string "10" is not a keypress anybody can make — it would look
        # like navigation the phone had and did not.
        dtmf_map={str(i): i for i in range(1, min(len(options), 9) + 1)},
        question=NAV_QUESTION,
    )


def _payload(
    briefing_id: str,
    position: int,
    content: SectionContent,
    options: Sequence[NavOption],
) -> dict[str, Any]:
    """The row's self-description, in the shape :mod:`jarvis.answers` can validate.

    The ``questions`` array is here so an arriving answer is checked by the
    spine's own validator rather than by a second one written in this package,
    and ``nav`` is the index-to-meaning map — the answer comes back as a LABEL
    from the frozen array and is resolved by local lookup, never parsed.
    """
    return {
        "questions": [
            {
                "question": NAV_QUESTION,
                "header": "briefing",
                "options": [{"label": o.label, "description": o.description} for o in options],
            }
        ],
        "briefing_id": briefing_id,
        "position": position,
        "section": content.key,
        "lines": list(content.lines),
        "item_ids": list(content.item_ids),
        "ok": content.ok,
        "nav": [
            {"index": i, "command": o.command, "target": o.target}
            for i, o in enumerate(options, start=1)
        ],
    }


# ───────────────────────────── delivering ─────────────────────────────


def deliver(
    con: sqlite3.Connection,
    briefing_id: str,
    *,
    sources: Sources | None = None,
    actor: str = BRIEFING_ACTOR,
    now_ts: str | None = None,
) -> Delivered | None:
    """The section at the pointer, as a pending request. THE RESUME PATH TOO.

    Called by whichever process has a channel: the desk at 10:00, the Telegram
    bot when the user taps, a different desk process after a reboot. It returns
    ``None`` when the briefing is over, which is the only "we are done" signal a
    caller needs.

    Three cases, one function:

    * the section has never been delivered — compose it, store the words, create
      the request;
    * the request is still pending — hand back THE SAME ROW, so a second channel
      cannot ask the same section twice;
    * the request was answered while nobody was applying it (the channel dropped
      between the tap and the handling) — apply it first, then deliver whatever
      the pointer now says. That is what makes a briefing resume at section
      three instead of re-reading section two.
    """
    briefing = get(con, briefing_id)
    if briefing is None:
        raise UnknownBriefing(briefing_id)
    if not briefing.running:
        return None

    settled = _settle_current(con, briefing, actor=actor, now_ts=now_ts)
    if settled is not None and settled.finished:
        return None
    briefing = get(con, briefing_id)
    if briefing is None or not briefing.running:
        return None

    row = section_at(con, briefing.id, briefing.position)
    if row is None:
        # The pointer walked off the end without anybody noticing. Ending here is
        # the honest outcome; leaving it running would make tomorrow's start
        # find a live briefing that can never say anything.
        _end(con, briefing.id, "finished", actor)
        return None

    pending = _pending_request(con, row)
    if pending is not None:
        content = row.content or _compose_for(con, row, sources, now_ts)
        return Delivered(briefing.id, row.position, pending, content, resumed=True)

    content = row.content or _compose_for(con, row, sources, now_ts)
    ahead = [spec_for(s.key) for s in sections(con, briefing.id) if s.position > row.position]
    options = nav_options(ahead)
    request = _ask(con, briefing.id, row, content, options, actor=actor)
    save_content(con, briefing.id, row.position, content, request_id=request.id)
    publish(
        con,
        "briefing.section.offered",
        actor,
        {
            "briefing_id": briefing.id,
            "position": row.position,
            "section": content.key,
            "ok": content.ok,
            "items": len(content.item_ids),
        },
        request_id=request.id,
        idem_key=f"briefing:{briefing.id}:{row.position}:{request.id}:offered",
    )
    return Delivered(briefing.id, row.position, request, content)


def _compose_for(
    con: sqlite3.Connection,
    row: SectionRow,
    sources: Sources | None,
    now_ts: str | None,
) -> SectionContent:
    cursor_name = spec_for(row.key).cursor
    cursor = cursor_of(con, cursor_name)
    return compose(
        con,
        row.key,
        sources=sources,
        cursor=cursor,
        # The whole set for this source, because the candidates are not known
        # until after the fetch. It is bounded by SEEN_KEEP_DAYS and scoped to one
        # source, so it is a few hundred short strings rather than a month of
        # everything — and a per-candidate query would need two round trips to the
        # source's own paging to save nothing.
        seen=seen_ids(con, cursor_name),
        now_ts=now_ts,
    )


def _pending_request(con: sqlite3.Connection, row: SectionRow) -> rq.Request | None:
    if not row.request_id:
        return None
    req = rq.get_request(con, row.request_id)
    return req if req is not None and req.state == "pending" else None


def _ask(
    con: sqlite3.Connection,
    briefing_id: str,
    row: SectionRow,
    content: SectionContent,
    options: Sequence[NavOption],
    *,
    actor: str,
) -> rq.Request:
    """Create the section's request. A REPEAT is a new attempt, not a new question.

    ``next_attempt`` is the spine's own answer to "the same question asked
    again": the dedupe key stays the same so the row is findable, and the attempt
    counter is what tells a re-fire apart from a genuine second ask. Without it,
    "repeat" would find the answered row from a moment ago and hand back the
    answer that caused the repeat.
    """
    key = dedupe_key(None, "briefing.section", {"briefing": briefing_id, "position": row.position})
    return rq.create_request(
        con,
        kind="briefing_gate",
        short_label=content.title,
        presentation=presentation_for(content, options),
        payload=_payload(briefing_id, row.position, content, options),
        actor=actor,
        urgency="low",
        # No deadline. An unanswered section must not age into a decision nobody
        # made: the pointer stays where it is and the next channel to attach
        # picks it up, which is the same mechanism as the dropped-channel resume.
        on_timeout="defer",
        dedupe_key=key,
        attempt=rq.next_attempt(con, None, key),
    )


# ───────────────────────────── answering ─────────────────────────────


def _norm(text: str) -> str:
    """NFKC + casefold + collapsed whitespace, for COMPARISON ONLY.

    Used to recognise a label the user said in their own words rather than by
    number. It is a lookup against the frozen option array — never a parser, and
    it can only ever return an option that was actually offered.
    """
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split()).strip(" .!?,;:")


def command_of(req: rq.Request, answer: rq.Answer) -> tuple[Command, str | None, str]:
    """An answer in, a navigation command out. PURE, and by LOOKUP.

    The label comes back from the frozen options array and is mapped to a command
    through the row's own ``nav`` map, so a command that was not offered cannot
    be produced. Anything unrecognised is ``unclear`` — never a guess, because
    guessing "next" on an unclear answer skips a section the user asked about.
    """
    said = ""
    value: Any = None
    given = answer.get("answers") or {}
    if NAV_QUESTION in given:
        value = given[NAV_QUESTION]
    if isinstance(value, list):
        value = value[0] if len(value) == 1 else None
    if isinstance(value, str):
        said = value
    elif isinstance(answer.get("text"), str):
        said = str(answer["text"])
    if not said:
        return "unclear", None, ""

    items = req.presentation["items"]
    index: int | None = None
    for item in items:
        if item["label"] == said:
            index = item["index"]
            break
    if index is None:
        wanted = _norm(said)
        for item in items:
            if _norm(item["label"]) == wanted:
                index = item["index"]
                break
    if index is None:
        return "unclear", None, said

    for entry in req.payload.get("nav") or ():
        if int(entry.get("index", 0)) == index:
            return str(entry.get("command", "unclear")), entry.get("target"), said  # type: ignore[return-value]
    return "unclear", None, said


def apply_answer(
    con: sqlite3.Connection,
    request_id: str,
    *,
    actor: str = BRIEFING_ACTOR,
    now_ts: str | None = None,
) -> Move | None:
    """Apply the answer to a section's request: commit the delivery, move the pointer.

    Returns ``None`` while the request is still pending — the caller can poll
    this safely, and a channel that has just won the answer race calls it once.

    Consuming the answer through :func:`jarvis.requests.consume` is deliberate
    and is what makes this idempotent: the second call gets the same answer back
    and every write below is itself a compare-and-swap, so a duplicated poke
    cannot advance the pointer twice.
    """
    row = section_for_request(con, request_id)
    if row is None:
        raise UnknownBriefing(f"no briefing section owns request {request_id}")
    answer = rq.consume(con, request_id, actor)
    if answer is None:
        return None
    req = rq.get_request(con, request_id)
    if req is None:  # pragma: no cover - consume() would have raised
        raise UnknownBriefing(request_id)
    command, target, said = command_of(req, answer)
    return _apply(con, row, command, target, said, actor=actor, now_ts=now_ts)


def _settle_current(
    con: sqlite3.Connection,
    briefing: Briefing,
    *,
    actor: str,
    now_ts: str | None,
) -> Move | None:
    """Apply an answer that arrived while nobody was listening. Idempotent."""
    row = section_at(con, briefing.id, briefing.position)
    if row is None or not row.request_id:
        return None
    req = rq.get_request(con, row.request_id)
    if req is None or req.state not in ("answered", "consumed"):
        return None
    if req.state == "consumed" and row.state != "pending":
        # Already applied; re-applying would be harmless but would re-publish.
        return None
    return apply_answer(con, row.request_id, actor=actor, now_ts=now_ts)


def _apply(
    con: sqlite3.Connection,
    row: SectionRow,
    command: Command,
    target: str | None,
    said: str,
    *,
    actor: str,
    now_ts: str | None,
) -> Move:
    """The pointer arithmetic, and the one place a cursor is allowed to move."""
    briefing_id = row.briefing_id
    here = row.position
    ts = now_ts or now()

    # An answer to this section's question is PROOF that this section reached a
    # human. That, and nothing else, is what lets its cursor move.
    if mark_delivered(con, briefing_id, here, now_ts=ts):
        publish(
            con,
            "briefing.section.delivered",
            actor,
            {
                "briefing_id": briefing_id,
                "position": here,
                "section": row.key,
                "items": len(row.content.item_ids) if row.content else 0,
            },
            request_id=row.request_id,
            idem_key=f"briefing:{briefing_id}:{here}:delivered",
        )

    if command == "repeat" or command == "unclear":
        return Move(command, briefing_id, here, here, target, said)

    if command == "stop":
        ended = _end(con, briefing_id, "stopped", actor)
        return Move("stop", briefing_id, here, here, None, said, finished=ended)

    if command == "skip" and target:
        ahead = [s for s in sections(con, briefing_id) if s.position > here and s.key == target]
        if not ahead:
            # Only backwards or nonexistent targets land here; the offered
            # options are forward-only. Refusing beats jumping somewhere the user
            # did not ask for.
            return Move("unclear", briefing_id, here, here, target, said)
        destination = ahead[0].position
        for skipped in sections(con, briefing_id):
            if here < skipped.position < destination:
                # NOTHING is committed for a skipped section: it was not heard, so
                # its cursor stays put and tomorrow says what today jumped over.
                mark_skipped(con, briefing_id, skipped.position, now_ts=ts)
        set_position(con, briefing_id, destination, expect=here)
        return Move("skip", briefing_id, here, destination, target, said)

    nxt = here + 1
    if section_at(con, briefing_id, nxt) is None:
        ended = _end(con, briefing_id, "finished", actor)
        return Move("next", briefing_id, here, here, None, said, finished=ended)
    set_position(con, briefing_id, nxt, expect=here)
    return Move("next", briefing_id, here, nxt, None, said)


def _end(con: sqlite3.Connection, briefing_id: str, state: str, actor: str) -> bool:
    ended = finish(con, briefing_id, state)  # type: ignore[arg-type]
    if ended:
        publish(
            con,
            "briefing.finished",
            actor,
            {"briefing_id": briefing_id, "state": state},
            idem_key=f"briefing:{briefing_id}:{state}",
        )
    return ended
