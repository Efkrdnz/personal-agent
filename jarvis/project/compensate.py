"""The undo path for a repository: run what the token has, and say what it could not do.

``delete_repo`` is off, so "undo that" is COMPENSATION and not reversal. The
compensation is up to three PATCHes — rename to ``zz-abandoned-<slug>``, make
private, archive — in the order :data:`jarvis.github.repos.COMPENSATION_ORDER`
fixes, and any of them can fail on its own.

A PARTIAL COMPENSATION THAT REPORTS SUCCESS IS THE FAILURE MODE. So the handler
here reports three different things three different ways:

* nothing worked and every refusal was a real answer -> it RAISES. The spine
  marks the effect ``undo_failed`` with the reason, leaves it retryable, and says
  "I already tried and it did not work, so nothing has changed";
* something worked and something did not -> it RETURNS, and the summary names
  each step it could not do and why. That sentence is the deliverable of this
  whole stage;
* a step got NO ANSWER -> it returns rather than raising, even if nothing else
  succeeded, because retrying a rename that may already have happened is the
  same double-effect hazard as retrying a create.

One :class:`jarvis.effects.Compensation` comes back, not three. The spine writes
the compensation row and its ``undo_effect_id`` link in ONE transaction — a
handler that wrote its own effect rows could leave a compensation nothing can
explain, and :func:`jarvis.effects.undo_handler` says so. The per-step outcome
lives in ``provider_ref``, which is what :func:`shortfall` reads, so a channel
rendering "and it could not rename it" never has to parse English.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from typing import Any

from jarvis import effects as fx
from jarvis.github import repos as gh
from jarvis.github.scopes import (
    COMPENSATIONS,
    Capabilities,
    Operation,
    abandoned_name,
    implied_undo_plan,
)
from jarvis.github.transport import GithubError, Transport, TransportError

__all__ = [
    "COMPENSATE_OP",
    "COMPENSATION_KIND",
    "DELETE_TRUTH",
    "UNSURE",
    "assert_honest",
    "ledger_class",
    "plan_for",
    "register_repo_compensator",
    "repo_compensator",
    "shortfall",
    "spoken_summary",
]

#: The declarative op the plan names — :func:`jarvis.github.scopes.implied_undo_plan`
#: writes it, the registry here answers it, and the string is the contract between
#: a row written today and a process started next month.
COMPENSATE_OP = "github.repo_compensate"

#: The kind of the row the compensation lands as. Past tense, like every other
#: effect summary in the ledger.
COMPENSATION_KIND = "github.repo_compensated"

#: Said every time a repository's fate is described, because it is the one fact
#: about this token that every such sentence has to carry.
DELETE_TRUTH = (
    "I can't delete it — the token deliberately has no delete_repo scope, so the repository is "
    "still there"
)

_PAST: dict[Operation, str] = {
    "rename": "renamed it to {to}",
    "set_private": "made it private",
    "archive": "archived it",
}

_PRESENT: dict[Operation, str] = {
    "rename": "rename it to {to}",
    "set_private": "make it private",
    "archive": "archive it",
}

#: One of these must appear in any sentence this module produces. Deleting the
#: honest half has to fail as loudly as adding a dishonest half.
_CANNOT_DELETE = ("can't delete", "cannot delete", "could not delete")

#: No answer came back for this step, so we do not know whether it happened. Its
#: own phrase because "I could not rename it" and "I do not know whether the
#: rename happened" call for different actions from the human who hears them.
UNSURE = "no answer from GitHub, so I don't know whether it happened"


def assert_honest(line: str) -> str:
    """Refuse a sentence that claims more than a compensation can deliver.

    The BEFORE sentence is guarded by
    :func:`jarvis.github.scopes.spoken_capability_line`, which owns the
    clause-level check against the spine's promise vocabulary. This is the AFTER
    sentence, and it has one job the other cannot do: whatever the steps did, it
    must still say that the repository could not be deleted.
    """
    low = line.lower()
    for word in fx.EXACT_WORDS:
        if word in low:
            raise fx.OverPromise(f"a compensation summary may not claim {word!r}: {line!r}")
    if not any(marker in low for marker in _CANNOT_DELETE):
        raise fx.OverPromise(f"a compensation summary must say it could not delete: {line!r}")
    return line


def spoken_summary(
    full_name: str,
    *,
    done: Sequence[Operation],
    failed: Sequence[tuple[Operation, str]],
    rename_to: str,
    unsure: Sequence[Operation] = (),
) -> str:
    """What the compensation really did, including what it did not.

    Generated rather than written at the call site, so the sentence and the
    ``provider_ref`` cannot disagree about which steps succeeded.

    ``unsure`` GETS ITS OWN SENTENCE, and that is the whole reason this function
    takes it separately from ``failed``. "I could not rename it (no answer from
    GitHub, so I don't know whether it happened)" is a sentence that both asserts
    the rename did not happen and admits it might have — and the definite half is
    the half a human acts on. They hear "it is still called comment-watcher", go
    looking for it under that name, or take the abandoned name for something
    else. :data:`UNSURE` exists precisely because "I could not" and "I don't know"
    call for different actions, so it must never be read out inside "I could not".
    """
    did = [_PAST[op].format(to=rename_to) for op in done]
    could_not = [f"{_PRESENT[op].format(to=rename_to)} ({why})" for op, why in failed]
    tried = [_PRESENT[op].format(to=rename_to) for op in unsure]

    parts: list[str] = []
    if did and could_not:
        parts.append(f"On {full_name} I {_join(did)}, but I could not {_join(could_not)}.")
    elif did:
        parts.append(f"On {full_name} I {_join(did)}.")
    elif could_not:
        parts.append(f"On {full_name} I could not {_join(could_not)}.")
    elif not tried:
        parts.append(f"On {full_name} there was nothing for me to do.")
    if tried:
        lead = "I tried to" if parts else f"On {full_name} I tried to"
        parts.append(f"{lead} {_join(tried)}, but got {UNSURE}.")
    parts.append(f"{DELETE_TRUTH}.")
    return assert_honest(" ".join(parts))


def plan_for(caps: Capabilities, *, owner: str, slug: str) -> dict[str, Any] | None:
    """The declarative plan for this matrix, HEDGED when nothing is certain.

    :func:`jarvis.github.scopes.implied_undo_plan` writes the plan for
    capabilities that came back ``yes``, and that is the plan whenever there is
    one. But a fine-grained token reports no scopes at all, so a whole common
    class of credential answers ``unknown`` to everything — and "unknown" is not
    "no". Refusing to store a plan for it would mean the confirmation says "I'm
    not sure I could archive it" and the later undo says "there is nothing I can
    do", which are two different answers to one question.

    So an all-unknown matrix gets the same steps with a ``speaks`` that promises
    an ATTEMPT rather than an outcome. If the attempt fails, the handler reports
    the shortfall in exactly the same words it would for a refused step.
    """
    plan = implied_undo_plan(caps, owner=owner, slug=slug)
    if plan is not None:
        return plan
    unsure = [op for op in gh.COMPENSATION_ORDER if op in caps.unknown_compensations]
    if not unsure:
        return None
    rename_to = abandoned_name(slug)
    return {
        "op": COMPENSATE_OP,
        "args": {
            "owner": owner,
            "slug": slug,
            "operations": [str(op) for op in unsure],
            "rename_to": rename_to if "rename" in unsure else None,
        },
        "speaks": "try to " + _join([_PRESENT[op].format(to=rename_to) for op in unsure]),
    }


def ledger_class(caps: Capabilities) -> fx.Reversibility:
    """The class the LIVE row is recorded with: compensatable, or nothing at all.

    Never ``reversible``, even for a token that reports ``delete_repo``: nothing
    in this package deletes a repository, so a row promising it would be offering
    a compensation no handler performs.

    THE CONDITION IS :func:`plan_for`'S, RESTATED — "is there a step to attempt?"
    — because :func:`jarvis.effects.record_effect` refuses a plan on an
    irreversible row, so a class that disagrees with the plan is not a wrong
    sentence but an exception thrown after the repository already exists.
    :func:`implied_reversibility` cannot answer this question: it reports
    ``reversible`` for a token holding ``delete_repo``, which is neither of the
    two classes this function may return, and reading it as "not compensatable"
    is how the most capable token in the world got the least capable ledger row.
    """
    if caps.available_compensations or caps.unknown_compensations:
        return "compensatable"
    return "irreversible"


def shortfall(effect: fx.Effect) -> tuple[str, ...]:
    """The steps a compensation did not complete, read off the row it wrote."""
    ref = effect.provider_ref or {}
    return tuple(str(s) for s in [*(ref.get("failed") or []), *(ref.get("unknown") or [])])


def repo_compensator(transport: Transport, caps: Capabilities | None = None) -> fx.UndoHandler:
    """Build the handler for ONE process's transport. Registered, never imported.

    :func:`jarvis.effects.undo_handler` dispatches ``(con, effect, args)`` — there
    is no slot for a client and there must be no module-level one, so the process
    holding the credential builds this at startup with its own transport, exactly
    as a :class:`jarvis.bus.Redactor` is built at startup and passed down. A
    process that never registered reports ``no_handler``, which the spine already
    handles honestly.

    ``caps`` is optional and is used to SKIP a step the token is known not to
    have. A plan written last month may name an operation a re-scoped token can no
    longer perform; believing the row over the matrix turns an honest shortfall
    into a refusal nobody expected. ``unknown`` is attempted — that is what
    unknown means.
    """

    def compensate(
        con: sqlite3.Connection, effect: fx.Effect, args: dict[str, Any]
    ) -> fx.Compensation:
        owner = str(args.get("owner") or "")
        slug = str(args.get("slug") or "")
        if not owner or not slug:
            raise ValueError(f"{COMPENSATE_OP} plan names no repository: {sorted(args)}")
        rename_to = str(args.get("rename_to") or abandoned_name(slug))
        steps = _steps(args.get("operations"), caps)

        current = slug
        done: list[Operation] = []
        failed: list[tuple[Operation, str]] = []
        unknown: list[Operation] = []
        for step in steps:
            try:
                if step == "rename":
                    gh.rename(transport, owner, current, rename_to)
                elif step == "set_private":
                    gh.set_private(transport, owner, current)
                else:
                    gh.archive(transport, owner, current)
            except TransportError:
                # No answer. NOT retried and NOT reported as a failure: the step
                # may well have happened.
                unknown.append(step)
            except GithubError as exc:
                failed.append((step, _why(step, rename_to, exc, moved="rename" in unknown)))
            else:
                done.append(step)
                if step == "rename":
                    # Everything after this has to address the new name, or it
                    # 404s on a repository that moved a moment ago.
                    current = rename_to

        if not done and not unknown:
            reasons = "; ".join(f"{s}: {why}" for s, why in failed) or "no steps were available"
            raise RuntimeError(f"nothing could be done to {owner}/{slug}: {reasons}")

        return fx.Compensation(
            kind=COMPENSATION_KIND,
            summary=spoken_summary(
                f"{owner}/{slug}",
                done=done,
                failed=failed,
                unsure=unknown,
                rename_to=rename_to,
            ),
            # Not offered back. Un-archiving and renaming again is a decision for
            # a human, and a plan here would be an offer to undo an undo.
            reversibility="irreversible",
            provider_ref={
                "full_name": f"{owner}/{current}",
                "was": f"{owner}/{slug}",
                "done": [str(s) for s in done],
                "failed": [str(s) for s, _ in failed],
                "unknown": [str(s) for s in unknown],
                "renamed_to": rename_to if "rename" in done else None,
                "undo_of": effect.id,
            },
        )

    return compensate


def register_repo_compensator(
    transport: Transport, caps: Capabilities | None = None
) -> fx.UndoHandler:
    """Register :func:`repo_compensator` for this process. Called once, at startup.

    Registering twice with different transports is a programming error in which
    the last call wins, which is exactly why it belongs in a startup path rather
    than in a request path.
    """
    return fx.undo_handler(COMPENSATE_OP)(repo_compensator(transport, caps))


def _steps(planned: Any, caps: Capabilities | None) -> tuple[Operation, ...]:
    """The plan's operations, in execution order, minus any the token lacks NOW."""
    named = planned if isinstance(planned, (list, tuple)) else COMPENSATIONS
    asked = {str(op) for op in named}
    out: list[Operation] = []
    for op in gh.COMPENSATION_ORDER:
        if op not in asked or op not in COMPENSATIONS:
            continue
        operation: Operation = op  # type: ignore[assignment]
        if caps is not None and caps.of(operation) == "no":
            continue
        out.append(operation)
    return tuple(out)


def _why(step: Operation, rename_to: str, exc: GithubError, *, moved: bool = False) -> str:
    """One short clause a human can act on, from a GitHub error.

    ``moved`` says an earlier rename got no answer, which makes a later 404 mean
    something quite specific: the repository is under one of two names and we
    addressed the other. Reporting that as a bare "404 Not Found" is accurate and
    useless — it sends a human looking for a fault in the wrong place.
    """
    if step == "rename" and gh.name_already_exists(exc):
        return f"{rename_to} is taken too"
    if gh.archived_read_only(exc):
        # The ordering guard fired: something archived this before we got here, and
        # an archived repository is read-only even to the token that made it.
        return "it is already archived, and an archived repository is read-only"
    if moved and exc.status == 404:
        return f"I couldn't find it — the rename to {rename_to} may have gone through after all"
    return f"{exc.status} {exc.message}"


def _join(parts: Iterable[str]) -> str:
    items = list(parts)
    if len(items) <= 1:
        return items[0] if items else ""
    return f"{', '.join(items[:-1])} and {items[-1]}"
