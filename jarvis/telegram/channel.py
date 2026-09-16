"""The channel proper: present a Presentation, post an Answer. Nothing else.

A channel has exactly two jobs and this module is deliberately shaped so that
both are visible in it:

    present -> render a Presentation and send it
    answer  -> turn a tap into an Answer and put it through answer_request

The second one is the interesting half. ``answer_request`` is a compare-and-swap
on ``state='pending'``, so a tap here and a spoken reply at the desk race through
the SAME update with no lock and no leader, and exactly one wins. THE LOSER MUST
SAY SO. A button left live under a question that was answered by voice is worse
than a button that does nothing: it invites a second decision on a settled
question and then silently discards it. So a lost race edits the message to say
where the answer came from, and the buttons go away.

WHERE THE ANSWER SHAPE COMES FROM, which is the one place this module is allowed
to know anything about requests: a plan question's answer is keyed by the exact
question STRING and a multi-question batch numbers its options across the whole
batch, so building it from indices is :func:`jarvis.cc.narrate.answer`'s job and
not this module's. That import is pure — ``jarvis.cc.narrate`` is stdlib plus the
spine, with no SDK and nothing from the desk — and duplicating it here is how the
two channels would eventually disagree about what "option three" means.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from jarvis import requests as rq
from jarvis.bus import publish
from jarvis.cc import narrate
from jarvis.telegram import render
from jarvis.telegram.transport import TelegramError, Transport, TransportError

__all__ = [
    "AFFIRMATIVE_LABELS",
    "APPROVE_INDEX",
    "BOOLEAN_KINDS",
    "NEGATIVE_LABELS",
    "AmbiguousApproval",
    "Outcome",
    "TelegramChannel",
    "build_answer",
    "needs_confirm",
]

#: Kinds whose answer the Claude Code driver reads as a BOOLEAN. For these the
#: option labels are only a rendering; ``approved`` is the load-bearing field.
BOOLEAN_KINDS = frozenset({"exit_plan", "tool_permission", "confirm_effect"})

#: ``jarvis.cc.gate`` builds those presentations with the affirmative FIRST.
#: Nothing in the Presentation itself says which option means yes — see the
#: report on this stage — so the index and the label are BOTH checked and a
#: disagreement raises rather than resolving to one of them. If the frozen option
#: array in gate.py is ever reordered, this turns a silently inverted approval
#: into a refusal to answer at all.
APPROVE_INDEX = 1
AFFIRMATIVE_LABELS = frozenset({"Approve", "Allow", "Yes"})
NEGATIVE_LABELS = frozenset({"Keep planning", "Deny", "No"})


class AmbiguousApproval(ValueError):
    """A yes/no request whose options do not say which one is yes."""


@dataclass(frozen=True, slots=True)
class Outcome:
    """What one update did. ``won`` is None when no race was entered."""

    handled: bool
    note: str
    request_id: str | None = None
    won: bool | None = None


def needs_confirm(req: rq.Request) -> bool:
    """Whether this question needs toggle-then-confirm rather than one tap.

    True for multi-select, obviously. Also true for a multi-QUESTION batch: the
    options there are numbered across the whole batch, so one tap answers one
    question and leaves the others unanswered, which
    :func:`jarvis.cc.narrate.answers_from_indices` correctly refuses. A keyboard
    that cannot complete an answer must not look like it can.
    """
    if bool(req.presentation.get("multi")):
        return True
    if req.kind != "plan_question":
        return False
    try:
        return len(narrate.questions_of(req.payload)) > 1
    except narrate.MalformedQuestions:
        return False


def build_answer(
    req: rq.Request,
    *,
    picks: tuple[int, ...] = (),
    free_text: str | None = None,
) -> rq.Answer:
    """Indices (or the user's own words) in, a spine :class:`Answer` out. PURE.

    Free text on a yes/no request is never an approval. Somebody who types
    instead of tapping is asking for something other than what was offered, and
    reading that as consent is the single worst failure this channel could have.
    """
    pres = req.presentation
    if req.kind == "plan_question":
        # The batch's own grammar: keyed by question string, list for multiSelect.
        return narrate.answer(req.payload, list(picks), free_text)

    if free_text is not None:
        words = free_text.strip()
        if not words:
            raise narrate.AnswerShapeError("an empty reply is not an answer")
        answer: rq.Answer = {"text": words}
        if req.kind in BOOLEAN_KINDS:
            answer["approved"] = False
        question = pres.get("question")
        if question:
            answer["answers"] = {str(question): words}
            answer["sources"] = {str(question): "free_text"}
        return answer

    if not picks:
        raise narrate.AnswerShapeError("no option was picked")
    labels = rq.labels_for_indices(pres, list(picks))
    multi = bool(pres.get("multi"))
    if not multi and len(labels) != 1:
        raise narrate.AnswerShapeError(
            f"{req.short_label} takes one option; {len(labels)} were picked"
        )

    out: rq.Answer = {"text": "; ".join(labels)}
    if req.kind in BOOLEAN_KINDS:
        out["approved"] = _approval(pres, picks[0])
    question = pres.get("question")
    if question:
        out["answers"] = {str(question): labels if multi else labels[0]}
        out["sources"] = {str(question): "option"}
    return out


def _approval(pres: rq.Presentation, index: int) -> bool:
    label = rq.label_for_index(pres, index)
    if label in AFFIRMATIVE_LABELS:
        approved = True
    elif label in NEGATIVE_LABELS:
        approved = False
    else:
        raise AmbiguousApproval(
            f"{label!r} is neither an approval nor a refusal I recognise, "
            "so I will not decide on your behalf"
        )
    if approved != (index == APPROVE_INDEX):
        raise AmbiguousApproval(
            f"option {index} is {label!r}: the label and the position disagree about "
            "which choice means yes"
        )
    return approved


@dataclass(frozen=True, slots=True)
class TelegramChannel:
    """One bound chat. Holds no connection and no transport state.

    Frozen and connection-free on purpose (house rule 3): every method takes the
    caller's open connection, so the same object is safe in the poll loop, in a
    delivery tick and in a test that drives two connections at once.
    """

    chat_id: int
    actor: str = "telegram"

    # ───────────────────────── presenting ─────────────────────────

    def present(
        self,
        con: sqlite3.Connection,
        transport: Transport,
        req: rq.Request,
        *,
        delivery_id: str | None = None,
    ) -> int:
        """Send the question. Returns the Telegram message id.

        The ``request.offered`` event carries the message id, and that event IS
        the message-to-request map: a reply arriving three hours later is matched
        against it. Keeping the map in the activity log rather than in a new
        column means "which message asked which question" is answerable by the
        same query that answers everything else about that evening.
        """
        message = render.render_request(
            req.presentation, req.id, require_confirm=needs_confirm(req)
        )
        result = transport.call(
            "sendMessage",
            {
                "chat_id": self.chat_id,
                "text": message.text,
                "reply_markup": message.markup(),
            },
        )
        message_id = int(result["message_id"])
        publish(
            con,
            "request.offered",
            self.actor,
            {
                "channel": "telegram",
                "chat_id": self.chat_id,
                "message_id": message_id,
                "short_label": req.short_label,
            },
            job_id=req.job_id,
            request_id=req.id,
            idem_key=f"req:{req.id}:offered:telegram:{message_id}",
        )
        if delivery_id is not None:
            rq.mark_presented(con, delivery_id, self.actor)
        return message_id

    def claim_and_present(
        self,
        con: sqlite3.Connection,
        transport: Transport,
        *,
        now_ts: str | None = None,
        lease_s: int = 60,
        limit: int = 20,
    ) -> list[str]:
        """Take every Telegram delivery that is due and ask it. Returns request ids.

        Claim first, present second, and never the other way round: two bots (an
        old process that has not noticed it was replaced) must not both send the
        same question, and the lease is what makes that recoverable when one of
        them dies between the two.
        """
        asked: list[str] = []
        for delivery in rq.due_deliveries(con, now_ts, limit=limit):
            if delivery.channel_kind != "telegram":
                continue
            if not rq.claim_delivery(con, delivery.id, self.actor, lease_s=lease_s):
                continue
            req = rq.get_request(con, delivery.request_id)
            if req is None or req.state != "pending":
                # Settled between the sweep and the claim. answer_request already
                # settled the row; nothing to send and nothing to repair.
                continue
            try:
                self.present(con, transport, req, delivery_id=delivery.id)
            except (TelegramError, TransportError) as e:
                rq.fail_delivery(con, delivery.id, f"{type(e).__name__}: {e}")
                continue
            asked.append(req.id)
        return asked

    # ───────────────────────── answering ─────────────────────────

    def on_callback(
        self, con: sqlite3.Connection, transport: Transport, query: dict[str, Any]
    ) -> Outcome:
        """One button press."""
        query_id = str(query.get("id") or "")
        message = query.get("message") or {}
        message_id = message.get("message_id")
        try:
            cb = render.decode_callback(str(query.get("data") or ""))
        except render.CallbackFormatError:
            self._toast(transport, query_id, "That button is from an older version.")
            return Outcome(False, "undecodable callback")

        req = rq.get_request(con, cb.request_id)
        if req is None:
            self._toast(transport, query_id, "I no longer have that question.")
            return Outcome(False, "unknown request", request_id=cb.request_id)
        if req.state != "pending":
            self._settle_ui(con, transport, req, message_id, query_id)
            return Outcome(True, f"already {req.state}", request_id=req.id, won=False)

        if cb.verb == "toggle":
            return self._toggle(transport, req, message, message_id, query_id, cb.index)
        if cb.verb == "free":
            self._toast(transport, query_id, "Reply to that message with your answer.")
            return Outcome(True, "free text invited", request_id=req.id)
        if cb.verb == "confirm":
            picks = render.selected_from_markup(message.get("reply_markup"))
            if not picks:
                self._toast(transport, query_id, "Tick at least one option first.")
                return Outcome(True, "nothing selected", request_id=req.id)
        else:
            picks = (cb.index,)

        return self._submit(
            con, transport, req, picks=picks, message_id=message_id, query_id=query_id
        )

    def on_reply(
        self, con: sqlite3.Connection, transport: Transport, message: dict[str, Any]
    ) -> Outcome:
        """A typed reply to a question this channel asked. The "none of these" path."""
        replied_to = (message.get("reply_to_message") or {}).get("message_id")
        if replied_to is None:
            return Outcome(False, "not a reply")
        request_id = request_for_message(con, self.chat_id, int(replied_to))
        if request_id is None:
            return Outcome(False, "reply names no question")
        req = rq.get_request(con, request_id)
        if req is None:
            return Outcome(False, "unknown request", request_id=request_id)
        if req.state != "pending":
            self._settle_ui(con, transport, req, int(replied_to), None)
            return Outcome(True, f"already {req.state}", request_id=req.id, won=False)
        return self._submit(
            con,
            transport,
            req,
            free_text=str(message.get("text") or ""),
            message_id=int(replied_to),
        )

    # ───────────────────────── plumbing ─────────────────────────

    def _submit(
        self,
        con: sqlite3.Connection,
        transport: Transport,
        req: rq.Request,
        *,
        picks: tuple[int, ...] = (),
        free_text: str | None = None,
        message_id: int | None = None,
        query_id: str | None = None,
    ) -> Outcome:
        try:
            answer = build_answer(req, picks=picks, free_text=free_text)
        except (narrate.AnswerShapeError, rq.OptionIndexError, AmbiguousApproval) as e:
            # The question stays PENDING. A shape this channel cannot build is not
            # a decision, and recording one would answer on the user's behalf.
            self._say(transport, f"I could not use that answer: {e}")
            self._toast(transport, query_id, "I could not use that answer.")
            return Outcome(False, str(e), request_id=req.id)

        won = rq.answer_request(
            con,
            req.id,
            answer,
            answered_by=f"telegram:{self.chat_id}",
            # ANSWER_MODES has no value for "typed into a chat", so a free-text
            # reply is recorded as the channel's own mode. The truth survives in
            # sources={...: 'free_text'} and in answer['text']; see the report.
            answer_mode="button",
        )
        fresh = rq.get_request(con, req.id) or req
        if not won:
            self._settle_ui(con, transport, fresh, message_id, query_id)
            return Outcome(True, "another channel answered first", request_id=req.id, won=False)

        publish(
            con,
            "request.answered",
            self.actor,
            {
                "channel": "telegram",
                "chat_id": self.chat_id,
                "picks": list(picks),
                "free_text": free_text is not None,
            },
            job_id=req.job_id,
            request_id=req.id,
            idem_key=f"req:{req.id}:answered",
        )
        self._edit(
            con,
            transport,
            fresh,
            message_id,
            _chosen_note(req, picks, free_text),
        )
        self._toast(transport, query_id, "Got it.")
        return Outcome(True, "answered", request_id=req.id, won=True)

    def _toggle(
        self,
        transport: Transport,
        req: rq.Request,
        message: dict[str, Any],
        message_id: int | None,
        query_id: str,
        index: int,
    ) -> Outcome:
        current = set(render.selected_from_markup(message.get("reply_markup")))
        current.symmetric_difference_update({index})
        redrawn = render.render_request(
            req.presentation,
            req.id,
            require_confirm=True,
            selected=tuple(sorted(current)),
        )
        if message_id is not None:
            self._quiet(
                transport,
                "editMessageReplyMarkup",
                {
                    "chat_id": self.chat_id,
                    "message_id": message_id,
                    "reply_markup": redrawn.markup(),
                },
            )
        self._toast(transport, query_id, "")
        return Outcome(True, f"{len(current)} selected", request_id=req.id)

    def _settle_ui(
        self,
        con: sqlite3.Connection,
        transport: Transport,
        req: rq.Request,
        message_id: int | None,
        query_id: str | None,
    ) -> None:
        """Take the buttons away and say who decided. The loser saying so aloud."""
        note = _settled_note(req)
        self._edit(con, transport, req, message_id, note)
        self._toast(transport, query_id, note)

    def _edit(
        self,
        con: sqlite3.Connection,
        transport: Transport,
        req: rq.Request,
        message_id: int | None,
        note: str,
    ) -> None:
        if message_id is None:
            self._say(transport, note)
            return
        message = render.settled_message(req.presentation, note)
        self._quiet(
            transport,
            "editMessageText",
            {
                "chat_id": self.chat_id,
                "message_id": message_id,
                "text": message.text,
                "reply_markup": message.markup(),
            },
        )

    def _say(self, transport: Transport, text: str) -> None:
        self._quiet(transport, "sendMessage", {"chat_id": self.chat_id, "text": text})

    def _toast(self, transport: Transport, query_id: str | None, text: str) -> None:
        """``answerCallbackQuery``: without it the client spins for a minute."""
        if not query_id:
            return
        self._quiet(transport, "answerCallbackQuery", {"callback_query_id": query_id, "text": text})

    @staticmethod
    def _quiet(transport: Transport, method: str, params: dict[str, Any]) -> None:
        """Cosmetics must never undo a decision that is already committed.

        ``editMessageText`` fails with 400 for a message that is unchanged or
        older than 48 hours, and letting that propagate after ``answer_request``
        has committed would make a successful answer look like a failure and
        invite a retry.
        """
        try:
            transport.call(method, params)
        except (TelegramError, TransportError):
            return


def request_for_message(con: sqlite3.Connection, chat_id: int, message_id: int) -> str | None:
    """Which question a given chat message asked, from the activity log.

    Reads backwards through ``request.offered`` because the newest match is the
    live one: re-presenting a question after a restart writes a second event, and
    a reply belongs to the message the user is actually looking at.
    """
    rows = con.execute(
        "SELECT request_id, payload FROM events WHERE kind='request.offered'"
        " ORDER BY seq DESC LIMIT 500"
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(str(row["payload"]))
        except ValueError:
            continue
        if payload.get("channel") != "telegram":
            continue
        if int(payload.get("message_id") or 0) != int(message_id):
            continue
        if int(payload.get("chat_id") or 0) != int(chat_id):
            continue
        return str(row["request_id"]) if row["request_id"] else None
    return None


def _chosen_note(req: rq.Request, picks: tuple[int, ...], free_text: str | None) -> str:
    if free_text is not None:
        return f"✓ You replied: {free_text.strip()}"
    try:
        labels = rq.labels_for_indices(req.presentation, list(picks))
    except rq.OptionIndexError:
        return "✓ Answered from Telegram."
    return "✓ You chose: " + ", ".join(labels)


def _settled_note(req: rq.Request) -> str:
    if req.state in ("answered", "consumed"):
        who = req.answered_by or "another channel"
        if who.startswith("telegram:"):
            return "✓ Already answered here."
        if who == "timeout":
            return "Nobody answered in time, so this was decided by the timeout rule."
        return f"✓ Answered at the {who} before this tap landed."
    return f"That question is no longer open ({req.state})."
