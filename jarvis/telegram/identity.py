"""Who may act on this channel. THIS IS THE AUTHENTICATION, so treat it as such.

A bot username is public. Anyone who finds it can message it, which makes the
two obvious designs both wrong:

*"First person to message the bot wins"* is a race against strangers, and the
prize is a shell on someone's desktop. It is lost the first time the username
appears in a screenshot.

*"Type the PIN into the chat"* moves the secret into the channel it is meant to
protect: it is now in Telegram's servers, in the chat history, and in the
notification preview on a lock screen — and a wrong guess costs an attacker
nothing.

So binding is ONE-TIME, OUT OF BAND and OPERATOR-INITIATED. The operator runs
``python -m jarvis.telegram --bind`` at the machine, reads a code off the
terminal, and sends it to the bot within ten minutes. The code exists only in
that terminal and in this process's memory for the length of one function call;
the database holds a salted hash. Three wrong guesses destroy it.

After that, exactly one chat id is bound and every update from anywhere else is
dropped and RECORDED. The recording is the point: an unexplained silent drop is
indistinguishable from a bug, and a stranger probing the bot is something the
operator should be able to read in the morning.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import timedelta

from jarvis.bus import publish
from jarvis.db import tx
from jarvis.ids import now, parse_ts

__all__ = [
    "BIND_ROW",
    "BOUND_ROW",
    "CODE_ALPHABET",
    "CODE_LENGTH",
    "DEFAULT_ATTEMPTS",
    "DEFAULT_TTL_S",
    "BindOutcome",
    "Binding",
    "PendingBind",
    "authorised",
    "bound_chat",
    "drop_update",
    "new_code",
    "offer_code",
    "pending_bind",
    "read_binding",
    "redeem",
    "unbind",
]

#: Both live in the generic ``cursors`` KV, which the schema comment already
#: describes as shared. No migration is added for this; migrations are frozen.
BIND_ROW = "telegram:bind_offer"
BOUND_ROW = "telegram:bound_chat"

#: No 0/O/1/I/L: the operator reads this off a terminal and types it on a phone,
#: and a transcription error costs one of three attempts.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8

DEFAULT_TTL_S = 600
DEFAULT_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class PendingBind:
    """An outstanding offer, WITHOUT the code. The plaintext is never stored."""

    expires_at: str
    attempts_left: int
    offered_by: str
    offered_at: str


@dataclass(frozen=True, slots=True)
class Binding:
    chat_id: int
    bound_at: str
    username: str | None = None


@dataclass(frozen=True, slots=True)
class BindOutcome:
    """What happened, in a shape the caller can turn into one sentence."""

    ok: bool
    reason: str
    attempts_left: int = 0
    chat_id: int | None = None


def new_code() -> str:
    """A fresh one-time code. Uses ``secrets``, never ``random``."""
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def normalise(code: str) -> str:
    """Upper-case, with spaces and dashes removed. Typing aid, not a secret op."""
    return "".join(ch for ch in code.upper() if ch.isalnum())


def offer_code(
    con: sqlite3.Connection,
    *,
    by: str,
    ttl_s: int = DEFAULT_TTL_S,
    attempts: int = DEFAULT_ATTEMPTS,
    code: str | None = None,
    now_ts: str | None = None,
) -> str:
    """Create a one-time binding code and return the PLAINTEXT, exactly once.

    The plaintext is returned and never written. What is stored is a salted
    SHA-256; the salt does not make an eight-character code unguessable offline,
    and it is not meant to — the real controls are the ten-minute expiry and the
    three attempts. The hash is there so that reading the database does not hand
    someone a code that is still live.

    Offering again REPLACES any outstanding offer, so an operator who lost the
    terminal scrollback is not locked out by their own earlier attempt.
    """
    if not by:
        raise ValueError("by must name who asked for the code")
    if attempts < 1:
        raise ValueError("a code with no attempts cannot be redeemed")
    plain = normalise(code) if code else new_code()
    if not plain:
        raise ValueError("an empty code is not a code")
    ts = now_ts or now()
    salt = secrets.token_hex(16)
    row = {
        "salt": salt,
        "digest": _digest(salt, plain),
        "expires_at": _plus(ts, ttl_s),
        "attempts_left": int(attempts),
        "offered_by": by,
        "offered_at": ts,
    }
    with tx(con) as t:
        _put(t, BIND_ROW, row, ts)
        publish(
            t,
            "telegram.bind_offered",
            by,
            {"expires_at": row["expires_at"], "attempts": int(attempts)},
            idem_key=f"tg:bind:offered:{ts}:{salt[:8]}",
            poke_peers=False,
        )
    return plain


def pending_bind(con: sqlite3.Connection, *, now_ts: str | None = None) -> PendingBind | None:
    """The outstanding offer, or None if there is none or it has expired."""
    row = _get(con, BIND_ROW)
    if row is None:
        return None
    if str(row.get("expires_at") or "") <= (now_ts or now()):
        return None
    return PendingBind(
        expires_at=str(row["expires_at"]),
        attempts_left=int(row["attempts_left"]),
        offered_by=str(row.get("offered_by") or "unknown"),
        offered_at=str(row.get("offered_at") or ""),
    )


def redeem(
    con: sqlite3.Connection,
    chat_id: int,
    text: str,
    *,
    username: str | None = None,
    now_ts: str | None = None,
) -> BindOutcome:
    """Try to bind ``chat_id`` with the code in ``text``. One shot per attempt.

    The comparison is :func:`hmac.compare_digest` over the hashes. A plain ``==``
    on a short code leaks its prefix through timing, which matters here precisely
    because the attempt limit means an attacker's only lever is learning
    something from a failure.

    The whole thing runs in ONE transaction, so two processes racing to redeem
    the same code cannot both spend the same attempt, and cannot both bind.
    """
    ts = now_ts or now()
    submitted = normalise(text)
    with tx(con) as t:
        row = _get(t, BIND_ROW)
        if row is None:
            _dropped(t, chat_id, "no binding code is outstanding", ts)
            return BindOutcome(False, "no binding code is outstanding")
        if str(row.get("expires_at") or "") <= ts:
            t.execute("DELETE FROM cursors WHERE name=?", (BIND_ROW,))
            _dropped(t, chat_id, "the binding code expired", ts)
            return BindOutcome(False, "that code expired; run --bind again")

        left = int(row["attempts_left"])
        expected = str(row["digest"])
        actual = _digest(str(row["salt"]), submitted)
        if not hmac.compare_digest(expected, actual):
            left -= 1
            if left <= 0:
                t.execute("DELETE FROM cursors WHERE name=?", (BIND_ROW,))
            else:
                _put(t, BIND_ROW, {**row, "attempts_left": left}, ts)
            publish(
                t,
                "telegram.bind_failed",
                f"telegram:{chat_id}",
                {"chat_id": chat_id, "attempts_left": max(left, 0)},
                idem_key=f"tg:bind:failed:{ts}:{chat_id}",
                poke_peers=False,
            )
            reason = (
                "that code is wrong, and it was the last attempt; run --bind again"
                if left <= 0
                else f"that code is wrong; {left} attempts left"
            )
            return BindOutcome(False, reason, attempts_left=max(left, 0))

        # A code is spent by USE as well as by failure: leaving it live would let
        # a second chat bind with a code the operator believes was consumed.
        t.execute("DELETE FROM cursors WHERE name=?", (BIND_ROW,))
        _put(t, BOUND_ROW, {"chat_id": int(chat_id), "bound_at": ts, "username": username}, ts)
        publish(
            t,
            "telegram.bound",
            f"telegram:{chat_id}",
            {"chat_id": int(chat_id), "username": username},
            idem_key=f"tg:bound:{ts}:{chat_id}",
            poke_peers=False,
        )
    return BindOutcome(True, "bound", chat_id=int(chat_id))


def read_binding(con: sqlite3.Connection) -> Binding | None:
    row = _get(con, BOUND_ROW)
    if row is None:
        return None
    return Binding(
        chat_id=int(row["chat_id"]),
        bound_at=str(row.get("bound_at") or ""),
        username=row.get("username"),
    )


def bound_chat(con: sqlite3.Connection) -> int | None:
    binding = read_binding(con)
    return None if binding is None else binding.chat_id


def authorised(con: sqlite3.Connection, chat_id: int | None) -> bool:
    """Hard equality against the ONE bound chat. Unbound means nobody is allowed.

    Not constant-time, deliberately: a chat id is not a secret — it is in every
    update the sender already sent — and pretending otherwise would suggest the
    binding code is not the thing being protected.
    """
    if chat_id is None:
        return False
    bound = bound_chat(con)
    return bound is not None and int(chat_id) == bound


def unbind(con: sqlite3.Connection, *, by: str) -> bool:
    """Forget the bound chat. Returns whether there was one."""
    ts = now()
    with tx(con) as t:
        row = _get(t, BOUND_ROW)
        if row is None:
            return False
        t.execute("DELETE FROM cursors WHERE name=?", (BOUND_ROW,))
        publish(
            t,
            "telegram.unbound",
            by,
            {"chat_id": row.get("chat_id")},
            idem_key=f"tg:unbound:{ts}",
            poke_peers=False,
        )
    return True


def drop_update(
    con: sqlite3.Connection,
    chat_id: int | None,
    *,
    why: str,
    update_id: int | None = None,
) -> None:
    """Record an update from an unbound chat. Called instead of acting on it.

    Nothing is ever sent back. Replying "you are not authorised" would confirm
    to a stranger that the bot is live and attended, which is the one thing a
    probe is trying to learn.
    """
    _dropped(con, chat_id, why, now(), update_id=update_id)


# ───────────────────────────── plumbing ─────────────────────────────


def _dropped(
    con: sqlite3.Connection,
    chat_id: int | None,
    why: str,
    ts: str,
    *,
    update_id: int | None = None,
) -> None:
    publish(
        con,
        "telegram.dropped",
        f"telegram:{chat_id}" if chat_id is not None else "telegram",
        {"chat_id": chat_id, "why": why, "update_id": update_id},
        idem_key=f"tg:dropped:{update_id}" if update_id is not None else f"tg:dropped:{ts}",
        poke_peers=False,
    )


def _digest(salt: str, code: str) -> str:
    return hashlib.sha256(f"{salt}:{code}".encode()).hexdigest()


def _get(con: sqlite3.Connection, name: str) -> dict[str, object] | None:
    row = con.execute("SELECT value FROM cursors WHERE name=?", (name,)).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(str(row["value"]))
    except ValueError:
        # A corrupt row must not lock the operator out forever; treat it as
        # absent so that --bind can overwrite it.
        return None
    return value if isinstance(value, dict) else None


def _put(con: sqlite3.Connection, name: str, value: dict[str, object], ts: str) -> None:
    con.execute(
        "INSERT INTO cursors (name, value, updated_at) VALUES (?,?,?)"
        " ON CONFLICT(name) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (name, json.dumps(value, ensure_ascii=False, separators=(",", ":")), ts),
    )


def _plus(ts: str, seconds: float) -> str:
    t = parse_ts(ts) + timedelta(seconds=seconds)
    return f"{t.strftime('%Y-%m-%dT%H:%M:%S')}.{t.microsecond // 1000:03d}Z"
