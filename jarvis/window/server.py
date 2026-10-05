"""The window's HTTP server: one local page, a small JSON API, and one event stream.

WHY A BROWSER AND NOT A TOOLKIT. A Qt or Tk window would put a GUI toolkit on
the import path of a process whose whole job is to read rows, and it would be a
second rendering engine to keep working on Windows and Linux. Every machine this
runs on already has a browser that can open a page as an app window; serving the
page from the standard library costs nothing to install.

THE PAGE IS THE ONLY WAY IN, AND IT IS LOCAL. Everything here is shaped by the
fact that a web server on 127.0.0.1 is reachable by every web page the user
visits, not just by ours:

* A random TOKEN guards every ``/api/*`` call. The launcher puts it in the URL
  FRAGMENT, which a browser never sends to a server, never writes to a log and
  never leaks in a Referer. The page moves it into a header.
* The ``Host`` header must name this port on a loopback name, which is what
  defeats DNS rebinding (an attacker's domain re-pointed at 127.0.0.1 still
  says ``Host: evil.example``).
* A present ``Origin`` must be our own, and no ``Access-Control-*`` header is
  ever sent, so no other origin can read a response even with the token.
* POST bodies are small JSON objects with the JSON content type, which a plain
  cross-site HTML form cannot produce.
* A strict CSP on every response: the page runs only its own script.

THE WINDOW READS AND WRITES ROWS, NOTHING ELSE. It never reaches into the desk's
memory. Asking the desk to say something is a ``say`` command row; what the
desk is doing is a heartbeat row; what was said is the event log. Each API call
opens its OWN connection through ``Services.open_db`` and closes it, because a
request handler is a process edge and runs on a thread that will not exist in a
second.

INSIDE THE APP it also carries the app's settings (``/api/setup*``) and its
processes (``/api/app*``), through two duck-typed objects the composition root
hands in — this layer may not import ``jarvis.app``. Outside the app both are
None and those routes answer 503, which is how the page knows to fall back to
terminal hints. A credential posted to ``/api/setup/secret`` goes to the
keyring and nowhere else: not into a reply, an error message or the log.
"""

from __future__ import annotations

import hmac
import json
import re
import secrets
import select
import socket
import socketserver
import sqlite3
import sys
import threading
import time
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from jarvis import kill
from jarvis import requests as rq
from jarvis.answers import build_answer
from jarvis.bus import Redactor, last_seq, publish
from jarvis.tools.confirm import CONFIRMATIONS, DIRECT_HUMAN, Confirmations
from jarvis.tools.ctx import ToolCtx
from jarvis.tools.registry import Registry
from jarvis.window import snapshot

__all__ = [
    "CSP",
    "MAX_BODY",
    "MAX_SECRET",
    "STATIC_DIR",
    "Services",
    "WindowServer",
    "make_server",
]

STATIC_DIR = Path(__file__).parent / "static"

#: A tool call, a chat line or an answer is a few hundred bytes. Anything near
#: this is not the page talking.
MAX_BODY = 64 * 1024

#: No key or token is longer. The same bound the setup service applies; the
#: window checks it too so a refused value never reaches the app at all.
MAX_SECRET = 4096

CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'"
)

#: On EVERY response, errors included: an error page is still a page.
_SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    ("Content-Security-Policy", CSP),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Cache-Control", "no-store"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("X-Frame-Options", "DENY"),
)

#: The ONLY files served, by exact path. Nothing from the request is ever joined
#: onto a filesystem path, so there is no traversal to get wrong.
_STATIC: dict[str, tuple[str, str]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/app.css": ("app.css", "text/css; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
}

_POLL_S = 0.25
_PING_S = 15.0

# A refused body is read and thrown away up to this much before answering.
# Closing a socket with unread data in it makes the kernel send a reset, and a
# reset can destroy the 413 the client was about to read.
_DRAIN_CAP = 1024 * 1024

_TOKEN_SHAPE = re.compile(r"[A-Za-z0-9_-]{16,}")
_TOKEN_IN_LOG = re.compile(r"([?&]t=)[^&\s\"]+")
# A request line is the client's to choose. Escaped before it is logged, so a
# GET carrying ESC sequences cannot retitle or repaint the terminal reading it,
# and a stray CR cannot overwrite the line that says who asked.
_LOG_ESCAPES = {c: f"\\x{c:02x}" for c in (*range(0x20), *range(0x7F, 0xA0))}
_HANGUPS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)


