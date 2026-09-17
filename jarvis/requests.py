"""THE unified gate: every human decision this system needs, as one row.

A plan-mode question, an ``ExitPlanMode`` approval, a Bash permission, an
effect confirmation, a tidied-prompt read-back, "is now a good time for your
briefing", "did the restaurant have the slot" — these are the same object. They
are raised by one process and answered by another, on a different channel,
possibly hours later, after the asker has died. So they are one table with one
lifecycle:

    pending -> answered -> consumed
    pending -> expired | cancelled | superseded

Four properties are load-bearing and each has a test:

*Creation is idempotent.* Spike S1 measured that ``tool_use_id`` is stable
across defer and resume, so re-creating the same question returns the EXISTING
row rather than raising. That is the whole resume path.

*Answering is a compare-and-swap.* ``UPDATE ... WHERE state='pending'
RETURNING`` is the entire mechanism by which desk, Telegram and phone race
safely. There is no lock and no leader. The loser is told it lost; it never
fails silently.

*Consumption is idempotent.* A resumed driver re-fires the question and asks
again; consuming twice returns the same answer and is not an error.

*Timeout has a NAMED outcome.* ``on_timeout`` is a typed enum precisely so the
zero-reachable-channel case resolves to something a human can read
(defer/deny/default/escalate) instead of hanging forever.

Nothing here talks to a channel, decides routing, or speaks. It writes rows.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal, NotRequired, TypedDict

from jarvis.db import tx
from jarvis.ids import dedupe_key as _dedupe_key
from jarvis.ids import nid, now, parse_ts

__all__ = [
    "Answer",
    "Delivery",
    "Expiry",
    "Item",
    "OptionIndexError",
    "Presentation",
    "Request",
    "UnknownRequest",
    "answer_request",
    "cancel_request",
    "claim_delivery",
    "consume",
    "create_request",
    "due_deliveries",
    "expire_due",
    "fail_delivery",
    "find_answer",
    "find_open_for_tool",
    "get_request",
    "index_for_dtmf",
    "label_for_index",
    "labels_for_indices",
    "make_presentation",
    "mark_presented",
    "next_attempt",
    "open_requests",
    "requests_for_job",
    "schedule_delivery",
    "supersede_request",
]

# ───────────────────────────── the vocabulary ─────────────────────────────

ReqKind = Literal[
    "plan_question",
    "exit_plan",
    "tool_permission",
    "confirm_effect",
    "readback",
    "briefing_gate",
    "free_text",
]
ReqState = Literal["pending", "answered", "consumed", "expired", "cancelled", "superseded"]
Urgency = Literal["low", "normal", "high", "critical"]
Reversibility = Literal["reversible", "compensatable", "irreversible"]
OnTimeout = Literal["defer", "deny", "default", "escalate"]
AnswerMode = Literal["voice", "dtmf", "button", "hud", "timeout"]
DeliveryState = Literal[
    "scheduled", "claimed", "presented", "answered", "released", "failed", "skipped"
]
Outcome = Literal["deferred", "denied", "defaulted", "escalated", "expired"]

REQ_KINDS: frozenset[str] = frozenset(
    (
        "plan_question",
        "exit_plan",
        "tool_permission",
        "confirm_effect",
        "readback",
        "briefing_gate",
        "free_text",
    )
)
URGENCIES: frozenset[str] = frozenset(("low", "normal", "high", "critical"))
REVERSIBILITIES: frozenset[str] = frozenset(("reversible", "compensatable", "irreversible"))
ON_TIMEOUTS: frozenset[str] = frozenset(("defer", "deny", "default", "escalate"))
ANSWER_MODES: frozenset[str] = frozenset(("voice", "dtmf", "button", "hud", "timeout"))

#: Deliveries that have not reached a terminal state yet. Answering a request
#: settles all of them at once, so a second channel never presents a question
#: that has already been decided.
_OPEN_DELIVERY_STATES = ("scheduled", "claimed", "presented")

#: Spoken by the router when nothing could reach a human in time. Stored on the
#: row so the denial the driver returns to Claude Code is the same sentence the
#: activity log shows.
TIMEOUT_DENY_TEXT = "Nobody was reachable before the deadline, so this was denied, not assumed."


class OptionIndexError(LookupError):
    """An option index that does not exist in the presentation.

    Its own type because the alternative — guessing, or falling back to the
    first option — silently builds the wrong thing and nobody ever notices.
    """


class UnknownRequest(KeyError):
    """No such request id. A typo or a stale id, never a lost race."""


# ───────────────────────────── the wire format ─────────────────────────────


class Item(TypedDict):
    """One rendered option. ``index`` is OURS, assigned from payload order."""

    index: int
    label: str
    description: str


class Answer(TypedDict):
    """What a human decided. Keys are exactly these four; see ``_check_answer``."""

    answers: NotRequired[dict[str, str | list[str]]]
    approved: NotRequired[bool]
    text: NotRequired[str]
    sources: NotRequired[dict[str, str]]


class Presentation(TypedDict):
    """The rendering payload: a verbatim flag and the ordered items.

    ``verbatim=True`` means this text MUST go to the deterministic reader and
    never through a generative model.

    ``default_answer`` and ``question`` are additions to the doc's shape and are
    both optional. ``default_answer`` is what makes ``on_timeout='default'``
    honest: the default is written down by whoever built the options, from the
    same frozen array, rather than invented at timeout by picking option one.
    """

    verbatim: bool
    intro: str
    items: list[Item]
    multi: bool
    allows_free_text: bool
    free_text_prompt: str
    dtmf_map: NotRequired[dict[str, int] | None]
    image: NotRequired[str | None]
    question: NotRequired[str]
    default_answer: NotRequired[Answer]


@dataclass(frozen=True, slots=True)
class Request:
    """One row of ``requests``, with the JSON columns already parsed."""

    id: str
    kind: ReqKind
    state: ReqState
    short_label: str
    presentation: Presentation
    payload: dict[str, Any]
    dedupe_key: str
    attempt: int
    urgency: Urgency
    created_at: str
    escalate_after_s: int
    on_timeout: OnTimeout
    job_id: str | None = None
    tool_use_id: str | None = None
    reversibility: Reversibility | None = None
    expires_at: str | None = None
    answer: Answer | None = None
    answered_at: str | None = None
    answered_by: str | None = None
    answer_mode: str | None = None
    consumed_at: str | None = None


@dataclass(frozen=True, slots=True)
class Delivery:
    """One row of ``deliveries``: this request, on this channel, this attempt."""

    id: str
    request_id: str
    channel_kind: str
    attempt: int
    due_at: str
    state: DeliveryState
    channel_id: str | None = None
    claimed_by: str | None = None
    claim_expires_at: str | None = None
    presented_at: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class Expiry:
    """What ``expire_due`` actually did to one row, so the caller can say it."""

    request_id: str
    job_id: str | None
    on_timeout: OnTimeout
    outcome: Outcome
    state: ReqState
    reason: str


# ───────────────────────────── presentation ─────────────────────────────


def make_presentation(
    *,
    intro: str,
    options: list[Any] | tuple[Any, ...] = (),
    verbatim: bool = True,
    multi: bool = False,
    allows_free_text: bool = True,
    free_text_prompt: str = "",
    dtmf_map: dict[str, int] | None = None,
    image: str | None = None,
    question: str | None = None,
    default_answer: Answer | None = None,
) -> Presentation:
    """Build a Presentation, numbering the options 1..n from payload order.

    The numbering is generated HERE, locally, and the labels are copied through
    untouched — not stripped, not title-cased, not translated. The model may
    emit an index; it may never emit a label. That is what makes reordering,
    merging or inventing an option structurally impossible rather than
    prompt-hoped.
    """
    items: list[Item] = []
    for i, opt in enumerate(options, start=1):
        if isinstance(opt, str):
            label, description = opt, ""
        elif isinstance(opt, dict):
            label = opt["label"]
            description = opt.get("description", "")
        else:
            raise TypeError(f"option {i} must be a str or a mapping with 'label', got {type(opt)}")
        if not isinstance(label, str) or not isinstance(description, str):
            raise TypeError(f"option {i}: label and description must be str")
        items.append({"index": i, "label": label, "description": description})

    pres: Presentation = {
        "verbatim": verbatim,
        "intro": intro,
        "items": items,
        "multi": multi,
        "allows_free_text": allows_free_text,
        "free_text_prompt": free_text_prompt,
        "dtmf_map": dtmf_map,
        "image": image,
    }
    if question is not None:
        pres["question"] = question
    if default_answer is not None:
        _check_answer(default_answer)
        pres["default_answer"] = default_answer
    if dtmf_map is not None:
        n = len(items)
        bad = sorted(d for d, ix in dtmf_map.items() if not 1 <= ix <= n)
        if bad:
            raise OptionIndexError(f"dtmf_map points at missing options: {bad}")
    return pres


def label_for_index(pres: Presentation, index: int) -> str:
    """Map ONE option index back to its exact label. Pure; raises on a miss."""
    items = pres["items"]
    if not isinstance(index, int) or isinstance(index, bool):
        raise OptionIndexError(f"option index must be an int, got {index!r}")
    # 1-based against the frozen array. Off-by-one here maps "one" to the second
    # option and the build is quietly wrong, so it is a raise and not a clamp.
    if not 1 <= index <= len(items):
        raise OptionIndexError(f"option {index} does not exist; there are {len(items)}")
    return items[index - 1]["label"]


def labels_for_indices(pres: Presentation, indices: list[int] | tuple[int, ...]) -> list[str]:
    """Map option indices back to exact labels, in the order given.

    Order is preserved because a spoken "three and one" means three and one.
    An out-of-range index raises: never guess, never return the first option.
    """
    return [label_for_index(pres, i) for i in indices]


def index_for_dtmf(pres: Presentation, digit: str) -> int:
    """Map a pressed digit to an option index via the presentation's own map.

    Lives here rather than in the phone channel because stage 6 must not touch
    this file: the guarantee "a keypress can only ever select an option that was
    actually offered" has to exist before the channel that needs it does.
    """
    mapping = pres.get("dtmf_map") or {}
    if digit not in mapping:
        raise OptionIndexError(f"digit {digit!r} is not mapped to an option")
    index = mapping[digit]
    label_for_index(pres, index)  # raises if the map outlived the option it points at
    return index


# ───────────────────────────── create ─────────────────────────────


def create_request(
    con: sqlite3.Connection,
    *,
    kind: ReqKind,
    short_label: str,
    presentation: Presentation,
    payload: dict[str, Any],
    actor: str,
    job_id: str | None = None,
    tool_use_id: str | None = None,
    tool_name: str | None = None,
    urgency: Urgency = "normal",
    reversibility: Reversibility | None = None,
    expires_in_s: int | None = None,
    escalate_after_s: int = 90,
    on_timeout: OnTimeout = "defer",
    dedupe_key: str | None = None,
    attempt: int = 1,
) -> Request:
    """Create the row, or return the one that already represents this question.

    Idempotent by ``tool_use_id`` first (S1: it is stable across defer and
    resume) and by ``UNIQUE(job_id, dedupe_key, attempt)`` second. Creating the
    same question twice returns the EXISTING row rather than raising, because
    that is not an error — it is a resumed driver re-firing a question whose
    answer may already be sitting in the database.

    ``attempt`` stays 1 unless the caller deliberately bumps it with
    :func:`next_attempt`. A genuine second ask gets a new row; a re-fire reuses
    the old one, and the attempt counter is the only thing that tells them
    apart.

    ``tool_name`` is what the dedupe key is actually hashed over, because the
    design fixes the key as ``sha256(job_id ‖ tool_name ‖ canonical_json(
    tool_input))`` and :func:`find_open_for_tool` computes it that way. For a
    tool-driven request (``AskUserQuestion``, ``ExitPlanMode``, a Bash
    permission) it MUST be passed, or the row is stored under a key the lookup
    will never compute and the dedupe leg silently never hits — which is only
    visible as "the resumed driver asked the user again". It falls back to
    ``kind`` for requests no tool raised (a briefing gate, a read-back), where
    nothing computes the key from a tool name.

    ``actor`` is required but is NOT stored: the requests table has no column
    for the asker. It belongs on the ``request.created`` bus event, which is the
    caller's to publish. It is validated here so the caller cannot forget it and
    discover the omission only when reading the activity log.
    """
    if not actor:
        raise ValueError("actor is required: an unattributed question is unauditable")
    if kind not in REQ_KINDS:
        raise ValueError(f"unknown request kind {kind!r}")
    if urgency not in URGENCIES:
        raise ValueError(f"unknown urgency {urgency!r}")
    if on_timeout not in ON_TIMEOUTS:
        raise ValueError(f"unknown on_timeout {on_timeout!r}")
    if reversibility is not None and reversibility not in REVERSIBILITIES:
        raise ValueError(f"unknown reversibility {reversibility!r}")
    if attempt < 1:
        raise ValueError("attempt is 1-based")
    if not short_label:
        raise ValueError("short_label is spoken aloud; it cannot be empty")
    _check_presentation(presentation)
    if not isinstance(payload, dict):
        raise TypeError("payload must be a dict; it is echoed back to Claude Code verbatim")

    key = dedupe_key if dedupe_key is not None else _dedupe_key(job_id, tool_name or kind, payload)
    ts = now()
    expires_at = _plus(ts, expires_in_s) if expires_in_s is not None else None

    # BEGIN IMMEDIATE takes the write lock before the lookup, so the
    # check-then-insert cannot interleave with another process doing the same:
    # the loser waits on busy_timeout and then finds the row it was about to
    # create. Without this, two resuming drivers could both miss and both
    # insert, and one would die on the partial unique index instead of getting
    # the answer that was already waiting for it.
    with tx(con):
        existing = _lookup_existing(con, tool_use_id, job_id, key, attempt)
        if existing is not None:
            return existing
        rid = nid("req")
        con.execute(
            """INSERT INTO requests
                 (id, job_id, kind, state, urgency, dedupe_key, attempt, tool_use_id,
                  short_label, presentation, payload, reversibility, created_at, expires_at,
                  escalate_after_s, on_timeout)
               VALUES (?,?,?,'pending',?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                rid,
                job_id,
                kind,
                urgency,
                key,
                attempt,
                tool_use_id,
                short_label,
                _dump(presentation),
                _dump(payload),
                reversibility,
                ts,
                expires_at,
                escalate_after_s,
                on_timeout,
            ),
        )
        row = con.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
    return _to_request(row)


