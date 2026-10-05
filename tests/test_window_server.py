"""The window's HTTP server, over REAL HTTP on a real loopback port.

Nothing here calls a handler method directly. Every request goes through a
socket, the way a browser tab (or a hostile page in another tab) would send it,
because the bugs this server can have are about what crosses that socket: a
header that leaks, a check that runs after the work, a stream that never ends.

Each security requirement in the build contract is its own test, so a failure
names the requirement that broke rather than "the server test failed".
"""

from __future__ import annotations

import ast
import http.client
import json
import socket
import sqlite3
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from jarvis import answers, kill, liveness
from jarvis import requests as rq
from jarvis.bus import last_seq, publish
from jarvis.db import connect, migrate
from jarvis.tools.ctx import ToolCtx
from jarvis.tools.default import registry
from jarvis.tools.registry import Tool
from jarvis.window import server as server_mod
from jarvis.window import snapshot
from jarvis.window.server import CSP, MAX_BODY, Services, WindowServer, make_server

TOKEN = "test-token-0123456789abcdefghijklmnop"
EXTRA = {"tz": "Europe/Istanbul", "home": "Istanbul"}

INDEX = b"<!doctype html><title>window</title>"
CSS = b"body{margin:0}"
JS = b"void 0;"

QUESTIONS = {
    "questions": [
        {
            "question": "Which database should the app use?",
            "header": "Database",
            "multiSelect": False,
            "options": [
                {"label": "SQLite", "description": "one file"},
                {"label": "Postgres", "description": "a server"},
            ],
        }
    ]
}


# ───────────────────────────── fixtures ─────────────────────────────


@dataclass
class Fakes:
    """What the fake chat, speaker and probe tool saw. One per test."""

    tool_calls: list[ToolCtx] = field(default_factory=list)
    chats: list[str] = field(default_factory=list)
    spoken: list[str] = field(default_factory=list)
    spoke: threading.Event = field(default_factory=threading.Event)
    #: Set to hold the fake speaker until the test lets it go.
    gate: threading.Event = field(default_factory=threading.Event)
    speak_result: str = "desk"
    chat_error: str | None = None

    def chat(self, text: str) -> tuple[str, tuple[tuple[str, str], ...]]:
        self.chats.append(text)
        if self.chat_error is not None:
            raise RuntimeError(self.chat_error)
        return f"You said: {text}", (("weather", "Sunny, 24 degrees."),)

    def speak(self, text: str) -> str:
        self.gate.wait(timeout=5)
        self.spoken.append(text)
        self.spoke.set()
        return self.speak_result


@pytest.fixture
def dbpath(tmp_path: Path) -> Path:
    p = tmp_path / "j.db"
    c = connect(p)
    migrate(c)
    c.close()
    return p


@pytest.fixture
def con(dbpath: Path) -> Iterator[sqlite3.Connection]:
    c = connect(dbpath)
    yield c
    c.close()


def _write_static(root: Path) -> Path:
    d = root / "static"
    d.mkdir()
    (d / "index.html").write_bytes(INDEX)
    (d / "app.css").write_bytes(CSS)
    (d / "app.js").write_bytes(JS)
    # A file next to the page that must NEVER be served.
    (root / "server.py").write_text("SECRET = 'source'\n")
    return d


@pytest.fixture
def static_dir(tmp_path: Path) -> Path:
    return _write_static(tmp_path)


@pytest.fixture
def fakes() -> Iterator[Fakes]:
    f = Fakes()
    f.gate.set()
    yield f
    f.gate.set()  # never leave a speaker thread parked


def _probe_tools(fakes: Fakes) -> tuple[Tool, ...]:
    def probe(text: str = "", ctx: ToolCtx | None = None) -> str:
        assert ctx is not None
        fakes.tool_calls.append(ctx)
        return f"probed {text}"

    def phone_only() -> str:
        return "should never run from the window"

    return (
        Tool(name="probe", description="records its ctx", handler=probe),
        Tool(name="phone_only", description="phone", handler=phone_only, channels=("phone",)),
    )


ServerFactory = Callable[..., WindowServer]


@pytest.fixture
def make(dbpath: Path, static_dir: Path, fakes: Fakes) -> Iterator[ServerFactory]:
    started: list[WindowServer] = []

    def build(**overrides: Any) -> WindowServer:
        kw: dict[str, Any] = {
            "open_db": lambda: connect(dbpath),
            "registry": registry(extra=_probe_tools(fakes)),
            "extra": dict(EXTRA),
            "chat": fakes.chat,
            "speak": fakes.speak,
            "wake_word": "hey jarvis",
        }
        kw.update(overrides)
        ws = make_server(Services(**kw), port=0, token=TOKEN, static_dir=static_dir)
        ws.start()
        started.append(ws)
        return ws

    yield build
    for ws in started:
        ws.shutdown()


@pytest.fixture
def srv(make: ServerFactory) -> WindowServer:
    return make()


@pytest.fixture(scope="module")
def shared(tmp_path_factory: pytest.TempPathFactory) -> Iterator[WindowServer]:
    """One server for the tests that only look at what crosses the socket.

    Stopping a server waits out one poll of its accept loop; sharing it across
    the read-only security tests keeps this file quick without weakening any.
    Tests that count rows or watch the fakes get their own server and database.
    """
    root = tmp_path_factory.mktemp("shared")
    path = root / "j.db"
    c = connect(path)
    migrate(c)
    c.close()
    f = Fakes()
    f.gate.set()
    svc = Services(
        open_db=lambda: connect(path),
        registry=registry(extra=_probe_tools(f)),
        extra=dict(EXTRA),
        chat=f.chat,
        speak=f.speak,
        wake_word="hey jarvis",
    )
    ws = make_server(svc, port=0, token=TOKEN, static_dir=_write_static(root))
    ws.start()
    yield ws
    ws.shutdown()


@dataclass
class Reply:
    status: int
    headers: dict[str, str]
    raw: bytes

    @property
    def body(self) -> dict[str, Any]:
        return json.loads(self.raw.decode("utf-8"))


def call(
    srv: WindowServer,
    method: str,
    path: str,
    *,
    json_body: Any = None,
    raw: bytes | None = None,
    token: str | None = TOKEN,
    headers: dict[str, str] | None = None,
    content_type: str | None = "application/json",
) -> Reply:
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    try:
        h: dict[str, str] = {}
        if token is not None:
            h["X-Jarvis-Token"] = token
        data = raw
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
        if data is not None and content_type is not None:
            h["Content-Type"] = content_type
        h.update(headers or {})
        conn.request(method, path, body=data, headers=h)
        resp = conn.getresponse()
        return Reply(resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read())
    finally:
        conn.close()


def post(srv: WindowServer, path: str, body: Any = None, **kw: Any) -> Reply:
    return call(srv, "POST", path, json_body={} if body is None else body, **kw)


def _events(dbpath: Path, kind: str | None = None) -> list[sqlite3.Row]:
    c = connect(dbpath)
    try:
        sql = "SELECT * FROM events" + (" WHERE kind=?" if kind else "") + " ORDER BY seq"
        return c.execute(sql, (kind,) if kind else ()).fetchall()
    finally:
        c.close()


