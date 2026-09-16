"""``can_use_tool``: a tool call becomes a row, a row becomes an answer.

This is the object the architecture's four-hour phone answer actually runs
through. Three paths, in the order they are tried:

1. **The resume path, first, always.** :func:`jarvis.requests.find_answer` on
   ``tool_use_id`` — measured stable across defer and resume in spike S1 — and it
   returns in microseconds with NOBODY asked a second time. The answer was
   written while this process did not exist.
2. **The blocking path.** Create the row through the spine and POLL it. Polling,
   not an in-process future: the answer arrives on a connection this process
   never sees, from a Telegram tap or a phone call, possibly on another machine.
   Blocking is cheap precisely BECAUSE the runner is its own process — it costs
   one idle process, not the assistant.
3. **The policy path**, for every other tool: a per-channel table, default-DENY
   everywhere but the desk, recorded on the bus whichever way it goes.

THE WAIT IS BOUNDED BY EVENTS, NOT BY A TIMER. It ends when the request is
answered, cancelled, superseded or expired; when the kill epoch advances; or when
the job row leaves the states a waiting job may be in. What it must never do is
return an assumed answer, so every non-answer exit is a
:class:`PermissionResultDeny` carrying a sentence explaining itself — Claude Code
stops and says why instead of quietly proceeding.

THE ANSWER PAYLOAD. ``PermissionResultAllow(updated_input={**input_data,
"answers": answers})`` and nothing else. ``response`` is NEVER set alongside
``answers``: with it present the CLI shows Claude "The user responded: …" and
silently discards the per-question answer list. That is asserted here at runtime,
not merely avoided, because the failure is invisible from the outside.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from typing import Any

from jarvis import jobs, kill
from jarvis import requests as rq
from jarvis.bus import publish
from jarvis.cc import narrate, policy
from jarvis.cc.gate import ASK_USER_QUESTION, EXIT_PLAN_MODE, ensure_request
from jarvis.cc.hooks import DeferLedger
from jarvis.cc.sdk import PermissionResult, PermissionResultAllow, PermissionResultDeny

__all__ = [
    "CANCELLED_TEXT",
    "EXPIRED_TEXT",
    "HEARTBEAT_S",
    "KILLED_TEXT",
    "PermissionHost",
    "WaitOutcome",
]

#: Sentences Claude Code receives when a question ends without an answer. They
#: are written to be ACTED ON: "stop" and "do not assume", never "error".
CANCELLED_TEXT = (
    "The user cancelled this question. Stop here and do not assume an answer; "
    "Jarvis will re-ask when they are back."
)
EXPIRED_TEXT = (
    "Nobody was reachable before the deadline, so this was denied, not assumed. "
    "Stop here; the question will be asked again later."
)
KILLED_TEXT = "Jarvis was stopped by the user. Abandon this session immediately."

#: How often a blocked runner stamps its heartbeat, in seconds of waiting.
HEARTBEAT_S = 20.0

#: The states a job may be in while one of its questions is pending. Anything
#: else means somebody else finished, killed or re-queued this job, and a
#: blocking callback in a dead job is the exact shape of a wedged build.
_WAITABLE_STATES = frozenset({"starting", "running", "blocked", "deferred"})


@dataclass(frozen=True, slots=True)
class WaitOutcome:
    """How a wait ended. ``answer`` is None for every non-answer ending."""

    answer: rq.Answer | None
    reason: str
    interrupt: bool = False


Sleeper = Callable[[float], Coroutine[Any, Any, Any]]


class PermissionHost:
    """One job's ``can_use_tool``. Takes the caller's connection; opens none.

    Instantiate it, pass the instance as ``ClaudeAgentOptions.can_use_tool``: it
    is callable, and holding the job id on the instance is what keeps every row
    this writes attributable to one build.
    """

    def __init__(
        self,
        con: sqlite3.Connection,
        job_id: str,
        *,
        actor: str | None = None,
        channel: str = "desk",
        policies: Mapping[str, policy.ChannelPolicy] | None = None,
        deferrals: DeferLedger | None = None,
        poll_s: float = 0.25,
        sleep: Sleeper = asyncio.sleep,
    ) -> None:
        self.con = con
        self.job_id = job_id
        self.actor = actor or f"runner:{job_id}"
        self.channel = channel
        self.policies = policies
        self.deferrals = deferrals if deferrals is not None else DeferLedger()
        self.poll_s = poll_s
        self._sleep = sleep
        self._ticks_per_beat = max(1, int(HEARTBEAT_S / poll_s) if poll_s > 0 else 1)

    async def __call__(
        self,
        tool_name: str,
        input_data: dict[str, Any],
        context: Any = None,
    ) -> PermissionResult:
        tuid = getattr(context, "tool_use_id", None)

        if self.deferrals.take(tuid):
            # The CLI dropped our defer: siblings in the batch, interactive mode,
            # or a cloud session. We are here, therefore it did not take, and the
            # honest response is to block — but the log has to record the pattern
            # or it looks like the away path simply never fires.
            publish(
                self.con,
                "job.blocked",
                self.actor,
                {"defer_rejected": True, "tool": tool_name, "tool_use_id": tuid},
                job_id=self.job_id,
                idem_key=f"defer_rejected:{tuid}",
            )

        if tool_name == ASK_USER_QUESTION:
            return await self._ask_user_question(input_data, tuid)
        if tool_name == EXIT_PLAN_MODE:
            return await self._exit_plan_mode(input_data, tuid)
        return await self._by_policy(tool_name, input_data, tuid)

    # ───────────────────────── AskUserQuestion ─────────────────────────

    async def _ask_user_question(
        self, input_data: dict[str, Any], tuid: str | None
    ) -> PermissionResult:
        try:
            questions = narrate.questions_of(input_data)
        except narrate.MalformedQuestions as e:
            return self._deny("AskUserQuestion", tuid, f"That question payload is unusable: {e}")

        if tuid:
            # The resume path. No lock, no wait, no human: this single SELECT is
            # the entire reason an answer given on a bus four hours ago works.
            prior = rq.find_answer(self.con, tuid)
            if prior is not None:
                return self._allow_answers(input_data, questions, prior, tuid, replay=True)

        req = ensure_request(
            self.con,
            job_id=self.job_id,
            actor=self.actor,
            tool_name=ASK_USER_QUESTION,
            tool_use_id=tuid,
            input_data=input_data,
        )
        jobs.mark_blocked(self.con, self.job_id, req.id, actor=self.actor)
        outcome = await self._wait(req)
        self._unblock()
        if outcome.answer is None:
            return self._deny(ASK_USER_QUESTION, tuid, outcome.reason, interrupt=outcome.interrupt)
        return self._allow_answers(
            input_data, questions, outcome.answer, tuid, replay=False, req_id=req.id
        )

    def _allow_answers(
        self,
        input_data: dict[str, Any],
        questions: list[dict[str, Any]],
        answer: rq.Answer,
        tuid: str | None,
        *,
        replay: bool,
        req_id: str | None = None,
    ) -> PermissionResult:
        answers = answer.get("answers")
        if answer.get("approved") is False:
            # A REFUSAL, written through the same compare-and-swap a human uses.
            # ``on_timeout='deny'`` produces exactly this shape, and passing its
            # own sentence through is what makes Claude hear "nobody was
            # reachable, so this was denied, not assumed" — the same words the
            # activity log shows — instead of a generic "no answer".
            return self._deny(
                ASK_USER_QUESTION, tuid, str(answer.get("text") or "").strip() or EXPIRED_TEXT
            )
        if not answers:
            # An 'approved'-only or text-only answer to a plan question is a
            # channel bug, and forwarding it would send Claude an empty dict that
            # reads as "the user picked nothing".
            return self._deny(
                ASK_USER_QUESTION, tuid, "No per-question answer was recorded for that question."
            )
        try:
            narrate.validate_answers(questions, answers)
        except narrate.AnswerShapeError as e:
            # The answer was written by another process, hours ago, by a channel
            # that does not import narrate. This is the only place it can be
            # checked against the frozen options array before it reaches Claude.
            return self._deny(
                ASK_USER_QUESTION, tuid, f"That answer does not fit the question: {e}"
            )

        updated = {**input_data, "answers": answers}
        if "response" in updated:
            # S1, measured: with 'response' present the CLI shows Claude "The
            # user responded: ..." and the per-question answers are discarded.
            # An assert rather than a filter: if it ever appears, the payload we
            # were handed is not the payload we think it is.
            raise AssertionError("'response' must never be sent alongside 'answers'")

        self._consume(ASK_USER_QUESTION, input_data, tuid, replay=replay, req_id=req_id)
        return PermissionResultAllow(updated_input=updated)

    # ───────────────────────── ExitPlanMode ─────────────────────────

    async def _exit_plan_mode(
        self, input_data: dict[str, Any], tuid: str | None
    ) -> PermissionResult:
        if tuid:
            prior = rq.find_answer(self.con, tuid)
            if prior is not None:
                return self._plan_verdict(input_data, prior, tuid, replay=True)

        req = ensure_request(
            self.con,
            job_id=self.job_id,
            actor=self.actor,
            tool_name=EXIT_PLAN_MODE,
            tool_use_id=tuid,
            input_data=input_data,
        )
        jobs.mark_blocked(self.con, self.job_id, req.id, actor=self.actor)
        outcome = await self._wait(req)
        self._unblock()
        if outcome.answer is None:
            return self._deny(EXIT_PLAN_MODE, tuid, outcome.reason, interrupt=outcome.interrupt)
        return self._plan_verdict(input_data, outcome.answer, tuid, replay=False, req_id=req.id)

    def _plan_verdict(
        self,
        input_data: dict[str, Any],
        answer: rq.Answer,
        tuid: str | None,
        *,
        replay: bool,
        req_id: str | None = None,
    ) -> PermissionResult:
        approved = answer.get("approved")
        if not isinstance(approved, bool):
            return self._deny(EXIT_PLAN_MODE, tuid, "No approve-or-revise decision was recorded.")
        self._consume(EXIT_PLAN_MODE, input_data, tuid, replay=replay, req_id=req_id)
        if approved:
            # updated_input unchanged: the plan Claude wrote is the plan that was
            # approved, and editing it here would approve a different one.
            return PermissionResultAllow(updated_input=input_data)
        words = str(answer.get("text") or "").strip()
        # A denial with a reason sends Claude back to PLANNING rather than
        # stopping, which is exactly what "no, change the storage" should do.
        message = "The user wants to keep planning."
        if words:
            message = f"{message} In their words: {words}"
        return self._deny(EXIT_PLAN_MODE, tuid, message)

    # ───────────────────────── everything else ─────────────────────────

    async def _by_policy(
        self, tool_name: str, input_data: dict[str, Any], tuid: str | None
    ) -> PermissionResult:
        decision, reason = policy.decide(
            self.channel, tool_name, input_data, policies=self.policies
        )
        if decision == "deny":
            return self._deny(tool_name, tuid, reason)
        if decision == "allow":
            publish(
                self.con,
                "tool.used",
                self.actor,
                {"tool": tool_name, "decision": "allow", "reason": reason, "tool_use_id": tuid},
                job_id=self.job_id,
                idem_key=f"perm:{tuid}:allow" if tuid else None,
            )
            return PermissionResultAllow()

        if tuid:
            prior = rq.find_answer(self.con, tuid)
            if prior is not None:
                return self._permission_verdict(
                    tool_name, input_data, prior, tuid, reason, replay=True
                )
        req = ensure_request(
            self.con,
            job_id=self.job_id,
            actor=self.actor,
            tool_name=tool_name,
            tool_use_id=tuid,
            input_data=input_data,
        )
        jobs.mark_blocked(self.con, self.job_id, req.id, actor=self.actor)
        outcome = await self._wait(req)
        self._unblock()
        if outcome.answer is None:
            return self._deny(tool_name, tuid, outcome.reason, interrupt=outcome.interrupt)
        return self._permission_verdict(
            tool_name, input_data, outcome.answer, tuid, reason, replay=False, req_id=req.id
        )

    def _permission_verdict(
        self,
        tool_name: str,
        input_data: dict[str, Any],
        answer: rq.Answer,
        tuid: str | None,
        reason: str,
        *,
        replay: bool,
        req_id: str | None = None,
    ) -> PermissionResult:
        approved = answer.get("approved")
        if approved is not True:
            # Not `if approved is False`: a missing decision is a denial too. The
            # one reading that is never allowed here is "absent means yes".
            return self._deny(tool_name, tuid, f"The user did not approve {tool_name}.")
        self._consume(tool_name, input_data, tuid, replay=replay, req_id=req_id)
        publish(
            self.con,
            "tool.used",
            self.actor,
            {"tool": tool_name, "decision": "allow", "reason": reason, "asked": True},
            job_id=self.job_id,
            idem_key=f"perm:{tuid}:allow" if tuid else None,
        )
        return PermissionResultAllow()

    # ───────────────────────── the wait ─────────────────────────

    async def _wait(self, req: rq.Request) -> WaitOutcome:
        """Poll the row until it is decided, or until waiting stops making sense.

        A poll rather than a notification because the writer is another process
        and may be another machine: the row IS the channel. ``poll_s`` is the
        whole latency cost of that choice and it is a quarter of a second.
        """
        epoch = kill.current_epoch(self.con)
        tick = 0
        while True:
            tick += 1
            row = rq.get_request(self.con, req.id)
            if row is None:
                return WaitOutcome(None, CANCELLED_TEXT)
            if row.state in ("answered", "consumed"):
                # consume() is idempotent, so a resumed runner re-firing the same
                # question gets the same answer rather than an error.
                answer = rq.consume(self.con, req.id, self.actor) or row.answer
                if answer is None:
                    return WaitOutcome(None, CANCELLED_TEXT)
                return WaitOutcome(answer, "answered")
            if row.state == "cancelled":
                return WaitOutcome(None, CANCELLED_TEXT)
            if row.state == "superseded":
                return WaitOutcome(None, "That question was replaced by a newer one. Stop here.")
            if row.state == "expired":
                return WaitOutcome(None, EXPIRED_TEXT)

            # (tick - 1) % n, not tick % n == 1: with n == 1 — any poll_s at or
            # above HEARTBEAT_S — `tick % 1 == 1` is never true, so the job would
            # stop beating entirely and reconcile would call a legitimately
            # blocked runner orphaned and respawn the build underneath it.
            stop = self._must_stop_waiting(epoch, beat=(tick - 1) % self._ticks_per_beat == 0)
            if stop is not None:
                return stop
            await self._sleep(self.poll_s)

    def _must_stop_waiting(self, epoch: int, *, beat: bool = True) -> WaitOutcome | None:
        """The kill switch and the job row, checked on every tick.

        Without this the wait is genuinely unbounded: the user says the kill
        phrase, the epoch advances, and a runner parked in ``can_use_tool`` would
        sit there until someone signals the process. The epoch is the durable
        half of the kill switch, so an idle runner must read it rather than wait
        to be told.
        """
        if kill.current_epoch(self.con) != epoch:
            return WaitOutcome(None, KILLED_TEXT, interrupt=True)
        job = jobs.get(self.con, self.job_id)
        if job is None:
            return WaitOutcome(None, KILLED_TEXT, interrupt=True)
        if job.state not in _WAITABLE_STATES:
            return WaitOutcome(
                None,
                f"This job is {job.state}; it is no longer waiting for an answer.",
                interrupt=job.terminal,
            )
        if beat:
            # Throttled, because 'blocked' is an ACTIVE state and reconcile calls
            # a job with a stale heartbeat orphaned — but a write every poll tick
            # would be twenty thousand UPDATEs across a ninety-minute wait, all
            # of them taking the same write lock the ANSWER needs.
            jobs.heartbeat(self.con, self.job_id)
        return None

    # ───────────────────────── bookkeeping ─────────────────────────

    def _unblock(self) -> None:
        """Back to running, if this job is still in a state that can run.

        Guarded rather than unconditional: by the time an answer lands the job
        may have been killed, and turning a killed job back into a running one
        would be this module lying to the briefing.
        """
        job = jobs.get(self.con, self.job_id)
        if job is not None and job.state in ("blocked", "deferred"):
            jobs.unblock(self.con, self.job_id, actor=self.actor)

    def _consume(
        self,
        tool_name: str,
        input_data: dict[str, Any],
        tuid: str | None,
        *,
        replay: bool,
        req_id: str | None = None,
    ) -> None:
        """Mark the answer as delivered. Idempotent, because a replay is normal.

        On the replay path the row id is not in hand — the answer came back from
        ``find_answer`` without one — so it is looked up the same way the driver
        looks it up, by ``tool_use_id`` first and the dedupe key second.
        """
        if req_id is None and tuid:
            found = rq.find_open_for_tool(self.con, self.job_id, tuid, tool_name, input_data)
            req_id = found.id if found is not None else None
        if req_id is not None:
            rq.consume(self.con, req_id, self.actor)
        publish(
            self.con,
            "request.consumed",
            self.actor,
            {"tool_use_id": tuid, "replayed": replay},
            job_id=self.job_id,
            request_id=req_id,
            idem_key=f"req:{tuid}:consumed" if tuid else None,
        )

    def _deny(
        self, tool_name: str, tuid: str | None, message: str, *, interrupt: bool = False
    ) -> PermissionResultDeny:
        publish(
            self.con,
            "tool.denied",
            self.actor,
            {"tool": tool_name, "tool_use_id": tuid, "reason": message},
            job_id=self.job_id,
            idem_key=f"perm:{tuid}:deny" if tuid else None,
        )
        return PermissionResultDeny(message=message, interrupt=interrupt)