def find_open_for_tool(
    con: sqlite3.Connection,
    job_id: str,
    tool_use_id: str | None,
    tool_name: str,
    tool_input: dict[str, Any],
) -> Request | None:
    """Find the row that already represents this tool call, or None.

    Lookup order, from the design: (1) ``tool_use_id`` exact; (2) ``dedupe_key``
    with ``state <> 'consumed'``, newest attempt first. A miss means this is a
    genuine new ask — the caller creates with ``attempt=next_attempt(...)``.

    Consumed rows are deliberately skipped: the question was asked, answered AND
    served. Claude asking it again is a second question, not a re-fire.
    """
    if tool_use_id is not None:
        found = _by_tool_use_id(con, tool_use_id)
        if found is not None:
            return found
    key = _dedupe_key(job_id, tool_name, tool_input)
    row = con.execute(
        """SELECT * FROM requests
            WHERE job_id IS ? AND dedupe_key=? AND state <> 'consumed'
            ORDER BY attempt DESC LIMIT 1""",
        (job_id, key),
    ).fetchone()
    return _to_request(row) if row is not None else None


def next_attempt(con: sqlite3.Connection, job_id: str | None, dedupe_key: str) -> int:
    """The attempt number a genuinely new ask of this same question should use."""
    row = con.execute(
        "SELECT MAX(attempt) AS n FROM requests WHERE job_id IS ? AND dedupe_key=?",
        (job_id, dedupe_key),
    ).fetchone()
    return int(row["n"] or 0) + 1


