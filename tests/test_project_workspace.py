"""Clone, reuse, or refuse — and never a credential on disk.

Two kinds of test.

SCRIPTED GIT covers the outcomes and the token discipline. ``FakeGit`` records
argv and asserts that the token never appears in it, so the "pass it in the
environment" rule is enforced by the seam rather than by a comment.

REAL GIT covers the parsing, offline. ``git init``, a commit and a remote in a
temporary directory are enough to exercise ``status --porcelain``,
``remote get-url`` and ``rev-parse --show-toplevel`` against the actual program,
which is the half a fake cannot honestly cover. No network, no server, no clone
from anywhere.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from jarvis.project import workspace as ws

URL = "https://github.com/Efkrdnz/comment-watcher.git"
TOKEN = "ghp_" + "t" * 36

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


# ───────────────────────────── the URL ─────────────────────────────


@pytest.mark.parametrize(
    ("a", "b"),
    [
        (URL, "https://github.com/Efkrdnz/comment-watcher"),
        (URL, "git@github.com:Efkrdnz/comment-watcher.git"),
        (URL, "ssh://git@github.com/Efkrdnz/comment-watcher.git"),
        (URL, "https://github.com/efkrdnz/Comment-Watcher.git"),
        (URL, "https://github.com/Efkrdnz/comment-watcher/"),
    ],
)
def test_the_same_repository_written_five_ways_is_one_repository(a: str, b: str) -> None:
    """A string compare here would refuse to reuse a clone somebody made over SSH."""
    assert ws.same_repo(a, b)


@pytest.mark.parametrize(
    "other",
    [
        "https://github.com/Efkrdnz/other-project.git",
        "https://github.com/SomebodyElse/comment-watcher.git",
        "https://gitlab.com/Efkrdnz/comment-watcher.git",
        "",
        "not a url",
    ],
)
def test_a_different_repository_is_never_mistaken_for_this_one(other: str) -> None:
    assert not ws.same_repo(URL, other)


def test_the_clone_url_is_derived_in_one_place() -> None:
    assert ws.clone_url("Efkrdnz/comment-watcher") == URL


# ───────────────────────────── cloning ─────────────────────────────


def test_an_empty_workspace_is_cloned(tmp_path: Path) -> None:
    git = ws.FakeGit()
    out = ws.prepare(root=tmp_path, name="comment-watcher", clone_url=URL, git=git, token=TOKEN)
    assert out.state == "cloned"
    assert out.ok
    assert out.path == tmp_path / "comment-watcher"
    assert git.calls[0][0] == "clone"
    assert str(out.path) in git.calls[0]


def test_the_token_goes_in_the_environment_and_never_into_argv(tmp_path: Path) -> None:
    """``git clone https://user:token@host/...`` is the mistake this rules out."""
    git = ws.FakeGit()
    ws.prepare(root=tmp_path, name="comment-watcher", clone_url=URL, git=git, token=TOKEN)
    assert TOKEN in git.tokens  # the seam was given it
    for argv in git.calls:
        assert not any(TOKEN in arg for arg in argv)
        assert not any("@github.com" in arg for arg in argv)


def test_a_token_found_in_the_clone_is_a_loud_failure(tmp_path: Path) -> None:
    """The check exists because "I was careful" is not a control.

    A ``Git`` implementation that embedded the credential in the remote URL would
    leave it in ``.git/config``, in the working tree, in plaintext, forever.
    """
    git = ws.FakeGit(clone_writes={".git/config": f"url = https://x:{TOKEN}@github.com/a/b\n"})
    with pytest.raises(ws.CredentialLeak):
        ws.prepare(root=tmp_path, name="comment-watcher", clone_url=URL, git=git, token=TOKEN)


def test_a_clone_that_failed_says_what_git_said(tmp_path: Path) -> None:
    git = ws.FakeGit(
        results={"clone": [ws.GitResult(128, err="fatal: Authentication failed for ...")]}
    )
    out = ws.prepare(root=tmp_path, name="comment-watcher", clone_url=URL, git=git, token=TOKEN)
    assert out.state == "clone_failed"
    assert not out.ok
    assert "Authentication failed" in out.spoken


