"""``python -m jarvis.cc`` — run one job and exit. Detached-friendly.

The dispatcher launches this with ``systemd-run --user``, with no terminal, no
controlling tty and possibly no stdout at all. So: no prompts, no colours, no
progress bars, and every write to stdout is allowed to fail silently. The real
output of this process is rows in SQLite.

EXIT CODES, because a supervisor reads them and a human reads the log:

    0   the job finished, or parked itself on a deferred question (both normal)
    1   the job failed — Claude Code returned an error
    2   refused to start: a settings file would auto-close a pending question,
        or the job asks for a permission mode that denies AskUserQuestion
    3   refused to start: the kill switch fired (the epoch moved)
    4   the job id does not exist, or the state machine refused the transition
    5   the run died in a way nothing else here anticipated

Resume is a fresh process with ``--resume``: the question re-fires, the stored
answer satisfies it, and nobody is asked twice.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence

from jarvis import jobs, kill
from jarvis.cc.driver import ClaudeJobRunner, RunOutcome
from jarvis.cc.settings import SettingsRefusal
from jarvis.db import open_db
from jarvis.reconcile import reconcile

__all__ = ["build_parser", "main"]

EXIT_OK = 0
EXIT_JOB_FAILED = 1
EXIT_REFUSED = 2
EXIT_KILLED = 3
EXIT_NO_JOB = 4
EXIT_CRASHED = 5


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m jarvis.cc", description=__doc__)
    p.add_argument("--job-id", required=True, help="the jobs row this process drives")
    p.add_argument("--db", default=None, help="database path; defaults to $JARVIS_DB")
    p.add_argument(
        "--resume",
        action="store_true",
        help="resume the job's existing session instead of starting one",
    )
    p.add_argument(
        "--prompt",
        default=None,
        help="the prompt; omitted on a resume, where the job row already holds it",
    )
    p.add_argument(
        "--channel",
        default="desk",
        help="which channel's permission policy applies (desk|telegram|phone)",
    )
    return p


def _say(payload: dict[str, object]) -> None:
    """One line of JSON, and never a reason to fail.

    A detached process writing to a closed pipe must not turn a successful build
    into a traceback, so every stdout error is swallowed here and nowhere else.
    """
    try:
        sys.stdout.write(json.dumps(payload, default=str) + "\n")
        sys.stdout.flush()
    except OSError:
        pass


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    con = open_db(args.db)
    try:
        # Every process reconciles at startup; this one runs with no spawner, so
        # it reports what needs picking up rather than picking it up itself — a
        # runner that spawned siblings would be a supervisor, which it is not.
        reconcile(con, actor=f"runner:{args.job_id}")

        prompt = args.prompt
        if prompt is None and not args.resume:
            job = jobs.get(con, args.job_id)
            prompt = job.prompt_text if job is not None else None

        runner = ClaudeJobRunner(con, args.job_id, channel=args.channel)
        outcome: RunOutcome = asyncio.run(runner.run(prompt, resume=args.resume))
        _say(
            {
                "job_id": outcome.job_id,
                "state": outcome.state,
                "stop_reason": outcome.stop_reason,
                "session_id": outcome.session_id,
                "deferred_request_id": outcome.deferred_request_id,
                "total_cost_usd": outcome.total_cost_usd,
            }
        )
        return EXIT_JOB_FAILED if outcome.state == "failed" else EXIT_OK
    except SettingsRefusal as e:
        _say({"job_id": args.job_id, "refused": str(e)})
        return EXIT_REFUSED
    except jobs.ForbiddenPermissionMode as e:
        _say({"job_id": args.job_id, "refused": str(e)})
        return EXIT_REFUSED
    except kill.KillEpochAdvanced as e:
        _say({"job_id": args.job_id, "killed": str(e)})
        return EXIT_KILLED
    except (jobs.UnknownJob, jobs.IllegalTransition) as e:
        _say({"job_id": args.job_id, "error": str(e)})
        return EXIT_NO_JOB
    except Exception as e:  # noqa: BLE001 - the exit code IS the report
        _say({"job_id": args.job_id, "crashed": f"{type(e).__name__}: {e}"})
        return EXIT_CRASHED
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