# ───────────────────────────── answer / consume ─────────────────────────────


def answer_request(
    con: sqlite3.Connection,
    request_id: str,
    answer: Answer,
    answered_by: str,
    answer_mode: AnswerMode = "voice",
) -> bool:
    """Record the answer. FIRST ANSWER WINS. Returns False if someone else won.

    The predicate ``WHERE state='pending'`` IS the concurrency control: two
    channels racing — a tap on Telegram and a spoken reply at the desk — produce
    exactly one winner and one clean loser, with no lock and no leader. The
    loser must SAY SO out loud ("never mind, that was answered on the phone");
    it never fails silently, which is why this returns a bool rather than
    swallowing the outcome.

    Settling the deliveries happens in the SAME transaction as the swap, so
    there is no window in which the request is decided but a second channel
    still believes it should present the question.
    """
    _check_answer(answer)
    if not answered_by:
        raise ValueError("answered_by is required: an anonymous answer is unauditable")
    if answer_mode not in ANSWER_MODES:
        # 'how did this decision arrive' is read back to the user and is how a
        # timeout denial is told apart from a human one. A free-text mode would
        # make that distinction unqueryable rather than merely ugly.
        raise ValueError(f"unknown answer_mode {answer_mode!r}; one of {sorted(ANSWER_MODES)}")
    ts = now()
    with tx(con):
        row = con.execute(
            """UPDATE requests
                  SET state='answered', answer=?, answered_at=?, answered_by=?, answer_mode=?
                WHERE id=? AND state='pending'
            RETURNING job_id""",
            (_dump(answer), ts, answered_by, answer_mode, request_id),
        ).fetchone()
        if row is None:
            # Distinguish "lost the race" from "that id does not exist". Both
            # return no row; only one of them is a bug in the caller.
            if _exists(con, request_id):
                return False
            raise UnknownRequest(request_id)
        _settle_deliveries(con, request_id, "answered")
    return True


