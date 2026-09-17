"""Local or cloud: the classifier ships, the answer is "local", and it says so.

Cloud mode is cut from v1 ([ADR 0009](../../docs/adr/0009-cloud-mode-cut.md)): a
deferred permission is converted to a hard DENY for calls served to a cloud
session, which removes the entire away-from-desk path that is the point of this
project. The ADR's revisit condition is SIX MONTHS OF RECORDED PHRASING, so the
phrasing is the deliverable here — the classification is written to the activity
log whether it was acted on or not, and "nobody ever actually asked for the
cloud" becomes a query instead of a memory.

The classifier itself is :func:`jarvis.spec.parse_mode`, which already knows the
English and Turkish phrasings and is already tested. Nothing is re-implemented
here; this module is the RECORDING and the honest sentence. A second regex would
be a second answer to the same question, and the one that drifted would be the
one nobody was looking at.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

from jarvis import spec
from jarvis.bus import publish
from jarvis.ids import dedupe_key

__all__ = ["MODE_EVENT", "ModeDecision", "classify", "resolve", "spoken_mode_line"]

#: Its own event kind so six months of phrasing is one SELECT. The bus takes any
#: string on purpose (see jarvis.bus): an unplanned kind must never be an outage.
MODE_EVENT = "project.mode_asked"


@dataclass(frozen=True, slots=True)
class ModeDecision:
    """What was asked, what is actually going to happen, and what to say.

    ``mode`` is the literal "local" and nothing else, for the same reason
    :attr:`jarvis.spec.Spec.mode` is: it is all v1 can do. ``asked`` is what the
    user's words meant, and it is the field worth keeping.
    """

    asked: Literal["unspecified", "local", "cloud", "unclear"]
    mode: Literal["local"] = "local"
    phrase: str | None = None
    spoken: str | None = None

    @property
    def honest(self) -> bool:
        """True when the user asked for something other than what they are getting.

        Then :attr:`spoken` is not optional — saying nothing here is exactly the
        "silently doing something else" the ADR forbids.
        """
        return self.asked in ("cloud", "unclear")


def classify(utterance: str) -> spec.ModeAsk:
    """The spine's classifier, named here so callers do not reach past this layer."""
    return spec.parse_mode(utterance)


def spoken_mode_line(ask: spec.ModeAsk) -> str | None:
    """The honesty line for one classification, or None when nothing was asked.

    Delegates to :func:`jarvis.spec.mode_note` rather than re-typing the sentence.
    ``mode_note`` takes a whole :class:`~jarvis.spec.Spec` because the spec flow
    already has one; building a hollow one here is cheaper than a second copy of
    the wording, and the wording is what must not drift.
    """
    return spec.mode_note(
        spec.Spec(
            requirements=(),
            mode="local",
            model=None,
            effort=None,
            repo_name="",
            model_ask=spec.ModelAsk(alias=None, phrase=None),
            effort_ask=spec.EffortAsk(level=None, phrase=None),
            mode_ask=ask,
        )
    )


def resolve(
    con: sqlite3.Connection,
    *,
    utterance: str,
    actor: str,
    job_id: str | None = None,
) -> ModeDecision:
    """Classify, RECORD, and hand back the sentence to say.

    The event is idempotent on the utterance, so a resumed process that
    re-classifies the same sentence adds one row rather than two — the log has to
    count how often the cloud was asked for, not how often a process restarted.
    """
    ask = classify(utterance)
    decision = ModeDecision(asked=ask.asked, phrase=ask.phrase, spoken=spoken_mode_line(ask))
    publish(
        con,
        MODE_EVENT,
        actor,
        {
            "asked": decision.asked,
            "phrase": decision.phrase,
            "mode": decision.mode,
            "utterance": utterance,
            "said": decision.spoken,
        },
        job_id=job_id,
        idem_key=f"mode:{dedupe_key(job_id, MODE_EVENT, utterance)}",
    )
    return decision
