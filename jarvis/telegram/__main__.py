"""``python -m jarvis.telegram`` — the bot as its own OS process.

Like ``python -m jarvis.cc``, this is a process and not a library: it opens the
one SQLite file, attaches to the bus as a channel, long-polls, and exits. It
speaks to nothing except Telegram and the database, which is what makes the
falsifiable test for this stage possible at all — nothing here imports
``jarvis.audio``, ``jarvis.voice`` or ``jarvis.live``, and nothing here knows
that a desk exists.

EXIT CODES, because a supervisor reads them:

    0   the loop ended cleanly (or ``--once`` finished, or the code was printed)
    1   gave up: too many failed polls in a row. The supervisor should restart it
    2   refused to start: no token
    5   the loop died in a way nothing here anticipated

The token is never a command-line argument. An argument is in ``ps``, in the
shell history and in the systemd unit file; the keyring and the environment are
the only two places it may come from.
"""

from __future__ import annotations

import argparse
import itertools
import os
import sqlite3
import sys
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from jarvis import requests as rq
from jarvis.bus import Capabilities, Peer, Redactor, publish
from jarvis.db import open_db
from jarvis.telegram import bot, commands, identity, voice
from jarvis.telegram.channel import TelegramChannel
from jarvis.telegram.transport import HttpTransport, Transport

__all__ = ["CAPS", "build_parser", "dispatch", "main", "resolve_token"]

EXIT_OK = 0
EXIT_UNREACHABLE = 1
EXIT_REFUSED = 2
EXIT_CRASHED = 5

TOKEN_ENV = "JARVIS_TELEGRAM_TOKEN"
KEYRING_SERVICE = "jarvis"
KEYRING_USER = "telegram_bot_token"

