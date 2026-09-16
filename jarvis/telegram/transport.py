"""The Bot API as a seam: one protocol, an HTTP client, and a fake.

Two facts shape this module and neither is negotiable.

*The network is not available where this is tested.* ``api.telegram.org`` is
blocked by the egress proxy on the build machine and in CI, so a test that talks
to it is a test that runs nowhere. :class:`FakeTransport` is therefore not a
convenience for fast tests; it is the only way any of this is testable at all,
and every other module in the package takes a :class:`Transport` rather than a
token so that it cannot accidentally grow a live dependency.

*There is no token and there will not be one.* Nothing here reads a credential
at import time. :class:`HttpTransport` is handed one by whoever built it, and
the token is never interpolated into an error message — a Bot API URL embeds the
token in its PATH, so the obvious ``raise RuntimeError(url)`` publishes the
credential into the activity log, a traceback and the operator's terminal at
once.

The Bot API's error shape is its own protocol: HTTP 200 with ``{"ok": false}``
is as common as a 4xx, and a 429 carries ``parameters.retry_after`` in seconds
which is an instruction rather than a hint. Both are handled here so that no
caller has to know the difference between a transport failure and a refusal.
"""

from __future__ import annotations

import json
import secrets
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from time import sleep as _sleep
from typing import Any, Protocol

__all__ = [
    "API_ROOT",
    "DEFAULT_ATTEMPTS",
    "DEFAULT_TIMEOUT_S",
    "MAX_RETRY_AFTER_S",
    "Call",
    "FakeTransport",
    "HttpTransport",
    "ScriptExhausted",
    "TelegramError",
    "Transport",
    "TransportError",
    "Upload",
]

API_ROOT = "https://api.telegram.org"

#: Long polling holds the connection open for ``getUpdates``' own timeout, so the
#: socket timeout has to be the poll timeout plus slack rather than a constant.
DEFAULT_TIMEOUT_S = 15.0

#: A 429 may ask for a long wait. Honour it up to here and then give up, because
#: a channel that sleeps for an hour inside one call is indistinguishable from a
#: channel that has died.
MAX_RETRY_AFTER_S = 60.0

DEFAULT_ATTEMPTS = 3


class TransportError(RuntimeError):
    """The request never produced a Bot API answer: DNS, TLS, timeout, reset."""