def consume(con: sqlite3.Connection, request_id: str, actor: str = "system") -> Answer | None:
    """Mark the answer as delivered to whoever asked, and return it.

    Idempotent on purpose. A driver that deferred, died and resumed re-fires the
    same question and consumes the same answer again; that is the normal path,
    not a double-delivery bug. Returns None while the request is still pending
    or if it reached a terminal state with no answer.
    """
    ts = now()
    row = con.execute(
        """UPDATE requests SET state='consumed', consumed_at=?
            WHERE id=? AND state='answered'
        RETURNING answer""",
        (ts, request_id),
    ).fetchone()
    if row is not None:
        return _load(row["answer"])
    cur = con.execute("SELECT state, answer FROM requests WHERE id=?", (request_id,)).fetchone()
    if cur is None:
        raise UnknownRequest(request_id)
    # Already consumed: hand back the same answer, do NOT move consumed_at. The
    # first consumption is the one that happened; later ones are replays.
    if cur["state"] == "consumed":
        return _load(cur["answer"])
    return None


def find_answer(con: sqlite3.Connection, tool_use_id: str) -> Answer | None:
    """The already-given answer for this tool call, or None. The fast path.

    This is what makes a four-hour phone answer work: the answer was written
    while the runner was dead, the runner comes back, the question re-fires, and
    this returns in microseconds with nobody involved twice. Consumed rows still
    answer — consumption is not expiry.
    """
    row = con.execute(
        """SELECT answer FROM requests
            WHERE tool_use_id=? AND state IN ('answered','consumed')""",
        (tool_use_id,),
    ).fetchone()
    return _load(row["answer"]) if row is not None else None