#: ``verbatim=True`` is the load-bearing one: a channel whose ``caps.verbatim``
#: is false is INELIGIBLE for plan questions, and on this channel the labels are
#: printed as literal text with no parse mode — which is as verbatim as a thing
#: can be. ``speak`` is false: a voice note is a file, not a room.
CAPS: Capabilities = {
    "human": True,
    "speak": False,
    "verbatim": True,
    "listen": False,
    "free_text": True,
    "dtmf": False,
    "buttons": True,
    "images": True,
    "max_options": 50,
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m jarvis.telegram", description=__doc__)
    p.add_argument("--db", default=None, help="database path; defaults to $JARVIS_DB")
    p.add_argument(
        "--bind",
        action="store_true",
        help="print a one-time binding code and exit; needs no token and no network",
    )
    p.add_argument("--unbind", action="store_true", help="forget the bound chat and exit")
    p.add_argument("--once", action="store_true", help="one poll, then exit (for smoke tests)")
    p.add_argument(
        "--poll-timeout",
        type=int,
        default=bot.DEFAULT_POLL_TIMEOUT_S,
        help="seconds getUpdates holds the connection open",
    )
    return p


def resolve_token() -> str | None:
    """The bot token, from the keyring first and the environment second.

    ``keyring`` is imported INSIDE the function and its absence is not an error:
    the house rule is that no secret enters the tree, not that every machine must
    have a keyring daemon. A headless box runs from the environment.
    """
    try:  # noqa: SIM105 - the fallback is the point, not a suppression
        import keyring  # type: ignore[import-not-found]

        stored = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
        if stored:
            return str(stored)
    except Exception:  # noqa: BLE001 - no keyring, no backend, locked keyring
        pass
    return os.environ.get(TOKEN_ENV) or None


def dispatch(
    con: sqlite3.Connection,
    transport: Transport,
    update: dict[str, Any],
    *,
    channel: TelegramChannel | None = None,
) -> None:
    """One update -> the right module. The whole routing table, in one function.

    THE FIRST THING IT DOES IS CHECK IDENTITY, and it does it for callbacks as
    well as messages: an inline keyboard can be forwarded, and a forwarded button
    tapped by a stranger arrives as a perfectly well-formed callback carrying a
    real request id. Authorising only the message path would leave every
    permission prompt this system has ever sent one forward away from being
    answered by somebody else.

    A callback is authorised on the PERSON who tapped as well as on the chat it
    arrived in. The two are the same id in a private chat and differ in a group,
    where every member can reach a button — so checking only the chat would let
    anyone in a bound group approve a tool call.

    ``channel`` is derived from the live binding unless a caller overrides it,
    because the binding can move while this process is up: it is created by an
    update handled in this very function.
    """
    chat_id = _chat_of(update)
    if not (identity.authorised(con, chat_id) and identity.authorised(con, _presser_of(update))):
        if _try_bind(con, transport, update, chat_id):
            return
        identity.drop_update(
            con, chat_id, why="not the bound chat", update_id=int(update.get("update_id") or 0)
        )
        return

    bound = int(chat_id)  # authorised() already refused None and every other chat
    if channel is None:
        channel = TelegramChannel(chat_id=bound)
    elif channel.chat_id != bound:
        channel = replace(channel, chat_id=bound)

    query = update.get("callback_query")
    if isinstance(query, dict):
        channel.on_callback(con, transport, query)
        return

    message = update.get("message")
    if not isinstance(message, dict):
        return

    if message.get("voice") or message.get("audio"):
        note = voice.save_voice_note(con, transport, message)
        transport.call(
            "sendMessage",
            {
                "chat_id": channel.chat_id,
                "text": (
                    "Saved your voice note. I have not transcribed it — "
                    "the desk does that when it is next up."
                ),
                "reply_to_message_id": note.message_id,
            },
        )
        return

    text = str(message.get("text") or "")

    # A message that REPLIES to one of my questions is tried as an answer first.
    # Command matching would otherwise eat it: "back" is a presence override and
    # also a perfectly good answer to "which branch?", and the user who used the
    # reply gesture has already said which of the two they meant.
    if message.get("reply_to_message") is not None:
        answered = channel.on_reply(con, transport, message)
        if answered.handled:
            return

    reply = commands.handle_command(con, text, chat_id=channel.chat_id)
    if reply is not None:
        transport.call("sendMessage", {"chat_id": channel.chat_id, "text": reply.text})
        return

    # Not a command: it may still be the "none of these" answer to a question
    # this channel asked. on_reply decides, and says nothing if it was neither.
    outcome = channel.on_reply(con, transport, message)
    if not outcome.handled and text.strip():
        transport.call(
            "sendMessage",
            {
                "chat_id": channel.chat_id,
                "text": "Reply to one of my questions to answer it, or send /help.",
            },
        )


def _try_bind(
    con: sqlite3.Connection,
    transport: Transport,
    update: dict[str, Any],
    chat_id: int | None,
) -> bool:
    """Redeem a one-time code. The ONLY thing an unbound chat is allowed to do.

    Without this the binding described in :mod:`jarvis.telegram.identity` cannot
    happen at all: the operator reads a code off the terminal, sends it to the
    bot, and the update is dropped unread because the chat is not yet bound.

    Narrow on purpose. Only a plain text message in a PRIVATE chat, only while
    the operator's own ten-minute offer is outstanding, and only when nothing is
    bound yet — moving to a new phone is ``--unbind`` then ``--bind``, an
    explicit act, rather than something a stranger can do by messaging during a
    window the operator opened for another reason. A group is refused because
    binding one would hand a button to every member of it.
    """
    if chat_id is None or identity.bound_chat(con) is not None:
        return False
    if identity.pending_bind(con) is None:
        return False
    message = update.get("message")
    if not isinstance(message, dict):
        return False
    if str((message.get("chat") or {}).get("type") or "private") != "private":
        return False
    text = str(message.get("text") or "").strip()
    if not _looks_like_a_code(text):
        # Anything that is not code-shaped is not a guess, and must not spend one
        # of the three attempts. Otherwise a stranger who says "hi" three times
        # during the offer window destroys the operator's code without ever
        # guessing at it.
        return False

    sender = message.get("from") or {}
    username = sender.get("username")
    outcome = identity.redeem(con, int(chat_id), text, username=str(username) if username else None)
    # The silence strangers get everywhere else is relaxed HERE and only here.
    # Inside the operator's own offer window a wrong code has to say so, or three
    # mistyped characters burn the attempts invisibly; outside it, no offer is
    # pending and this function has already returned False.
    transport.call(
        "sendMessage",
        {
            "chat_id": int(chat_id),
            "text": (
                "Bound. This chat can now answer questions and run commands."
                if outcome.ok
                else f"No: {outcome.reason}."
            ),
        },
    )
    return True


def _looks_like_a_code(text: str) -> bool:
    """Whether this message is even in the shape of a binding code.

    Cheap shape check, not a comparison: it tells an attempt apart from small
    talk. The code itself is still checked in one transaction by
    :func:`jarvis.telegram.identity.redeem`, in constant time, against a hash.
    """
    flat = identity.normalise(text)
    return len(flat) == identity.CODE_LENGTH and all(c in identity.CODE_ALPHABET for c in flat)


def _presser_of(update: dict[str, Any]) -> int | None:
    """Who physically tapped, for a callback. The chat itself, for anything else.

    A callback_query always carries ``from``; a missing one is not a shape this
    bot wrote, so None (which never authorises) is the right answer.
    """
    query = update.get("callback_query")
    if isinstance(query, dict):
        sender = query.get("from") or {}
        return int(sender["id"]) if sender.get("id") is not None else None
    return _chat_of(update)


def _chat_of(update: dict[str, Any]) -> int | None:
    for key in ("message", "edited_message", "channel_post"):
        node = update.get(key)
        if isinstance(node, dict):
            chat = node.get("chat") or {}
            if chat.get("id") is not None:
                return int(chat["id"])
    query = update.get("callback_query")
    if isinstance(query, dict):
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        if chat.get("id") is not None:
            return int(chat["id"])
        sender = query.get("from") or {}
        if sender.get("id") is not None:
            return int(sender["id"])
    return None


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    con = open_db(args.db)
    try:
        if args.unbind:
            existed = identity.unbind(con, by="operator")
            print("Unbound." if existed else "Nothing was bound.")
            return EXIT_OK
        if args.bind:
            code = identity.offer_code(con, by="operator")
            print(
                f"Send this to the bot within {identity.DEFAULT_TTL_S // 60} minutes:\n\n"
                f"    {code}\n\n"
                f"{identity.DEFAULT_ATTEMPTS} attempts. It is not stored anywhere you can read."
            )
            return EXIT_OK

        token = resolve_token()
        if not token:
            print(
                f"No bot token. Put it in the keyring ({KEYRING_SERVICE}/{KEYRING_USER}) "
                f"or in ${TOKEN_ENV}.",
                file=sys.stderr,
            )
            return EXIT_REFUSED
        chat_id = identity.bound_chat(con)
        if chat_id is None:
            # NOT a refusal. Binding happens through an update this loop has to be
            # running to receive, so a bot that exits when unbound can never
            # become bound — it would wait for a code it has no way to hear.
            print(
                "No chat is bound; polling anyway so that a code from --bind can be "
                "redeemed. Every other update will be dropped.",
                file=sys.stderr,
            )

        transport = HttpTransport(token, timeout_s=float(args.poll_timeout) + 10.0)
        # The token is the one literal that must never reach the activity log,
        # and the bus cannot know what a secret looks like — so it is told.
        redactor = Redactor.of([token])
        peer = Peer.attach(con, "telegram", "telegram", CAPS, {"chat_id": chat_id})
        publish(
            con,
            "telegram.started",
            "telegram",
            {"chat_id": chat_id},
            channel_id=peer.peer_id,
            redactor=redactor,
            poke_peers=False,
        )
        try:
            polls = itertools.count()

            def stop() -> bool:
                return bool(args.once) and next(polls) > 0

            bot.run(
                con,
                transport,
                lambda c, u: dispatch(c, transport, u),
                stop=stop,
                on_tick=lambda c: _tick(c, transport, peer),
                timeout_s=int(args.poll_timeout),
            )
        finally:
            peer.detach(con, reason="exit")
        if args.once:
            return EXIT_OK
        # A daemon loop that RETURNS has given up on the poll, which is not a
        # clean exit however tidily it unwound: say so in the code a supervisor
        # reads rather than restarting silently forever.
        print("poll loop gave up; see telegram.poll_failed in the log", file=sys.stderr)
        return EXIT_UNREACHABLE
    except KeyboardInterrupt:
        return EXIT_OK
    except Exception as e:  # noqa: BLE001 - the exit code IS the report
        print(f"crashed: {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_CRASHED
    finally:
        con.close()


def _tick(con: sqlite3.Connection, transport: Transport, peer: Peer) -> None:
    """Between polls: expire what is due, then ask what is waiting.

    ``expire_due`` runs here as well as wherever else it runs because the typed
    timeout outcome must fire even when this is the only process awake — a
    question whose deadline passed while the desk was off is exactly the case
    Telegram exists for.

    The binding is re-read every tick rather than captured at startup, so a chat
    bound while this process is running starts receiving questions on the next
    tick instead of after a restart.
    """
    peer.heartbeat(con)
    rq.expire_due(con)
    bound = identity.bound_chat(con)
    if bound is not None:
        TelegramChannel(chat_id=bound).claim_and_present(con, transport)


if __name__ == "__main__":
    sys.exit(main())
