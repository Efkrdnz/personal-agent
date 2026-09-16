"""Tool call -> ``requests`` row. The one translation, used by three callers.

The hook creates the row BEFORE it asks the CLI to defer, so the question
survives a process that exits a millisecond later. The permission host creates it
when it is about to block. The runner creates it from
``ResultMessage.deferred_tool_use`` because defer fires BEFORE ``can_use_tool``
and the hook may not have run in this process at all. Three call sites, one
shape — so they cannot drift apart and produce two rows for one question.

Creation is idempotent by ``tool_use_id``, which spike S1 measured to be STABLE
across defer and resume. That is what makes the three callers safe rather than
merely tidy: whoever gets there first creates the row and the others find it.

No SDK import here on purpose. The away channel must be able to build and read
these rows on a machine that has never installed ``claude-agent-sdk``.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from jarvis import answers
from jarvis.bus import publish
from jarvis.ids import dedupe_key
from jarvis.requests import (
    Presentation,
    ReqKind,
    Request,
    create_request,
    find_open_for_tool,
    make_presentation,
    next_attempt,
)

__all__ = [
    "ASK_USER_QUESTION",
    "EXIT_PLAN_MODE",
    "ensure_request",
    "exit_plan_presentation",
    "tool_permission_presentation",
]

ASK_USER_QUESTION = "AskUserQuestion"
EXIT_PLAN_MODE = "ExitPlanMode"

#: Our own words, not Claude's, and therefore safe to number: they are what the
#: user picks BETWEEN, and the plan body they apply to rides in ``intro``.
_EXIT_PLAN_OPTIONS = (
    {"label": "Approve", "description": "start building this plan"},
    {"label": "Keep planning", "description": "go back and change the plan"},
)
_TOOL_PERMISSION_OPTIONS = (
    {"label": "Allow", "description": "run it once"},
    {"label": "Deny", "description": "do not run it"},
)


def exit_plan_presentation(plan: str) -> Presentation:
    """Approve / keep planning, with the plan body as the spoken intro.

    ``verbatim`` is True for the whole presentation even though the plan body is
    only FAITHFUL tier: the two labels ARE the answer keys, and a Presentation
    carries one flag. Routing a faithful body to the deterministic reader costs a
    slightly stiffer reading; routing an exact label to a generative one is the
    failure this project exists to prevent.
    """
    return make_presentation(
        intro=plan,
        options=list(_EXIT_PLAN_OPTIONS),
        verbatim=True,
        multi=False,
        allows_free_text=True,
        free_text_prompt="Or say what to change.",
        dtmf_map={"1": 1, "2": 2},
        question="Start building this plan?",
    )


def tool_permission_presentation(tool_name: str, tool_input: Any) -> Presentation:
    """Allow / deny for one tool call, with the call itself in the intro."""
    detail = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(detail, str) or not detail.strip():
        detail = tool_input.get("file_path") if isinstance(tool_input, dict) else None
    intro = f"Claude Code wants to run {tool_name}"
    if isinstance(detail, str) and detail.strip():
        intro = f"{intro}: {detail.strip()}"
    return make_presentation(
        intro=intro,
        options=list(_TOOL_PERMISSION_OPTIONS),
        verbatim=True,
        multi=False,
        allows_free_text=True,
        free_text_prompt="Or say what to do instead.",
        dtmf_map={"1": 1, "2": 2},
        question=f"Allow {tool_name}?",
    )


def _shape(tool_name: str, input_data: dict[str, Any]) -> tuple[ReqKind, str, Presentation]:
    if tool_name == ASK_USER_QUESTION:
        return (
            "plan_question",
            answers.short_label(input_data),
            answers.presentation(input_data),
        )
    if tool_name == EXIT_PLAN_MODE:
        plan = input_data.get("plan")
        return "exit_plan", "the plan", exit_plan_presentation(str(plan or "").strip())
    return (
        "tool_permission",
        f"{tool_name.lower()} approval",
        tool_permission_presentation(tool_name, input_data),
    )


def ensure_request(
    con: sqlite3.Connection,
    *,
    job_id: str,
    actor: str,
    tool_name: str,
    tool_use_id: str | None,
    input_data: dict[str, Any],
    urgency: str = "normal",
    on_timeout: str = "defer",
    expires_in_s: int | None = None,
) -> Request:
    """Find the row that already represents this tool call, or create it.

    The ``request.created`` event carries the row id as its idempotency key, so
    a resumed runner re-firing the same question logs nothing new: the activity
    log must never claim the user was asked twice when they were asked once.

    ``on_timeout='defer'`` is the default because the question SURVIVING is
    almost always right for a build: nobody was reachable yet, and the answer can
    still arrive in the morning. A caller that means "deny if nobody answers"
    says so explicitly.
    """
    existing = find_open_for_tool(con, job_id, tool_use_id, tool_name, input_data)
    if existing is not None:
        return existing

    # A MISS MEANS A GENUINELY NEW ASK, so the attempt counter must move.
    # ``find_open_for_tool`` deliberately skips CONSUMED rows: that question was
    # asked, answered AND served. Creating at attempt=1 anyway would land on the
    # old row through UNIQUE(job_id, dedupe_key, attempt) and hand back its
    # answer — so the second `rm -rf build` of the session would be approved by
    # the yes the user gave the first one, hours ago, with nobody asked.
    key = dedupe_key(job_id, tool_name, input_data)
    kind, label, pres = _shape(tool_name, input_data)
    req = create_request(
        con,
        dedupe_key=key,
        attempt=next_attempt(con, job_id, key),
        kind=kind,  # type: ignore[arg-type]
        short_label=label,
        presentation=pres,
        payload=input_data,
        actor=actor,
        job_id=job_id,
        tool_use_id=tool_use_id,
        tool_name=tool_name,
        urgency=urgency,  # type: ignore[arg-type]
        on_timeout=on_timeout,  # type: ignore[arg-type]
        expires_in_s=expires_in_s,
    )
    publish(
        con,
        "request.created",
        actor,
        {
            "kind": kind,
            "short_label": label,
            "tool": tool_name,
            "tool_use_id": tool_use_id,
        },
        job_id=job_id,
        request_id=req.id,
        idem_key=f"req:{req.id}:created",
    )
    return req