def cancel_request(con: sqlite3.Connection, request_id: str) -> bool:
    """pending -> cancelled. False if it was already decided."""
    return _terminate(con, request_id, "cancelled")


def supersede_request(con: sqlite3.Connection, request_id: str) -> bool:
    """pending -> superseded, for a question a newer one has replaced."""
    return _terminate(con, request_id, "superseded")


def get_request(con: sqlite3.Connection, request_id: str) -> Request | None:
    row = con.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
    return _to_request(row) if row is not None else None


def requests_for_job(
    con: sqlite3.Connection, job_id: str, *, kind: str | None = None, limit: int = 50
) -> list[Request]:
    """Every request this job ever raised, NEWEST FIRST, whatever its state.

    Distinct from :func:`open_requests`, which is "what is waiting on a human".
    This is "what was agreed", and it is how a multi-step runner picks up after a
    restart: the answered row it acted on is the only record of what the user
    said yes to, and it is settled by then, so an open-only query cannot see it.

    Tie-broken on ``rowid``, not on the id: two rows written in the same
    millisecond would otherwise come back in an order that depends on a random
    identifier, which is a flake that reproduces one run in three.
    """
    sql = "SELECT * FROM requests WHERE job_id=?"
    args: list[Any] = [job_id]
    if kind is not None:
        sql += " AND kind=?"
        args.append(kind)
    sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
    args.append(limit)
    return [_to_request(r) for r in con.execute(sql, tuple(args)).fetchall()]


def open_requests(con: sqlite3.Connection, job_id: str | None = None) -> list[Request]:
    """Every still-pending request, oldest first. Feeds the morning briefing."""
    sql = "SELECT * FROM requests WHERE state='pending'"
    args: tuple[Any, ...] = ()
    if job_id is not None:
        sql += " AND job_id=?"
        args = (job_id,)
    # rowid, not id: ids are random, so two rows written in the same
    # millisecond would come back in arbitrary order. These tables are
    # append-only, so rowid is insertion order.
    sql += " ORDER BY created_at, rowid"
    return [_to_request(r) for r in con.execute(sql, args).fetchall()]


# ───────────────────────────── expiry ─────────────────────────────


def expire_due(con: sqlite3.Connection, now_ts: str | None = None) -> list[Expiry]:
    """Apply the TYPED ``on_timeout`` outcome to every pending row that is due.

    The point of the enum is that "no channel could reach the human" has a named
    outcome instead of an implicit hang:

    ``defer``     the question survives; it stops having a deadline and waits
                  for an out-of-band answer. State stays ``pending``.
    ``deny``      the answer IS a denial, written through the same CAS a human
                  would use, so the waiting driver reads it the usual way and
                  tells Claude Code to stop rather than assume.
    ``default``   the pre-agreed answer from the presentation is applied. If the
                  presentation carries none, the row EXPIRES and says why — a
                  default nobody wrote down is a guess, and guessing here builds
                  the wrong thing.
    ``escalate``  open deliveries are released so the router can try the next
                  channel; the question stays ``pending`` and loses its
                  deadline, because the router owns the clock from here.

    Each row is handled in its own transaction and re-checks ``state='pending'``
    inside it, so a human answering mid-sweep always wins and two processes
    sweeping at once produce exactly one outcome per row.

    ``defer`` and ``escalate`` clear ``expires_at`` rather than leaving it in the
    past. Otherwise every later sweep would fire them again, forever.
    """
    ts = now_ts or now()
    due = con.execute(
        """SELECT * FROM requests
            WHERE state='pending' AND expires_at IS NOT NULL AND expires_at <= ?
            ORDER BY expires_at""",
        (ts,),
    ).fetchall()
    out: list[Expiry] = []
    for row in due:
        req = _to_request(row)
        applied = _apply_timeout(con, req, ts)
        if applied is not None:
            out.append(applied)
    return out