@dataclass
class Services:
    """Everything the window can do, handed in by the composition root.

    Callables rather than objects so that this module never learns what Gemini,
    a sound card or a reader voice is: ``chat`` and ``speak`` are built in
    ``jarvis/__main__.py`` from layers this one may not import.
    """

    #: A NEW connection on every call; the caller closes it.
    open_db: Callable[[], sqlite3.Connection]
    registry: Registry
    #: Passed to ToolCtx.extra for every tool call (location, search, timezone).
    extra: dict[str, Any]
    #: One user message -> (reply, ((tool, said), ...)). Raises RuntimeError(sentence).
    chat: Callable[[str], tuple[str, tuple[tuple[str, str], ...]]] | None = None
    chat_why: str = ""
    #: Text -> "desk" | "local". Raises RuntimeError(sentence).
    speak: Callable[[str], str] | None = None
    speak_why: str = ""
    wake_word: str = ""
    wake_threshold: float = 0.5
    spend_threshold_usd: float = 20.0
    tz: str = "Europe/Istanbul"
    #: Applied to what the window publishes. What the user types is kept in a
    #: log that is kept forever, and a pasted token must not be.
    redactor: Redactor | None = None
    #: The app's: ``{"wake_word", "wake_threshold", "tz"}`` as the settings are
    #: NOW, overriding the fields above, which hold what they were at launch.
    readouts: Callable[[], dict[str, Any]] | None = None
    #: The app's settings and onboarding (``jarvis.app.setup.SetupService``),
    #: duck-typed: this layer may not import the app. None outside the app.
    setup: Any | None = None
    #: The app's processes: ``status() -> {name: {...}}``, ``restart(name)``,
    #: ``quit()``. None when this window was started on its own.
    control: Any | None = None


class _Refusal(Exception):
    """An HTTP error whose message is a sentence the page can show as-is."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class _Server(ThreadingHTTPServer):
    """The socket server, plus the per-instance state its handlers share."""

    # The app asks for the port it had last time, so a restart must not wait
    # out TIME_WAIT. Never on Windows, where SO_REUSEADDR means something else:
    # it lets another socket bind the port we are listening on. Windows needs
    # nothing to rebind past TIME_WAIT, and SO_EXCLUSIVEADDRUSE would add a wait
    # to guard against a same-user process, which can read app.json anyway.
    allow_reuse_address = sys.platform != "win32"
    daemon_threads = True

    def __init__(
        self, address: tuple[str, int], *, services: Services, token: str, static_dir: Path
    ) -> None:
        self.services = services
        self.token = token
        self.static_dir = static_dir
        self.stopping = threading.Event()
        # One conversation has one history; two chats at once would interleave it.
        self.chat_lock = threading.Lock()
        # Local playback queues rather than overlapping into one garbled voice.
        self.speech_lock = threading.Lock()
        self._streams = 0
        self._streams_lock = threading.Lock()
        super().__init__(address, _Handler)

    def server_bind(self) -> None:
        # HTTPServer.server_bind calls socket.getfqdn(), a reverse DNS lookup
        # that can take seconds on Windows, for a name nothing here uses.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)

    def handle_error(self, request: Any, client_address: Any) -> None:
        if isinstance(sys.exc_info()[1], (*_HANGUPS, TimeoutError)):
            return  # a closed tab is not an incident
        super().handle_error(request, client_address)

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    @property
    def active_streams(self) -> int:
        with self._streams_lock:
            return self._streams

    def stream_delta(self, n: int) -> None:
        with self._streams_lock:
            self._streams += n

    def speak_later(self, text: str) -> None:
        """Speak on a thread of its own, after the HTTP response has gone."""
        threading.Thread(target=self._speak, args=(text,), name="window-speak", daemon=True).start()

    def _speak(self, text: str) -> None:
        speak = self.services.speak
        if speak is None:
            return
        with self.speech_lock:
            if self.stopping.is_set():
                return
            try:
                speak(text)
            except Exception as exc:  # noqa: BLE001 - a dead speaker is a line in the feed
                self.note_failure(f"I couldn't say that out loud: {exc}")

    def quit_app(self, control: Any) -> None:
        """Ask the app to quit, from a thread of its own, once the reply has gone."""
        time.sleep(0.05)  # let the socket drain before the server is torn down
        try:
            control.quit()
        except Exception as exc:  # noqa: BLE001 - nobody waits on this response any more
            self.note_failure(f"I couldn't quit: {type(exc).__name__}")

    def note_failure(self, sentence: str) -> None:
        """Put a failure nobody is waiting on an HTTP response for into the feed."""
        print(f"jarvis window: {sentence}", file=sys.stderr)
        try:
            con = self.services.open_db()
        except Exception:  # noqa: BLE001 - already said on stderr
            return
        try:
            publish(
                con, "window.error", "window", {"text": sentence}, redactor=self.services.redactor
            )
        except Exception:  # noqa: BLE001 - already said on stderr
            pass
        finally:
            con.close()


class WindowServer:
    """A running (or startable) window server. ``url`` is what the launcher opens."""

    def __init__(self, httpd: _Server) -> None:
        self._httpd = httpd
        self.port: int = httpd.port
        self.token: str = httpd.token
        self.url: str = f"http://127.0.0.1:{self.port}/#t={self.token}"
        self._thread: threading.Thread | None = None
        self._serving = False
        self._closed = False
        self._lock = threading.Lock()

    @property
    def active_streams(self) -> int:
        """Open ``/api/stream`` connections. Diagnostics, and how tests see a leak."""
        return self._httpd.active_streams

    def start(self) -> None:
        """Serve on a daemon thread and return at once."""
        with self._lock:
            if self._serving or self._closed:
                raise RuntimeError("this window server was already started")
            self._serving = True
            self._thread = threading.Thread(
                target=self._httpd.serve_forever,
                kwargs={"poll_interval": _POLL_S},
                name="window-http",
                daemon=True,
            )
            self._thread.start()

    def serve_forever(self) -> None:
        """Serve on THIS thread until :meth:`shutdown` is called from another one."""
        with self._lock:
            if self._serving or self._closed:
                raise RuntimeError("this window server was already started")
            self._serving = True
        self._httpd.serve_forever(poll_interval=_POLL_S)

    def shutdown(self) -> None:
        """Stop serving, end every open stream, release the port. Idempotent."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            serving = self._serving
        # Streams poll this every tick, so they end within one tick even though
        # their threads are daemons nobody joins.
        self._httpd.stopping.set()
        if serving:
            # Only when serve_forever was entered: TCPServer.shutdown() waits for
            # a loop that, if it never started, would never signal it is done.
            self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