class TelegramError(RuntimeError):
    """The Bot API answered, and the answer was ``ok: false``.

    Its own type because the caller's response differs: a transport failure is
    retried by the poll loop, whereas ``400 chat not found`` is a fact about the
    world that retrying cannot change.
    """

    def __init__(
        self,
        method: str,
        error_code: int,
        description: str,
        parameters: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(f"{method}: {error_code} {description}")
        self.method = method
        self.error_code = error_code
        self.description = description
        self.parameters: dict[str, Any] = dict(parameters or {})

    @property
    def retry_after(self) -> float | None:
        """Seconds the server told us to wait, or None if it did not."""
        value = self.parameters.get("retry_after")
        return float(value) if isinstance(value, (int, float)) else None


class ScriptExhausted(RuntimeError):
    """A :class:`FakeTransport` ran out of scripted updates.

    Raised rather than blocking forever, so a test can drive the real polling
    loop to completion instead of reimplementing it.
    """


@dataclass(frozen=True, slots=True)
class Upload:
    """One multipart file part: the only non-JSON thing this channel sends."""

    field: str
    filename: str
    content: bytes
    content_type: str = "application/octet-stream"


@dataclass(frozen=True, slots=True)
class Call:
    """One recorded outbound call. The fake's entire assertion surface."""

    method: str
    params: dict[str, Any]
    upload: Upload | None = None


class Transport(Protocol):
    """Everything this channel needs from Telegram, and nothing else.

    Three methods rather than one because the Bot API genuinely has three
    encodings — JSON for calls, multipart for uploads, and a plain GET on a
    different URL prefix for downloads — and collapsing them would put the
    encoding choice inside every caller.
    """

    def call(
        self, method: str, params: Mapping[str, Any] | None = None, *, timeout: float | None = None
    ) -> Any:
        """Invoke a Bot API method and return its ``result``."""

    def upload(
        self,
        method: str,
        params: Mapping[str, Any],
        upload: Upload,
        *,
        timeout: float | None = None,
    ) -> Any:
        """Invoke a Bot API method with one file part, returning its ``result``."""

    def download(self, file_path: str, *, timeout: float | None = None) -> bytes:
        """Fetch a file by the ``file_path`` that ``getFile`` handed back."""


# ───────────────────────────── the real one ─────────────────────────────


@dataclass(slots=True)
class HttpTransport:
    """``urllib.request`` against the Bot API. No third-party dependency.

    ``sleep`` and ``opener`` are injected so the retry ladder is testable without
    a network and without a test that really waits five seconds. They are
    constructor arguments rather than module globals because two bots (a real one
    and a probe) must be able to exist in one process without sharing state.
    """

    token: str
    api_root: str = API_ROOT
    attempts: int = DEFAULT_ATTEMPTS
    timeout_s: float = DEFAULT_TIMEOUT_S
    sleep: Callable[[float], None] = _sleep
    opener: Any = None

    def __post_init__(self) -> None:
        if not self.token:
            raise ValueError("HttpTransport needs a bot token; FakeTransport needs none")
        if self.attempts < 1:
            raise ValueError("attempts is 1-based")
        if self.opener is None:
            self.opener = urllib.request.build_opener()

    def call(
        self, method: str, params: Mapping[str, Any] | None = None, *, timeout: float | None = None
    ) -> Any:
        body = json.dumps(dict(params or {}), ensure_ascii=False).encode("utf-8")
        return self._invoke(method, body, "application/json", timeout)

    def upload(
        self,
        method: str,
        params: Mapping[str, Any],
        upload: Upload,
        *,
        timeout: float | None = None,
    ) -> Any:
        boundary = f"----jarvis{secrets.token_hex(12)}"
        body = _multipart(params, upload, boundary)
        return self._invoke(
            method, body, f"multipart/form-data; boundary={boundary}", timeout, is_upload=True
        )

    def download(self, file_path: str, *, timeout: float | None = None) -> bytes:
        url = f"{self.api_root}/file/bot{self.token}/{file_path.lstrip('/')}"
        raw, _ = self._open(urllib.request.Request(url), timeout or self.timeout_s, "getFile")
        return raw

    # The token lives in the URL path, so every error message in here names the
    # METHOD and never the URL. One f-string with the url in it would put a live
    # credential into the activity log and the operator's scrollback at once.
    def _invoke(
        self,
        method: str,
        body: bytes,
        content_type: str,
        timeout: float | None,
        *,
        is_upload: bool = False,
    ) -> Any:
        url = f"{self.api_root}/bot{self.token}/{method}"
        budget = timeout if timeout is not None else self.timeout_s
        last: Exception | None = None
        for attempt in range(1, self.attempts + 1):
            req = urllib.request.Request(url, data=body, method="POST")
            req.add_header("Content-Type", content_type)
            try:
                raw, _ = self._open(req, budget, method)
                return _result(method, raw)
            except TelegramError as e:
                wait = e.retry_after
                # 429 is the only refusal worth repeating: the server has told us
                # exactly how long to wait, and not waiting is how a bot gets its
                # limits tightened. Every other ok:false is a fact, not a delay.
                if wait is None or attempt == self.attempts or wait > MAX_RETRY_AFTER_S:
                    raise
                last = e
                self.sleep(wait)
            except TransportError as e:
                if attempt == self.attempts or is_upload:
                    # An upload is not retried blind: the Bot API has no
                    # idempotency key, and a reset AFTER the server accepted the
                    # body sends the voice note twice.
                    raise
                last = e
                self.sleep(min(2.0 ** (attempt - 1), MAX_RETRY_AFTER_S))
        raise TransportError(f"{method}: gave up after {self.attempts} attempts") from last

    def _open(self, req: urllib.request.Request, timeout: float, method: str) -> tuple[bytes, int]:
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                return resp.read(), int(getattr(resp, "status", 200) or 200)
        except urllib.error.HTTPError as e:
            # Telegram answers 4xx with a JSON body that explains itself, and
            # throwing that body away turns "chat not found" into "HTTP 400".
            raw = e.read()
            if raw:
                _result(method, raw)  # raises TelegramError with the real reason
            raise TransportError(f"{method}: HTTP {e.code}") from e
        except (urllib.error.URLError, OSError) as e:
            raise TransportError(f"{method}: {type(e).__name__}") from e


def _result(method: str, raw: bytes) -> Any:
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise TransportError(f"{method}: response was not JSON") from e
    if not isinstance(body, dict):
        raise TransportError(f"{method}: response was not an object")
    if body.get("ok"):
        return body.get("result")
    raise TelegramError(
        method,
        int(body.get("error_code") or 0),
        str(body.get("description") or "no description"),
        body.get("parameters") if isinstance(body.get("parameters"), dict) else None,
    )


def _multipart(params: Mapping[str, Any], upload: Upload, boundary: str) -> bytes:
    out = bytearray()
    for key, value in params.items():
        if value is None:
            continue
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        out += f"--{boundary}\r\n".encode()
        out += f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode()
        out += text.encode("utf-8") + b"\r\n"
    out += f"--{boundary}\r\n".encode()
    out += (
        f'Content-Disposition: form-data; name="{upload.field}"; filename="{upload.filename}"\r\n'
    ).encode()
    out += f"Content-Type: {upload.content_type}\r\n\r\n".encode()
    out += upload.content + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out)


