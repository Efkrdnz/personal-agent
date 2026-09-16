"""The honest undo ledger: three classes, decided BEFORE the thing happens.

Every side effect this system causes lands in ``effects`` carrying a
reversibility class that was decided *before* execution, not guessed afterwards:

``reversible``      it can be put back exactly — a file edit whose previous
                    bytes were captured, a local commit.
``compensatable``   it cannot be reversed, but something can still be done —
                    archive + rename + private a GitHub repo, delete a Telegram
                    message inside its 48-hour window.
``irreversible``    nothing can be done — a placed phone call, a force-push
                    somebody already pulled.

THE RECONCILIATION THIS MODULE EXISTS FOR. The GitHub token deliberately has no
``delete_repo`` scope, so creating a repo is NOT reversible and never will be.
Three consequences, all mechanical here rather than remembered:

1. the class is known before acting — :func:`reversibility_of` raises on a kind
   nobody has classified, so "act first, work out the blast radius later" has no
   spelling;
2. it is said out loud — :data:`CONFIRM_STRENGTH` maps the class to how hard the
   confirmation is, and :func:`spoken_effect_line` generates the sentence from
   the class so the wording cannot drift into a promise;
3. the harm is engineered down instead — :func:`github_repo_create_effect`
   REFUSES to record a repo creation that is not private and empty, because a
   wrong repo that is private and empty costs nothing.

WHY THE PLAN IS JSON AND NEVER A CLOSURE. The reference build keeps
``run: Callable[[], str]`` in one module-level slot with a 90-second timeout. A
closure cannot survive a restart, cannot be answered by a second channel, and
cannot describe a remote side effect — which is why that build's undo is
useless. Here ``undo_plan`` is declarative JSON (``{"op":…, "args":{…}}``)
dispatched through :func:`undo_handler`, and :func:`record_effect` REJECTS a
plan containing anything that will not round-trip through JSON. A plan written
today must be executable by a process started tomorrow, on another machine,
after the writer died.

Nothing here does network I/O inside a transaction. :func:`undo` claims the row,
COMMITS, runs the handler outside any transaction, and then records the
compensation as a NEW effect row linked back by ``undo_effect_id`` — so a
compensation can never exist without its link, and an unlinked compensation can
never exist at all. When the link cannot be made (the lease was released while
the handler was out), the ROW is rolled back but the ``effect.undo_failed``
event still names what the handler did, because a real side effect that nothing
records is how you do it a second time.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, NotRequired, TypedDict

from jarvis.bus import publish
from jarvis.clock import spoken_time
from jarvis.db import tx
from jarvis.ids import nid, now, parse_ts

__all__ = [
    "CONFIRM_STRENGTH",
    "EXACT_WORDS",
    "IRREVERSIBILITY_REASONS",
    "KIND_REVERSIBILITY",
    "PROMISE_WORDS",
    "REFUSAL_MARKERS",
    "REVERSIBILITIES",
    "TELEGRAM_DELETE_WINDOW_S",
    "Compensation",
    "Effect",
    "NotDeclarative",
    "OverPromise",
    "Reversibility",
    "UndoOutcome",
    "UndoPlan",
    "UndoResult",
    "UnknownEffect",
    "confirm_strength",
    "confirm_strength_for_kind",
    "deadline_in",
    "effects_since",
    "expire_windows",
    "get_effect",
    "github_repo_create_effect",
    "handler_for",
    "record_effect",
    "recent_effects",
    "registered_ops",
    "release_undo_claim",
    "reversibility_of",
    "spoken_effect_line",
    "stale_undo_claims",
    "undo",
    "undo_handler",
    "undo_verdict",
    "undo_window_remaining_s",
]

# ───────────────────────────── the vocabulary ─────────────────────────────

Reversibility = Literal["reversible", "compensatable", "irreversible"]
EffectState = Literal["applied", "undone", "undo_failed", "expired"]
ConfirmStrength = Literal["notify", "confirm", "confirm_readback"]
UndoOutcome = Literal[
    "undone",
    "already_undone",
    "irreversible",
    "expired",
    "no_plan",
    "no_handler",
    "failed",
    "busy",
    "lost_claim",
]

REVERSIBILITIES: frozenset[str] = frozenset(("reversible", "compensatable", "irreversible"))

#: How hard the confirmation has to be, straight from the design doc. This is
#: the whole point of deciding the class up front: the gate is chosen by data,
#: not by whoever wrote the tool remembering to be careful today.
CONFIRM_STRENGTH: dict[Reversibility, ConfirmStrength] = {
    "reversible": "notify",
    "compensatable": "confirm",
    "irreversible": "confirm_readback",
}

#: How bad each class is. A caller may record something MORE pessimistic than
#: the table below (an ``fs.edit`` whose previous bytes were NOT captured really
#: is irreversible), never more optimistic — see :func:`_check_not_optimistic`.
_HARM: dict[str, int] = {"reversible": 0, "compensatable": 1, "irreversible": 2}

#: The classification, decided once, here, before anything acts. A kind that is
#: absent is not "probably fine": :func:`reversibility_of` raises, because the
#: failure mode this whole module exists to prevent is acting first and
#: discovering the blast radius afterwards.
KIND_REVERSIBILITY: dict[str, Reversibility] = {
    # Reversible ONLY because the plan captures the previous bytes; an edit
    # recorded without them must be recorded as something worse.
    "fs.edit": "reversible",
    "fs.write": "reversible",
    "git.commit": "reversible",
    # A pushed commit can be answered with a revert commit, which is not the
    # same as never having pushed it.
    "git.push": "compensatable",
    # Once somebody has pulled the old history there is no putting it back.
    "git.push_force": "irreversible",
    "github.repo_create": "irreversible",
    "github.issue_comment": "compensatable",
    "github.issue_create": "compensatable",
    "telegram.send": "compensatable",
    "telegram.send_document": "compensatable",
    "phone.call": "irreversible",
}

#: Spoken as part of the refusal, so the user hears WHY rather than a flat no.
#: Module-owned text, never caller text — see :func:`_assert_honest`.
IRREVERSIBILITY_REASONS: dict[str, str] = {
    "github.repo_create": "the GitHub token deliberately has no delete_repo scope",
    "git.push_force": "the old history is already gone from anyone who pulled it",
    "phone.call": "the call already happened",
}

#: Telegram lets a bot delete its own message for 48 hours and not one second
#: longer. That number is the reason ``undo_deadline`` exists at all.
TELEGRAM_DELETE_WINDOW_S = 48 * 3600


class UnknownEffect(KeyError):
    """No such effect id. A typo or a stale id, never a lost race."""


class NotDeclarative(TypeError):
    """An undo plan that will not survive a restart.

    Raised at WRITE time, in the caller's own stack, because the alternative is
    discovering at 3am that the thing you were promised could be undone was
    holding a lambda that died with the process that made it.
    """


class OverPromise(AssertionError):
    """Wording that claims more than the reversibility class allows.

    An AssertionError subclass on purpose: this fires when the code is wrong,
    not when the world is. It is raised rather than softened because a silent
    downgrade from "I can put it back" to a lie is the exact failure this module
    exists to make impossible.
    """


# ───────────────────────────── the wire format ─────────────────────────────


class UndoPlan(TypedDict):
    """DECLARATIVE JSON. Never a closure, never a reference to live objects.

    ``op`` names a handler in the registry; ``args`` is everything that handler
    needs, already serialised. ``speaks`` completes the sentence "I can …" and
    is validated against the effect's class when the row is written, so an
    over-promising phrase cannot reach the speaker.
    """

    op: str
    args: dict[str, Any]
    speaks: NotRequired[str]


@dataclass(frozen=True, slots=True)
class Effect:
    """One row of ``effects``, with the JSON columns already parsed."""

    id: str
    kind: str
    ts: str
    summary: str
    reversibility: Reversibility
    state: EffectState
    job_id: str | None = None
    provider_ref: dict[str, Any] | None = None
    undo_plan: UndoPlan | None = None
    undo_deadline: str | None = None
    confirmed_by_request_id: str | None = None
    undone_at: str | None = None
    undo_effect_id: str | None = None
    undo_error: str | None = None

    @property
    def confirm_strength(self) -> ConfirmStrength:
        return CONFIRM_STRENGTH[self.reversibility]

    @property
    def undo_claimed(self) -> bool:
        """An undo is in flight, or died in flight.

        ``state='applied'`` with ``undone_at`` set is the claim: the schema has
        no ``undoing`` state and is frozen, so the timestamp column doubles as
        the lease. See :func:`undo`.
        """
        return self.state == "applied" and self.undone_at is not None


@dataclass(frozen=True, slots=True)
class Compensation:
    """What a handler actually did in the world, on its way to becoming a row.

    ``reversibility`` has no default deliberately: a handler author who has not
    decided whether their own compensation can be taken back has not finished
    thinking about it.
    """

    kind: str
    summary: str
    reversibility: Reversibility
    provider_ref: dict[str, Any] | None = None
    undo_plan: UndoPlan | None = None
    undo_deadline: str | None = None


@dataclass(frozen=True, slots=True)
class UndoResult:
    """What :func:`undo` did, and the sentence to say about it."""

    effect_id: str
    outcome: UndoOutcome
    spoken: str
    undo_effect_id: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome in ("undone", "already_undone")


UndoHandler = Callable[[sqlite3.Connection, Effect, dict[str, Any]], Compensation]

#: op -> handler.  A registry of CODE, not of state: it is rebuilt identically
#: at import time in every process, holds nothing durable, and losing it costs
#: nothing because the plan itself lives in the row. That is exactly the
#: distinction the house rule is protecting — the reference build's failure was
#: keeping the *plan* in memory, not keeping the *dispatch table* in memory. A
#: process that has not imported a handler package reports ``no_handler``
#: honestly rather than pretending the undo is impossible.
_HANDLERS: dict[str, UndoHandler] = {}


def undo_handler(op: str) -> Callable[[UndoHandler], UndoHandler]:
    """Register a compensation handler for one declarative ``op``.

    Keyed by the plan's ``op`` rather than by the effect kind (the doc's
    signature is ``undo_handler(op)``), because one kind can have several
    compensations — a Telegram message is deleted inside 48 hours and edited to
    a retraction afterwards — and because the op is what the durable row names.

    The handler runs OUTSIDE any transaction and may do network I/O. It gets the
    caller's connection for reads, the effect row, and the plan's ``args``; it
    returns the :class:`Compensation` it performed. It must NOT write the
    effects table: :func:`undo` does that, atomically, with the link.
    """
    if not op or not isinstance(op, str):
        raise ValueError("an undo op must be a non-empty string")

    def register(fn: UndoHandler) -> UndoHandler:
        _HANDLERS[op] = fn
        return fn

    return register


def handler_for(op: str) -> UndoHandler | None:
    """The handler this process has for ``op``, or None. Never raises."""
    return _HANDLERS.get(op)


def registered_ops() -> frozenset[str]:
    """Ops this process can currently compensate. A snapshot, not a view."""
    return frozenset(_HANDLERS)


# ───────────────────────────── classification ─────────────────────────────


def reversibility_of(kind: str, *, default: Reversibility | None = None) -> Reversibility:
    """The class for an effect kind, decided BEFORE anything is done.

    Raises ``KeyError`` for an unclassified kind unless ``default`` is given.
    That raise is the mechanism: a new tool cannot cause a side effect until
    somebody has written down what undoing it would cost.
    """
    found = KIND_REVERSIBILITY.get(kind)
    if found is not None:
        return found
    if default is not None:
        _check_reversibility(default)
        return default
    raise KeyError(
        f"effect kind {kind!r} has no reversibility class; classify it in "
        "KIND_REVERSIBILITY before anything performs it"
    )


def confirm_strength(reversibility: Reversibility) -> ConfirmStrength:
    """How hard to confirm, from the class alone."""
    _check_reversibility(reversibility)
    return CONFIRM_STRENGTH[reversibility]


def confirm_strength_for_kind(
    kind: str, *, default: Reversibility | None = None
) -> ConfirmStrength:
    """How hard to confirm, from the kind. Raises on an unclassified kind."""
    return CONFIRM_STRENGTH[reversibility_of(kind, default=default)]


# ───────────────────────────── the spoken line ─────────────────────────────

_IRREVERSIBLE = "irreversible"
_EXPIRED = "expired"
_NO_PLAN = "no_plan"
_FAILED = "failed"
_UNDONE = "undone"
_ACTIONABLE = "actionable"

#: Phrases that promise an undo. None of these may appear in a verdict for an
#: irreversible effect, an expired window, a missing plan or a failed attempt.
#: Deliberately phrase-level ("delete it", not "delete") so that a REASON like
#: "the token has no delete_repo scope" is still sayable.
PROMISE_WORDS: tuple[str, ...] = (
    "undo",
    "reverse",
    "revert",
    "restore",
    "put it back",
    "put that back",
    "roll back",
    "rollback",
    "take it back",
    "take that back",
    "cancel it",
    "cancel that",
    "delete it",
    "delete that",
    "remove it",
    "remove that",
    "as it was",
    "as they were",
    "exactly",
    "no harm done",
)

#: Phrases that promise EXACT restoration. Only ``reversible`` may say these.
EXACT_WORDS: tuple[str, ...] = (
    "exactly",
    "put it back",
    "put that back",
    "as it was",
    "as they were",
    "restore",
    "identical",
    "no trace",
)

#: A refusal has to be audible, not merely non-promising. A verdict that cannot
#: act must contain one of these, so deleting the honest half of the sentence
#: fails just as loudly as adding a dishonest half.
REFUSAL_MARKERS: tuple[str, ...] = (
    "nothing i can do",
    "nothing i can act on",
    "too late",
    "nothing has changed",
    "can't",
    "cannot",
)

# Verdict templates. Module-owned text ONLY — nothing here interpolates a
# caller's summary or a handler's error message, because _assert_honest checks
# the finished string and free text would make that check fire on the user's own
# words instead of on our promise.
_VERDICTS: dict[str, str] = {
    _IRREVERSIBLE: "That one is irreversible, so there is nothing I can do about it now.",
    "irreversible_because": (
        "That one is irreversible — {reason} — so there is nothing I can do about it now."
    ),
    _EXPIRED: "The window for doing anything about that closed {when}, so it is too late now.",
    _NO_PLAN: (
        "I marked that {reversibility}, but no plan was stored with it, "
        "so there is nothing I can act on."
    ),
    _FAILED: "I already tried and it did not work, so nothing has changed.",
    _UNDONE: "That one is already dealt with — I did it at {when}.",
    "reversible": "I can put that back exactly as it was.",
    "compensatable": "I can't reverse that, but I can {speaks}.",
    "compensatable_window": "I can't reverse that, but I can {speaks} — the window closes {when}.",
}

_DEFAULT_SPEAKS = "run the {op} compensation"


def _situation(e: Effect, now_ts: str) -> str:
    """Which verdict applies. Pure, and the ONLY place the order is decided."""
    if e.state == "undone":
        return _UNDONE
    if e.state == "undo_failed":
        return _FAILED
    if e.state == "expired":
        return _EXPIRED
    # Before the deadline and plan checks: an irreversible effect can carry
    # neither, and if a hand-written row somehow carries both, the class still
    # wins. Optimism is never allowed to come from the second-most-specific
    # field on the row.
    if e.reversibility == "irreversible":
        return _IRREVERSIBLE
    if _deadline_passed(e, now_ts):
        return _EXPIRED
    if e.undo_plan is None:
        return _NO_PLAN
    return _ACTIONABLE


def undo_verdict(e: Effect, *, now_ts: str | None = None) -> str:
    """The generated half of the spoken line: what can and cannot be done.

    Separate from :func:`spoken_effect_line` because this string contains NO
    caller text, which is what lets the honesty check below be exact rather than
    heuristic. Raises :class:`OverPromise` rather than speaking a lie.
    """
    ts = now_ts or now()
    situation = _situation(e, ts)
    verdict = _compose(e, situation, ts)
    _assert_honest(e, situation, verdict)
    return verdict


def _compose(e: Effect, situation: str, now_ts: str) -> str:
    if situation == _IRREVERSIBLE:
        reason = IRREVERSIBILITY_REASONS.get(e.kind)
        if reason:
            return _VERDICTS["irreversible_because"].format(reason=reason)
        return _VERDICTS[_IRREVERSIBLE]
    if situation == _EXPIRED:
        return _VERDICTS[_EXPIRED].format(when=_ago(e.undo_deadline, now_ts))
    if situation == _NO_PLAN:
        return _VERDICTS[_NO_PLAN].format(reversibility=e.reversibility)
    if situation == _FAILED:
        return _VERDICTS[_FAILED]
    if situation == _UNDONE:
        return _VERDICTS[_UNDONE].format(when=_spoken_clock(e.undone_at))
    if e.reversibility == "reversible":
        return _VERDICTS["reversible"]
    plan = e.undo_plan or {}
    speaks = plan.get("speaks") or _DEFAULT_SPEAKS.format(op=plan.get("op", "stored"))
    if e.undo_deadline is not None:
        return _VERDICTS["compensatable_window"].format(
            speaks=speaks, when=_in(e.undo_deadline, now_ts)
        )
    return _VERDICTS["compensatable"].format(speaks=speaks)


#: Situations in which nothing can be done. Their wording is held to the
#: strictest standard: no promise vocabulary at all, and an audible refusal.
_CANNOT_ACT = (_IRREVERSIBLE, _EXPIRED, _NO_PLAN, _FAILED)


def _assert_honest(e: Effect, situation: str, verdict: str) -> None:
    """Make the honesty requirement mechanical rather than remembered.

    Checked on the finished sentence every single time it is generated, so a
    future edit that softens "there is nothing I can do" into "I'll see what I
    can do" fails here — at runtime and in the suite — instead of being
    discovered by a user who believed it. It raises rather than falling back to
    a safe sentence for the same reason the verbatim path raises rather than
    paraphrasing: a silent downgrade is the bug.
    """
    low = verdict.lower()

    # Structural, not stylistic: an irreversible effect must never reach the
    # branch that offers to do something. If it ever does, the situation table
    # has been reordered and every irreversible line in the system is suspect.
    if e.reversibility == "irreversible" and situation not in _CANNOT_ACT + (_UNDONE,):
        raise OverPromise(f"an irreversible effect was given the {situation!r} verdict")

    if situation in _CANNOT_ACT:
        for word in PROMISE_WORDS:
            if word in low:
                raise OverPromise(
                    f"{situation} verdict for a {e.reversibility} effect promises {word!r}: "
                    f"{verdict!r}"
                )
        if not any(marker in low for marker in REFUSAL_MARKERS):
            raise OverPromise(
                f"{situation} verdict says nothing a user would hear as a refusal: {verdict!r}"
            )
        return

    if situation == _ACTIONABLE and e.reversibility == "compensatable":
        # A compensation is not a restoration. "Archived, renamed and made
        # private" must never be spoken as "put it back".
        for word in EXACT_WORDS:
            if word in low:
                raise OverPromise(
                    f"compensatable verdict claims exact restoration ({word!r}): {verdict!r}"
                )
        if not any(marker in low for marker in REFUSAL_MARKERS):
            raise OverPromise(f"compensatable verdict does not say what it cannot do: {verdict!r}")


def spoken_effect_line(e: Effect, *, now_ts: str | None = None) -> str:
    """THE sentence Jarvis says when asked to undo this effect.

    ``summary`` (the caller's past-tense description of what happened) followed
    by the generated verdict, and the verdict is always LAST: nothing may be
    appended after the part that says what is and is not possible.
    """
    verdict = undo_verdict(e, now_ts=now_ts)
    summary = e.summary.strip()
    if summary and summary[-1] not in ".!?":
        summary += "."
    return f"{summary} {verdict}".strip()


# ───────────────────────────── writing ─────────────────────────────


def record_effect(
    con: sqlite3.Connection,
    *,
    kind: str,
    summary: str,
    reversibility: Reversibility,
    job_id: str | None = None,
    provider_ref: dict[str, Any] | None = None,
    undo_plan: UndoPlan | dict[str, Any] | None = None,
    undo_deadline: str | None = None,
    confirmed_by: str | None = None,
    actor: str = "system",
    publish_event: bool = True,
) -> Effect:
    """Append one effect row. The class is an argument because it was decided first.

    Validation is deliberately loud and deliberately at write time:

    * an ``irreversible`` effect may not carry an undo plan — a plan for
      something that cannot be undone is a lie waiting to be spoken;
    * the plan must round-trip through JSON, so a closure, a connection, a
      ``Path`` or a datetime is rejected here rather than at 3am tomorrow;
    * the class may not be more optimistic than :data:`KIND_REVERSIBILITY`;
    * ``speaks`` is checked against the class, so an over-promising phrase can
      never reach the speaker at all.

    A ``reversible`` or ``compensatable`` effect with NO plan is allowed and is
    spoken honestly ("no plan was stored"). Requiring one would only tempt
    callers to write a plan that does not work.
    """
    _check_reversibility(reversibility)
    _check_not_optimistic(kind, reversibility)
    if not summary or not summary.strip():
        raise ValueError("an effect with no summary cannot be spoken, so it cannot be recorded")

    plan = _check_plan(undo_plan, reversibility)
    if undo_deadline is not None:
        _check_deadline(undo_deadline)
        if plan is None:
            raise ValueError(
                "undo_deadline with no undo_plan promises a window for a mechanism "
                "that does not exist"
            )

    with tx(con):
        effect = _insert(
            con,
            kind=kind,
            summary=summary,
            reversibility=reversibility,
            job_id=job_id,
            provider_ref=provider_ref,
            undo_plan=plan,
            undo_deadline=undo_deadline,
            confirmed_by=confirmed_by,
        )
        if publish_event:
            # Inside the same atom as the row: an effect nobody was told about
            # is exactly the gap the activity log exists to close.
            publish(
                con,
                "effect.recorded",
                actor,
                {
                    "kind": kind,
                    "summary": summary,
                    "reversibility": reversibility,
                    "confirm_strength": CONFIRM_STRENGTH[reversibility],
                    "undo_deadline": undo_deadline,
                    "undo_op": (plan or {}).get("op"),
                },
                job_id=job_id,
                effect_id=effect.id,
                idem_key=f"eff:{effect.id}:applied",
            )
    return effect


def github_repo_create_effect(
    con: sqlite3.Connection,
    *,
    full_name: str,
    private: bool,
    empty: bool,
    job_id: str | None = None,
    confirmed_by: str | None = None,
    actor: str = "system",
) -> Effect:
    """Record a repo creation — the effect the whole three-class design is for.

    The token has no ``delete_repo`` scope, so this is irreversible and carries
    no plan. Since it cannot be taken back, the harm is engineered down instead:
    this REFUSES to record a repo that is not private and empty, which makes
    "created the wrong repo" cost nothing but a name.
    """
    if not private or not empty:
        raise ValueError(
            "a repo Jarvis creates must be private and empty: the token has no delete_repo "
            "scope, so 'harmless if wrong' is the only available safety property"
        )
    return record_effect(
        con,
        kind="github.repo_create",
        summary=f"I created the private, empty repository {full_name}",
        reversibility="irreversible",
        job_id=job_id,
        provider_ref={"full_name": full_name, "private": True, "empty": True},
        confirmed_by=confirmed_by,
        actor=actor,
    )


# ───────────────────────────── reading ─────────────────────────────


def get_effect(con: sqlite3.Connection, effect_id: str) -> Effect | None:
    row = con.execute("SELECT * FROM effects WHERE id=?", (effect_id,)).fetchone()
    return _to_effect(row) if row is not None else None


def recent_effects(
    con: sqlite3.Connection,
    *,
    limit: int = 20,
    job_id: str | None = None,
    state: EffectState | None = None,
) -> list[Effect]:
    """Newest first — "what did you just do" is a SELECT, not a memory."""
    sql = "SELECT * FROM effects WHERE 1=1"
    args: list[Any] = []
    if job_id is not None:
        sql += " AND job_id=?"
        args.append(job_id)
    if state is not None:
        sql += " AND state=?"
        args.append(state)
    sql += " ORDER BY ts DESC, id DESC LIMIT ?"
    args.append(limit)
    return [_to_effect(r) for r in con.execute(sql, args)]


def effects_since(con: sqlite3.Connection, ts: str) -> list[Effect]:
    """Oldest first, for narration and the morning briefing."""
    return [
        _to_effect(r)
        for r in con.execute("SELECT * FROM effects WHERE ts > ? ORDER BY ts, id", (ts,))
    ]


def undo_window_remaining_s(e: Effect, *, now_ts: str | None = None) -> float | None:
    """Seconds left on the undo window; negative if it has closed, None if none."""
    if e.undo_deadline is None:
        return None
    return (parse_ts(e.undo_deadline) - parse_ts(now_ts or now())).total_seconds()


def deadline_in(seconds: float, *, from_ts: str | None = None) -> str:
    """A deadline ``seconds`` from now, in the one timestamp format that sorts."""
    return _fmt(parse_ts(from_ts or now()) + timedelta(seconds=seconds))


# ───────────────────────────── undo ─────────────────────────────


def undo(
    con: sqlite3.Connection,
    effect_id: str,
    *,
    actor: str = "system",
    now_ts: str | None = None,
    publish_event: bool = True,
) -> UndoResult:
    """Attempt the compensation, and record it as a NEW linked effect row.

    The order matters and every step of it is a failure somebody has had:

    1. refuse outright for ``irreversible`` — nothing is attempted, the row is
       untouched, and the spoken line says plainly that nothing can be done;
    2. an ``undo_deadline`` in the past is reported as EXPIRED, not attempted:
       calling Telegram's delete endpoint at 48h + 1s and relaying its error is
       an attempt that failed, which is a different and less honest sentence
       than "the window closed";
    3. CLAIM the row with a compare-and-swap before doing anything in the world.
       The schema is frozen and has no ``undoing`` state, so ``undone_at`` set
       while ``state='applied'`` IS the lease. Two processes asked to undo the
       same effect produce exactly one compensation and one clean ``busy``;
    4. run the handler with NO transaction open — it does network I/O, and the
       house rule against I/O inside ``tx`` is what keeps the single writer from
       being held by a socket;
    5. record the compensation and the link in ONE transaction, so a
       compensation row without its ``undo_effect_id`` link cannot exist.

    A crash between 3 and 5 leaves a claimed row: ``state='applied'`` with
    ``undone_at`` set. That is deliberately NOT auto-retried — re-running a
    compensation that may have half-happened is the ``phone.dial`` problem — see
    :func:`stale_undo_claims` and :func:`release_undo_claim`.
    """
    ts = now_ts or now()
    e = get_effect(con, effect_id)
    if e is None:
        raise UnknownEffect(effect_id)

    if e.state == "undone":
        return _result(con, e, "already_undone", ts, undo_effect_id=e.undo_effect_id)

    if e.reversibility == "irreversible":
        # Nothing is written. The row is not "failed": we never tried, and a
        # ledger that says we tried is a ledger that is lying.
        return _result(con, e, "irreversible", ts, publish_event=publish_event, actor=actor)

    if e.state == "expired":
        return _result(con, e, "expired", ts, publish_event=publish_event, actor=actor)

    if e.undo_claimed:
        return _result(con, e, "busy", ts)

    if _deadline_passed(e, ts):
        # REPORTED as expired, never attempted. Calling Telegram's delete
        # endpoint at 48 hours plus one second and relaying its error is an
        # attempt that failed, which is a different and less honest sentence
        # than "the window closed".
        if not _cas_expire(con, e.id):
            return _result(con, e, "busy", ts)
        return _result(con, e, "expired", ts, publish_event=publish_event, actor=actor)

    plan = e.undo_plan
    if plan is None:
        # Left untouched, for the same reason the irreversible branch above is:
        # we never tried. Marking it 'undo_failed' would also replace the honest
        # standing verdict ("no plan was stored, so there is nothing I can act
        # on") with "I already tried and it did not work" — permanently, since a
        # plan is written once at record time and never added later. The ask
        # itself is history, so it goes on the bus rather than onto the row.
        return _result(con, e, "no_plan", ts, publish_event=publish_event, actor=actor)

    op = str(plan["op"])
    fn = handler_for(op)
    if fn is None:
        # Possibly transient: another process may have imported the handler
        # package this one never loaded. Recorded as a failure — the undo did
        # not happen — but left retryable, which is what _fail's cleared claim
        # is for.
        _fail(con, e.id, f"no handler registered for op {op!r}", claimed=False)
        return _result(con, e, "no_handler", ts, publish_event=publish_event, actor=actor)

    if not _claim(con, e.id, ts):
        return _result(con, e, "busy", ts)

    try:
        comp = fn(con, e, dict(plan.get("args") or {}))
    except Exception as exc:
        # A handler talks to the network, so anything at all can come back. The
        # claim is cleared so a human (or a later process) can try again.
        _fail(con, e.id, f"{type(exc).__name__}: {exc}", claimed=True)
        return _result(
            con, e, "failed", ts, publish_event=publish_event, actor=actor, error=str(exc)
        )

    if not isinstance(comp, Compensation):
        _fail(con, e.id, f"handler for {op!r} returned {type(comp).__name__}", claimed=True)
        return _result(con, e, "failed", ts, publish_event=publish_event, actor=actor)

    # From here the compensation HAS HAPPENED in the world, so nothing below may
    # raise its way out of recording it. Every field a handler controls is
    # DEGRADED rather than rejected: losing the row that says what was done is
    # worse than an over-cautious class or a dropped onward plan.
    comp_kind, comp_summary, comp_rev, comp_plan, comp_deadline, degraded = _sanitise(comp, op)

    done_ts = now()
    try:
        with tx(con):
            compensation = _insert(
                con,
                kind=comp_kind,
                summary=comp_summary,
                reversibility=comp_rev,
                job_id=e.job_id,
                provider_ref=comp.provider_ref,
                undo_plan=comp_plan,
                undo_deadline=comp_deadline,
                confirmed_by=e.confirmed_by_request_id,
            )
            row = con.execute(
                """UPDATE effects
                      SET state='undone', undone_at=?, undo_effect_id=?, undo_error=NULL
                    WHERE id=? AND state='applied' AND undone_at=?
                RETURNING id""",
                (done_ts, compensation.id, e.id, ts),
            ).fetchone()
            if row is None:
                # Our lease went away while the handler was on the network —
                # reachable without any hand-written SQL, because
                # release_undo_claim() cannot tell a hung handler from a dead
                # one. Roll the compensation row back with the rest of the atom
                # rather than leaving an orphan the ledger cannot explain.
                raise _Lost(e.id)
            if publish_event:
                publish(
                    con,
                    "effect.undone",
                    actor,
                    {
                        "kind": e.kind,
                        "op": op,
                        "compensation": comp_kind,
                        "summary": comp_summary,
                        "compensation_plan_rejected": degraded,
                    },
                    job_id=e.job_id,
                    effect_id=e.id,
                    idem_key=f"eff:{e.id}:undone",
                )
    except _Lost:
        # The rollback threw away the ROW, so the EVENT is the only place left
        # that can say the compensation happened. Publishing it is not optional:
        # the alternative is a Telegram message that is really deleted, a ledger
        # that says it is not, and a second delete the next time anyone asks.
        return _result(
            con,
            e,
            "lost_claim",
            ts,
            publish_event=publish_event,
            actor=actor,
            error=f"undo claim lost mid-flight; {comp_kind} was performed but could not be linked",
            performed=comp_summary,
        )
    return _result(con, e, "undone", ts, undo_effect_id=compensation.id)


def expire_windows(con: sqlite3.Connection, *, now_ts: str | None = None) -> list[str]:
    """Flip every applied effect whose window has closed to ``expired``.

    Called from ``reconcile()``. Idempotent and safe to run concurrently: each
    row is moved by a CAS, so two sweepers between them report each id once.
    Unclaimed rows only — a claimed row has an undo in flight and taking its
    window away underneath the handler would be a lie in the other direction.
    """
    ts = now_ts or now()
    ids = [
        str(r["id"])
        for r in con.execute(
            """SELECT id FROM effects
                WHERE state='applied' AND undone_at IS NULL
                  AND undo_deadline IS NOT NULL AND undo_deadline <= ?""",
            (ts,),
        )
    ]
    return [eid for eid in ids if _cas_expire(con, eid)]


def stale_undo_claims(con: sqlite3.Connection, *, older_than_s: float = 300.0) -> list[Effect]:
    """Effects whose undo was claimed and never finished — a process died mid-undo.

    Reported, never auto-released: the compensation may have reached the network
    before the crash, and re-running it is exactly the double-dial hazard the
    outbox refuses to take on itself.
    """
    cutoff = _fmt(parse_ts(now()) - timedelta(seconds=older_than_s))
    return [
        _to_effect(r)
        for r in con.execute(
            """SELECT * FROM effects
                WHERE state='applied' AND undone_at IS NOT NULL AND undone_at <= ?
                ORDER BY undone_at""",
            (cutoff,),
        )
    ]


def release_undo_claim(con: sqlite3.Connection, effect_id: str, *, reason: str) -> bool:
    """Drop a dead claim so the effect can be undone again. An explicit decision.

    ``reason`` is mandatory and stored, because "who decided it was safe to try
    that compensation a second time" is a question the ledger has to answer.
    """
    if not reason or not reason.strip():
        raise ValueError("releasing an undo claim needs a stated reason")
    row = con.execute(
        """UPDATE effects SET undone_at=NULL, undo_error=?
            WHERE id=? AND state='applied' AND undone_at IS NOT NULL
        RETURNING id""",
        (f"claim released: {reason}", effect_id),
    ).fetchone()
    return row is not None


# ───────────────────────────── plumbing ─────────────────────────────


class _Lost(RuntimeError):
    """The claimed row moved underneath us.

    Never escapes this module: it exists to roll the compensation atom back, and
    :func:`undo` catches it immediately and returns the ``lost_claim`` outcome.
    Callers get a typed result, not a private exception nothing documents.
    """


def _insert(
    con: sqlite3.Connection,
    *,
    kind: str,
    summary: str,
    reversibility: Reversibility,
    job_id: str | None,
    provider_ref: dict[str, Any] | None,
    undo_plan: UndoPlan | None,
    undo_deadline: str | None,
    confirmed_by: str | None,
) -> Effect:
    """Insert one row. Assumes the caller owns a transaction; never opens one."""
    effect = Effect(
        id=nid("eff"),
        kind=kind,
        ts=now(),
        summary=summary,
        reversibility=reversibility,
        state="applied",
        job_id=job_id,
        provider_ref=provider_ref,
        undo_plan=undo_plan,
        undo_deadline=undo_deadline,
        confirmed_by_request_id=confirmed_by,
    )
    con.execute(
        """INSERT INTO effects (id, job_id, ts, kind, summary, reversibility, provider_ref,
                                undo_plan, undo_deadline, state, confirmed_by_request_id)
           VALUES (?,?,?,?,?,?,?,?,?,'applied',?)""",
        (
            effect.id,
            job_id,
            effect.ts,
            kind,
            summary,
            reversibility,
            _dump(provider_ref),
            _dump(undo_plan),
            undo_deadline,
            confirmed_by,
        ),
    )
    return effect


#: Rows an undo may still be attempted on. ``undo_failed`` is included because a
#: failed compensation is worth retrying — the network was down, the handler had
#: not been imported — and the ledger keeps the reason in ``undo_error``.
_RETRYABLE = ("applied", "undo_failed")

#: Placeholders, not the tuple's repr: interpolating ``_RETRYABLE`` directly
#: renders valid SQL only while it has two or more entries.
_RETRYABLE_SQL = ",".join("?" * len(_RETRYABLE))


def _claim(con: sqlite3.Connection, effect_id: str, ts: str) -> bool:
    """CAS the lease. ``undone_at`` set while ``state='applied'`` IS the claim.

    The schema is frozen and has no ``undoing`` state, so the timestamp column
    doubles as the lease. One statement, so two connections racing produce
    exactly one winner: the loser's UPDATE matches no row.
    """
    row = con.execute(
        """UPDATE effects SET state='applied', undone_at=?
            WHERE id=? AND state IN ('applied','undo_failed') AND undone_at IS NULL
        RETURNING id""",
        (ts, effect_id),
    ).fetchone()
    return row is not None


def _fail(con: sqlite3.Connection, effect_id: str, error: str, *, claimed: bool) -> bool:
    """Record a failed undo and CLEAR the claim, so a retry can re-claim.

    ``claimed`` picks the predicate: after the handler ran we hold the lease and
    must match a claimed row; before it ran we must NOT clobber somebody else's.
    """
    lease = "undone_at IS NOT NULL" if claimed else "undone_at IS NULL"
    row = con.execute(
        f"""UPDATE effects SET state='undo_failed', undo_error=?, undone_at=NULL
             WHERE id=? AND state IN ({_RETRYABLE_SQL}) AND {lease}
         RETURNING id""",
        (error, effect_id, *_RETRYABLE),
    ).fetchone()
    return row is not None


def _cas_expire(con: sqlite3.Connection, effect_id: str) -> bool:
    """Close the window, but never underneath an undo that is already in flight."""
    row = con.execute(
        f"""UPDATE effects SET state='expired', undone_at=NULL
             WHERE id=? AND state IN ({_RETRYABLE_SQL}) AND undone_at IS NULL
         RETURNING id""",
        (effect_id, *_RETRYABLE),
    ).fetchone()
    return row is not None


def _sanitise(
    comp: Compensation, op: str
) -> tuple[str, str, Reversibility, UndoPlan | None, str | None, str | None]:
    """Make a handler's Compensation safe to store, degrading instead of raising.

    ``record_effect`` refuses a bad argument because nothing has happened yet.
    Here the world has already moved, so every check below DOWNGRADES the row
    and reports why on the bus. Four things a handler controls could otherwise
    put a row in the ledger that no later process can use:

    * a reversibility outside the three classes — ``Effect.confirm_strength``
      would then raise ``KeyError`` forever on a durable row, i.e. the gate that
      decides how hard to confirm would crash instead of choosing;
    * an empty summary, which ``record_effect`` refuses because every row is
      spoken eventually, and which reaches the ledger unchecked otherwise;
    * a deadline in the wrong shape. Deadlines are compared as STRINGS in SQL
      (see :func:`_check_deadline`), so ``…:00Z`` sorts after ``…:00.000Z`` and
      such a window would never close, while every parse-based reader
      (:func:`undo_window_remaining_s`, the spoken line) raises on it;
    * a malformed onward plan.

    A bad deadline drops the plan with it. A compensation offered with no window
    reads as unbounded, and "you can still delete that" with no closing time is
    a worse lie than "no plan was stored".
    """
    notes: list[str] = []

    rev = comp.reversibility
    if rev not in REVERSIBILITIES:
        notes.append(f"reversibility {rev!r} is not a class; recorded as irreversible")
        rev = "irreversible"
    comp_rev = _worse_of(comp.kind, rev)

    summary = (comp.summary or "").strip()
    if not summary:
        summary = f"I ran the {op} compensation"
        notes.append("handler returned no summary; a module-owned one was substituted")

    deadline = comp.undo_deadline
    plan: UndoPlan | None
    try:
        plan = _check_plan(comp.undo_plan, comp_rev)
    except (NotDeclarative, ValueError, OverPromise) as exc:
        plan, notes = None, [*notes, f"onward plan dropped: {exc}"]

    if deadline is not None:
        try:
            _check_deadline(deadline)
        except ValueError as exc:
            plan, deadline = None, None
            notes.append(f"onward plan and window dropped: {exc}")
    if plan is None:
        deadline = None

    return comp.kind, summary, comp_rev, plan, deadline, "; ".join(notes) or None


def _worse_of(kind: str, reversibility: Reversibility) -> Reversibility:
    """The more pessimistic of the handler's claim and the table's.

    Used only for a compensation that has ALREADY happened: refusing to record
    it would lose the ledger row entirely, and an over-cautious class costs one
    extra confirmation while an over-optimistic one costs a broken promise.
    """
    known = KIND_REVERSIBILITY.get(kind)
    if known is None:
        return reversibility
    return reversibility if _HARM[reversibility] >= _HARM[known] else known


def _result(
    con: sqlite3.Connection,
    before: Effect,
    outcome: UndoOutcome,
    now_ts: str,
    *,
    undo_effect_id: str | None = None,
    error: str | None = None,
    publish_event: bool = False,
    actor: str = "system",
    performed: str | None = None,
) -> UndoResult:
    """Build the result from the row as it is NOW, so the sentence matches the DB."""
    if outcome == "busy":
        spoken = "Something else is already working on that one."
    elif outcome == "lost_claim":
        # NOT generated from the row: the row no longer describes reality. It
        # says the effect still stands while the compensation has in fact been
        # performed, so reading the verdict off it would offer to do again the
        # one thing that must not happen twice.
        spoken = (
            "I went ahead and did it, but something else changed that entry while I was "
            "working, so I couldn't record it. Check it before asking me again."
        )
    else:
        after = get_effect(con, before.id) or before
        spoken = spoken_effect_line(after, now_ts=now_ts)
    if publish_event and outcome != "undone":
        # One kind for every non-success, with the real outcome in the payload.
        # `expired` and `irreversible` are refusals rather than failures, and
        # the payload says which; inventing new EventKinds for each would drift
        # the bus vocabulary for no reader's benefit.
        publish(
            con,
            "effect.undo_failed",
            actor,
            {
                "kind": before.kind,
                "outcome": outcome,
                "error": error,
                "spoken": spoken,
                # Set only for lost_claim, and the whole point of that event:
                # the compensation row was rolled back, so this string is the
                # system's only surviving record that the thing was done.
                "performed": performed,
            },
            job_id=before.job_id,
            effect_id=before.id,
            # No natural key: every refused or failed attempt is its own line in
            # the honesty log. Deduping them would hide a user asking four times.
            idem_key=None,
        )
    return UndoResult(
        effect_id=before.id,
        outcome=outcome,
        spoken=spoken,
        undo_effect_id=undo_effect_id,
        error=error,
    )


def _check_reversibility(value: Any) -> None:
    if value not in REVERSIBILITIES:
        raise ValueError(f"reversibility must be one of {sorted(REVERSIBILITIES)}, got {value!r}")


def _check_not_optimistic(kind: str, reversibility: Reversibility) -> None:
    known = KIND_REVERSIBILITY.get(kind)
    if known is None:
        return
    if _HARM[reversibility] < _HARM[known]:
        raise ValueError(
            f"{kind!r} is classified {known!r}; recording it as {reversibility!r} claims an undo "
            "that does not exist. Recording something WORSE than the table is allowed."
        )


def _check_plan(plan: Any, reversibility: Reversibility) -> UndoPlan | None:
    """Reject anything that will not survive a restart, at write time."""
    if plan is None:
        return None
    if reversibility == "irreversible":
        raise ValueError(
            "an irreversible effect may not carry an undo plan: a stored plan for something "
            "that cannot be undone is a promise this system will eventually speak"
        )
    if callable(plan) or not isinstance(plan, dict):
        raise NotDeclarative(
            "undo_plan must be declarative JSON ({'op':…, 'args':{…}}), not a callable or "
            f"object: a closure dies with the process that made it, got {type(plan).__name__}"
        )
    op = plan.get("op")
    if not isinstance(op, str) or not op:
        raise NotDeclarative("undo_plan needs a non-empty string 'op' naming a handler")
    args = plan.get("args", {})
    if not isinstance(args, dict):
        raise NotDeclarative(f"undo_plan['args'] must be a dict, got {type(args).__name__}")
    unexpected = set(plan) - {"op", "args", "speaks"}
    if unexpected:
        raise NotDeclarative(f"undo_plan has unknown keys: {sorted(unexpected)}")

    try:
        json.dumps(plan, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        # This is the closure check with teeth: a lambda, a Path, a datetime, an
        # open connection — anything at any depth that cannot be written down
        # cannot be executed by a process started tomorrow.
        raise NotDeclarative(
            f"undo_plan must round-trip through JSON so a future process can run it: {exc}"
        ) from exc

    speaks = plan.get("speaks")
    if speaks is not None:
        if not isinstance(speaks, str) or not speaks.strip():
            raise NotDeclarative("undo_plan['speaks'] must be a non-empty string when present")
        _check_speaks(speaks, reversibility)
    else:
        # The fallback phrase interpolates the OP, which is caller text, so it
        # can smuggle promise vocabulary into a verdict the honesty check would
        # then reject — leaving a row that was accepted at write time and
        # explodes every time anyone tries to speak it. An op like
        # 'fs.restore' has to be given a phrase of its own instead.
        try:
            _check_speaks(_DEFAULT_SPEAKS.format(op=op), reversibility)
        except OverPromise as exc:
            raise OverPromise(
                f"the default phrase for op {op!r} would over-promise ({exc}); give this plan an "
                "explicit 'speaks' that says what the compensation actually does"
            ) from exc
    return dict(plan)  # type: ignore[return-value]


def _check_speaks(speaks: str, reversibility: Reversibility) -> None:
    """A compensation phrase may not claim restoration. Checked at WRITE time.

    Here rather than at speak time so the failure lands in the stack of whoever
    wrote the lie, with the row not yet in the ledger.
    """
    if reversibility != "compensatable":
        return
    low = speaks.lower()
    for word in EXACT_WORDS:
        if word in low:
            raise OverPromise(
                f"a compensatable effect may not say {word!r}: compensation is not restoration "
                f"({speaks!r})"
            )


def _check_deadline(ts: str) -> None:
    """Deadlines are compared as STRINGS in SQL, so the format is load-bearing.

    ``2026-09-16T12:00:00Z`` and ``2026-09-16T12:00:00.000Z`` are the same
    instant and sort in opposite directions ('Z' > '.'), so a deadline in the
    wrong shape would silently never expire.
    """
    try:
        parse_ts(ts)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"undo_deadline must be RFC3339 UTC millis from jarvis.ids.now(), got {ts!r}"
        ) from exc


def _deadline_passed(e: Effect, now_ts: str) -> bool:
    return e.undo_deadline is not None and e.undo_deadline <= now_ts


def _fmt(dt: datetime) -> str:
    return f"{dt.strftime('%Y-%m-%dT%H:%M:%S')}.{dt.microsecond // 1000:03d}Z"


def _spoken_clock(ts: str | None) -> str:
    """'14:05' in the zone Jarvis SPEAKS in, not the zone it stores in.

    Slicing HH:MM straight off the stored string would say 11:05 for a 14:05
    event, because the column is UTC by construction and the user is on +03.
    jarvis.clock is the single place that conversion is allowed to live, so it
    is called rather than reimplemented. A column that will not parse must still
    not break a sentence: this is on the speaking path.
    """
    if not ts:
        return "an unknown time"
    try:
        return spoken_time(ts)
    except (TypeError, ValueError):
        return ts[11:16]


def _duration(seconds: float) -> str:
    seconds = abs(seconds)
    if seconds < 90:
        return "a moment"
    if seconds < 5400:
        return f"{round(seconds / 60)} minutes"
    if seconds < 172800:
        return f"{round(seconds / 3600)} hours"
    return f"{round(seconds / 86400)} days"


def _ago(deadline: str | None, now_ts: str) -> str:
    if deadline is None:
        return "already"
    return f"{_duration((parse_ts(now_ts) - parse_ts(deadline)).total_seconds())} ago"


def _in(deadline: str, now_ts: str) -> str:
    return f"in {_duration((parse_ts(deadline) - parse_ts(now_ts)).total_seconds())}"


def _dump(obj: Any) -> str | None:
    return None if obj is None else json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _load(text: Any) -> Any:
    return None if text is None else json.loads(text)


def _to_effect(row: sqlite3.Row) -> Effect:
    return Effect(
        id=str(row["id"]),
        kind=str(row["kind"]),
        ts=str(row["ts"]),
        summary=str(row["summary"]),
        reversibility=str(row["reversibility"]),  # type: ignore[arg-type]
        state=str(row["state"]),  # type: ignore[arg-type]
        job_id=row["job_id"],
        provider_ref=_load(row["provider_ref"]),
        undo_plan=_load(row["undo_plan"]),
        undo_deadline=row["undo_deadline"],
        confirmed_by_request_id=row["confirmed_by_request_id"],
        undone_at=row["undone_at"],
        undo_effect_id=row["undo_effect_id"],
        undo_error=row["undo_error"],
    )