def make_server(
    services: Services,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    token: str | None = None,
    static_dir: Path | None = None,
) -> WindowServer:
    """Bind the window's server on loopback. Call ``start()`` or ``serve_forever()``.

    ``static_dir`` exists for tests; the page is the package's own.
    """
    if host not in ("127.0.0.1", "localhost"):
        raise ValueError(
            f"the window only serves on 127.0.0.1, not {host!r}: it can stop builds "
            "and answer questions, and a LAN is not the user"
        )
    tok = token if token is not None else secrets.token_urlsafe(32)
    if not _TOKEN_SHAPE.fullmatch(tok):
        raise ValueError("the token must be at least 16 URL-safe characters")
    httpd = _Server(
        ("127.0.0.1", port),
        services=services,
        token=tok,
        static_dir=static_dir if static_dir is not None else STATIC_DIR,
    )
    return WindowServer(httpd)


# ───────────────────────────── the handler ─────────────────────────────


class _Handler(BaseHTTPRequestHandler):
    server: _Server
    server_version = "jarvis-window"
    sys_version = ""
    # HTTP/1.0: one request per connection, so a stream ends exactly when its
    # socket closes and no keep-alive bookkeeping can leave a reader hanging.
    protocol_version = "HTTP/1.0"
    # Reading a request line or a body; a stalled writer must not pin a thread.
    timeout = 30

    def setup(self) -> None:
        super().setup()
        self._responded = False
        self._body_read = False

    # -- the front door -------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's naming
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's naming
        self._handle("POST")

    def _handle(self, method: str) -> None:
        try:
            self._gate()
            parts = urlsplit(self.path)
            path = parts.path
            if method == "GET" and path in _STATIC:
                self._static(path)
                return
            if not path.startswith("/api/"):
                raise _Refusal(404, "There is nothing at that address.")
            query = parse_qs(parts.query)
            self._authorise(query if path == "/api/stream" else None)
            route = _ROUTES.get(path)
            if route is None:
                raise _Refusal(404, "There is no such API call.")
            want, run = route
            if method != want:
                raise _Refusal(405, f"{path} takes {want}.")
            run(self, query)
        except _Refusal as refusal:
            self._refuse(refusal.status, refusal.message)
        except _HANGUPS:
            return
        except Exception as exc:  # noqa: BLE001 - the page gets a sentence, stderr the traceback
            traceback.print_exc(file=sys.stderr)
            self._refuse(
                500,
                f"Something went wrong in the window server ({type(exc).__name__}); "
                "the details are in its log.",
            )

    def _gate(self) -> None:
        port = self.server.port
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            raise _Refusal(403, "This server only answers to its own address.")
        origin = self.headers.get("Origin")
        if origin is not None and origin.strip().lower() not in (
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
        ):
            raise _Refusal(403, "Requests from other sites are refused.")

    def _authorise(self, query: dict[str, list[str]] | None) -> None:
        supplied = self.headers.get("X-Jarvis-Token")
        if supplied is None and query is not None:
            # EventSource cannot set a header, so the stream alone takes ?t=.
            supplied = (query.get("t") or [None])[0]
        expected = self.server.token.encode("utf-8")
        if supplied is None or not hmac.compare_digest(supplied.encode("utf-8"), expected):
            raise _Refusal(401, "Open this from python -m jarvis window; the token is missing.")

    # -- responses ------------------------------------------------------------

    def _begin(self, status: int, content_type: str, length: int | None) -> None:
        self.send_response(status)
        for name, value in _SECURITY_HEADERS:
            self.send_header(name, value)
        self.send_header("Content-Type", content_type)
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.end_headers()
        self._responded = True

    def _json(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self._begin(status, "application/json; charset=utf-8", len(data))
        self.wfile.write(data)

    def _refuse(self, status: int, message: str) -> None:
        if self._responded:
            return  # headers are out; all that is left is to close
        if self.command == "POST" and not self._body_read:
            self._discard_body()
        with suppress(*_HANGUPS):
            self._json(status, {"ok": False, "error": message})

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        """The base class's own errors (bad request line, unknown method), as JSON.

        Overridden so that they too carry the security headers and never echo
        anything but a fixed phrase.
        """
        self.close_connection = True
        try:
            phrase = HTTPStatus(code).phrase
        except ValueError:
            phrase = "Error"
        data = json.dumps({"ok": False, "error": phrase}).encode("utf-8")
        self.send_response(code, phrase)
        for name, value in _SECURITY_HEADERS:
            self.send_header(name, value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD" and code >= 200 and code not in (204, 304):
            self.wfile.write(data)

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        # A HUD polls; a line per request would bury the one line that matters.
        status = getattr(code, "value", code)
        if isinstance(status, int) and status >= 400:
            super().log_request(code, size)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - base signature
        # The stream's ?t= is the one place the token is in a URL. It must not
        # reach a terminal scrollback that gets pasted into a bug report.
        line = _TOKEN_IN_LOG.sub(r"\1[redacted]", (format % args).translate(_LOG_ESCAPES))
        # None under a windowed exe that has not been given a log yet; a log
        # line is never worth the request it describes.
        if sys.stderr is not None:
            with suppress(Exception):
                sys.stderr.write(f"jarvis window: {self.address_string()} {line}\n")

    # -- request bodies -------------------------------------------------------

    def _body(self) -> dict[str, Any]:
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            raise _Refusal(415, "Send JSON, with Content-Type: application/json.")
        if self.headers.get("Transfer-Encoding"):
            raise _Refusal(411, "Send the body with a Content-Length.")
        n = self._length()
        if n is None:
            raise _Refusal(411, "Send the body with a Content-Length.")
        if n > MAX_BODY:
            raise _Refusal(
                413, f"That body is over {MAX_BODY // 1024} KiB; the page never sends one."
            )
        raw = self.rfile.read(n)
        self._body_read = True
        if len(raw) != n:
            raise _Refusal(400, "The body ended early.")
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise _Refusal(400, "The body is not valid JSON.") from exc
        if not isinstance(body, dict):
            raise _Refusal(400, "The body must be a JSON object.")
        return body

    def _length(self) -> int | None:
        raw = self.headers.get("Content-Length")
        if raw is None:
            return None
        try:
            n = int(raw)
        except ValueError as exc:
            raise _Refusal(400, "Content-Length is not a number.") from exc
        if n < 0:
            raise _Refusal(400, "Content-Length is negative.")
        return n

    def _discard_body(self) -> None:
        self._body_read = True
        try:
            left = min(self._length() or 0, _DRAIN_CAP)
        except _Refusal:
            return
        with suppress(OSError):
            while left > 0:
                chunk = self.rfile.read(min(left, 65536))
                if not chunk:
                    return
                left -= len(chunk)

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        con = self.server.services.open_db()
        try:
            yield con
        finally:
            con.close()

    # -- static ---------------------------------------------------------------

    def _static(self, path: str) -> None:
        name, ctype = _STATIC[path]
        try:
            data = (self.server.static_dir / name).read_bytes()
        except OSError as exc:
            raise _Refusal(500, f"The window's {name} is missing; reinstall Jarvis.") from exc
        self._begin(200, ctype, len(data))
        self.wfile.write(data)

    # -- GET ------------------------------------------------------------------

    def _api_state(self, query: dict[str, list[str]]) -> None:
        s = self.server.services
        now: dict[str, Any] = {}
        if s.readouts is not None:
            try:
                now = dict(s.readouts())
            except Exception:  # noqa: BLE001 - the launch-time values still render
                traceback.print_exc(file=sys.stderr)
        with self._db() as con:
            body = snapshot.state(
                con,
                tools=s.registry.declarations("cli"),
                wake_word=now.get("wake_word", s.wake_word),
                wake_threshold=now.get("wake_threshold", s.wake_threshold),
                spend_threshold_usd=s.spend_threshold_usd,
                tz=now.get("tz", s.tz),
                chat_available=s.chat is not None,
                chat_why=s.chat_why,
                speech_available=s.speak is not None,
                speech_why=s.speak_why,
                app=s.control is not None,
            )
        self._json(200, body)

    def _api_feed(self, query: dict[str, list[str]]) -> None:
        after = _int_param(query, "after")
        limit = _int_param(query, "limit")
        with self._db() as con:
            body = snapshot.feed(
                con,
                after=after,
                limit=snapshot.DEFAULT_FEED if limit is None else limit,
            )
        self._json(200, body)

    def _api_stream(self, query: dict[str, list[str]]) -> None:
        """Server-sent events until the client goes away or the server stops.

        Polls, deliberately: the bus's poke socket is a Unix datagram socket and
        Windows has none that this process could bind portably, while a 250 ms
        SELECT on an indexed integer costs nothing. Never opens a transaction,
        so a stream left open all day holds no lock any writer could wait on.
        """
        after = _int_param(query, "after")
        srv = self.server
        con = srv.services.open_db()
        srv.stream_delta(+1)
        try:
            self._begin(200, "text/event-stream; charset=utf-8", None)
            self._send(b"retry: 3000\n\n")
            top = last_seq(con)
            last = top if after is None else after
            prev_kind = snapshot.previous_kind(con, last) if last > 0 else None
            fp = snapshot.fingerprint(con)
            shown: tuple[Any, ...] | None = None
            next_ping = time.monotonic() + _PING_S
            while not srv.stopping.is_set() and not self._client_gone():
                refresh = False
                top = last_seq(con)
                if top < last:
                    # The database was replaced under us; start from its end.
                    last, prev_kind, refresh = top, snapshot.previous_kind(con, top), True
                elif top > last:
                    refresh = snapshot.changed_since(con, last, top)
                    items, last, prev_kind = snapshot.feed_after(
                        con, last, limit=snapshot.MAX_FEED, prev_kind=prev_kind, upto=top
                    )
                    if items:
                        self._event("feed", {"items": items, "last_seq": last})
                procs = snapshot.processes(con)
                signature = tuple(
                    (n, p["online"], p["state"], p["since"]) for n, p in procs.items()
                )
                if signature != shown:
                    self._event("state", {"processes": procs, "last_seq": last})
                    shown = signature
                now_fp = snapshot.fingerprint(con)
                if now_fp != fp:
                    fp, refresh = now_fp, True
                if refresh:
                    self._event("refresh", {})
                if time.monotonic() >= next_ping:
                    self._send(b": ping\n\n")
                    next_ping = time.monotonic() + _PING_S
                if last < top:
                    continue  # a backlog bigger than one batch: no sleep until caught up
                srv.stopping.wait(_POLL_S)
        except (*_HANGUPS, TimeoutError):
            pass
        finally:
            con.close()
            srv.stream_delta(-1)

    def _client_gone(self) -> bool:
        """True once the browser has closed the socket, without waiting for a write to fail.

        A closed socket is readable with nothing to read. Peeking finds that out
        in microseconds, where waiting for the next ping's write would hold a
        thread and a connection for up to fifteen seconds per closed tab.
        """
        try:
            ready, _, _ = select.select([self.connection], [], [], 0)
            if not ready:
                return False
            return self.connection.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True

    def _event(self, name: str, data: dict[str, Any]) -> None:
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        self._send(f"event: {name}\ndata: {payload}\n\n".encode())

    def _send(self, data: bytes) -> None:
        self.wfile.write(data)
        self.wfile.flush()

    # -- POST -----------------------------------------------------------------

    def _api_tool(self, query: dict[str, list[str]]) -> None:
        body = self._body()
        name = body.get("name")
        args = body.get("args") if body.get("args") is not None else {}
        if not isinstance(name, str) or not name.strip():
            raise _Refusal(400, 'Which tool? Send {"name": "...", "args": {...}}.')
        if not isinstance(args, dict):
            raise _Refusal(400, "args must be a JSON object.")
        s = self.server.services
        with self._db() as con:
            # channel "cli": the window is a screen with a keyboard, which is the
            # capability the cli column already describes. dispatch never raises;
            # a refusal comes back as the sentence to show.
            # A person pressed Run in the Tools tab: that press is the yes a
            # tool that needs one asks for (jarvis.tools.confirm).
            extra = {**dict(s.extra), CONFIRMATIONS: Confirmations(), DIRECT_HUMAN: True}
            ctx = ToolCtx(con=con, channel="cli", actor="window", extra=extra)
            said = s.registry.dispatch(name.strip(), args, ctx)
        self._json(200, {"ok": True, "said": said})

    def _api_chat(self, query: dict[str, list[str]]) -> None:
        body = self._body()
        s = self.server.services
        if s.chat is None:
            raise _Refusal(503, s.chat_why or "Chat isn't available in this window.")
        text = _required_text(body, "text")
        speak = body.get("speak") is True
        with self._db() as con, self.server.chat_lock:
            publish(con, "window.said", "window", {"text": text}, redactor=s.redactor)
            try:
                reply, tools = s.chat(text)
            except RuntimeError as exc:
                sentence = str(exc) or "The chat failed."
                publish(con, "window.error", "window", {"text": sentence}, redactor=s.redactor)
                raise _Refusal(502, sentence) from exc
            pairs = [[str(name), str(said)] for name, said in tools]
            publish(
                con,
                "window.reply",
                "window",
                {"text": reply, "tools": pairs},
                redactor=s.redactor,
            )
        self._json(200, {"ok": True, "reply": reply, "tools": pairs})
        if speak and s.speak is not None and reply.strip():
            # After the response is written: a reply is read on screen at once,
            # and the voice catches up without holding the request open.
            self.server.speak_later(reply)

    def _api_say(self, query: dict[str, list[str]]) -> None:
        body = self._body()
        s = self.server.services
        if s.speak is None:
            raise _Refusal(503, s.speak_why or "There is no voice in this window.")
        text = _required_text(body, "text")
        with self._db() as con:
            via_desk = snapshot.desk_online(con)
        if via_desk:
            # Writing a command row is quick; the desk's reader does the talking.
            try:
                via = s.speak(text)
            except RuntimeError as exc:
                raise _Refusal(500, str(exc) or "The desk could not be asked to say that.") from exc
            self._json(200, {"ok": True, "via": via})
            return
        self.server.speak_later(text)
        self._json(200, {"ok": True, "via": "local"})

    def _api_answer(self, query: dict[str, list[str]]) -> None:
        body = self._body()
        rid = body.get("request_id")
        if not isinstance(rid, str) or not rid:
            raise _Refusal(400, "Which question? request_id is missing.")
        picks = body.get("picks") if body.get("picks") is not None else []
        if not isinstance(picks, list) or not all(
            isinstance(p, int) and not isinstance(p, bool) for p in picks
        ):
            raise _Refusal(400, "picks are option numbers.")
        text = body.get("text")
        if text is not None and not isinstance(text, str):
            raise _Refusal(400, "text is your own words, as a string.")
        free = text if text and text.strip() else None
        with self._db() as con:
            req = rq.get_request(con, rid)
            if req is None:
                raise _Refusal(404, "There is no question with that id.")
            if req.state != "pending":
                self._json(200, {"ok": True, "answered": False, "message": _late(req.state)})
                return
            try:
                answer = build_answer(req, picks=tuple(picks), free_text=free)
            except (ValueError, LookupError, TypeError) as exc:
                # Every shape error here is written to be read by the person who
                # picked: "option 4 does not exist; there are 3".
                raise _Refusal(400, str(exc)) from exc
            # 'hud': a typed answer at a screen. The vocabulary already has the
            # word, and a sixth one for the same fact would split every query.
            won = rq.answer_request(con, req.id, answer, answered_by="window", answer_mode="hud")
            if not won:
                self._json(200, {"ok": True, "answered": False, "message": "already answered"})
                return
            publish(
                con,
                "request.answered",
                "window",
                {"channel": "window", "picks": list(picks), "free_text": free is not None},
                job_id=req.job_id,
                request_id=req.id,
                idem_key=f"req:{req.id}:answered",
            )
        self._json(200, {"ok": True, "answered": True, "message": f"answered: {req.short_label}"})

    def _api_stop(self, query: dict[str, list[str]]) -> None:
        self._body()
        with self._db() as con:
            cid = kill.stop_everything(con, "window", "the STOP button in the window")
        self._json(
            200,
            {
                "ok": True,
                "message": "Stop sent. Every build and call has been told to stop.",
                "command_id": cid,
            },
        )

    # -- the app: settings, onboarding, processes ------------------------------

    def _setup(self) -> Any:
        setup = self.server.services.setup
        if setup is None:
            raise _Refusal(
                503, "Settings live in the Jarvis app; this window was started without it."
            )
        return setup

    def _control(self) -> Any:
        control = self.server.services.control
        if control is None:
            raise _Refusal(503, "This window is not running inside the Jarvis app.")
        return control

    def _app_processes(self) -> dict[str, dict[str, Any]] | None:
        """The supervisor's view, or None outside the app or when it cannot say."""
        control = self.server.services.control
        if control is None:
            return None
        try:
            return _processes(control.status())
        except Exception:  # noqa: BLE001 - problems still render without it
            traceback.print_exc(file=sys.stderr)
            return None

    def _api_setup(self, query: dict[str, list[str]]) -> None:
        setup = self._setup()
        procs = self._app_processes()
        # The supervisor's view is handed over so a held process is a problem
        # card even when the app built the service without a control.
        # ?rescan=1 asks for a fresh look at the audio hardware; the page
        # sends it only when the user asks, since it costs PortAudio a restart.
        rescan = query.get("rescan", [""])[0] == "1"
        self._json(200, _ask(lambda: setup.status(processes=procs, rescan=rescan)))

    def _api_setup_secret(self, query: dict[str, list[str]]) -> None:
        """Store a credential. The value goes to the keyring and nowhere else.

        Not in the reply, not in an error message, not in a traceback: every
        exception is caught here and turned into a sentence with the value
        scrubbed out, because a keyring backend is free to quote what it was
        given and the default handler would print it to the log.
        """
        setup = self._setup()
        body = self._body()
        name = body.get("name")
        value = body.get("value")
        if not isinstance(name, str) or not name:
            raise _Refusal(400, 'Which key? Send {"name": "...", "value": "..."}.')
        if not isinstance(value, str) or not value:
            raise _Refusal(400, "The key is empty.")
        if len(value.encode("utf-8")) > MAX_SECRET:
            raise _Refusal(400, f"That is longer than any key (over {MAX_SECRET // 1024} KiB).")
        if any(ch.isspace() for ch in value):
            raise _Refusal(
                400, "A key has no spaces or line breaks in it; check that only the key was pasted."
            )
        restart = body.get("restart") is not False
        try:
            reply = setup.set_secret(name, value, restart=restart)
        except ValueError as exc:
            raise _Refusal(400, _scrub(_sentence(exc, "That key was refused."), value)) from None
        except RuntimeError as exc:
            raise _Refusal(
                502, _scrub(_sentence(exc, "I couldn't store that key."), value)
            ) from None
        except Exception as exc:  # noqa: BLE001 - the type alone; the message may hold the value
            print(f"jarvis window: storing a key failed ({type(exc).__name__})", file=sys.stderr)
            raise _Refusal(500, "I couldn't store that key.") from None
        restarted = reply.get("restarted", []) if isinstance(reply, dict) else []
        self._json(200, {"ok": True, "present": True, "restarted": list(restarted)})

    def _api_setup_setting(self, query: dict[str, list[str]]) -> None:
        setup = self._setup()
        body = self._body()
        key = body.get("key")
        if not isinstance(key, str) or not key:
            raise _Refusal(400, 'Which setting? Send {"key": "...", "value": ...}.')
        if "value" not in body:
            raise _Refusal(400, f"What should {key} be? value is missing.")
        restart = body.get("restart") is not False
        reply = _ask(lambda: setup.set_setting(key, body["value"], restart=restart))
        self._json(200, {**(reply if isinstance(reply, dict) else {}), "ok": True})

    def _api_setup_wake(self, query: dict[str, list[str]]) -> None:
        setup = self._setup()
        body = self._body()
        restart = body.get("restart") is not False
        reply = _ask(lambda: setup.download_wake(restart=restart))
        self._json(200, {**(reply if isinstance(reply, dict) else {}), "ok": True})

    def _api_setup_preview(self, query: dict[str, list[str]]) -> None:
        setup = self._setup()
        body = self._body()
        voice = body.get("voice")
        if not isinstance(voice, str) or not voice.strip():
            raise _Refusal(400, 'Which voice? Send {"voice": "Charon"}.')
        # The service plays on a thread of its own and returns at once.
        reply = _ask(lambda: setup.preview(voice.strip()))
        self._json(200, {**(reply if isinstance(reply, dict) else {}), "ok": True})

    def _api_setup_phone(self, query: dict[str, list[str]]) -> None:
        """Pair or unpair the Telegram chat. The pairing code is in this reply and nowhere else."""
        setup = self._setup()
        body = self._body()
        action = body.get("action")
        if action == "pair":
            reply = _ask(setup.pair_phone)
        elif action == "unpair":
            reply = _ask(setup.unpair_phone)
        else:
            raise _Refusal(400, 'Send {"action": "pair"} or {"action": "unpair"}.')
        self._json(200, {**(reply if isinstance(reply, dict) else {}), "ok": True})

    def _api_setup_claude(self, query: dict[str, list[str]]) -> None:
        setup = self._setup()
        self._body()
        reply = _ask(setup.sign_in_claude)
        self._json(200, {**(reply if isinstance(reply, dict) else {}), "ok": True})

    def _api_app(self, query: dict[str, list[str]]) -> None:
        control = self._control()
        status = _ask(control.status)
        body: dict[str, Any] = dict(status) if isinstance(status, dict) else {}
        body.update(ok=True, app=True, processes=_processes(status))
        self._json(200, body)

    def _api_app_restart(self, query: dict[str, list[str]]) -> None:
        control = self._control()
        body = self._body()
        name = body.get("process")
        if not isinstance(name, str) or not name:
            raise _Refusal(400, 'Which process? Send {"process": "desk"}.')
        _ask(lambda: control.restart(name))
        self._json(200, {"ok": True, "message": f"Restarting {_PROCESS_WORDS.get(name, name)}."})

    def _api_app_quit(self, query: dict[str, list[str]]) -> None:
        control = self._control()
        self._body()
        self._json(200, {"ok": True, "message": "Shutting down. Good night."})
        self.wfile.flush()
        # After the reply is on the wire: quitting tears down this very server,
        # and a reply cut off mid-write would read as the quit having failed.
        threading.Thread(
            target=self.server.quit_app, args=(control,), name="window-quit", daemon=True
        ).start()


_Route = Callable[[_Handler, dict[str, list[str]]], None]

_ROUTES: dict[str, tuple[str, _Route]] = {
    "/api/state": ("GET", _Handler._api_state),
    "/api/feed": ("GET", _Handler._api_feed),
    "/api/stream": ("GET", _Handler._api_stream),
    "/api/tool": ("POST", _Handler._api_tool),
    "/api/chat": ("POST", _Handler._api_chat),
    "/api/say": ("POST", _Handler._api_say),
    "/api/answer": ("POST", _Handler._api_answer),
    "/api/stop": ("POST", _Handler._api_stop),
    "/api/setup": ("GET", _Handler._api_setup),
    "/api/setup/secret": ("POST", _Handler._api_setup_secret),
    "/api/setup/setting": ("POST", _Handler._api_setup_setting),
    "/api/setup/wake": ("POST", _Handler._api_setup_wake),
    "/api/setup/preview": ("POST", _Handler._api_setup_preview),
    "/api/setup/claude": ("POST", _Handler._api_setup_claude),
    "/api/setup/phone": ("POST", _Handler._api_setup_phone),
    "/api/app": ("GET", _Handler._api_app),
    "/api/app/restart": ("POST", _Handler._api_app_restart),
    "/api/app/quit": ("POST", _Handler._api_app_quit),
}

#: The supervisor's names, as a sentence says them.
_PROCESS_WORDS: dict[str, str] = {
    "desk": "the desk",
    "schedule": "the scheduler",
    "telegram": "Telegram",
}


def _int_param(query: dict[str, list[str]], name: str) -> int | None:
    values = query.get(name)
    if not values or values[0] == "":
        return None
    try:
        n = int(values[0])
    except ValueError as exc:
        raise _Refusal(400, f"{name} must be a whole number.") from exc
    if n < 0:
        raise _Refusal(400, f"{name} cannot be negative.")
    return n


def _required_text(body: dict[str, Any], key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _Refusal(400, f"{key} is empty.")
    return value.strip()


def _late(state: str) -> str:
    return "already answered" if state in ("answered", "consumed") else f"it was {state}"


def _ask(fn: Callable[[], Any]) -> Any:
    """Call into the app. Its ValueError is a bad request, its RuntimeError a failure.

    Both carry a sentence written for the person at the window; anything else
    is a bug and goes to the default handler (a generic 500, a traceback in
    the log).
    """
    try:
        return fn()
    except _Refusal:
        raise
    except (ValueError, LookupError) as exc:
        raise _Refusal(400, _sentence(exc, "That was refused.")) from exc
    except RuntimeError as exc:
        raise _Refusal(502, _sentence(exc, "That didn't work.")) from exc


def _sentence(exc: BaseException, fallback: str) -> str:
    # str(KeyError("x")) is "'x'", quotes and all; its argument is the sentence.
    raw = exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc)
    return snapshot.plain_sentence(str(raw), fallback)


def _scrub(sentence: str, value: str) -> str:
    return sentence.replace(value, "[hidden]") if value else sentence


def _processes(status: Any) -> dict[str, dict[str, Any]]:
    """The supervisor's status as the page reads it, reasons command-free.

    Accepts ``{name: {...}}`` (the supervisor's own shape) or the same under a
    ``processes`` key, so the page does not care which the app hands over.
    """
    if not isinstance(status, dict):
        return {}
    procs = status.get("processes") if isinstance(status.get("processes"), dict) else status
    out: dict[str, dict[str, Any]] = {}
    for name, p in procs.items():
        if not isinstance(name, str) or not isinstance(p, dict):
            continue
        pid, restarts, last_exit = p.get("pid"), p.get("restarts"), p.get("last_exit")
        out[name] = {
            "running": p.get("running") is True,
            "held": p.get("held") is True,
            "pid": pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
            "restarts": restarts if isinstance(restarts, int) else 0,
            "last_exit": last_exit if isinstance(last_exit, int) else None,
            "reason": snapshot.plain_sentence(str(p.get("reason") or "")),
            "log": str(p.get("log") or ""),
        }
    return out