def test_no_clone_is_attempted_without_a_token_being_demanded(tmp_path: Path) -> None:
    """A caller may legitimately clone a public repo with no token; it is not read HERE."""
    git = ws.FakeGit()
    out = ws.prepare(root=tmp_path, name="comment-watcher", clone_url=URL, git=git)
    assert out.state == "cloned"
    assert git.tokens == [None]


# ───────────────────────────── what is already there ─────────────────────────────


def test_a_clean_clone_of_the_same_repository_is_reused(tmp_path: Path) -> None:
    target = tmp_path / "comment-watcher"
    (target / ".git").mkdir(parents=True)
    git = ws.FakeGit(
        results={
            "rev-parse": [ws.GitResult(0, out=f"{target}\n")],
            "remote": [ws.GitResult(0, out=f"{URL}\n")],
            "status": [ws.GitResult(0, out="")],
        }
    )
    out = ws.prepare(root=tmp_path, name="comment-watcher", clone_url=URL, git=git, token=TOKEN)
    assert out.state == "reused"
    assert out.ok
    assert not any(argv[0] == "clone" for argv in git.calls)


def test_a_clone_of_a_different_repository_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "comment-watcher"
    (target / ".git").mkdir(parents=True)
    other = "https://github.com/Efkrdnz/something-else.git"
    git = ws.FakeGit(
        results={
            "rev-parse": [ws.GitResult(0, out=f"{target}\n")],
            "remote": [ws.GitResult(0, out=f"{other}\n")],
        }
    )
    out = ws.prepare(root=tmp_path, name="comment-watcher", clone_url=URL, git=git, token=TOKEN)
    assert out.state == "different_repo"
    assert not out.ok
    assert other in out.spoken
    assert out.details == (other,)


def test_uncommitted_work_is_refused_and_the_files_are_named(tmp_path: Path) -> None:
    target = tmp_path / "comment-watcher"
    (target / ".git").mkdir(parents=True)
    git = ws.FakeGit(
        results={
            "rev-parse": [ws.GitResult(0, out=f"{target}\n")],
            "remote": [ws.GitResult(0, out=f"{URL}\n")],
            "status": [ws.GitResult(0, out=" M main.py\n?? notes.txt\n M README.md\n D old.py\n")],
        }
    )
    out = ws.prepare(root=tmp_path, name="comment-watcher", clone_url=URL, git=git, token=TOKEN)
    assert out.state == "dirty"
    assert not out.ok
    assert "M main.py" in out.spoken
    assert "and 1 more" in out.spoken
    assert len(out.details) == 4


def test_a_directory_that_is_not_a_repository_is_left_alone(tmp_path: Path) -> None:
    (tmp_path / "comment-watcher").mkdir()
    git = ws.FakeGit(results={"rev-parse": [ws.GitResult(128, err="fatal: not a git repository")]})
    out = ws.prepare(root=tmp_path, name="comment-watcher", clone_url=URL, git=git, token=TOKEN)
    assert out.state == "not_a_repo"
    assert "not a git repository" in out.spoken
    assert not any(argv[0] == "clone" for argv in git.calls)


def test_a_directory_inside_another_repository_is_refused(tmp_path: Path) -> None:
    """Nested repositories are how a build's commits land in the OUTER project."""
    target = tmp_path / "comment-watcher"
    target.mkdir()
    git = ws.FakeGit(results={"rev-parse": [ws.GitResult(0, out=f"{tmp_path}\n")]})
    out = ws.prepare(root=tmp_path, name="comment-watcher", clone_url=URL, git=git, token=TOKEN)
    assert out.state == "not_a_repo"
    assert str(tmp_path) in out.spoken


def test_a_file_where_the_directory_should_be_is_refused(tmp_path: Path) -> None:
    (tmp_path / "comment-watcher").write_text("not a directory")
    git = ws.FakeGit()
    out = ws.prepare(root=tmp_path, name="comment-watcher", clone_url=URL, git=git, token=TOKEN)
    assert out.state == "not_a_repo"
    assert git.calls == []


def test_a_repository_with_no_origin_is_refused_rather_than_assumed(tmp_path: Path) -> None:
    target = tmp_path / "comment-watcher"
    (target / ".git").mkdir(parents=True)
    git = ws.FakeGit(
        results={
            "rev-parse": [ws.GitResult(0, out=f"{target}\n")],
            "remote": [ws.GitResult(2, err="error: No such remote 'origin'")],
        }
    )
    out = ws.prepare(root=tmp_path, name="comment-watcher", clone_url=URL, git=git, token=TOKEN)
    assert out.state == "different_repo"
    assert "can't tell" in out.spoken


