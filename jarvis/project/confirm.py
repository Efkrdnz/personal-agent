"""The confirmation and the rename loop, as rows in ``requests``. Nothing new invented.

Two questions live here and both go through the spine's one gate:

*"Shall I create this?"* — a ``confirm_effect`` request whose spoken intro is a
VERBATIM read-back of the three things that can be wrong (the owner, the exact
slug, the visibility) plus the capability-derived sentence about what can and
cannot be undone afterwards. The user hears the slug they are ACTUALLY getting,
spelled out when transliteration means they could not have predicted it — which
is precisely the Turkish case.

*"That name is taken, what shall I call it?"* — a ``free_text`` request. There is
no auto-suffixing anywhere in this package: ``comment-watcher-2`` chosen silently
is how you end up with four repositories and no idea which one is live. The
rename is a spoken turn through the same table, and each turn is its own row.

THE OPTION LABELS ARE NOT FREE. ``jarvis.telegram.channel`` reads every
``confirm_effect`` as a boolean and refuses — loudly, rather than guessing — any
presentation whose first option is not one of its affirmative labels. So this
module offers exactly "Yes" and "No", with the detail in the descriptions, and
the third possibility (a different name) arrives as FREE TEXT. That is not a
workaround: free text on a yes/no question is measured to mean "something other
than what was offered", which is exactly what "call it something else" is.

ON TIMEOUT THIS DENIES. Creation is irreversible, so an unanswered read-back
must never age into consent: ``on_timeout='deny'`` writes a real denial through
the same compare-and-swap a human would use, and the waiting caller reads it the
usual way.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Literal

from jarvis import answers as ans
from jarvis.bus import publish
from jarvis.github.scopes import Capabilities, implied_reversibility, spoken_capability_line
from jarvis.ids import dedupe_key
from jarvis.project.slug import SlugError, slugify
from jarvis.requests import (
    Presentation,
    Request,
    create_request,
    get_request,
    make_presentation,
)

__all__ = [
    "CONFIRM_QUESTION",
    "NO_LABEL",
    "RENAME_QUESTION",
    "TIMEOUT_CANCEL_TEXT",
    "YES_LABEL",
    "Decision",
    "confirmation_payload",
    "confirmation_presentation",
    "decision",
    "raise_confirmation",
    "raise_rename",
    "readback",
    "spell",
    "spoken_name_from",
]

#: The affirmative must be option ONE and must be a label the existing Telegram
#: channel recognises; see the module docstring.
YES_LABEL = "Yes"
NO_LABEL = "No"

#: Short, because it is the key the answer is stored under and it is read back in
#: the briefing. The long verbatim text is the presentation's intro.
CONFIRM_QUESTION = "Shall I create it?"
RENAME_QUESTION = "What should I call it?"

#: Said when a read-back aged out with nobody answering. Only a fallback: the
#: spine writes its own denial sentence onto the row, and that one is preferred
#: because it is the same words the activity log shows.
TIMEOUT_CANCEL_TEXT = "Nobody answered that in time, so I created nothing."

#: How long the read-back waits before it becomes a denial. Long enough for the
#: user to have wandered off and come back, short enough that an unanswered
#: question does not sit pending forever pretending to be a live offer.
DEFAULT_EXPIRES_IN_S = 900

DecisionKind = Literal["create", "rename", "cancel", "unclear", "pending"]


@dataclass(frozen=True, slots=True)
class Decision:
    """What the human decided about one proposed repository.

    ``spoken`` is always populated, because every one of these outcomes is said
    out loud — including ``unclear``, which is the one a caller is most likely to
    forget and the one where silence would look like consent.
    """

    kind: DecisionKind
    request_id: str
    spoken: str
    spoken_name: str | None = None
    name: str | None = None

    @property
    def approved(self) -> bool:
        return self.kind == "create"


def spell(name: str) -> str:
    """``istanbul-takip`` -> ``i s t a n b u l, hyphen, t a k i p``.

    Said only when the slug is not what the spoken words would obviously produce.
    A user who says "İstanbul takip" cannot know whether they are getting
    ``istanbul`` or something with an invisible combining mark in it, and the one
    thing that settles it is hearing the letters.
    """
    return ", hyphen, ".join(" ".join(part) for part in name.split("-"))


def _unpredictable(spoken_name: str, name: str) -> bool:
    """Would a listener be surprised by this slug?

    The naive reading — lowercase the words and join them with hyphens — is what
    a user assumes happened. When the real slug differs, transliteration did
    something, and that is exactly when the letters have to be said.
    """
    naive = "-".join(spoken_name.lower().split())
    return naive != name


def readback(
    *,
    owner: str,
    name: str,
    spoken_name: str,
    caps: Capabilities,
    private: bool = True,
) -> str:
    """The verbatim sentence: owner, exact slug, visibility, and the undo truth.

    Refuses to describe a public repository: a repository this system creates is
    private and empty because that is the only safety property available when
    deletion is off the table, and a read-back that can SAY "public" is a
    read-back that will eventually be given one.
    """
    if not private:
        raise ValueError(
            "a repository Jarvis creates is private and empty; there is no read-back for a "
            "public one because there is no compensation for one either"
        )
    where = f"github.com/{owner}/{name}"
    spelled = f", spelled {spell(name)}" if _unpredictable(spoken_name, name) else ""
    return (
        f"I'll create {where}. Owner {owner}, repository name {name}{spelled}, "
        f"and it will be private and empty. {spoken_capability_line(caps, slug=name)}"
    )


def confirmation_presentation(
    *,
    owner: str,
    name: str,
    spoken_name: str,
    caps: Capabilities,
) -> Presentation:
    """The read-back as a Presentation. ``verbatim=True``: this text is not paraphrased."""
    return make_presentation(
        intro=readback(owner=owner, name=name, spoken_name=spoken_name, caps=caps),
        options=[
            {"label": YES_LABEL, "description": f"create {owner}/{name}, private and empty"},
            {"label": NO_LABEL, "description": "create nothing"},
        ],
        verbatim=True,
        multi=False,
        allows_free_text=True,
        free_text_prompt="Or say the name you want instead.",
        dtmf_map={"1": 1, "2": 2},
        question=CONFIRM_QUESTION,
    )


def confirmation_payload(
    *,
    owner: str,
    name: str,
    spoken_name: str,
    caps: Capabilities,
) -> dict[str, Any]:
    """What was asked, in the shape :mod:`jarvis.answers` can validate an answer against.

    The ``questions`` array is there so the arrived answer is checked by the
    spine's own validator rather than by a second one written here; the repository
    facts sit alongside it so the row is self-describing months later, when the
    only surviving record of what the user agreed to is this JSON.
    """
    return {
        "questions": [
            {
                "question": CONFIRM_QUESTION,
                "header": "new repo",
                "options": [
                    {"label": YES_LABEL, "description": f"create {owner}/{name}"},
                    {"label": NO_LABEL, "description": "create nothing"},
                ],
            }
        ],
        "owner": owner,
        "name": name,
        "spoken_name": spoken_name,
        "private": True,
        "auto_init": False,
        "readback": readback(owner=owner, name=name, spoken_name=spoken_name, caps=caps),
        # The measured matrix, verbatim, including its notes and the source it
        # was read from: six months later "why did it say that" is answerable
        # from the row rather than from a guess about which token was in the
        # keyring that day.
        "capabilities": caps.as_dict(),
    }


def raise_confirmation(
    con: sqlite3.Connection,
    *,
    owner: str,
    name: str,
    spoken_name: str,
    caps: Capabilities,
    actor: str,
    job_id: str | None = None,
    expires_in_s: int | None = DEFAULT_EXPIRES_IN_S,
) -> Request:
    """Create (or find) the ``confirm_effect`` row for this exact repository.

    Idempotent through the dedupe key, which is hashed over the owner and the
    slug: a desk that asks twice about the same repository — because it restarted,
    or because the user said it again — must find the answer already waiting
    rather than ask a second time.
    """
    payload = confirmation_payload(owner=owner, name=name, spoken_name=spoken_name, caps=caps)
    key = dedupe_key(job_id, "project.confirm_repo", {"owner": owner, "name": name})
    req = create_request(
        con,
        kind="confirm_effect",
        short_label="new repo",
        presentation=confirmation_presentation(
            owner=owner, name=name, spoken_name=spoken_name, caps=caps
        ),
        payload=payload,
        actor=actor,
        job_id=job_id,
        urgency="normal",
        # From the class, not from an opinion: the gate's strength is whatever
        # effects says this kind of side effect deserves.
        reversibility=implied_reversibility(caps),
        expires_in_s=expires_in_s,
        # Creation is irreversible. An unanswered read-back becomes a DENIAL,
        # never a default and never a silent hang.
        on_timeout="deny",
        dedupe_key=key,
    )
    publish(
        con,
        "request.created",
        actor,
        {
            "kind": "confirm_effect",
            "short_label": req.short_label,
            "owner": owner,
            "name": name,
            "readback": payload["readback"],
        },
        job_id=job_id,
        request_id=req.id,
        idem_key=f"req:{req.id}:created",
    )
    return req


def raise_rename(
    con: sqlite3.Connection,
    *,
    owner: str,
    rejected: str,
    reason: str,
    actor: str,
    job_id: str | None = None,
    expires_in_s: int | None = DEFAULT_EXPIRES_IN_S,
) -> Request:
    """Ask for a different name, in the user's own words. No options, on purpose.

    A list of generated alternatives would be this module choosing the name after
    all. ``free_text`` with no options is the shape that means "say what you
    want", and the answer arrives in ``Answer['text']``.
    """
    intro = f"{reason} {RENAME_QUESTION}"
    pres = make_presentation(
        intro=intro,
        options=(),
        verbatim=True,
        multi=False,
        allows_free_text=True,
        free_text_prompt="Say the name you want.",
        question=RENAME_QUESTION,
    )
    key = dedupe_key(job_id, "project.rename_repo", {"owner": owner, "rejected": rejected})
    req = create_request(
        con,
        kind="free_text",
        short_label="the repo name",
        presentation=pres,
        payload={"owner": owner, "rejected": rejected, "reason": reason},
        actor=actor,
        job_id=job_id,
        # Nothing has been created, so an unanswered question can simply wait:
        # 'defer' keeps it alive for an answer on any channel, in the morning.
        on_timeout="defer",
        expires_in_s=expires_in_s,
        dedupe_key=key,
    )
    publish(
        con,
        "request.created",
        actor,
        {"kind": "free_text", "short_label": req.short_label, "owner": owner, "taken": rejected},
        job_id=job_id,
        request_id=req.id,
        idem_key=f"req:{req.id}:created",
    )
    return req


def spoken_name_from(req: Request) -> str | None:
    """The words the user said in answer to a rename request, or None.

    ``Answer['text']`` rather than ``answers``: the rename question has no
    options, so there is no frozen array to look a label up in and the user's own
    words ARE the answer.
    """
    answer = req.answer or {}
    words = answer.get("text")
    if isinstance(words, str) and words.strip():
        return " ".join(words.split())
    picked = answer.get("answers")
    if isinstance(picked, dict):
        value = picked.get(RENAME_QUESTION)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    return None


def decision(con: sqlite3.Connection, request_id: str) -> Decision:
    """Read a confirmation row and say what it means. Never guesses consent.

    The three ways this refuses to decide are the point of it:

    * a still-pending row is ``pending``, not "no";
    * an answer whose ``approved`` flag and whose chosen label disagree is
      ``unclear`` — the same refusal the Telegram channel makes on the way in,
      because a presentation whose first option is not an affirmative would
      otherwise invert consent silently;
    * free text that is not a usable repository name is ``unclear`` and says so,
      rather than being treated as a yes or as a name nobody can predict.
    """
    req = get_request(con, request_id)
    if req is None:
        raise KeyError(request_id)
    if req.state == "pending":
        return Decision("pending", req.id, "I'm still waiting on you for that one.")
    if req.state in ("expired", "cancelled", "superseded"):
        return Decision(
            "cancel",
            req.id,
            "That one lapsed before it was answered, so I created nothing.",
        )

    answer = req.answer or {}
    approved = answer.get("approved")
    label = _label_of(req, answer)

    if req.answer_mode == "timeout":
        # NOBODY ANSWERED. The spine writes its own denial text into `text`, and
        # reading that as free text would slug the sentence "Nobody was reachable
        # before the deadline…" into a repository NAME — a timeout creating a
        # repository is the worst outcome this module could produce, and it is one
        # line of code away at all times.
        said = answer.get("text")
        return Decision(
            "cancel",
            req.id,
            str(said) if isinstance(said, str) and said.strip() else TIMEOUT_CANCEL_TEXT,
        )

    words = _free_text_of(req, answer)

    if label is not None and approved is not None:
        agrees = (label == YES_LABEL) == bool(approved)
        if not agrees:
            return Decision(
                "unclear",
                req.id,
                "I got a yes and a no for the same question, so I haven't created anything. "
                "Say it again.",
            )

    if approved is True or (approved is None and label == YES_LABEL):
        return Decision(
            "create",
            req.id,
            "Right, creating it now.",
            name=_payload_name(req),
        )

    if words is not None:
        try:
            name = slugify(words)
        except SlugError as exc:
            return Decision("unclear", req.id, exc.spoken, spoken_name=words)
        return Decision(
            "rename",
            req.id,
            f"Right — {name} instead.",
            spoken_name=words,
            name=name,
        )

    return Decision("cancel", req.id, "Fine, I've created nothing.")


def _payload_name(req: Request) -> str | None:
    name = req.payload.get("name")
    return str(name) if isinstance(name, str) and name else None


def _label_of(req: Request, answer: dict[str, Any]) -> str | None:
    """The label the user picked, by lookup against the frozen array. Never parsed."""
    picked = answer.get("answers")
    if not isinstance(picked, dict):
        return None
    value = picked.get(CONFIRM_QUESTION)
    if not isinstance(value, str):
        return None
    labels = {str(item["label"]) for item in req.presentation.get("items") or []}
    if value not in labels:
        return None
    # The spine's own validator, on an answer that arrived from another process
    # hours later: a Telegram bot, a phone leg and a repair script all write this
    # column and none of them imports this module.
    ans.validate_answers(req.payload, {CONFIRM_QUESTION: value})
    return value


def _free_text_of(req: Request, answer: dict[str, Any]) -> str | None:
    """The user's own words, when they answered with something not on offer."""
    sources = answer.get("sources")
    picked = answer.get("answers")
    said_own_words = isinstance(sources, dict) and sources.get(CONFIRM_QUESTION) == "free_text"
    if said_own_words and isinstance(picked, dict):
        value = picked.get(CONFIRM_QUESTION)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    if _label_of(req, answer) is not None:
        return None
    text = answer.get("text")
    if isinstance(text, str) and text.strip():
        return " ".join(text.split())
    return None