# ───────────────────────────── the fake ─────────────────────────────


@dataclass(slots=True)
class FakeTransport:
    """Records what was sent, replays what was scripted. Every test runs on this.

    ``updates`` is a list of BATCHES, because that is the shape ``getUpdates``
    returns and because the offset cursor is only interesting across more than
    one round trip. Each batch is filtered by the offset the caller passed, so a
    poll loop that forgets its cursor replays updates here exactly as it would
    against the real server — which is the bug the cursor exists to prevent and
    therefore the one worth being able to reproduce.
    """

    updates: list[list[dict[str, Any]]] = field(default_factory=list)
    results: dict[str, list[Any]] = field(default_factory=dict)
    errors: dict[str, list[Exception]] = field(default_factory=dict)
    files: dict[str, bytes] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)
    downloads: list[str] = field(default_factory=list)
    exhausted_raises: bool = True
    next_message_id: int = 1000

    def call(
        self, method: str, params: Mapping[str, Any] | None = None, *, timeout: float | None = None
    ) -> Any:
        p = dict(params or {})
        self.calls.append(Call(method, p))
        self._maybe_raise(method)
        if method == "getUpdates":
            return self._updates(p)
        queued = self.results.get(method)
        if queued:
            return queued.pop(0)
        return self._default(method, p)

    def upload(
        self,
        method: str,
        params: Mapping[str, Any],
        upload: Upload,
        *,
        timeout: float | None = None,
    ) -> Any:
        self.calls.append(Call(method, dict(params), upload))
        self._maybe_raise(method)
        queued = self.results.get(method)
        if queued:
            return queued.pop(0)
        return self._default(method, dict(params))

    def download(self, file_path: str, *, timeout: float | None = None) -> bytes:
        self.downloads.append(file_path)
        if file_path not in self.files:
            raise TelegramError("download", 400, f"no such file: {file_path}")
        return self.files[file_path]

    # -- what a test asks it afterwards ------------------------------------

    def sent(self, method: str) -> list[Call]:
        return [c for c in self.calls if c.method == method]

    def last(self, method: str) -> Call | None:
        found = self.sent(method)
        return found[-1] if found else None

    # -- internals ---------------------------------------------------------

    def _maybe_raise(self, method: str) -> None:
        queued = self.errors.get(method)
        if queued:
            raise queued.pop(0)

    def _updates(self, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        offset = int(params.get("offset") or 0)
        while self.updates:
            batch = [u for u in self.updates.pop(0) if int(u["update_id"]) >= offset]
            if batch:
                return batch
        if self.exhausted_raises:
            raise ScriptExhausted("no scripted updates left")
        return []

    def _default(self, method: str, params: Mapping[str, Any]) -> Any:
        if method in ("sendMessage", "sendVoice", "sendDocument", "sendPhoto"):
            self.next_message_id += 1
            return {
                "message_id": self.next_message_id,
                "chat": {"id": params.get("chat_id")},
                "text": params.get("text"),
            }
        if method == "getFile":
            file_id = str(params.get("file_id") or "unknown")
            return {"file_id": file_id, "file_path": f"voice/{file_id}.oga"}
        if method == "getMe":
            return {"id": 1, "is_bot": True, "username": "fake_bot"}
        return True
