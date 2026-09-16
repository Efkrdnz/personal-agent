"""The hooks: the activity log, the defer decision, and the "go find them" events.

A hook NEVER renders anything and never decides routing. It emits and returns.

THREE THINGS HERE ARE MEASURED FACTS, not preferences.

``PreToolUse`` RETURNS ``continue_``, NOT AN ALLOW. A hook ``allow`` does not
short-circuit the deny and ask rules that follow it, so "allow" here would buy
nothing and would cost the ability to tell, later, whether a decision came from
the hook or from the permission host. ``AskUserQuestion`` reaches
``can_use_tool`` either way, which is the only thing the hook needs to be true.

``defer`` IS A REQUEST, NOT AN OUTCOME. Spike S1 proved a PreToolUse hook CAN
return ``permissionDecision: "defer"`` and that the CLI then exits with
``stop_reason="tool_deferred"``. But the shipped CLI drops a defer with only a
warn log in three cases — more than one tool call in the assistant batch,
interactive (non-print) mode, and calls served to a cloud session, where it is
converted to a hard deny. So the runner NEVER assumes the defer took. Every
requested defer is recorded in a :class:`DeferLedger` that the permission host
consults; if ``can_use_tool`` fires for that same ``tool_use_id``, the defer was
ignored and the host falls through to BLOCKING, publishing the fact rather than
leaving it mysterious.

THE ROW IS CREATED BEFORE THE DEFER IS ASKED FOR. Defer fires before
``can_use_tool``, and the process may be gone a millisecond later. If the row is
not already durable at that point, the away channel has nothing to present and
the question exists only inside a dead process's memory.

DELIBERATE DEVIATION FROM THE DESIGN SKETCH: the hook does NOT move the job to
``deferred``. It cannot know whether the defer will be honoured, and a state that
records an intention as a fact is worse than no state at all — ``deferred`` also
has no legal transition to ``blocked``, so a dropped defer would leave the fall
back-to-blocking path raising IllegalTransition. The runner sets ``deferred``
when the CLI actually exits that way, which is the moment it becomes true.

No SDK import: the hook payloads are plain dicts in both directions.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any

from jarvis import answers, presence
from jarvis.bus import publish
from jarvis.cc.gate import ASK_USER_QUESTION, ensure_request

__all__ = [
    "DEFER_REASON",
    "STOP_EVENT",
    "TASK_COMPLETED_EVENT",
    "DeferLedger",
    "Hooks",
    "defer_decision",
]

#: Spoken back to Claude Code, and read by a human in the log. It says what will
#: happen next, because "deferred" on its own reads like an error.
DEFER_REASON = "The user is away. Jarvis will reach them and resume this session."

#: Not in bus.EventKind's Literal, which is documentation rather than a runtime
#: gate — the bus takes any string by design, precisely so a new kind at 3am is a
#: log line and not an outage. The router needs a kind it can filter on to
#: implement "the build finished, go find the user"; a payload flag on
#: job.progress would make that a scan instead of an index lookup.
TASK_COMPLETED_EVENT = "task.completed"
STOP_EVENT = "job.stopped"


def defer_decision(reason: str = DEFER_REASON) -> dict[str, Any]:
    """The exact hook output S1 measured. Shape first, wording second."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "defer",
            "permissionDecisionReason": reason,
        }
    }


class DeferLedger:
    """Which tool calls we asked to defer, so a DROPPED defer is detectable.

    Instance state, never module state: two runners in one process (tests, or a
    future supervisor) must not see each other's deferrals. It is in-memory on
    purpose — its whole lifetime is one CLI run, and the durable half of the same
    fact is the ``requests`` row.
    """

    __slots__ = ("_ids",)

    def __init__(self) -> None:
        self._ids: set[str] = set()

    def add(self, tool_use_id: str | None) -> None:
        if tool_use_id:
            self._ids.add(tool_use_id)

    def __contains__(self, tool_use_id: object) -> bool:
        return tool_use_id in self._ids

    def take(self, tool_use_id: str | None) -> bool:
        """True if we asked to defer this call — and forget it, so it reads once.

        Reading it once matters: the same ``tool_use_id`` re-fires after a resume
        (S1), and a sticky flag would report "the defer was rejected" on the
        replay, when in fact it was honoured and the answer is already waiting.
        """
        if tool_use_id and tool_use_id in self._ids:
            self._ids.discard(tool_use_id)
            return True
        return False