def _apply_timeout(con: sqlite3.Connection, req: Request, ts: str) -> Expiry | None:
    if req.on_timeout == "defer":
        ok = _clear_deadline(con, req.id)
        reason = "no deadline any more; waiting for an answer on any channel"
        return _expiry(req, "deferred", "pending", reason) if ok else None

    if req.on_timeout == "escalate":
        with tx(con):
            row = con.execute(
                "UPDATE requests SET expires_at=NULL WHERE id=? AND state='pending' RETURNING id",
                (req.id,),
            ).fetchone()
            if row is None:
                return None
            _settle_deliveries(con, req.id, "released")
        return _expiry(req, "escalated", "pending", "deliveries released for the next channel")

    if req.on_timeout == "deny":
        answer: Answer = {"approved": False, "text": TIMEOUT_DENY_TEXT}
        ok = _timeout_answer(con, req.id, answer)
        return _expiry(req, "denied", "answered", TIMEOUT_DENY_TEXT) if ok else None

    default = req.presentation.get("default_answer")
    malformed: str | None = None
    if default is not None:
        try:
            _check_answer(default)
        except (TypeError, ValueError) as exc:
            # Rows created before default_answer was validated at the door, or by
            # a caller that hand-built the presentation. One poisoned row must
            # not abort the sweep: every deadline behind it in the queue would go
            # unhandled and the failure would look like "Jarvis stopped timing
            # out", which is the hardest possible thing to notice.
            default, malformed = None, f"default_answer is malformed: {exc}"
    if default is None:
        ok = _terminate(con, req.id, "expired")
        reason = malformed or "on_timeout='default' but the presentation carries no default_answer"
        return _expiry(req, "expired", "expired", reason) if ok else None
    if not _timeout_answer(con, req.id, default):
        return None
    return _expiry(req, "defaulted", "answered", "the presentation's own default_answer")


def _expiry(req: Request, outcome: Outcome, state: ReqState, reason: str) -> Expiry:
    return Expiry(
        request_id=req.id,
        job_id=req.job_id,
        on_timeout=req.on_timeout,
        outcome=outcome,
        state=state,
        reason=reason,
    )


def _timeout_answer(con: sqlite3.Connection, request_id: str, answer: Answer) -> bool:
    try:
        return answer_request(con, request_id, answer, "timeout", "timeout")
    except UnknownRequest:
        # Deleted between the sweep's SELECT and now. Nothing to report.
        return False


def _clear_deadline(con: sqlite3.Connection, request_id: str) -> bool:
    row = con.execute(
        "UPDATE requests SET expires_at=NULL WHERE id=? AND state='pending' RETURNING id",
        (request_id,),
    ).fetchone()
    return row is not None


# ───────────────────────────── deliveries ─────────────────────────────


def schedule_delivery(
    con: sqlite3.Connection,
    request_id: str,
    channel_kind: str,
    due_at: str,
    *,
    channel_id: str | None = None,
    attempt: int = 1,
) -> Delivery:
    """One row per (request, channel, attempt), idempotently.

    The router materialises its ladder through here, and it runs again after a
    restart, so re-scheduling the same rung must return the existing row instead
    of tripping ``UNIQUE(request_id, channel_kind, attempt)``.

    A rung for a request that is no longer pending is recorded as ``skipped``
    rather than ``scheduled``. ``answer_request`` can only settle the deliveries
    that exist at the instant it commits, so a ladder being materialised while a
    human is answering would otherwise leave one live rung behind and Telegram
    would ask a question that already has an answer. Both sides run under BEGIN
    IMMEDIATE, so they serialise and there is no window either way round.
    """
    with tx(con):
        row = con.execute(
            "SELECT * FROM deliveries WHERE request_id=? AND channel_kind=? AND attempt=?",
            (request_id, channel_kind, attempt),
        ).fetchone()
        if row is None:
            req = con.execute("SELECT state FROM requests WHERE id=?", (request_id,)).fetchone()
            # A missing request is left to the foreign key: an unroutable rung
            # should fail loudly, not be filed as merely skipped.
            state = "scheduled" if req is not None and req["state"] == "pending" else "skipped"
            did = nid("del")
            con.execute(
                """INSERT INTO deliveries
                     (id, request_id, channel_kind, channel_id, attempt, due_at, state)
                   VALUES (?,?,?,?,?,?,?)""",
                (did, request_id, channel_kind, channel_id, attempt, due_at, state),
            )
            row = con.execute("SELECT * FROM deliveries WHERE id=?", (did,)).fetchone()
    return _to_delivery(row)


def due_deliveries(
    con: sqlite3.Connection, now_ts: str | None = None, limit: int = 200
) -> list[Delivery]:
    """Deliveries a channel may pick up: scheduled and due, or a dead claim.

    Expired claims reappear here because the process that claimed one can die
    between claiming and presenting. The lease, not a heartbeat, is what makes
    that recoverable from another process.
    """
    ts = now_ts or now()
    rows = con.execute(
        """SELECT * FROM deliveries
            WHERE (state='scheduled' AND due_at <= ?)
               OR (state='claimed' AND claim_expires_at IS NOT NULL AND claim_expires_at <= ?)
            ORDER BY due_at LIMIT ?""",
        (ts, ts, limit),
    ).fetchall()
    return [_to_delivery(r) for r in rows]


