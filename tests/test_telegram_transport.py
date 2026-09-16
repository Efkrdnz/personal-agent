"""The Bot API seam, tested where it lies about what happened.

api.telegram.org is unreachable from this machine and from CI by design, so
nothing here opens a socket. What IS tested is every place the Bot API's shape
differs from what a naive HTTP client would assume: an ``ok: false`` body inside
an HTTP 200, a JSON explanation inside an HTTP 4xx, and a 429 whose
``retry_after`` is an instruction rather than a suggestion.
"""

from __future__ import annotations

import io
import json
from typing import Any

import pytest

from jarvis.telegram.transport import (
    FakeTransport,
    HttpTransport,
    ScriptExhausted,
    TelegramError,
    TransportError,
    Upload,
)

TOKEN = "123456:THIS-IS-NOT-A-REAL-TOKEN"


class _Response(io.BytesIO):
    status = 200

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class _Opener:
    """Stands in for ``urllib.request.OpenerDirector``. Never touches a network."""

    def __init__(self, bodies: list[Any]) -> None:
        self.bodies = bodies
        self.requests: list[Any] = []

    def open(self, req: Any, timeout: float | None = None) -> _Response:
        self.requests.append(req)
        body = self.bodies.pop(0)
        if isinstance(body, Exception):
            raise body
        return _Response(json.dumps(body).encode("utf-8"))


def _ok(result: Any) -> dict[str, Any]:
    return {"ok": True, "result": result}


def _err(code: int, description: str, **parameters: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"ok": False, "error_code": code, "description": description}
    if parameters:
        body["parameters"] = parameters
    return body


def _transport(bodies: list[Any], slept: list[float]) -> HttpTransport:
    return HttpTransport(TOKEN, opener=_Opener(bodies), sleep=slept.append)


def test_ok_false_inside_http_200_is_an_error_not_a_result() -> None:
    slept: list[float] = []
    t = _transport([_err(400, "chat not found")], slept)
    with pytest.raises(TelegramError) as caught:
        t.call("sendMessage", {"chat_id": 1})
    assert caught.value.error_code == 400
    assert "chat not found" in str(caught.value)


def test_429_is_retried_after_exactly_the_seconds_the_server_named() -> None:
    slept: list[float] = []
    t = _transport([_err(429, "Too Many Requests", retry_after=7), _ok({"message_id": 9})], slept)
    assert t.call("sendMessage", {"chat_id": 1}) == {"message_id": 9}
    # Not a backoff of our own invention: the server said seven, we waited seven.
    assert slept == [7.0]


def test_a_429_asking_for_longer_than_we_will_wait_is_raised_not_slept_off() -> None:
    slept: list[float] = []
    t = _transport([_err(429, "Too Many Requests", retry_after=3600)], slept)
    with pytest.raises(TelegramError):
        t.call("sendMessage", {"chat_id": 1})
    assert slept == []


def test_a_refusal_that_is_not_429_is_never_retried() -> None:
    slept: list[float] = []
    opener = _Opener([_err(403, "bot was blocked by the user")])
    t = HttpTransport(TOKEN, opener=opener, sleep=slept.append)
    with pytest.raises(TelegramError):
        t.call("sendMessage", {"chat_id": 1})
    assert len(opener.requests) == 1


def test_the_token_never_appears_in_an_error_message() -> None:
    """The token is in the URL PATH, so the obvious error message leaks it."""
    slept: list[float] = []
    t = _transport([OSError("connection reset"), OSError("again"), OSError("and again")], slept)
    with pytest.raises(TransportError) as caught:
        t.call("getMe")
    assert TOKEN not in str(caught.value)
    assert TOKEN not in repr(caught.value.__cause__)


def test_a_json_body_inside_an_http_error_is_read_rather_than_discarded() -> None:
    import urllib.error

    slept: list[float] = []
    failure = urllib.error.HTTPError(
        "https://example.invalid",
        400,
        "Bad Request",
        {},  # type: ignore[arg-type]
        io.BytesIO(json.dumps(_err(400, "message is not modified")).encode()),
    )
    t = _transport([failure], slept)
    with pytest.raises(TelegramError) as caught:
        t.call("editMessageText", {"chat_id": 1})
    assert "message is not modified" in str(caught.value)


def test_an_upload_is_never_retried_blind() -> None:
    """The Bot API has no idempotency key: a retried upload sends it twice."""
    slept: list[float] = []
    opener = _Opener([OSError("reset after the server accepted it")])
    t = HttpTransport(TOKEN, opener=opener, sleep=slept.append)
    with pytest.raises(TransportError):
        t.upload("sendVoice", {"chat_id": 1}, Upload("voice", "a.ogg", b"OggS", "audio/ogg"))
    assert len(opener.requests) == 1


def test_multipart_carries_the_bytes_and_the_params() -> None:
    slept: list[float] = []
    opener = _Opener([_ok({"message_id": 4})])
    t = HttpTransport(TOKEN, opener=opener, sleep=slept.append)
    t.upload("sendVoice", {"chat_id": 7, "caption": "hi"}, Upload("voice", "a.ogg", b"OggS\x00"))
    body = opener.requests[0].data
    assert b"OggS\x00" in body
    assert b'name="voice"; filename="a.ogg"' in body
    assert b"7" in body and b"hi" in body


# ───────────────────────────── the fake ─────────────────────────────


def test_the_fake_honours_the_offset_it_was_given() -> None:
    """A poll loop that forgets its cursor must reproduce the bug here too."""
    t = FakeTransport(updates=[[{"update_id": 3}, {"update_id": 4}]])
    assert t.call("getUpdates", {"offset": 4}) == [{"update_id": 4}]


def test_the_fake_runs_out_loudly_rather_than_blocking_forever() -> None:
    t = FakeTransport(updates=[])
    with pytest.raises(ScriptExhausted):
        t.call("getUpdates", {"offset": 1})


def test_the_fake_records_every_call_in_order() -> None:
    t = FakeTransport()
    t.call("sendMessage", {"chat_id": 1, "text": "one"})
    t.call("sendMessage", {"chat_id": 1, "text": "two"})
    assert [c.params["text"] for c in t.sent("sendMessage")] == ["one", "two"]
    assert t.last("sendMessage") is not None