# ───────────────────────────── against the real program ─────────────────────────────


def _real_repo(path: Path, *, remote: str) -> None:
    """A real git repository with one commit, built offline."""
    git = ws.SubprocessGit()
    path.mkdir(parents=True, exist_ok=True)
    assert git.run(["init", "-q", "-b", "main", str(path)]).ok
    (path / "README.md").write_text("hello\n")
    assert git.run(["add", "README.md"], cwd=path).ok
    assert git.run(
        [
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "user.name=test",
            "commit",
            "-q",
            "-m",
            "first",
        ],
        cwd=path,
    ).ok
    assert git.run(["remote", "add", "origin", remote], cwd=path).ok


@needs_git
def test_real_git_a_clean_clone_of_the_same_repository_is_reused(tmp_path: Path) -> None:
    _real_repo(tmp_path / "comment-watcher", remote=URL)
    out = ws.prepare(root=tmp_path, name="comment-watcher", clone_url=URL, git=ws.SubprocessGit())
    assert out.state == "reused", out.spoken


@needs_git
def test_real_git_uncommitted_work_is_seen_and_named(tmp_path: Path) -> None:
    target = tmp_path / "comment-watcher"
    _real_repo(target, remote=URL)
    (target / "notes.txt").write_text("unsaved\n")
    out = ws.prepare(root=tmp_path, name="comment-watcher", clone_url=URL, git=ws.SubprocessGit())
    assert out.state == "dirty", out.spoken
    assert "notes.txt" in out.spoken


@needs_git
def test_real_git_a_clone_of_another_repository_is_refused(tmp_path: Path) -> None:
    _real_repo(tmp_path / "comment-watcher", remote="git@github.com:Efkrdnz/something-else.git")
    out = ws.prepare(root=tmp_path, name="comment-watcher", clone_url=URL, git=ws.SubprocessGit())
    assert out.state == "different_repo", out.spoken


@needs_git
def test_real_git_an_ordinary_directory_is_refused(tmp_path: Path) -> None:
    (tmp_path / "comment-watcher").mkdir()
    out = ws.prepare(root=tmp_path, name="comment-watcher", clone_url=URL, git=ws.SubprocessGit())
    assert out.state == "not_a_repo", out.spoken


@needs_git
def test_real_git_fails_fast_instead_of_hanging_on_a_prompt(tmp_path: Path) -> None:
    """A clone that cannot work must END, not wait.

    The source is a local path that does not exist, so nothing leaves this machine
    — and ``GIT_TERMINAL_PROMPT=0`` is asserted directly in the test below, because
    a voice assistant hanging on an invisible password prompt is indistinguishable
    from one that died.
    """
    out = ws.prepare(
        root=tmp_path,
        name="comment-watcher",
        clone_url=str(tmp_path / "nowhere.git"),
        git=ws.SubprocessGit(timeout_s=30.0),
        token=TOKEN,
    )
    assert out.state == "clone_failed"
    assert not (tmp_path / "comment-watcher" / ".git").exists()


@needs_git
def test_real_git_gets_the_token_from_its_environment_and_not_from_argv() -> None:
    """The credential path, end to end, with no server and no secret on disk.

    A git alias beginning with ``!`` runs a shell command, which is the cheapest
    way to ask the child process what it can actually see. The token reaches the
    child's environment — which is where the credential helper reads it — while
    argv stays clean and nothing is written anywhere.
    """
    git = ws.SubprocessGit(
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/nonexistent"}
    )
    seen = git.run(
        ["-c", "alias.peek=!printenv JARVIS_GIT_TOKEN GIT_TERMINAL_PROMPT", "peek"],
        token=TOKEN,
    )
    assert seen.ok, seen.err
    assert seen.out.split() == [TOKEN, "0"]

    # And with no token there is nothing in the environment to leak.
    without = git.run(["-c", "alias.peek=!printenv GIT_TERMINAL_PROMPT", "peek"])
    assert without.out.strip() == "0"
    empty = git.run(["-c", "alias.peek=!printenv JARVIS_GIT_TOKEN", "peek"])
    assert not empty.ok or TOKEN not in empty.out