def claim_delivery(
    con: sqlite3.Connection, delivery_id: str, claimed_by: str, lease_s: int = 60
) -> bool:
    """Take a lease on presenting this question. False means someone else has it.

    Same CAS shape as the answer race, for the same reason: two desk processes
    (an old one that has not noticed it was replaced, say) must not both read
    the question aloud.
    """
    ts = now()
    row = con.execute(
        """UPDATE deliveries SET state='claimed', claimed_by=?, claim_expires_at=?
            WHERE id=? AND (state='scheduled'
                            OR (state='claimed' AND claim_expires_at IS NOT NULL
                                AND claim_expires_at <= ?))
        RETURNING id""",
        (claimed_by, _plus(ts, lease_s), delivery_id, ts),
    ).fetchone()
    return row is not None


def mark_presented(con: sqlite3.Connection, delivery_id: str, claimed_by: str) -> bool:
    """Record that the question actually reached the human on this channel.

    Refuses if someone else has taken the claim over — that presenter is the one
    whose reading of the question the user actually heard, and a stale process
    must not overwrite its ``presented_at``, which is what stops a duplicate
    poke reading the same question aloud twice.

    It deliberately does NOT check the lease clock. A presenter whose lease
    lapsed while it was speaking, and whom nobody has displaced, did present the
    question; refusing it would leave the row claimable and get the question read
    out a second time, which is the failure this guard exists to prevent.
    """
    row = con.execute(
        """UPDATE deliveries SET state='presented', presented_at=?
            WHERE id=? AND state='claimed' AND claimed_by=? RETURNING id""",
        (now(), delivery_id, claimed_by),
    ).fetchone()
    return row is not None


def fail_delivery(con: sqlite3.Connection, delivery_id: str, error: str) -> bool:
    """This channel could not deliver. The router decides what happens next."""
    row = con.execute(
        """UPDATE deliveries SET state='failed', error=?
            WHERE id=? AND state IN ('scheduled','claimed','presented') RETURNING id""",
        (error, delivery_id),
    ).fetchone()
    return row is not None


def _settle_deliveries(con: sqlite3.Connection, request_id: str, state: DeliveryState) -> None:
    con.execute(
        f"""UPDATE deliveries SET state=?
             WHERE request_id=? AND state IN ({",".join("?" * len(_OPEN_DELIVERY_STATES))})""",
        (state, request_id, *_OPEN_DELIVERY_STATES),
    )


# ───────────────────────────── plumbing ─────────────────────────────


def _terminate(con: sqlite3.Connection, request_id: str, state: ReqState) -> bool:
    with tx(con):
        row = con.execute(
            "UPDATE requests SET state=? WHERE id=? AND state='pending' RETURNING id",
            (state, request_id),
        ).fetchone()
        if row is None:
            return False
        _settle_deliveries(con, request_id, "skipped")
    return True


def _lookup_existing(
    con: sqlite3.Connection,
    tool_use_id: str | None,
    job_id: str | None,
    key: str,
    attempt: int,
) -> Request | None:
    if tool_use_id is not None:
        found = _by_tool_use_id(con, tool_use_id)
        if found is not None:
            return found
    # The natural key can hit even when tool_use_id misses: the CLI may re-issue
    # the same question with a fresh tool_use_id. The UNIQUE constraint would
    # reject the insert anyway, so returning the existing row is both correct and
    # the only thing that lets the caller find the answer already stored on it.
    row = con.execute(
        "SELECT * FROM requests WHERE job_id IS ? AND dedupe_key=? AND attempt=?",
        (job_id, key, attempt),
    ).fetchone()
    return _to_request(row) if row is not None else None


def _by_tool_use_id(con: sqlite3.Connection, tool_use_id: str) -> Request | None:
    row = con.execute("SELECT * FROM requests WHERE tool_use_id=?", (tool_use_id,)).fetchone()
    return _to_request(row) if row is not None else None


def _exists(con: sqlite3.Connection, request_id: str) -> bool:
    return con.execute("SELECT 1 FROM requests WHERE id=?", (request_id,)).fetchone() is not None


#: Every non-optional key of :class:`Presentation`. A channel renders all of
#: them, so a hand-built dict missing one is a KeyError in the Telegram or phone
#: process at the moment it tries to ask — far from here, and hours later.
_PRESENTATION_KEYS = ("verbatim", "intro", "items", "multi", "allows_free_text", "free_text_prompt")


def _check_presentation(pres: Any) -> None:
    if not isinstance(pres, dict):
        raise TypeError("presentation must be a Presentation mapping")
    for field in _PRESENTATION_KEYS:
        if field not in pres:
            raise ValueError(f"presentation is missing {field!r}")
    items = pres["items"]
    if not isinstance(items, list):
        raise TypeError("presentation['items'] must be a list")
    for i, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise TypeError(f"item {i} must be a mapping")
        if item.get("index") != i:
            raise ValueError(
                f"items must be numbered 1..n in payload order; item {i} says {item.get('index')!r}"
            )
        if not isinstance(item.get("label"), str):
            raise TypeError(f"item {i} has no string label")
    default = pres.get("default_answer")
    if default is not None:
        # Validated here as well as in make_presentation, because a presentation
        # can be hand-built. A default that only fails when it is applied fails
        # inside the expiry sweep, at 3am, on a question nobody is awake for.
        _check_answer(default)


