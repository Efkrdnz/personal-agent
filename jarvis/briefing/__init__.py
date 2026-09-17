"""The morning briefing: four sections, one server-side pointer, no channel.

Nothing in this package knows how a briefing is said. It composes sections from
rows and from three remote sources, writes each one into ``requests`` as a
question, and moves a pointer when the answer comes back. Whether that answer
was spoken at the desk, tapped on Telegram or keyed on a phone is not a fact
this package can read, and ``tools/check_layers.py`` fails the build if it ever
becomes one.

The whole surface is small on purpose — the coupling this stage claims is one
call to start and one call to deliver::

    from jarvis.briefing import Sources, begin, deliver, apply_answer

    briefing = begin(con)                                   # idempotent per day
    while (offer := deliver(con, briefing.id, sources=s)):   # None when it is over
        ...                                                 # a channel presents offer.request
        apply_answer(con, offer.request.id)                 # once the answer lands

:mod:`jarvis.briefing.navigator` explains why that is all there is.
"""

from __future__ import annotations

from jarvis.briefing.navigator import (
    NAV_QUESTION,
    Command,
    Delivered,
    Move,
    NavOption,
    UnknownBriefing,
    apply_answer,
    begin,
    command_of,
    deliver,
    nav_options,
    presentation_for,
)
from jarvis.briefing.sections import (
    SECTION_KEYS,
    SPECS,
    SectionContent,
    SectionSpec,
    Sources,
    compose,
    spec_for,
)
from jarvis.briefing.sources import CandidateItem, Fetch, Scored, Source, Triage
from jarvis.briefing.store import Briefing, SectionRow, run_key_for

__all__ = [
    "NAV_QUESTION",
    "SECTION_KEYS",
    "SPECS",
    "Briefing",
    "CandidateItem",
    "Command",
    "Delivered",
    "Fetch",
    "Move",
    "NavOption",
    "Scored",
    "SectionContent",
    "SectionRow",
    "SectionSpec",
    "Source",
    "Sources",
    "Triage",
    "UnknownBriefing",
    "apply_answer",
    "begin",
    "command_of",
    "compose",
    "deliver",
    "nav_options",
    "presentation_for",
    "run_key_for",
    "spec_for",
]
