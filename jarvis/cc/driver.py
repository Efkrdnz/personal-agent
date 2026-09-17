"""``ClaudeJobRunner`` — one job, one ``ClaudeSDKClient``, one OS process.

Started detached by the dispatcher, it drives exactly one Claude Code session and
then exits. It speaks to the rest of the system only through SQLite, which is why
killing the voice app cannot touch a build and why a resume can happen on another
machine hours later.

WHAT THE RUNNER OWNS, AND WHAT IT REFUSES TO ASSUME.

*It refuses to start* if ``askUserQuestionTimeout`` is configured anywhere in the
merged settings. See :mod:`jarvis.cc.settings`: a managed 60s value auto-closes
the very wait this design rests on, and the symptom would be "Jarvis lost my
answer" rather than an error.

*It refuses to start* if the kill epoch has moved since the job was stamped. The
epoch is the durable half of the kill switch and it needs no delivery: a job
killed while the machine was off is still killed when it boots.

*It never lists ``AskUserQuestion`` in ``allowed_tools``.* A whole-tool allow
entry auto-approves the call before ``can_use_tool`` is consulted, and the SDK
warns that allow rules in SETTINGS FILES can shadow the callback invisibly.
Listing the one tool the permission host exists to serve is how you silently lose
the host, so :func:`options` asserts it is absent.

*It never sets ``permission_mode='dontAsk'``.* That mode denies AskUserQuestion
outright. The jobs schema has a CHECK constraint, and this module raises the same
named exception before the CLI is even spawned.

*It never assumes a defer took.* ``stop_reason == "tool_deferred"`` is the only
evidence that counts; anything else and the permission host blocks instead.

*It never assumes a cost.* ``ResultMessage.total_cost_usd`` is None under a Max
subscription. That is a real case, recorded as an unpriced meter, not an error.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any

from jarvis import jobs, kill, ledger
from jarvis.bus import publish
from jarvis.cc.gate import ensure_request
from jarvis.cc.hooks import DeferLedger, Hooks
from jarvis.cc.permission_host import PermissionHost
from jarvis.cc.sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    TextBlock,
)
from jarvis.cc.settings import assert_no_ask_user_question_timeout

__all__ = [
    "CONTINUE_PROMPT",
    "EFFORT_LEVELS",
    "ClaudeJobRunner",
    "RunOutcome",
    "assert_no_ask_user_question_timeout",
]

#: What a resumed session is told when the caller has nothing new to say. The
#: question re-fires on its own; this is only here because the SDK needs a turn.
CONTINUE_PROMPT = "Continue."

#: ``ClaudeAgentOptions.effort``. Validated here because a typo in a job row would
#: otherwise reach the CLI as an unknown flag value and fail at spawn time, long
#: after the voice layer that wrote it has forgotten why.
EFFORT_LEVELS: frozenset[str] = frozenset({"low", "medium", "high", "xhigh", "max"})

#: Heartbeats say "this process still exists" to reconcile. Cheap enough to do
#: often, but NOT on every message: a chatty build would write thousands of rows
#: a minute for one bit of information.
HEARTBEAT_S = 20.0

ClientFactory = Callable[[ClaudeAgentOptions], AbstractAsyncContextManager[Any]]


def _default_client(options: ClaudeAgentOptions) -> AbstractAsyncContextManager[Any]:
    return ClaudeSDKClient(options=options)


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """What one run did. The process exits on this; nothing else survives it."""

    job_id: str
    state: str
    session_id: str | None = None
    stop_reason: str | None = None
    is_error: bool = False
    deferred_request_id: str | None = None
    total_cost_usd: float | None = None
    text: tuple[str, ...] = field(default_factory=tuple)

    @property
    def deferred(self) -> bool:
        return self.state == "deferred"


class WrongJobKind(ValueError):
    """This runner was handed a job it does not know how to drive."""

    def __init__(self, job_id: str, kind: str) -> None:
        super().__init__(
            f"{job_id} is a {kind!r} job; this runner only drives 'claude_code'. "
            "Nothing was changed."
        )
        self.kind = kind


class ClaudeJobRunner:
    """Drive one job. Takes the caller's open connection; opens none of its own."""

    def __init__(
        self,
        con: sqlite3.Connection,
        job_id: str,
        *,
        actor: str | None = None,
        channel: str = "desk",
        client_factory: ClientFactory = _default_client,
        env: Mapping[str, str] | None = None,
        cli_path: str | None = None,
        settings_paths: list[str] | None = None,
        ledger_config: ledger.LedgerConfig = ledger.DEFAULT_CONFIG,
        max_turns: int | None = None,
        deferrals: DeferLedger | None = None,
        heartbeat_s: float = HEARTBEAT_S,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.con = con
        self.job_id = job_id
        self.actor = actor or f"runner:{job_id}"
        self.channel = channel
        self.client_factory = client_factory
        self.env = dict(env or {})
        self.cli_path = cli_path
        self.settings_paths = settings_paths
        self.ledger_config = ledger_config
        self.max_turns = max_turns
        self.heartbeat_s = heartbeat_s
        self._monotonic = monotonic
        self.deferrals = deferrals if deferrals is not None else DeferLedger()
        self.hooks = Hooks(con, job_id, actor=self.actor, deferrals=self.deferrals)
        self.host = PermissionHost(
            con, job_id, actor=self.actor, channel=channel, deferrals=self.deferrals
        )
        self._last_beat = 0.0

    # ───────────────────────── options ─────────────────────────

    def job(self) -> jobs.Job:
        job = jobs.get(self.con, self.job_id)
        if job is None:
            raise jobs.UnknownJob(self.job_id)
        if job.kind != "claude_code":
            # Defence in depth against a kind-blind caller. `reconcile`'s phase 2
            # used to hand this runner a `repo_setup` row, which it then drove to
            # a TERMINAL failure — destroying a build the user had approved. A
            # wrong caller must be a refusal, and it must happen here, before any
            # state has moved.
            raise WrongJobKind(self.job_id, job.kind)
        return job

    def options(self, *, resume: bool = False, allowed_tools: list[str] | None = None) -> Any:
        """Build ``ClaudeAgentOptions`` from the JOB ROW, not from arguments.

        The row is the only thing that survives this process, so a resumed run on
        another machine must produce the same options from the same row. Anything
        passed in here that is not in the row is a thing a resume would lose.
        """
        job = self.job()
        mode = job.permission_mode or "plan"
        if mode in jobs.FORBIDDEN_PERMISSION_MODES:
            raise jobs.ForbiddenPermissionMode(mode)
        if job.effort is not None and job.effort not in EFFORT_LEVELS:
            raise ValueError(
                f"job {self.job_id}: effort {job.effort!r} is not one of {sorted(EFFORT_LEVELS)}"
            )
        tools = list(allowed_tools or [])
        if "AskUserQuestion" in tools:
            raise ValueError(
                "AskUserQuestion must never be in allowed_tools: a whole-tool allow entry "
                "auto-approves it BEFORE can_use_tool is consulted, and a settings-file allow "
                "rule can shadow the callback invisibly — the permission host would vanish "
                "with no error at all."
            )
        if not job.cc_session_id:
            raise ValueError(
                f"job {self.job_id} has no cc_session_id; it cannot be resumed after a reboot"
            )
        # session_id on a first run, resume on a later one. Ours either way: the
        # UUID is generated when the job row is created, so a job that has never
        # started can still be named by a reconcile that respawns it.
        ident = {"resume": job.cc_session_id} if resume else {"session_id": job.cc_session_id}
        extra: dict[str, Any] = {}
        if self.cli_path:
            extra["cli_path"] = self.cli_path
        if self.max_turns is not None:
            extra["max_turns"] = self.max_turns
        return ClaudeAgentOptions(
            cwd=job.cwd,
            permission_mode=mode,  # type: ignore[arg-type]
            model=job.model,
            effort=job.effort,  # type: ignore[arg-type]
            allowed_tools=tools,
            can_use_tool=self.host,
            hooks={
                "PreToolUse": [HookMatcher(matcher=None, hooks=[self.hooks.pre_tool])],
                "Stop": [HookMatcher(matcher=None, hooks=[self.hooks.stop])],
                # Not in the SDK's HookEvent Literal for 0.2.153, but the SDK
                # forwards hook keys to the CLI verbatim and v2.1.273 does run
                # TaskCompleted hooks. The type list is behind the CLI here; the
                # cost of being wrong is a hook that never fires, not a crash.
                "TaskCompleted": [HookMatcher(matcher=None, hooks=[self.hooks.task_completed])],
            },  # type: ignore[dict-item]
            env=self.env,
            **ident,
            **extra,
        )

    # ───────────────────────── the run ─────────────────────────

    async def run(self, prompt: str | None = None, *, resume: bool = False) -> RunOutcome:
        """Take the job from wherever it is to ``done``, ``deferred`` or ``failed``."""
        job = self.job()
        if job.terminal:
            raise jobs.IllegalTransition(self.job_id, job.state, "starting")
        # Before the settings check, because a killed job must not even read the
        # disk on the user's behalf.
        kill.assert_epoch(self.con, job.kill_epoch)
        assert_no_ask_user_question_timeout(self.settings_paths, cwd=job.cwd)

        if job.state not in ("starting", "running"):
            jobs.set_state(self.con, self.job_id, "starting", actor=self.actor)
        jobs.attach_process(self.con, self.job_id, state="running", actor=self.actor)

        try:
            outcome = await self._drive(prompt, resume=resume)
        except BaseException as e:
            # Including CancelledError and KeyboardInterrupt: a runner that dies
            # without writing why leaves a 'running' row that reconcile can only
            # call orphaned, and the user is told "I don't know" instead of what
            # happened.
            jobs.set_state(
                self.con,
                self.job_id,
                "failed",
                actor=self.actor,
                reason=type(e).__name__,
                stop_reason=f"{type(e).__name__}: {e}"[:500],
            )
            raise
        return outcome

    async def _drive(self, prompt: str | None, *, resume: bool) -> RunOutcome:
        options = self.options(resume=resume)
        text: list[str] = []
        session_id: str | None = None
        result: Any = None

        async with self.client_factory(options) as client:
            await client.query(prompt or CONTINUE_PROMPT)
            async for message in client.receive_response():
                self._beat()
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock) and block.text.strip():
                            text.append(block.text.strip())
                elif isinstance(message, ResultMessage):
                    result = message
                    session_id = message.session_id

        if result is None:
            # No ResultMessage means the CLI went away mid-stream. Saying
            # "finished" here would be the runner inventing an outcome.
            raise RuntimeError(f"job {self.job_id}: the session ended with no result message")

        self._record_session_id(session_id)
        self._record_spend(result)

        deferred = getattr(result, "deferred_tool_use", None)
        if result.stop_reason == "tool_deferred" and deferred is not None:
            return self._park_deferred(result, deferred, tuple(text))
        return self._finish(result, tuple(text))

    # ───────────────────────── endings ─────────────────────────

    def _park_deferred(self, result: Any, deferred: Any, text: tuple[str, ...]) -> RunOutcome:
        """The away path: make the question durable, park the job, exit 0.

        Defer fires BEFORE ``can_use_tool``, so the permission host never saw
        this call and there may be no row yet. The questions are recovered from
        ``deferred_tool_use.input`` — the only place they exist at this point —
        and the tool_use_id is the same one that will re-fire on resume (S1), so
        creating the row here is idempotent with whatever the hook already did.
        """
        payload = dict(getattr(deferred, "input", {}) or {})
        req = ensure_request(
            self.con,
            job_id=self.job_id,
            actor=self.actor,
            tool_name=str(getattr(deferred, "name", "AskUserQuestion")),
            tool_use_id=str(getattr(deferred, "id", "")) or None,
            input_data=payload,
        )
        jobs.mark_blocked(self.con, self.job_id, req.id, actor=self.actor, state="deferred")
        jobs.set_state(
            self.con,
            self.job_id,
            "deferred",
            actor=self.actor,
            stop_reason="tool_deferred",
            cc_session_id=result.session_id or None,
        )
        return RunOutcome(
            job_id=self.job_id,
            state="deferred",
            session_id=result.session_id,
            stop_reason=result.stop_reason,
            is_error=bool(result.is_error),
            deferred_request_id=req.id,
            total_cost_usd=result.total_cost_usd,
            text=text,
        )

    def _finish(self, result: Any, text: tuple[str, ...]) -> RunOutcome:
        summary = (result.result or (text[-1] if text else "") or "")[:2000]
        if result.is_error:
            jobs.set_state(
                self.con,
                self.job_id,
                "failed",
                actor=self.actor,
                stop_reason=result.stop_reason or "error",
                result_summary=summary,
            )
            state = "failed"
        else:
            jobs.set_state(self.con, self.job_id, "finishing", actor=self.actor)
            jobs.set_state(
                self.con,
                self.job_id,
                "done",
                actor=self.actor,
                stop_reason=result.stop_reason,
                result_summary=summary,
            )
            state = "done"
        return RunOutcome(
            job_id=self.job_id,
            state=state,
            session_id=result.session_id,
            stop_reason=result.stop_reason,
            is_error=bool(result.is_error),
            total_cost_usd=result.total_cost_usd,
            text=text,
        )

    # ───────────────────────── bookkeeping ─────────────────────────

    def _record_session_id(self, session_id: str | None) -> None:
        """Store the CLI's session id if it differs from the one we asked for.

        It should not differ — we pass ``session_id`` ourselves — but a resume
        that forks would otherwise leave the row pointing at a session nobody can
        resume, which only shows up as "resume did nothing" days later.
        """
        if not session_id:
            return
        job = self.job()
        if job.cc_session_id != session_id and not job.terminal:
            jobs.set_state(
                self.con, self.job_id, job.state, actor=self.actor, cc_session_id=session_id
            )

    def _record_spend(self, result: Any) -> None:
        """Spend, in the unit that is actually true for this auth mode.

        Under a Max subscription ``total_cost_usd`` is None or is an
        API-equivalent estimate for usage nobody is billed per call for. Both are
        handled by :func:`jarvis.ledger.record_claude_code`, which is the ONE
        place that decision lives — so this function must not second-guess it.
        """
        usage = getattr(result, "usage", None)
        tokens: float | None = None
        if isinstance(usage, Mapping):
            counted = [
                usage.get(k)
                for k in (
                    "input_tokens",
                    "output_tokens",
                    "cache_creation_input_tokens",
                    "cache_read_input_tokens",
                )
            ]
            numbers = [float(v) for v in counted if isinstance(v, (int, float))]
            tokens = sum(numbers) if numbers else None
        cost = getattr(result, "total_cost_usd", None)
        if cost is None and tokens is None:
            # Genuinely nothing measured. Recording a zero would put "this build
            # was free" into the spend table, which is a lie with a number on it.
            return
        ids = ledger.record_claude_code(
            self.con,
            config=self.ledger_config,
            usd_est=cost,
            tokens=tokens,
            job_id=self.job_id,
            note=f"session {result.session_id}",
        )
        publish(
            self.con,
            "spend.recorded",
            self.actor,
            {"usd_est": cost, "tokens": tokens, "rows": len(ids)},
            job_id=self.job_id,
            idem_key=f"spend:{self.job_id}:{result.session_id}:{result.num_turns}",
        )

    def _beat(self) -> None:
        elapsed = self._monotonic()
        if elapsed - self._last_beat < self.heartbeat_s:
            return
        self._last_beat = elapsed
        jobs.heartbeat(self.con, self.job_id)