_ANSWER_KEYS = frozenset(("answers", "approved", "text", "sources"))


def _check_answer(answer: Any) -> None:
    if not isinstance(answer, dict):
        raise TypeError("answer must be an Answer mapping")
    if not answer:
        # An empty answer reads as "answered" downstream and delivers nothing,
        # which is the one failure that looks like success.
        raise ValueError("an empty answer is not an answer")
    if "response" in answer:
        # S1, measured: when 'response' is present the CLI shows Claude "The user
        # responded: ..." and silently discards the per-question answer list.
        raise ValueError("'response' must never be set alongside 'answers'; it discards them")
    extra = sorted(set(answer) - _ANSWER_KEYS)
    if extra:
        raise ValueError(f"unknown answer keys {extra}; the wire format is {sorted(_ANSWER_KEYS)}")
    # The types matter as much as the keys: this dict becomes the CLI's
    # ``updated_input["answers"]``, which is keyed by the EXACT question text and
    # whose value is ONE label for a single-select and a LIST for multiSelect.
    # A str where a list belongs is accepted by json.dumps and rejected — or
    # worse, misread — only once it reaches Claude Code.
    answers = answer.get("answers")
    if answers is not None:
        if not isinstance(answers, dict) or not answers:
            raise ValueError("'answers' must be a non-empty {question: label|[labels]} mapping")
        for question, value in answers.items():
            if not isinstance(question, str) or not question:
                raise ValueError("'answers' keys are the exact question text")
            if isinstance(value, list):
                if not value or not all(isinstance(v, str) for v in value):
                    raise ValueError(
                        f"multi-select answer to {question!r} must be a list of labels"
                    )
            elif not isinstance(value, str):
                raise ValueError(f"answer to {question!r} must be a label or a list of labels")
    if "approved" in answer and not isinstance(answer["approved"], bool):
        raise ValueError(
            "'approved' is a bool; a truthy string is how a denial becomes an approval"
        )
    if "text" in answer and not isinstance(answer["text"], str):
        raise ValueError("'text' is the human's own words, as a string")
    sources = answer.get("sources")
    if sources is not None and (
        not isinstance(sources, dict)
        or not all(isinstance(k, str) and isinstance(v, str) for k, v in sources.items())
    ):
        raise ValueError("'sources' maps question -> 'option' | 'free_text'")


def _dump(obj: Any) -> str:
    """JSON with key order PRESERVED — canonical form is for hashing only.

    ``payload`` is echoed back to Claude Code as ``{**payload, "answers": ...}``
    and the CLI's validator rejects a changed shown field, so this must not sort,
    re-space or ASCII-escape anything on the way through.
    """
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _load(raw: str | None) -> Any:
    return json.loads(raw) if raw is not None else None


def _plus(ts: str, seconds: float) -> str:
    """``ts`` shifted by ``seconds``, in exactly :func:`jarvis.ids.now`'s format.

    Fixed millisecond width is not cosmetic: these strings are compared
    lexicographically in SQL, and a microsecond-width timestamp would sort wrong
    against every other row in the table.
    """
    t = parse_ts(ts) + timedelta(seconds=seconds)
    return f"{t.strftime('%Y-%m-%dT%H:%M:%S')}.{t.microsecond // 1000:03d}Z"


def _to_request(row: sqlite3.Row) -> Request:
    return Request(
        id=row["id"],
        kind=row["kind"],
        state=row["state"],
        short_label=row["short_label"],
        presentation=_load(row["presentation"]),
        payload=_load(row["payload"]),
        dedupe_key=row["dedupe_key"],
        attempt=row["attempt"],
        urgency=row["urgency"],
        created_at=row["created_at"],
        escalate_after_s=row["escalate_after_s"],
        on_timeout=row["on_timeout"],
        job_id=row["job_id"],
        tool_use_id=row["tool_use_id"],
        reversibility=row["reversibility"],
        expires_at=row["expires_at"],
        answer=_load(row["answer"]),
        answered_at=row["answered_at"],
        answered_by=row["answered_by"],
        answer_mode=row["answer_mode"],
        consumed_at=row["consumed_at"],
    )


def _to_delivery(row: sqlite3.Row) -> Delivery:
    return Delivery(
        id=row["id"],
        request_id=row["request_id"],
        channel_kind=row["channel_kind"],
        attempt=row["attempt"],
        due_at=row["due_at"],
        state=row["state"],
        channel_id=row["channel_id"],
        claimed_by=row["claimed_by"],
        claim_expires_at=row["claim_expires_at"],
        presented_at=row["presented_at"],
        error=row["error"],
    )