def _wait(pred: Callable[[], bool], timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


class Stream:
    """A minimal EventSource: one connection, events parsed as they arrive."""

    def __init__(self, srv: WindowServer, query: str = f"t={TOKEN}") -> None:
        self.conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
        self.conn.request("GET", f"/api/stream?{query}")
        self.resp = self.conn.getresponse()

    def next(self) -> tuple[str, Any]:
        name: str | None = None
        data: list[str] = []
        while True:
            line = self.resp.readline()
            if not line:
                raise EOFError("the stream ended")
            text = line.decode("utf-8").rstrip("\r\n")
            if text == "":
                if name is not None:
                    return name, json.loads("\n".join(data)) if data else None
                data = []
                continue
            if text.startswith(":"):
                continue
            key, _, value = text.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if key == "event":
                name = value
            elif key == "data":
                data.append(value)

    def until(self, want: str, limit: int = 20) -> Any:
        for _ in range(limit):
            name, data = self.next()
            if name == want:
                return data
        raise AssertionError(f"no {want!r} event in {limit} events")

    def close(self) -> None:
        self.resp.close()
        self.conn.close()


# ───────────────────────────── construction ─────────────────────────────


def test_url_puts_the_token_in_the_fragment(shared: WindowServer) -> None:
    assert shared.url == f"http://127.0.0.1:{shared.port}/#t={TOKEN}"
    assert shared.token == TOKEN
    assert shared.port > 0


def test_default_token_is_random_and_long(dbpath: Path, static_dir: Path) -> None:
    svc = Services(open_db=lambda: connect(dbpath), registry=registry(), extra={})
    a = make_server(svc, static_dir=static_dir)
    b = make_server(svc, static_dir=static_dir)
    try:
        assert len(a.token) >= 32 and a.token != b.token
        assert a.port != b.port
    finally:
        a.shutdown()
        b.shutdown()


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.5", ""])
def test_make_server_refuses_anything_but_loopback(host: str, dbpath: Path) -> None:
    svc = Services(open_db=lambda: connect(dbpath), registry=registry(), extra={})
    with pytest.raises(ValueError, match="127.0.0.1"):
        make_server(svc, host=host, port=0)


def test_make_server_refuses_a_guessable_token(dbpath: Path) -> None:
    svc = Services(open_db=lambda: connect(dbpath), registry=registry(), extra={})
    with pytest.raises(ValueError, match="token"):
        make_server(svc, token="short")


def test_shutdown_is_idempotent_and_safe_before_start(dbpath: Path, static_dir: Path) -> None:
    svc = Services(open_db=lambda: connect(dbpath), registry=registry(), extra={})
    never_started = make_server(svc, static_dir=static_dir)
    never_started.shutdown()
    never_started.shutdown()
    ws = make_server(svc, static_dir=static_dir)
    ws.start()
    with pytest.raises(RuntimeError):
        ws.start()
    ws.shutdown()
    ws.shutdown()
    with pytest.raises(RuntimeError):
        ws.start()


def test_serve_forever_runs_until_shutdown_from_another_thread(
    dbpath: Path, static_dir: Path
) -> None:
    svc = Services(open_db=lambda: connect(dbpath), registry=registry(), extra={})
    ws = make_server(svc, token=TOKEN, static_dir=static_dir)
    t = threading.Thread(target=ws.serve_forever, daemon=True)
    t.start()
    try:
        assert call(ws, "GET", "/api/feed").status == 200
    finally:
        ws.shutdown()
    t.join(timeout=3)
    assert not t.is_alive()


# ───────────────────────────── security: the token ─────────────────────────────


def test_missing_token_is_401(shared: WindowServer) -> None:
    r = call(shared, "GET", "/api/state", token=None)
    assert r.status == 401
    assert r.body["ok"] is False and "python -m jarvis window" in r.body["error"]


def test_wrong_token_is_401(shared: WindowServer) -> None:
    assert call(shared, "GET", "/api/state", token=TOKEN[:-1] + "X").status == 401
    assert call(shared, "GET", "/api/state", token="").status == 401


def test_every_api_route_needs_the_token(shared: WindowServer) -> None:
    for method, path in (
        ("GET", "/api/state"),
        ("GET", "/api/feed"),
        ("GET", "/api/stream"),
        ("POST", "/api/tool"),
        ("POST", "/api/chat"),
        ("POST", "/api/say"),
        ("POST", "/api/answer"),
        ("POST", "/api/stop"),
        ("GET", "/api/nope"),
    ):
        body = {} if method == "POST" else None
        r = call(shared, method, path, json_body=body, token=None)
        assert r.status == 401, (method, path)


def test_a_401_does_no_work(srv: WindowServer, dbpath: Path) -> None:
    post(srv, "/api/stop", token=None)
    post(srv, "/api/chat", {"text": "hi"}, token=None)
    c = connect(dbpath)
    try:
        assert c.execute("SELECT COUNT(*) FROM commands").fetchone()[0] == 0
    finally:
        c.close()
    assert _events(dbpath, "window.said") == []


def test_query_token_is_accepted_only_on_the_stream(shared: WindowServer) -> None:
    assert call(shared, "GET", f"/api/state?t={TOKEN}", token=None).status == 401
    assert call(shared, "GET", f"/api/feed?t={TOKEN}", token=None).status == 401
    assert post(shared, f"/api/stop?t={TOKEN}", token=None).status == 401
    s = Stream(shared)
    try:
        assert s.resp.status == 200
        assert s.resp.getheader("Content-Type", "").startswith("text/event-stream")
    finally:
        s.close()
    bad = Stream(shared, query="t=wrong-token-wrong-token")
    try:
        assert bad.resp.status == 401
    finally:
        bad.close()


# ───────────────────────────── security: Host and Origin ─────────────────────────────


def test_wrong_host_is_403(shared: WindowServer) -> None:
    r = call(shared, "GET", "/api/state", headers={"Host": f"127.0.0.1:{shared.port + 1}"})
    assert r.status == 403
    assert call(shared, "GET", "/", headers={"Host": "127.0.0.1"}).status == 403


def test_dns_rebinding_host_is_403_even_with_the_token(shared: WindowServer) -> None:
    for host in (
        f"evil.example:{shared.port}",
        "evil.example",
        f"127.0.0.1.evil.example:{shared.port}",
    ):
        assert call(shared, "GET", "/api/state", headers={"Host": host}).status == 403, host
        assert call(shared, "GET", "/", headers={"Host": host}).status == 403, host


def test_localhost_host_is_accepted(shared: WindowServer) -> None:
    assert (
        call(shared, "GET", "/api/state", headers={"Host": f"localhost:{shared.port}"}).status
        == 200
    )


def test_foreign_origin_is_403(srv: WindowServer, dbpath: Path) -> None:
    for origin in ("http://evil.example", "null", f"http://evil.example:{srv.port}"):
        r = post(srv, "/api/stop", headers={"Origin": origin})
        assert r.status == 403, origin
    c = connect(dbpath)
    try:
        assert c.execute("SELECT COUNT(*) FROM commands").fetchone()[0] == 0
    finally:
        c.close()


def test_own_origin_is_accepted(shared: WindowServer) -> None:
    for origin in (f"http://127.0.0.1:{shared.port}", f"http://localhost:{shared.port}"):
        assert call(shared, "GET", "/api/state", headers={"Origin": origin}).status == 200


# ───────────────────────────── security: bodies ─────────────────────────────


def test_post_without_json_content_type_is_415(shared: WindowServer) -> None:
    for ctype in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data"):
        r = call(shared, "POST", "/api/stop", raw=b"{}", content_type=ctype)
        assert r.status == 415, ctype
    assert call(shared, "POST", "/api/stop", raw=b"{}", content_type=None).status == 415


def test_json_content_type_with_charset_is_fine(shared: WindowServer) -> None:
    r = call(
        shared,
        "POST",
        "/api/tool",
        raw=b'{"name": "probe"}',
        content_type="application/json; charset=utf-8",
    )
    assert r.status == 200


def test_oversize_body_is_413(shared: WindowServer) -> None:
    big = json.dumps({"text": "x" * MAX_BODY}).encode()
    assert len(big) > MAX_BODY
    r = call(shared, "POST", "/api/chat", raw=big)
    assert r.status == 413
    assert r.body["ok"] is False


def test_non_object_json_is_400(shared: WindowServer) -> None:
    for raw in (b"[1, 2]", b'"stop"', b"null", b"42"):
        assert call(shared, "POST", "/api/stop", raw=raw).status == 400, raw
    r = call(shared, "POST", "/api/stop", raw=b"{not json")
    assert r.status == 400
    assert "JSON" in r.body["error"]


def test_chunked_body_is_refused(shared: WindowServer) -> None:
    r = call(shared, "POST", "/api/stop", raw=b"{}", headers={"Transfer-Encoding": "chunked"})
    assert r.status in (400, 411)


# ───────────────────────────── security: static files and headers ─────────────────────────────


def test_the_three_static_files_need_no_token(shared: WindowServer) -> None:
    for path, data, ctype in (
        ("/", INDEX, "text/html"),
        ("/static/app.css", CSS, "text/css"),
        ("/static/app.js", JS, "text/javascript"),
    ):
        r = call(shared, "GET", path, token=None)
        assert r.status == 200, path
        assert r.raw == data
        assert r.headers["content-type"].startswith(ctype)


@pytest.mark.parametrize(
    "path",
    [
        "/static/../server.py",
        "/static/%2e%2e/server.py",
        "/../server.py",
        "/static/x.js",
        "/static/",
        "/index.html",
        "/static/app.js/",
        "/static/app.js.map",
        "/server.py",
    ],
)
def test_nothing_outside_the_allow_list_is_served(shared: WindowServer, path: str) -> None:
    r = call(shared, "GET", path, token=TOKEN)
    assert r.status == 404, path
    assert b"SECRET" not in r.raw
    assert r.body["ok"] is False


def _assert_security_headers(r: Reply) -> None:
    assert r.headers.get("content-security-policy") == CSP
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers.get("cache-control") == "no-store"
    assert r.headers.get("referrer-policy") == "no-referrer"


def test_csp_exactly_as_the_contract_says() -> None:
    assert CSP == (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'none'"
    )


def test_security_headers_on_static_api_and_error_responses(shared: WindowServer) -> None:
    replies = [
        call(shared, "GET", "/", token=None),
        call(shared, "GET", "/static/app.js", token=None),
        call(shared, "GET", "/api/state"),
        call(shared, "GET", "/api/feed"),
        post(shared, "/api/tool", {"name": "probe"}),
        call(shared, "GET", "/api/state", token=None),  # 401
        call(shared, "GET", "/nope"),  # 404
        call(shared, "GET", "/api/state", headers={"Host": "evil.example"}),  # 403
        call(shared, "POST", "/api/stop", raw=b"{}", content_type="text/plain"),  # 415
        call(shared, "GET", "/api/stop"),  # 405
        call(shared, "PUT", "/api/stop", raw=b"{}"),  # 501 from the base class
    ]
    for r in replies:
        _assert_security_headers(r)
    assert {r.status for r in replies} >= {200, 401, 403, 404, 405, 415, 501}


def test_no_access_control_header_on_any_response(shared: WindowServer) -> None:
    replies = [
        call(shared, "GET", "/", token=None),
        call(shared, "GET", "/api/state", headers={"Origin": f"http://127.0.0.1:{shared.port}"}),
        call(shared, "GET", "/api/state", headers={"Origin": "http://evil.example"}),
        call(
            shared,
            "OPTIONS",
            "/api/stop",
            token=None,
            headers={
                "Origin": "http://evil.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "x-jarvis-token",
            },
        ),
        post(shared, "/api/stop"),
    ]
    for r in replies:
        assert not [h for h in r.headers if h.startswith("access-control-")], r.headers
    s = Stream(shared)
    try:
        assert not [h for h, _ in s.resp.getheaders() if h.lower().startswith("access-control-")]
    finally:
        s.close()


def test_errors_are_json_sentences_never_tracebacks(
    shared: WindowServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*a: Any, **k: Any) -> Any:
        raise ValueError("internal detail /home/user/secret.db")

    monkeypatch.setattr(snapshot, "state", explode)
    r = call(shared, "GET", "/api/state")
    assert r.status == 500
    assert r.body["ok"] is False
    assert "Traceback" not in r.body["error"]
    assert "secret.db" not in r.body["error"]
    assert r.headers["content-type"].startswith("application/json")
    _assert_security_headers(r)


def test_the_token_never_reaches_the_log(
    shared: WindowServer, capsys: pytest.CaptureFixture[str]
) -> None:
    # Two stream URLs that DO get logged (only failures are): a refused Host,
    # and the right token on the wrong method.
    call(
        shared,
        "GET",
        f"/api/stream?t={TOKEN}&after=0",
        token=None,
        headers={"Host": "evil.example"},
    )
    call(shared, "POST", f"/api/stream?t={TOKEN}", raw=b"{}", token=None)
    call(shared, "GET", f"/api/stream?after=3&t={TOKEN}", token=None, headers={"Host": "x"})
    err = capsys.readouterr().err
    assert "/api/stream" in err
    assert "[redacted]" in err
    assert TOKEN not in err


def test_successful_requests_are_not_logged(
    shared: WindowServer, capsys: pytest.CaptureFixture[str]
) -> None:
    call(shared, "GET", "/api/state")
    call(shared, "GET", "/", token=None)
    assert capsys.readouterr().err == ""


def _raw_request(srv: WindowServer, line: bytes) -> None:
    # http.client refuses control characters in a path; a hostile page does not.
    with socket.create_connection(("127.0.0.1", srv.port), timeout=5) as sock:
        sock.sendall(line + b"\r\nHost: evil.example\r\n\r\n")
        while sock.recv(4096):
            pass


def test_a_request_line_cannot_write_control_characters_to_the_terminal(
    shared: WindowServer, capsys: pytest.CaptureFixture[str]
) -> None:
    # ESC ] 0 ; ... BEL retitles a terminal; a bare CR overwrites the line.
    _raw_request(shared, b"GET /\x1b]0;pwned\x07/\rfake HTTP/1.0")
    err = capsys.readouterr().err
    assert "\\x1b]0;pwned\\x07" in err and "\\x0d" in err
    assert not any(ch in err for ch in "\x1b\x07\r")


def test_escaping_happens_before_the_token_is_redacted(
    shared: WindowServer, capsys: pytest.CaptureFixture[str]
) -> None:
    # A control character between "?t=" and the token must not split it into
    # a part the redaction misses.
    _raw_request(shared, f"GET /api/stream?x=\x01&t={TOKEN} HTTP/1.0".encode())
    err = capsys.readouterr().err
    assert TOKEN not in err and "[redacted]" in err


def test_no_stderr_is_no_crash(shared: WindowServer, monkeypatch: pytest.MonkeyPatch) -> None:
    # A windowed exe starts with sys.stderr = None; a refused request must
    # still get its answer rather than kill the handler thread.
    monkeypatch.setattr(sys, "stderr", None)
    r = call(shared, "GET", "/api/state", headers={"Host": "evil.example"})
    assert r.status == 403


# ───────────────────────────── routes ─────────────────────────────


def test_unknown_api_is_404_and_wrong_method_is_405(shared: WindowServer) -> None:
    assert call(shared, "GET", "/api/nope").status == 404
    r = call(shared, "GET", "/api/stop")
    assert r.status == 405
    assert "POST" in r.body["error"]
    assert post(shared, "/api/state").status == 405


def test_state_shape(srv: WindowServer, dbpath: Path) -> None:
    c = connect(dbpath)
    try:
        liveness.beat(c, "desk", state="awake")
    finally:
        c.close()
    r = call(srv, "GET", "/api/state")
    assert r.status == 200
    s = r.body
    for key in (
        "now",
        "last_seq",
        "processes",
        "presence",
        "spend",
        "projects",
        "jobs",
        "pending",
        "reminders",
        "notes",
        "hearing",
        "wake",
        "chat",
        "speech",
        "tools",
    ):
        assert key in s, key
    assert s["processes"]["desk"]["online"] is True
    assert s["processes"]["desk"]["state"] == "awake"
    assert s["speech"] == {"available": True, "via": "desk", "why": ""}
    assert s["chat"] == {"available": True, "why": ""}
    assert s["wake"] == {"word": "hey jarvis", "threshold": 0.5}
    assert s["tools"] == registry(extra=_probe_tools(Fakes())).declarations("cli")
    assert "phone_only" not in {t["name"] for t in s["tools"]}


def test_state_says_why_chat_and_speech_are_missing(make: ServerFactory) -> None:
    ws = make(chat=None, chat_why="No Gemini key.", speak=None, speak_why="No voice.")
    s = call(ws, "GET", "/api/state").body
    assert s["chat"] == {"available": False, "why": "No Gemini key."}
    assert s["speech"] == {"available": False, "via": None, "why": "No voice."}


def test_feed_endpoint_merges_and_continues(srv: WindowServer, con: sqlite3.Connection) -> None:
    publish(con, "live.input_transcript", "desk", {"at": "x", "text": "Hello"})
    publish(con, "channel.attached", "desk", {})
    publish(con, "live.input_transcript", "desk", {"at": "x", "text": " there"})
    publish(con, "window.said", "window", {"text": "typed"})
    first = call(srv, "GET", "/api/feed").body
    assert [(i["role"], i["text"]) for i in first["items"]] == [
        ("user", "Hello there"),
        ("user", "typed"),
    ]
    assert first["last_seq"] == last_seq(con)
    publish(con, "live.output_transcript", "desk", {"at": "x", "text": "Hi"})
    after = last_seq(con)
    publish(con, "live.output_transcript", "desk", {"at": "x", "text": "!"})
    nxt = call(srv, "GET", f"/api/feed?after={after}&limit=5").body
    assert [(i["role"], i["text"], i["merge"]) for i in nxt["items"]] == [("jarvis", "!", True)]
    assert nxt["last_seq"] == last_seq(con)
    assert len(call(srv, "GET", "/api/feed?limit=1").body["items"]) == 1


def test_feed_rejects_bad_numbers(shared: WindowServer) -> None:
    assert call(shared, "GET", "/api/feed?after=abc").status == 400
    assert call(shared, "GET", "/api/feed?after=-1").status == 400
    assert call(shared, "GET", "/api/feed?limit=x").status == 400
    assert call(shared, "GET", "/api/feed?limit=100000").status == 200


def test_tool_reaches_the_registry_as_cli_and_window(
    srv: WindowServer, fakes: Fakes, dbpath: Path
) -> None:
    r = post(srv, "/api/tool", {"name": "probe", "args": {"text": "abc"}})
    assert r.status == 200
    assert r.body == {"ok": True, "said": "probed abc"}
    [ctx] = fakes.tool_calls
    assert (ctx.channel, ctx.actor) == ("cli", "window")
    assert ctx.extra == EXTRA
    [used] = _events(dbpath, "tool.used")
    assert used["actor"] == "window"
    assert json.loads(used["payload"])["channel"] == "cli"


def test_a_real_tool_writes_its_row(srv: WindowServer, dbpath: Path) -> None:
    r = post(srv, "/api/tool", {"name": "remember", "args": {"fact": "locker is 214"}})
    assert r.status == 200 and r.body["ok"] is True
    state = call(srv, "GET", "/api/state").body
    assert "locker is 214" in [n["text"] for n in state["notes"]]


def test_tool_refusals_are_the_said_text_not_errors(srv: WindowServer) -> None:
    r = post(srv, "/api/tool", {"name": "phone_only"})
    assert r.status == 200
    assert "isn't available over cli" in r.body["said"]
    r = post(srv, "/api/tool", {"name": "no_such_tool"})
    assert r.status == 200 and "don't have a tool" in r.body["said"]
    r = post(srv, "/api/tool", {"name": "probe", "args": {"invented": 1}})
    assert r.status == 200 and "invented" in r.body["said"]


def test_tool_rejects_bad_shapes(shared: WindowServer) -> None:
    assert post(shared, "/api/tool", {}).status == 400
    assert post(shared, "/api/tool", {"name": "  "}).status == 400
    assert post(shared, "/api/tool", {"name": "probe", "args": [1]}).status == 400


def test_chat_publishes_said_and_reply_and_returns_the_reply(
    srv: WindowServer, fakes: Fakes, dbpath: Path
) -> None:
    r = post(srv, "/api/chat", {"text": "  what's the weather?  ", "speak": False})
    assert r.status == 200
    assert r.body == {
        "ok": True,
        "reply": "You said: what's the weather?",
        "tools": [["weather", "Sunny, 24 degrees."]],
    }
    assert fakes.chats == ["what's the weather?"]
    rows = [e for e in _events(dbpath) if e["kind"].startswith("window.")]
    assert [e["kind"] for e in rows] == ["window.said", "window.reply"]
    assert all(e["actor"] == "window" for e in rows)
    assert json.loads(rows[0]["payload"]) == {"text": "what's the weather?"}
    assert json.loads(rows[1]["payload"]) == {
        "text": "You said: what's the weather?",
        "tools": [["weather", "Sunny, 24 degrees."]],
    }
    assert fakes.spoken == []
    feed = call(srv, "GET", "/api/feed").body["items"]
    assert [(i["role"], i["kind"]) for i in feed][-2:] == [
        ("user", "window.said"),
        ("jarvis", "window.reply"),
    ]


def test_chat_is_503_when_there_is_no_chat(make: ServerFactory, dbpath: Path) -> None:
    ws = make(chat=None, chat_why="Chat needs a Gemini key: python -m jarvis secrets set ...")
    r = post(ws, "/api/chat", {"text": "hi"})
    assert r.status == 503
    assert r.body == {
        "ok": False,
        "error": "Chat needs a Gemini key: python -m jarvis secrets set ...",
    }
    assert _events(dbpath, "window.said") == []


def test_chat_failure_is_a_sentence_and_a_feed_line(
    srv: WindowServer, fakes: Fakes, dbpath: Path
) -> None:
    fakes.chat_error = "Gemini said the key is invalid."
    r = post(srv, "/api/chat", {"text": "hi"})
    assert r.status == 502
    assert r.body == {"ok": False, "error": "Gemini said the key is invalid."}
    assert [e["kind"] for e in _events(dbpath) if e["kind"].startswith("window.")] == [
        "window.said",
        "window.error",
    ]


def test_chat_rejects_empty_text(shared: WindowServer) -> None:
    assert post(shared, "/api/chat", {"text": "   "}).status == 400
    assert post(shared, "/api/chat", {"text": 5}).status == 400


def test_chat_speaks_on_a_background_thread_after_responding(
    srv: WindowServer, fakes: Fakes
) -> None:
    fakes.gate.clear()  # the speaker blocks until released
    r = post(srv, "/api/chat", {"text": "hello", "speak": True})
    # The reply arrived while the voice was still held: it did not wait for audio.
    assert r.status == 200
    assert fakes.spoken == []
    fakes.gate.set()
    assert fakes.spoke.wait(timeout=3)
    assert fakes.spoken == ["You said: hello"]


def test_say_goes_through_the_desk_when_its_beat_is_fresh(
    srv: WindowServer, fakes: Fakes, con: sqlite3.Connection
) -> None:
    liveness.beat(con, "desk", state="asleep")
    r = post(srv, "/api/say", {"text": "Option one."})
    assert r.status == 200
    assert r.body == {"ok": True, "via": "desk"}
    # Synchronous on this route: the command row exists before the reply does.
    assert fakes.spoken == ["Option one."]


def test_say_plays_locally_when_the_desk_is_not_running(
    srv: WindowServer, fakes: Fakes, con: sqlite3.Connection
) -> None:
    liveness.beat(con, "desk", state="asleep")
    liveness.gone(con, "desk")
    fakes.gate.clear()
    fakes.speak_result = "local"
    r = post(srv, "/api/say", {"text": "Option two."})
    assert r.status == 200
    assert r.body == {"ok": True, "via": "local"}
    assert fakes.spoken == []  # playback had not even started when we got the answer
    fakes.gate.set()
    assert fakes.spoke.wait(timeout=3)
    assert fakes.spoken == ["Option two."]


def test_say_without_a_voice_is_503(make: ServerFactory) -> None:
    ws = make(speak=None, speak_why="No reader voice is installed.")
    r = post(ws, "/api/say", {"text": "hi"})
    assert r.status == 503
    assert r.body == {"ok": False, "error": "No reader voice is installed."}


def test_say_failure_through_the_desk_is_500(make: ServerFactory, con: sqlite3.Connection) -> None:
    def broken(text: str) -> str:
        raise RuntimeError("The desk did not take the command.")

    ws = make(speak=broken)
    liveness.beat(con, "desk", state="awake")
    r = post(ws, "/api/say", {"text": "hi"})
    assert r.status == 500
    assert r.body == {"ok": False, "error": "The desk did not take the command."}


def _ask(con: sqlite3.Connection) -> rq.Request:
    return rq.create_request(
        con,
        kind="plan_question",
        short_label=answers.short_label(QUESTIONS),
        presentation=answers.presentation(QUESTIONS),
        payload=QUESTIONS,
        actor="test",
        tool_name="AskUserQuestion",
    )


def test_answer_settles_a_real_request_once(
    srv: WindowServer, con: sqlite3.Connection, dbpath: Path
) -> None:
    req = _ask(con)
    assert [p["id"] for p in call(srv, "GET", "/api/state").body["pending"]] == [req.id]
    r = post(srv, "/api/answer", {"request_id": req.id, "picks": [2], "text": None})
    assert r.status == 200
    assert r.body == {"ok": True, "answered": True, "message": "answered: database"}
    done = rq.get_request(con, req.id)
    assert done is not None
    assert done.state == "answered"
    assert (done.answered_by, done.answer_mode) == ("window", "hud")
    assert done.answer is not None
    assert done.answer["answers"] == {"Which database should the app use?": "Postgres"}
    # Published once, by the server: answer_request writes no event of its own.
    [ev] = _events(dbpath, "request.answered")
    assert ev["request_id"] == req.id and ev["actor"] == "window"
    again = post(srv, "/api/answer", {"request_id": req.id, "picks": [1]})
    assert again.status == 200
    assert again.body == {"ok": True, "answered": False, "message": "already answered"}
    assert len(_events(dbpath, "request.answered")) == 1
    assert call(srv, "GET", "/api/state").body["pending"] == []


def test_answer_in_free_text(srv: WindowServer, con: sqlite3.Connection) -> None:
    req = _ask(con)
    r = post(srv, "/api/answer", {"request_id": req.id, "picks": [], "text": "DuckDB, please"})
    assert r.body["answered"] is True
    done = rq.get_request(con, req.id)
    assert done is not None and done.answer is not None
    assert done.answer["answers"] == {"Which database should the app use?": "DuckDB, please"}


def test_answer_loses_the_race_cleanly(
    srv: WindowServer, con: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    req = _ask(con)
    real = rq.answer_request

    def telegram_got_there_first(c: sqlite3.Connection, rid: str, *a: Any, **k: Any) -> bool:
        real(c, rid, {"text": "SQLite"}, answered_by="telegram:1", answer_mode="button")
        return real(c, rid, *a, **k)

    monkeypatch.setattr(rq, "answer_request", telegram_got_there_first)
    r = post(srv, "/api/answer", {"request_id": req.id, "picks": [1]})
    assert r.body == {"ok": True, "answered": False, "message": "already answered"}


def test_answer_refuses_bad_picks_with_a_sentence(
    srv: WindowServer, con: sqlite3.Connection
) -> None:
    req = _ask(con)
    r = post(srv, "/api/answer", {"request_id": req.id, "picks": [9]})
    assert r.status == 400 and r.body["ok"] is False and r.body["error"]
    assert post(srv, "/api/answer", {"request_id": req.id, "picks": ["1"]}).status == 400
    assert post(srv, "/api/answer", {"request_id": req.id, "picks": [True]}).status == 400
    assert post(srv, "/api/answer", {"picks": [1]}).status == 400
    assert post(srv, "/api/answer", {"request_id": "req_nope", "picks": [1]}).status == 404
    still = rq.get_request(con, req.id)
    assert still is not None and still.state == "pending"


def test_stop_issues_a_real_stop_all_command(srv: WindowServer, con: sqlite3.Connection) -> None:
    epoch = kill.current_epoch(con)
    r = post(srv, "/api/stop", {})
    assert r.status == 200
    assert r.body["ok"] is True and r.body["message"]
    cmd = kill.get_command(con, r.body["command_id"])
    assert cmd is not None
    assert (cmd.verb, cmd.target_kind, cmd.issued_by) == ("stop_all", "all", "window")
    assert kill.current_epoch(con) == epoch + 1


# ───────────────────────────── the stream ─────────────────────────────


def test_stream_sends_state_first_then_feed_after_a_publish(
    srv: WindowServer, con: sqlite3.Connection
) -> None:
    s = Stream(srv)
    try:
        first = s.until("state")
        assert first["processes"]["desk"]["online"] is False
        publish(con, "window.said", "window", {"text": "hello from a test"})
        feed = s.until("feed")
        assert [(i["role"], i["text"]) for i in feed["items"]] == [("user", "hello from a test")]
        assert feed["last_seq"] == last_seq(con)
        # window.said can change the snapshot (it is not a transcript fragment).
        name, _ = s.next()
        assert name == "refresh"
    finally:
        s.close()


def test_stream_sends_state_after_a_beat(srv: WindowServer, con: sqlite3.Connection) -> None:
    s = Stream(srv)
    try:
        s.until("state")
        liveness.beat(con, "desk", state="listening")
        data = s.until("state")
        assert data["processes"]["desk"]["online"] is True
        assert data["processes"]["desk"]["state"] == "listening"
        liveness.beat(con, "desk", state="speaking")
        assert s.until("state")["processes"]["desk"]["state"] == "speaking"
        liveness.gone(con, "desk")
        gone = s.until("state")["processes"]["desk"]
        assert (gone["online"], gone["state"]) == (False, "offline")
    finally:
        s.close()


def test_stream_does_not_refresh_for_transcript_fragments(
    srv: WindowServer, con: sqlite3.Connection
) -> None:
    s = Stream(srv)
    try:
        s.until("state")
        publish(con, "live.input_transcript", "desk", {"at": "x", "text": "hel"})
        assert s.until("feed")["items"][0]["text"] == "hel"
        publish(con, "live.input_transcript", "desk", {"at": "x", "text": "lo"})
        more = s.next()
        # The second fragment continues the first bubble, and nothing on the
        # snapshot moved, so no refresh was sent between them.
        assert more[0] == "feed"
        assert (more[1]["items"][0]["text"], more[1]["items"][0]["merge"]) == ("lo", True)
        publish(con, "tool.used", "desk", {"tool": "weather"})
        assert [s.next()[0], s.next()[0]] == ["feed", "refresh"]
    finally:
        s.close()


def test_stream_refreshes_when_a_question_is_answered_without_an_event(
    srv: WindowServer, con: sqlite3.Connection
) -> None:
    req = _ask(con)
    s = Stream(srv)
    try:
        s.until("state")
        rq.answer_request(
            con, req.id, answers.build_answer(req, picks=(1,)), answered_by="cli", answer_mode="hud"
        )
        assert s.until("refresh") == {}
    finally:
        s.close()


def test_stream_replays_from_after(srv: WindowServer, con: sqlite3.Connection) -> None:
    publish(con, "window.said", "window", {"text": "one"})
    mark = last_seq(con)
    publish(con, "window.said", "window", {"text": "two"})
    s = Stream(srv, query=f"t={TOKEN}&after={mark}")
    try:
        feed = s.until("feed")
        assert [i["text"] for i in feed["items"]] == ["two"]
    finally:
        s.close()


def test_stream_ends_when_the_client_disconnects(srv: WindowServer) -> None:
    s = Stream(srv)
    s.until("state")
    assert srv.active_streams == 1
    s.close()
    assert _wait(lambda: srv.active_streams == 0), "the stream thread outlived its client"


def test_stream_ends_on_shutdown(make: ServerFactory) -> None:
    ws = make()
    streams = [Stream(ws), Stream(ws)]
    for s in streams:
        s.until("state")
    assert ws.active_streams == 2
    ws.shutdown()
    assert _wait(lambda: ws.active_streams == 0), "a stream survived shutdown"
    for s in streams:
        with pytest.raises((EOFError, OSError)):
            for _ in range(50):
                s.next()
        s.close()


# ───────────────────────────── the callers exist ─────────────────────────────


def test_every_spine_function_the_server_calls_exists() -> None:
    """The server once called ``snapshot.changed_since``, which did not exist.

    Nothing raised until the first stream saw a new event — the bug class this
    tree is prone to — so every ``module.attr`` the server reaches for is
    checked against the real module here.
    """
    tree = ast.parse(Path(server_mod.__file__).read_text(encoding="utf-8"))
    modules = {"snapshot": snapshot, "rq": rq, "kill": kill}
    used = {
        (node.value.id, node.attr)
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in modules
    }
    assert ("snapshot", "changed_since") in used
    missing = sorted(f"{m}.{a}" for m, a in used if not hasattr(modules[m], a))
    assert missing == []


# ───────────────────────────── the app: setup and processes ─────────────────────────────

SECRET = "AIzaSyD-SERVER-TEST-VALUE-0123456789abcdef"

APP_ROUTES = (
    ("GET", "/api/setup"),
    ("POST", "/api/setup/secret"),
    ("POST", "/api/setup/setting"),
    ("POST", "/api/setup/wake"),
    ("POST", "/api/setup/preview"),
    ("POST", "/api/setup/claude"),
    ("POST", "/api/setup/phone"),
    ("GET", "/api/app"),
    ("POST", "/api/app/restart"),
    ("POST", "/api/app/quit"),
)


class _Keyring:
    """The jarvis.secrets surface SetupService uses, holding values in a dict."""

    def __init__(self) -> None:
        self.stored: dict[str, str] = {}
        self.fail: BaseException | None = None

    def get(self, name: str) -> str | None:
        return self.stored.get(name)

    def store(self, name: str, value: str) -> None:
        if self.fail is not None:
            raise self.fail
        self.stored[name] = value

    def keyring_available(self) -> tuple[bool, str]:
        return True, "keyring backend: Fake"


class _Control:
    """The app's control: the supervisor's status, a restart, a quit."""

    def __init__(self) -> None:
        self.restarts: list[str] = []
        self.quit_called = threading.Event()
        self.procs: dict[str, dict[str, Any]] = {
            "desk": {
                "running": True,
                "pid": 4242,
                "restarts": 1,
                "last_exit": None,
                "held": False,
                "reason": "",
                "log": "/logs/desk.log",
            },
            "telegram": {
                "running": False,
                "pid": None,
                "restarts": 0,
                "last_exit": 2,
                "held": True,
                "reason": (
                    "telegram_bot_token is not set.\n  store it : python -m jarvis secrets set x"
                ),
                "log": "/logs/telegram.log",
            },
        }

    def status(self) -> dict[str, dict[str, Any]]:
        return json.loads(json.dumps(self.procs))

    def restart(self, name: str) -> None:
        if name not in self.procs:
            raise ValueError(f"There is no process called {name!r} to restart.")
        self.restarts.append(name)

    def quit(self) -> None:
        self.quit_called.set()


@dataclass
class AppParts:
    setup: Any
    control: _Control
    keyring: _Keyring
    config: Path
    calls: dict[str, list[Any]]


@pytest.fixture
def parts(tmp_path: Path, dbpath: Path) -> AppParts:
    from jarvis.app.setup import SetupService

    calls: dict[str, list[Any]] = {
        "restart": [],
        "download": [],
        "preview": [],
        "login": [],
        "autostart": [],
    }
    keyring = _Keyring()
    control = _Control()
    config = tmp_path / "cfg" / "config.toml"

    def download(word: str) -> str:
        calls["download"].append(word)
        return "Fetched the wake-word model."

    def restart(name: str) -> None:
        calls["restart"].append(name)

    setup = SetupService(
        config_path=config,
        db_path=dbpath,
        secrets_mod=keyring,
        list_devices=lambda: [{"label": "Jabra Evolve2 40"}],
        wake_ready=lambda word: False,
        download_wake=download,
        preview_voice=lambda voice, sentence: calls["preview"].append((voice, sentence)),
        restart=restart,
        autostart=lambda on: calls["autostart"].append(on),
        claude_login=lambda: calls["login"].append(1) or "The sign-in is open in your browser.",
        run_later=lambda fn: fn(),
    )
    return AppParts(setup=setup, control=control, keyring=keyring, config=config, calls=calls)


@pytest.fixture
def app_srv(make: ServerFactory, parts: AppParts) -> WindowServer:
    return make(setup=parts.setup, control=parts.control)


def test_every_app_route_needs_the_token(shared: WindowServer) -> None:
    for method, path in APP_ROUTES:
        r = call(shared, method, path, json_body={} if method == "POST" else None, token=None)
        assert r.status == 401, (method, path)


def test_app_routes_refuse_a_foreign_origin_even_with_the_token(
    app_srv: WindowServer, parts: AppParts
) -> None:
    for method, path in APP_ROUTES:
        r = call(
            app_srv,
            method,
            path,
            json_body={} if method == "POST" else None,
            headers={"Origin": "http://evil.example"},
        )
        assert r.status == 403, (method, path)
    assert parts.control.restarts == [] and not parts.control.quit_called.is_set()


def test_app_posts_need_json(app_srv: WindowServer, parts: AppParts) -> None:
    for method, path in APP_ROUTES:
        if method == "POST":
            r = call(app_srv, "POST", path, raw=b"{}", content_type="text/plain")
            assert r.status == 415, path
    assert parts.keyring.stored == {}


def test_setup_and_app_are_503_outside_the_app(shared: WindowServer) -> None:
    for method, path in APP_ROUTES:
        r = call(shared, method, path, json_body={} if method == "POST" else None)
        assert r.status == 503, (method, path)
        assert r.body["ok"] is False and r.body["error"].endswith(".")
        assert "python -m" not in r.body["error"]


def test_state_says_whether_it_runs_in_the_app(shared: WindowServer, app_srv: WindowServer) -> None:
    assert call(shared, "GET", "/api/state").body["app"] is False
    assert call(app_srv, "GET", "/api/state").body["app"] is True


def test_setup_status_carries_the_supervisors_problems(app_srv: WindowServer) -> None:
    r = call(app_srv, "GET", "/api/setup")
    assert r.status == 200
    s = r.body
    assert s["first_run"] is True
    assert s["secrets"]["gemini_api_key"] is False
    assert s["devices"] == [{"label": "Jabra Evolve2 40", "selected": False}]
    assert s["problems"] == [
        {
            "process": "telegram",
            "sentence": "telegram_bot_token is not set.",
            "action": "restart:telegram",
        }
    ]
    assert "python -m" not in r.raw.decode()


def test_a_secret_is_stored_and_never_comes_back(
    app_srv: WindowServer, parts: AppParts, dbpath: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    r = post(app_srv, "/api/setup/secret", {"name": "gemini_api_key", "value": SECRET})
    assert r.status == 200
    assert r.body == {"ok": True, "present": True, "restarted": ["desk"]}
    assert parts.keyring.stored == {"gemini_api_key": SECRET}
    assert parts.calls["restart"] == ["desk"]
    status = call(app_srv, "GET", "/api/setup")
    assert status.body["secrets"]["gemini_api_key"] is True
    for reply in (r, status, call(app_srv, "GET", "/api/state"), call(app_srv, "GET", "/api/feed")):
        assert SECRET.encode() not in reply.raw
    c = connect(dbpath)
    try:
        dump = "\n".join(c.iterdump())
    finally:
        c.close()
    assert SECRET not in dump, "the key reached the database"
    assert SECRET not in capsys.readouterr().err


def test_onboarding_stores_a_secret_without_restarting(
    app_srv: WindowServer, parts: AppParts
) -> None:
    r = post(
        app_srv, "/api/setup/secret", {"name": "gemini_api_key", "value": SECRET, "restart": False}
    )
    assert r.body["restarted"] == []
    assert parts.calls["restart"] == []


@pytest.mark.parametrize(
    "body",
    [
        {"name": "gemini_api_key", "value": f"{SECRET} trailing"},
        {"name": "gemini_api_key", "value": f"{SECRET}\n"},
        {"name": "gemini_api_key", "value": SECRET + "x" * 4096},
        {"name": "google_oauth_client", "value": SECRET},
        {"name": "../../etc", "value": SECRET},
        {"name": "gemini_api_key", "value": ""},
        {"name": "gemini_api_key", "value": 7},
        {"name": "gemini_api_key"},
        {"value": SECRET},
    ],
)
def test_a_refused_secret_is_a_400_sentence_that_never_quotes_it(
    app_srv: WindowServer, parts: AppParts, capsys: pytest.CaptureFixture[str], body: dict
) -> None:
    r = post(app_srv, "/api/setup/secret", body)
    assert r.status == 400
    assert r.body["ok"] is False and r.body["error"]
    assert SECRET.encode() not in r.raw
    assert parts.keyring.stored == {}
    assert SECRET not in capsys.readouterr().err


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError(f"backend refused password {SECRET!r}"),
        TypeError(f"cannot store {SECRET}"),
        OSError(f"locked: {SECRET}"),
    ],
)
def test_a_keyring_failure_never_leaks_the_value(
    app_srv: WindowServer,
    parts: AppParts,
    capsys: pytest.CaptureFixture[str],
    failure: Exception,
) -> None:
    parts.keyring.fail = failure
    r = post(app_srv, "/api/setup/secret", {"name": "gemini_api_key", "value": SECRET})
    assert r.status == 502
    assert SECRET.encode() not in r.raw
    assert "keyring" in r.body["error"]
    assert SECRET not in capsys.readouterr().err


def test_an_unexpected_error_in_the_service_never_prints_the_value(
    make: ServerFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    class Leaky:
        def set_secret(self, name: str, value: str, *, restart: bool = True) -> dict:
            raise LookupError(f"lost {value}")

    class Buggy:
        def set_secret(self, name: str, value: str, *, restart: bool = True) -> dict:
            raise AttributeError(f"no attribute for {value}")

    for setup in (Leaky(), Buggy()):
        ws = make(setup=setup)
        r = post(ws, "/api/setup/secret", {"name": "gemini_api_key", "value": SECRET})
        assert r.status == 500
        assert r.body["error"] == "I couldn't store that key."
        err = capsys.readouterr().err
        assert SECRET not in err
        assert "Traceback" not in err


def test_a_setting_is_saved_through_the_service(app_srv: WindowServer, parts: AppParts) -> None:
    from jarvis import config as cfgmod

    r = post(app_srv, "/api/setup/setting", {"key": "persona.address", "value": "boss"})
    assert r.status == 200
    assert r.body["ok"] is True and r.body["restarted"] == ["desk"]
    assert cfgmod.load(parts.config).persona.address == "boss"
    assert call(app_srv, "GET", "/api/setup").body["settings"]["persona.address"] == "boss"
    r = post(
        app_srv,
        "/api/setup/setting",
        {"key": "voice.vocabulary", "value": ["quote", "Kadıköy"], "restart": False},
    )
    assert r.body["restarted"] == []
    assert cfgmod.load(parts.config).voice.vocabulary == ("quote", "Kadıköy")


@pytest.mark.parametrize(
    ("body", "words"),
    [
        ({"key": "desk.permission_mode", "value": "dontAsk"}, "not a setting"),
        ({"key": "app.start_telegram", "value": "false"}, "switch"),
        ({"key": "location.units", "value": "furlongs"}, "metric or imperial"),
        ({"key": "voice.wake_threshold", "value": 7}, "between 0 and 1"),
        ({"key": "location.city", "value": SECRET}, "credential"),
        ({"key": "persona.name"}, "missing"),
        ({"value": "x"}, "Which setting"),
        ({"key": 3, "value": "x"}, "Which setting"),
    ],
)
def test_a_bad_setting_is_a_400_sentence(
    app_srv: WindowServer, parts: AppParts, body: dict, words: str
) -> None:
    r = post(app_srv, "/api/setup/setting", body)
    assert r.status == 400
    assert r.body["ok"] is False
    assert words.lower() in r.body["error"].lower()
    assert SECRET not in r.body["error"]
    assert "python -m" not in r.body["error"]
    assert parts.calls["restart"] == []
    assert not parts.config.with_name("app-settings.toml").exists()


def test_the_wake_model_downloads_on_request(app_srv: WindowServer, parts: AppParts) -> None:
    r = post(app_srv, "/api/setup/wake", {"restart": False})
    assert r.status == 200
    assert r.body["ok"] is True and r.body["message"] == "Fetched the wake-word model."
    assert parts.calls["download"] == ["hey_jarvis"]
    assert parts.calls["restart"] == []
    assert "non-commercial" in r.body["licence"]


def test_a_failed_download_is_a_502_sentence(make: ServerFactory, parts: AppParts) -> None:
    def broken(word: str) -> str:
        raise OSError("connection reset; run python -m jarvis wake download")

    parts.setup._download_wake = broken
    r = post(make(setup=parts.setup), "/api/setup/wake")
    assert r.status == 502
    assert "connection reset" in r.body["error"].lower()
    assert "python -m" not in r.body["error"]


def test_a_voice_sample_is_played(app_srv: WindowServer, parts: AppParts) -> None:
    r = post(app_srv, "/api/setup/preview", {"voice": "Charon"})
    assert r.status == 200 and r.body["ok"] is True
    ((voice, sentence),) = parts.calls["preview"]
    assert voice == "Charon" and "Charon" in sentence
    for bad in ({"voice": "Nobody"}, {"voice": ""}, {}, {"voice": 3}):
        assert post(app_srv, "/api/setup/preview", bad).status == 400, bad
    assert len(parts.calls["preview"]) == 1


def test_claude_sign_in(app_srv: WindowServer, parts: AppParts) -> None:
    r = post(app_srv, "/api/setup/claude")
    assert r.status == 200
    assert r.body == {"ok": True, "message": "The sign-in is open in your browser."}
    assert parts.calls["login"] == [1]


def test_pairing_a_phone_from_the_window(
    app_srv: WindowServer, parts: AppParts, dbpath: Path
) -> None:
    from jarvis.telegram import identity

    r = post(app_srv, "/api/setup/phone", {"action": "pair"})
    assert r.status == 502 and "bot token first" in r.body["error"]

    parts.keyring.stored["telegram_bot_token"] = "123456:" + "A" * 35
    r = post(app_srv, "/api/setup/phone", {"action": "pair"})
    assert r.status == 200 and r.body["minutes"] == 10
    code = r.body["code"]
    assert code and "bot" in r.body["message"]

    # The code the window showed is the one the bot accepts.
    c = connect(dbpath)
    try:
        assert identity.redeem(c, 42, code).ok
        assert identity.bound_chat(c) == 42
    finally:
        c.close()
    assert call(app_srv, "GET", "/api/setup").body["phone"] == {"paired": True}

    r = post(app_srv, "/api/setup/phone", {"action": "unpair"})
    assert r.status == 200 and "no longer paired" in r.body["message"]
    assert call(app_srv, "GET", "/api/setup").body["phone"] == {"paired": False}
    for bad in ({}, {"action": "steal"}, {"action": 1}):
        assert post(app_srv, "/api/setup/phone", bad).status == 400, bad


def test_the_readouts_follow_the_settings_not_the_launch(make: ServerFactory) -> None:
    now = {"wake_word": "hey jarvis", "wake_threshold": 0.5, "tz": "Europe/Istanbul"}
    srv = make(readouts=lambda: dict(now))
    first = call(srv, "GET", "/api/state").body
    now.update(wake_word="", wake_threshold=0.3, tz="Europe/London")
    second = call(srv, "GET", "/api/state").body
    assert first != second, "a setting changed and the readout did not"


def test_a_broken_readout_falls_back_to_the_launch_values(make: ServerFactory) -> None:
    def boom() -> dict[str, Any]:
        raise RuntimeError("config.toml is half-written")

    assert call(make(readouts=boom), "GET", "/api/state").status == 200


def test_app_status_is_the_supervisors_with_command_free_reasons(app_srv: WindowServer) -> None:
    r = call(app_srv, "GET", "/api/app")
    assert r.status == 200
    body = r.body
    assert body["app"] is True and body["ok"] is True
    assert body["processes"]["desk"] == {
        "running": True,
        "held": False,
        "pid": 4242,
        "restarts": 1,
        "last_exit": None,
        "reason": "",
        "log": "/logs/desk.log",
    }
    assert body["processes"]["telegram"]["held"] is True
    assert body["processes"]["telegram"]["reason"] == "telegram_bot_token is not set."
    assert "python -m" not in json.dumps(body["processes"])


def test_restart_reaches_the_control(app_srv: WindowServer, parts: AppParts) -> None:
    r = post(app_srv, "/api/app/restart", {"process": "desk"})
    assert r.status == 200 and r.body["message"] == "Restarting the desk."
    assert parts.control.restarts == ["desk"]
    bad = post(app_srv, "/api/app/restart", {"process": "nope"})
    assert bad.status == 400 and "nope" in bad.body["error"]
    assert post(app_srv, "/api/app/restart", {}).status == 400
    assert parts.control.restarts == ["desk"]


def test_quit_answers_first_then_quits(app_srv: WindowServer, parts: AppParts) -> None:
    r = post(app_srv, "/api/app/quit")
    assert r.status == 200 and r.body["ok"] is True
    assert parts.control.quit_called.wait(3)


def test_the_new_routes_are_registered_with_their_methods() -> None:
    for method, path in APP_ROUTES:
        assert server_mod._ROUTES[path][0] == method, path


def test_every_setup_and_control_method_the_server_calls_exists() -> None:
    """The callers exist, and so do the callees: a renamed method is a 500 at the first click."""
    from jarvis.app.setup import SetupService

    tree = ast.parse(Path(server_mod.__file__).read_text(encoding="utf-8"))
    called: dict[str, set[str]] = {"setup": set(), "control": set()}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in called
        ):
            called[node.value.id].add(node.attr)
    assert called["setup"] >= {
        "status",
        "set_secret",
        "set_setting",
        "download_wake",
        "preview",
        "sign_in_claude",
    }
    assert called["control"] >= {"status", "restart", "quit"}
    for name in called["setup"]:
        assert callable(getattr(SetupService, name, None)), f"SetupService.{name}"
    try:
        from jarvis.app.supervisor import Control
    except ImportError:  # the app layer is another builder's; its own tests cover it
        return
    for name in called["control"]:
        assert callable(getattr(Control, name, None)), f"Control.{name}"


def test_the_secret_bound_matches_the_services() -> None:
    from jarvis.app.setup import MAX_SECRET

    assert server_mod.MAX_SECRET == MAX_SECRET


def test_feed_renders_the_apps_events_without_commands(
    srv: WindowServer, con: sqlite3.Connection
) -> None:
    publish(
        con,
        "desk.refused",
        "desk",
        {
            "sentence": "gemini_api_key is not set.\n  store it : python -m jarvis secrets set x",
            "action": "secret:gemini_api_key",
        },
    )
    publish(con, "app.process_exited", "app", {"process": "telegram", "code": 2, "reason": ""})
    publish(con, "app.process_exited", "app", {"process": "schedule", "code": 1, "reason": "boom"})
    publish(con, "app.started", "app", {"version": "1.0"})
    items = call(srv, "GET", "/api/feed").body["items"]
    assert [(i["role"], i["kind"], i["text"]) for i in items] == [
        ("system", "desk.refused", "desk couldn't start: gemini_api_key is not set."),
        ("system", "app.process_exited", "Telegram is waiting for you"),
        ("system", "app.process_exited", "scheduler stopped unexpectedly (exit 1): Boom."),
        ("system", "app.started", "Jarvis is online"),
    ]
