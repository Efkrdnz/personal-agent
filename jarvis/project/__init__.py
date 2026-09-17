"""The project lifecycle: a spoken name becomes a repository, a clone and a job.

Stage 4's upper half. It sits ABOVE :mod:`jarvis.github` and BELOW the channels
and the driver: it may import the spine and the GitHub client, and it must never
import ``jarvis.cc``, ``jarvis.telegram``, ``jarvis.voice``, ``jarvis.audio``,
``jarvis.live`` or ``jarvis.capture``. ``tools/check_layers.py`` enforces both
directions, because a layer nobody checks is a layer that quietly becomes a mutual
dependency.

    slug.py        spoken Turkish -> a repository name, or a refusal. Pure.
    confirm.py     the verbatim read-back and the rename loop, as ``requests`` rows
    outbox.py      the at-most-once row that makes creation un-retryable
    compensate.py  the undo handler, and the sentence that admits the shortfall
    workspace.py   clone or refuse, with the caller's token and none of it on disk
    lifecycle.py   the order: propose, create, clone, start the job, undo
    mode.py        local-vs-cloud, recorded rather than acted on (ADR 0009)

What it does NOT own: the HTTP client, the capability matrix and the spoken line
about what can be undone all live in :mod:`jarvis.github`, next to the responses
they are read from.

NOTHING HERE READS A CREDENTIAL. The GitHub transport and the git token are
parameters on every call that needs them, which is what makes it structurally
impossible for a test to reach somebody's real account.
"""

from __future__ import annotations

from jarvis.project.compensate import (
    COMPENSATE_OP,
    COMPENSATION_KIND,
    register_repo_compensator,
    shortfall,
)
from jarvis.project.confirm import Decision, raise_confirmation, raise_rename, readback
from jarvis.project.lifecycle import (
    REPO_CREATE_KIND,
    REPO_LIVE_KIND,
    Creation,
    ProjectConfig,
    Proposal,
    create,
    prepare_workspace,
    propose,
    run_repo_create,
    start_build_job,
    undo_repo,
    undoable_repo_effect,
)
from jarvis.project.mode import ModeDecision
from jarvis.project.mode import resolve as resolve_mode
from jarvis.project.slug import SlugError, abandoned_name, slugify
from jarvis.project.workspace import SubprocessGit, Workspace

__all__ = [
    "COMPENSATE_OP",
    "COMPENSATION_KIND",
    "REPO_CREATE_KIND",
    "REPO_LIVE_KIND",
    "Creation",
    "Decision",
    "ModeDecision",
    "ProjectConfig",
    "Proposal",
    "SlugError",
    "SubprocessGit",
    "Workspace",
    "abandoned_name",
    "create",
    "prepare_workspace",
    "propose",
    "raise_confirmation",
    "raise_rename",
    "readback",
    "register_repo_compensator",
    "resolve_mode",
    "run_repo_create",
    "shortfall",
    "slugify",
    "start_build_job",
    "undo_repo",
    "undoable_repo_effect",
]
