"""Clone the repository into the workspace, or refuse and say exactly why.

Five things can be sitting at ``<workspace>/<name>`` when Jarvis gets there, and
four of them are not "fine". Each gets its own outcome and its own sentence,
because "I couldn't set up the directory" is the kind of answer that makes a user
delete the wrong folder:

``cloned``         nothing was there; it is there now.
``reused``         a clone of the SAME repository, with nothing uncommitted.
``different_repo`` a clone of something else. REFUSED — running a build in it
                   would commit this project's work to another repository.
``dirty``          the same repository with uncommitted work in it. REFUSED, and
                   the sentence NAMES the files, because the whole reason to
                   refuse is that somebody has something there they have not
                   saved.
``not_a_repo``     a directory with no git in it, or one that is merely inside
                   another repository's working tree. Refused rather than cloned
                   into, and refused rather than deleted.

THE TOKEN NEVER TOUCHES THE DISK. ``git clone https://user:token@github.com/...``
writes the credential into ``.git/config``, in the working tree, in plaintext,
forever — the same class of mistake as a ``.gitignore`` pattern that matches
nothing. So the token is passed to the child process in its ENVIRONMENT and read
by a one-line credential helper installed with ``-c`` for that single invocation:
``-c`` settings are not persisted, the helper text names the variable rather than
the secret, and ``git`` never learns a URL with a password in it. After a clone
this module READS the config back and raises if the token is in it anyway,
because "I was careful" is not a control.

NOTHING HERE TOUCHES THE NETWORK IN A TEST. Every git call goes through the
:class:`Git` seam; the suite passes :class:`FakeGit`, which also asserts that the
token never appears in ``argv``.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

__all__ = [
    "CLONE_HOST",
    "CredentialLeak",
    "FakeGit",
    "Git",
    "GitResult",
    "SubprocessGit",
    "Workspace",
    "WorkspaceState",
    "clone_url",
    "prepare",
    "same_repo",
]

#: Where a clone comes from. A constant rather than an f-string at each call site
#: so a GitHub Enterprise host is one edit, and so the URL can never be built
#: next to a token by somebody in a hurry.
CLONE_HOST = "https://github.com"

WorkspaceState = Literal[
    "cloned", "reused", "different_repo", "dirty", "not_a_repo", "clone_failed"
]

#: A shell function installed for ONE invocation. It names the environment
#: variable; the value never appears in argv, in the config file or in a log.
_CREDENTIAL_HELPER = (
    '!f() { test "$1" = get && '
    'printf "username=x-access-token\\npassword=%s\\n" "$JARVIS_GIT_TOKEN"; }; f'
)


class CredentialLeak(RuntimeError):
    """A token was found in the working tree. Raised loudly, never logged quietly.

    Reachable only through a bug — or through a ``Git`` implementation that
    embeds the credential in the URL — which is exactly why it is checked rather
    than assumed.
    """


@dataclass(frozen=True, slots=True)
class GitResult:
    """One finished git invocation."""

    code: int
    out: str = ""
    err: str = ""

    @property
    def ok(self) -> bool:
        return self.code == 0

    @property
    def message(self) -> str:
        """The line worth showing a human, whichever stream it came out on."""
        return (self.err.strip() or self.out.strip() or f"git exited {self.code}").splitlines()[-1]


class Git(Protocol):
    """Run one git command. The seam that keeps the network out of the suite."""

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        token: str | None = None,
        timeout: float | None = None,
    ) -> GitResult:
        """Run ``git <args>``; ``token`` is passed by ENVIRONMENT, never in ``args``."""


@dataclass(slots=True)
class SubprocessGit:
    """The real one. No third-party dependency, and no secret on disk.

    ``env`` is the base environment (defaults to this process's) so a caller can
    run git in a sanitised environment without this module reaching for
    ``os.environ`` behind its back.
    """

    timeout_s: float = 120.0
    env: Mapping[str, str] | None = None

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        token: str | None = None,
        timeout: float | None = None,
    ) -> GitResult:
        argv = ["git"]
        if token is not None:
            # The empty value first RESETS any helper inherited from the user's
            # global config, so a machine with a cached credential does not
            # authenticate as somebody else.
            argv += ["-c", "credential.helper=", "-c", f"credential.helper={_CREDENTIAL_HELPER}"]
        argv += list(args)

        env = dict(self.env if self.env is not None else os.environ)
        # A git that cannot prompt fails in seconds instead of hanging a voice
        # assistant forever on an invisible password prompt.
        env["GIT_TERMINAL_PROMPT"] = "0"
        if token is not None:
            env["JARVIS_GIT_TOKEN"] = token
        else:
            env.pop("JARVIS_GIT_TOKEN", None)

        try:
            done = subprocess.run(  # noqa: S603 - argv is built here, never a shell string
                argv,
                cwd=str(cwd) if cwd is not None else None,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout if timeout is not None else self.timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return GitResult(code=124, err="git timed out")
        except OSError as exc:
            return GitResult(code=127, err=f"{type(exc).__name__}: {exc}")
        return GitResult(code=done.returncode, out=done.stdout, err=done.stderr)


@dataclass(slots=True)
class FakeGit:
    """Scripted git. Records argv, and refuses to let a token reach it.

    ``clone_writes`` is what a clone leaves on disk, so the reuse, dirty and
    wrong-remote paths can be exercised without a server: a test clones once and
    then runs ``prepare`` again against the directory that clone made.
    """

    results: dict[str, list[GitResult]] = field(default_factory=dict)
    default: GitResult = GitResult(0)
    calls: list[tuple[str, ...]] = field(default_factory=list)
    tokens: list[str | None] = field(default_factory=list)
    clone_writes: dict[str, str] = field(default_factory=dict)
    clone_creates_dir: bool = True

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        token: str | None = None,
        timeout: float | None = None,
    ) -> GitResult:
        argv = tuple(str(a) for a in args)
        self.calls.append(argv)
        self.tokens.append(token)
        if token is not None and any(token in a for a in argv):
            raise AssertionError(f"the token reached git's argv: {argv}")
        verb = argv[0] if argv else ""
        queued = self.results.get(verb)
        result = queued.pop(0) if queued else self.default
        if verb == "clone" and result.ok and self.clone_creates_dir:
            self._materialise(Path(argv[-1]))
        return result

    def _materialise(self, target: Path) -> None:
        (target / ".git").mkdir(parents=True, exist_ok=True)
        for name, body in self.clone_writes.items():
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")


@dataclass(frozen=True, slots=True)
class Workspace:
    """Where the job will run, or why it will not run anywhere.

    ``details`` is the machine-readable half of ``spoken`` — the uncommitted
    paths, the other repository's URL — so a channel can render it without
    parsing an English sentence.
    """

    state: WorkspaceState
    path: Path
    spoken: str
    details: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.state in ("cloned", "reused")


def clone_url(full_name: str) -> str:
    """The HTTPS clone URL for ``owner/name``.

    :class:`jarvis.github.repos.Repo` carries ``html_url`` and no clone URL, and
    the two differ by four characters — deriving it here is one string rather than
    another request, and it keeps the ``.git`` suffix in exactly one place.
    """
    return f"{CLONE_HOST}/{full_name.strip('/')}.git"


def same_repo(a: str, b: str) -> bool:
    """Do two remote URLs name the same GitHub repository?

    Compared on HOST, owner and name, case-insensitively, across the forms the
    same repository is written in (``https://``, ``git@host:owner/name``,
    ``ssh://``) with or without ``.git``. A plain string compare would refuse to
    reuse a clone somebody made over SSH, and then refuse to clone over it
    either — and leaving the host out would make ``gitlab.com/me/app`` and
    ``github.com/me/app`` the same repository, which is how a build's commits end
    up being pushed somewhere nobody looked.

    A URL this cannot parse compares equal to nothing, including itself: the
    caller's response to "I can't tell" and to "it is a different one" is the
    same refusal, and guessing is what must not happen.
    """
    return _repo_id(a) == _repo_id(b) != ()


def _repo_id(url: str) -> tuple[str, ...]:
    text = (url or "").strip()
    if not text:
        return ()
    for prefix in ("ssh://", "git://", "https://", "http://"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    text = text.split("@", 1)[-1]  # git@github.com:owner/name -> github.com:owner/name
    # Only the FIRST colon is scp-syntax; a ':443' port is part of the host and is
    # dropped with it, because one host reachable two ways is still one host.
    host, _, path = text.replace(":", "/", 1).partition("/")
    host = host.split(":", 1)[0]
    path = path.rstrip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    parts = [p for p in path.split("/") if p]
    if not host or len(parts) < 2:
        return ()
    return (host.casefold(), *(p.casefold() for p in parts[-2:]))


def prepare(
    *,
    root: Path,
    name: str,
    clone_url: str,
    git: Git,
    token: str | None = None,
) -> Workspace:
    """Make ``<root>/<name>`` a clean clone of ``clone_url``, or refuse.

    ``token`` is supplied by the CALLER, every time. Nothing in this package reads
    a credential: the process that holds one passes it in, which is what makes an
    accidentally-live test impossible rather than merely unlikely.
    """
    target = (root / name).expanduser()
    if not target.exists():
        return _clone(target=target, clone_url=clone_url, git=git, token=token)

    if not target.is_dir():
        return Workspace(
            "not_a_repo",
            target,
            f"There is already a file at {target}, so I have not touched it.",
        )

    toplevel = git.run(["rev-parse", "--show-toplevel"], cwd=target)
    if not toplevel.ok or not toplevel.out.strip():
        return Workspace(
            "not_a_repo",
            target,
            f"{target} already exists and is not a git repository, so I have left it alone.",
            (toplevel.message,),
        )
    if Path(toplevel.out.strip()).resolve() != target.resolve():
        # Inside somebody else's working tree. Cloning here would nest a
        # repository in a repository and the build's commits would land in the
        # OUTER one, which is the kind of mistake that is only noticed after a push.
        return Workspace(
            "not_a_repo",
            target,
            f"{target} sits inside the git repository at {toplevel.out.strip()}, "
            "so I have not touched it.",
            (toplevel.out.strip(),),
        )

    remote = git.run(["remote", "get-url", "origin"], cwd=target)
    if not remote.ok or not remote.out.strip():
        return Workspace(
            "different_repo",
            target,
            f"{target} is a git repository with no origin remote, so I can't tell whether it is "
            "the right one. I have left it alone.",
            (remote.message,),
        )
    found = remote.out.strip()
    if not same_repo(found, clone_url):
        return Workspace(
            "different_repo",
            target,
            f"{target} is already a clone of {found}, not of {clone_url}. "
            "I have not touched it — tell me where to put this one.",
            (found,),
        )

    status = git.run(["status", "--porcelain"], cwd=target)
    if not status.ok:
        return Workspace(
            "different_repo",
            target,
            f"I couldn't read the state of {target}, so I have left it alone.",
            (status.message,),
        )
    dirty = tuple(line.strip() for line in status.out.splitlines() if line.strip())
    if dirty:
        return Workspace(
            "dirty",
            target,
            f"{target} has uncommitted changes — {_spoken_list(dirty)} — so I have not touched it.",
            dirty,
        )

    return Workspace("reused", target, f"{target} is already a clean clone, so I am using it.")


def _clone(*, target: Path, clone_url: str, git: Git, token: str | None) -> Workspace:
    target.parent.mkdir(parents=True, exist_ok=True)
    result = git.run(
        ["clone", clone_url, str(target)],
        cwd=target.parent,
        token=token,
    )
    if not result.ok:
        return Workspace(
            "clone_failed",
            target,
            f"I couldn't clone {clone_url}: {result.message}",
            (result.message,),
        )
    if token is not None:
        _assert_no_credential(target, token)
    return Workspace("cloned", target, f"I cloned it into {target}.")


def _assert_no_credential(target: Path, token: str) -> None:
    """Read the config back. A secret in the tree is a bug, not a note in a log."""
    config = target / ".git" / "config"
    try:
        text = config.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    if token in text:
        raise CredentialLeak(
            f"{config} contains the access token; the clone must authenticate through the "
            "credential helper in the child environment, never through a URL"
        )


def _spoken_list(items: Sequence[str], limit: int = 3) -> str:
    shown = list(items[:limit])
    rest = len(items) - len(shown)
    text = ", ".join(shown)
    return f"{text} and {rest} more" if rest > 0 else text