PresenceReader = Callable[[sqlite3.Connection], str]


def _live_presence(con: sqlite3.Connection) -> str:
    return presence.evaluate_presence(con).state


class Hooks:
    """One job's hooks. Holds the caller's connection; opens none of its own."""

    def __init__(
        self,
        con: sqlite3.Connection,
        job_id: str,
        *,
        actor: str | None = None,
        deferrals: DeferLedger | None = None,
        presence_state: PresenceReader = _live_presence,
        defer_when_away: bool = True,
    ) -> None:
        self.con = con
        self.job_id = job_id
        self.actor = actor or f"runner:{job_id}"
        self.deferrals = deferrals if deferrals is not None else DeferLedger()
        self.presence_state = presence_state
        self.defer_when_away = defer_when_away

    # ───────────────────────── PreToolUse ─────────────────────────

    async def pre_tool(
        self,
        data: dict[str, Any],
        tool_use_id: str | None,
        context: Any = None,
    ) -> dict[str, Any]:
        """Log every tool call; ask for a defer only when nobody could answer."""
        tool_name = str(data.get("tool_name") or "")
        tool_input = data.get("tool_input") or {}
        tuid = tool_use_id or data.get("tool_use_id")

        publish(
            self.con,
            "tool.used",
            self.actor,
            {"tool": tool_name, "input": tool_input, "tool_use_id": tuid},
            job_id=self.job_id,
            # The same tool_use_id re-fires after a resume. Deduping on it is
            # what keeps the activity log from claiming the tool ran twice.
            idem_key=f"tool:{tuid}" if tuid else None,
        )

        if tool_name != ASK_USER_QUESTION or not self.defer_when_away:
            return {"continue_": True}

        try:
            answers.questions_of(tool_input)
        except answers.MalformedQuestions as e:
            # Not the hook's problem to solve: can_use_tool will deny it with a
            # message Claude can act on. Raising here would kill the whole run
            # over a payload we merely could not narrate.
            publish(
                self.con,
                "tool.denied",
                self.actor,
                {"tool": tool_name, "tool_use_id": tuid, "reason": str(e)},
                job_id=self.job_id,
            )
            return {"continue_": True}

        req = ensure_request(
            self.con,
            job_id=self.job_id,
            actor=self.actor,
            tool_name=tool_name,
            tool_use_id=tuid,
            input_data=dict(tool_input),
        )

        state = self.presence_state(self.con)
        if not presence.should_defer(state):
            return {"continue_": True}

        self.deferrals.add(tuid)
        publish(
            self.con,
            "job.deferred",
            self.actor,
            {
                "defer_requested": True,
                "presence": state,
                "tool_use_id": tuid,
                "short_label": req.short_label,
            },
            job_id=self.job_id,
            request_id=req.id,
            idem_key=f"defer:{tuid}" if tuid else None,
        )
        return defer_decision()

    # ───────────────────────── the "go find them" hooks ─────────────────────────

    async def task_completed(
        self,
        data: dict[str, Any],
        tool_use_id: str | None = None,
        context: Any = None,
    ) -> dict[str, Any]:
        """A task finished. Someone else decides whether to interrupt the user."""
        return self._announce(TASK_COMPLETED_EVENT, data, tool_use_id)

    async def stop(
        self,
        data: dict[str, Any],
        tool_use_id: str | None = None,
        context: Any = None,
    ) -> dict[str, Any]:
        """The session stopped talking. The trigger for "the build finished"."""
        return self._announce(STOP_EVENT, data, tool_use_id)

    def _announce(self, kind: str, data: dict[str, Any], tool_use_id: str | None) -> dict[str, Any]:
        payload = {
            "hook": data.get("hook_event_name"),
            "session_id": data.get("session_id"),
            "tool_use_id": tool_use_id,
        }
        publish(self.con, kind, self.actor, payload, job_id=self.job_id)
        # Never {"decision": "block"}: a hook that blocks the stop makes the
        # model keep going, and "keep going" is a routing decision, which a hook
        # is not allowed to make.
        return {"continue_": True}
