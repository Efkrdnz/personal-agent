"""One Jarvis per user, and where the app keeps its own files.

The lock is tested with TWO REAL PROCESSES because that is the only case it
exists for: a second double-click is a second process, and an OS file lock
taken twice inside one process proves nothing about another one.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from jarvis.app import instance, paths
from jarvis.app.instance import Running

ROOT = Path(__file__).resolve().parents[1]

HOLDER = """
import sys
from pathlib import Path
from jarvis.app import instance
lock = instance.acquire(directory=Path(sys.argv[1]))
print("held" if lock is not None else "busy", flush=True)
sys.stdin.read()
"""


def holder(directory: Path) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(directory)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        cwd=ROOT,
    )


def release(proc: subprocess.Popen[str]) -> None:
    assert proc.stdin is not None
    proc.stdin.close()
    proc.wait(timeout=20)


def test_a_second_process_cannot_take_the_lock_until_the_first_is_gone(tmp_path: Path) -> None:
    first = holder(tmp_path)
    try:
        assert first.stdout is not None
        assert first.stdout.readline().strip() == "held"
        assert instance.acquire(directory=tmp_path) is None
    finally:
        release(first)
    lock = instance.acquire(directory=tmp_path)
    assert lock is not None, "the OS releases a dead holder's lock"
    lock.release()


def test_the_other_way_round_the_child_is_the_one_refused(tmp_path: Path) -> None:
    lock = instance.acquire(directory=tmp_path)
    assert lock is not None
    try:
        second = holder(tmp_path)
        try:
            assert second.stdout is not None
            assert second.stdout.readline().strip() == "busy"
        finally:
            release(second)
    finally:
        lock.release()


def test_a_holder_that_is_killed_frees_the_lock(tmp_path: Path) -> None:
    first = holder(tmp_path)
    assert first.stdout is not None and first.stdout.readline().strip() == "held"
    first.kill()
    first.wait(timeout=20)
    lock = instance.acquire(directory=tmp_path)
    assert lock is not None, "a crash must never leave Jarvis unstartable"
    lock.release()


def test_release_is_idempotent_and_frees_it(tmp_path: Path) -> None:
    lock = instance.acquire(directory=tmp_path)
    assert lock is not None
    lock.release()
    lock.release()
    again = instance.acquire(directory=tmp_path)
    assert again is not None
    again.release()


def test_releasing_the_lock_withdraws_the_address(tmp_path: Path) -> None:
    lock = instance.acquire(directory=tmp_path)
    assert lock is not None
    instance.publish(4321, TOKEN, path=tmp_path / "app.json")
    lock.release()
    assert not (tmp_path / "app.json").exists()


def test_taking_the_lock_forgets_a_stale_address(tmp_path: Path) -> None:
    (tmp_path / "app.json").write_text('{"pid": 1, "port": 1, "token": "x"}', encoding="utf-8")
    lock = instance.acquire(directory=tmp_path)
    assert lock is not None
    try:
        assert not (tmp_path / "app.json").exists()
    finally:
        lock.release()


# ───────────────────────────── app.json ─────────────────────────────


TOKEN = "t" * 43


def test_publish_and_running_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "app.json"
    instance.publish(4321, TOKEN, path=path)
    found = instance.running(path=path)
    assert found == Running(pid=os.getpid(), port=4321, token=TOKEN)
    assert found.url == f"http://127.0.0.1:4321/#t={TOKEN}"
    assert not list(tmp_path.glob(".*tmp")), "the temporary file is renamed, not left behind"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_the_address_file_is_readable_by_this_user_only(tmp_path: Path) -> None:
    path = tmp_path / "app.json"
    instance.publish(4321, TOKEN, path=path)
    assert path.stat().st_mode & 0o777 == 0o600


def test_the_url_matches_the_window_servers_own() -> None:
    from jarvis.window.server import Services, make_server

    server = make_server(Services(open_db=lambda: None, registry=None, extra={}))  # type: ignore[arg-type]
    try:
        assert Running(pid=1, port=server.port, token=server.token).url == server.url
    finally:
        server.shutdown()


@pytest.mark.parametrize(
    "body",
    [
        "",
        "not json",
        "[]",
        '{"pid": 1, "port": 80}',
        json.dumps({"pid": True, "port": 80, "token": TOKEN}),
        json.dumps({"pid": 1, "port": 0, "token": TOKEN}),
        json.dumps({"pid": 1, "port": 70000, "token": TOKEN}),
        '{"pid": 1, "port": 80, "token": "short"}',
        '{"pid": 1, "port": 80, "token": "has spaces in it, sixteen+"}',
    ],
)
def test_a_garbled_address_is_no_address(tmp_path: Path, body: str) -> None:
    path = tmp_path / "app.json"
    path.write_text(body, encoding="utf-8")
    assert instance.running(path=path, gone=lambda pid: False) is None


def test_an_address_whose_process_is_dead_is_no_address(tmp_path: Path) -> None:
    path = tmp_path / "app.json"
    path.write_text(json.dumps({"pid": 4242, "port": 80, "token": TOKEN}), encoding="utf-8")
    assert instance.running(path=path, gone=lambda pid: True) is None
    assert instance.running(path=path, gone=lambda pid: False) is not None
    assert instance.running(path=tmp_path / "missing.json") is None


def test_a_real_dead_pid_is_detected(tmp_path: Path) -> None:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=20)
    path = tmp_path / "app.json"
    instance.publish(4321, TOKEN, path=path, pid=child.pid)
    assert instance.running(path=path) is None


# ───────────────────────────── the second double-click ─────────────────────────────


def test_the_second_copy_opens_the_first_ones_window() -> None:
    opened: list[str] = []
    found = Running(pid=7, port=5555, token=TOKEN)
    said = instance.hand_off(opened.append, find=lambda: found, sleep=lambda s: None)
    assert opened == [found.url] and "already running" in said


def test_it_waits_out_a_first_copy_that_has_not_published_yet() -> None:
    opened: list[str] = []
    answers = iter([None, None, Running(pid=7, port=5555, token=TOKEN)])
    slept: list[float] = []
    instance.hand_off(opened.append, find=lambda: next(answers), sleep=slept.append)
    assert len(opened) == 1 and slept == [0.25, 0.25]


def test_it_gives_up_after_the_wait_without_opening_anything() -> None:
    opened: list[str] = []
    slept: list[float] = []
    said = instance.hand_off(opened.append, find=lambda: None, sleep=slept.append, wait_s=3.0)
    assert opened == [] and sum(slept) == pytest.approx(3.0)
    assert "try again" in said


# ───────────────────────────── paths ─────────────────────────────


def test_app_dir_is_localappdata_on_windows_and_xdg_state_elsewhere() -> None:
    win = paths.app_dir(env={"LOCALAPPDATA": r"C:\Users\u\AppData\Local"}, platform="win32")
    assert win == Path(r"C:\Users\u\AppData\Local") / "Jarvis"
    assert paths.app_dir(env={"XDG_STATE_HOME": "/s"}, platform="linux") == Path("/s/jarvis")
    assert paths.app_dir(env={}, platform="linux") == Path.home() / ".local" / "state" / "jarvis"


def test_the_app_dir_is_beside_the_database_on_linux(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from jarvis import db

    monkeypatch.delenv("JARVIS_DB", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert db.default_path().parent == paths.app_dir(platform="linux")


def test_log_paths_are_created_and_rotate_past_five_megabytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(sys, "platform", "linux")
    path = paths.log_path("desk")
    assert path == tmp_path / "jarvis" / "logs" / "desk.log" and path.parent.is_dir()
    path.write_bytes(b"x" * 100)
    assert paths.log_path("desk") == path and path.read_bytes() == b"x" * 100
    path.write_bytes(b"x" * (paths.ROTATE_BYTES + 1))
    paths.log_path("desk")
    assert not path.exists()
    assert (path.parent / "desk.log.1").stat().st_size == paths.ROTATE_BYTES + 1
    assert paths.state_file() == tmp_path / "jarvis" / "app.json"


@pytest.mark.parametrize("bad", ["", "../desk", "a/b", "a\\b", "x" * 65])
def test_a_process_name_is_never_a_path(bad: str) -> None:
    with pytest.raises(ValueError):
        paths.log_path(bad)


# ───────────────────────────── the remembered port ─────────────────────────────


class _Bound:
    def __init__(self, port: int) -> None:
        self.port = port


def test_the_window_gets_the_port_it_had_last_time(tmp_path: Path) -> None:
    # The port is part of the page's origin, and the page's preferences are
    # kept per origin: a new port each launch forgets them each launch.
    path = tmp_path / "window.port"
    asked: list[int] = []

    def make(port: int) -> _Bound:
        asked.append(port)
        return _Bound(port or 49152)

    first = instance.bind_remembered(make, path=path)
    second = instance.bind_remembered(make, path=path)
    assert asked == [0, 49152] and first.port == second.port == 49152


def test_a_taken_port_falls_back_to_any_free_one_and_is_forgotten(tmp_path: Path) -> None:
    path = tmp_path / "window.port"
    path.write_text("50000", encoding="utf-8")
    asked: list[int] = []

    def make(port: int) -> _Bound:
        asked.append(port)
        if port == 50000:
            raise OSError(98, "Address already in use")
        return _Bound(50001)

    assert instance.bind_remembered(make, path=path).port == 50001
    assert asked == [50000, 0]
    assert path.read_text(encoding="utf-8") == "50001"


@pytest.mark.parametrize("body", ["", "abc", "80", "70000", "-5", "5e4"])
def test_a_garbled_or_privileged_port_is_not_tried(tmp_path: Path, body: str) -> None:
    path = tmp_path / "window.port"
    path.write_text(body, encoding="utf-8")
    asked: list[int] = []
    instance.bind_remembered(lambda port: asked.append(port) or _Bound(51000), path=path)
    assert asked == [0]


def test_a_real_server_rebinds_its_own_port_after_shutdown(tmp_path: Path) -> None:
    # The one that matters on Linux: a server that served connections leaves
    # them in TIME_WAIT, and the next launch must still get the same port.
    import http.client

    from jarvis.tools.default import registry
    from jarvis.window.server import Services, make_server

    def services() -> Services:
        return Services(open_db=lambda: None, registry=registry(), extra={})  # type: ignore[arg-type, return-value]

    path = tmp_path / "window.port"
    a = instance.bind_remembered(lambda port: make_server(services(), port=port), path=path)
    a.start()
    conn = http.client.HTTPConnection("127.0.0.1", a.port, timeout=5)
    conn.request("GET", "/app.css")
    conn.getresponse().read()
    conn.close()
    a.shutdown()
    b = instance.bind_remembered(lambda port: make_server(services(), port=port), path=path)
    try:
        assert b.port == a.port
    finally:
        b.shutdown()
