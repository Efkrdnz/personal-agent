"""The long-polling loop, and the offset cursor that makes a restart safe.

NO WEBHOOK, and this is a decision rather than a default. A webhook needs an
inbound port, a public hostname and a certificate on a Turkish residential
connection behind CGNAT — which means a tunnel, a third party in the path of
every question this system asks, and a new thing to be down at 3am. Long polling
needs an outbound HTTPS connection and nothing else. The cost is one held
connection per process; the saving is an entire class of deployment.

THE CURSOR IS THE WHOLE CORRECTNESS ARGUMENT. Telegram keeps undelivered updates
for 24 hours and hands them back until an ``offset`` confirms them. Confirming
too early loses a tap; confirming too late replays one, and a replayed tap on an
already-answered question is harmless only because
:func:`jarvis.requests.answer_request` is a compare-and-swap. So the order here
is handle-then-commit, the same discipline :func:`jarvis.bus.read_since` uses,
and the cursor is durable in the spine's ``cursors`` table rather than in memory:
a restart resumes exactly where the last handled update left off.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from time import sleep as _sleep
from typing import Any

from jarvis.bus import publish
from jarvis.ids import now
from jarvis.telegram.transport import ScriptExhausted, TelegramError, Transport, TransportError

__all__ = [
    "ALLOWED_UPDATES",
    "DEFAULT_POLL_TIMEOUT_S",
    "OFFSET_ROW",
    "POLL_FAILURE_LIMIT",
    "commit_offset",
    "poll_once",
    "read_offset",
    "run",
]

#: Also in the generic ``cursors`` KV. Migrations are frozen; this needs no column.
OFFSET_ROW = "telegram:offset"

#: Long enough that the loop is mostly idle, short enough that a due delivery
#: waits less than half a minute for the tick between polls.
DEFAULT_POLL_TIMEOUT_S = 20

#: How many CONSECUTIVE failed polls before this process gives up and lets its
#: supervisor restart it. Not 1: a 502 from a load balancer is a normal event on
#: a connection held open for twenty seconds, and a channel that dies of one is a
#: channel that is unreachable every time Telegram sneezes. Not unbounded either:
#: a bot whose token was revoked would retry forever and look alive.
POLL_FAILURE_LIMIT = 5

#: Anything not named here is not even sent to us. A channel that never asked for
#: ``edited_message`` cannot be confused by one.
ALLOWED_UPDATES: tuple[str, ...] = ("message", "callback_query")

Handler = Callable[[sqlite3.Connection, dict[str, Any]], None]
Tick = Callable[[sqlite3.Connection], None]


def read_offset(con: sqlite3.Connection) -> int:
    """The highest update_id already handled. 0 on a machine that never polled."""
    row = con.execute("SELECT value FROM cursors WHERE name=?", (OFFSET_ROW,)).fetchone()
    if row is None:
        return 0
    try:
        return int(str(row["value"]))
    except ValueError:
        return 0


def commit_offset(con: sqlite3.Connection, update_id: int) -> int:
    """Advance the cursor, MONOTONICALLY. Returns what is now stored.

    Monotonic for the same reason :func:`jarvis.bus.commit_cursor` is: two poll
    loops can briefly overlap across a restart, and letting the slower one write
    its lower value would re-deliver every update in between — which on this
    channel means asking the user a question they have already answered.
    """
    con.execute(
        "INSERT INTO cursors (name, value, updated_at) VALUES (?,?,?)"
        " ON CONFLICT(name) DO UPDATE SET"
        " value=CAST(MAX(CAST(cursors.value AS INTEGER), CAST(excluded.value AS INTEGER))"
        " AS TEXT), updated_at=excluded.updated_at",
        (OFFSET_ROW, str(int(update_id)), now()),
    )
    return read_offset(con)


def poll_once(
    con: sqlite3.Connection,
    transport: Transport,
    *,
    timeout_s: int = DEFAULT_POLL_TIMEOUT_S,
    limit: int = 100,
    allowed_updates: Sequence[str] = ALLOWED_UPDATES,
) -> list[dict[str, Any]]:
    """One ``getUpdates``, from the durable cursor. Does NOT advance it."""
    result = transport.call(
        "getUpdates",
        {
            "offset": read_offset(con) + 1,
            "timeout": int(timeout_s),
            "limit": int(limit),
            "allowed_updates": list(allowed_updates),
        },
        # The socket must outlive the long poll itself or every call times out
        # exactly when the API is behaving correctly.
        timeout=float(timeout_s) + 10.0,
    )
    return [u for u in (result or []) if isinstance(u, dict) and "update_id" in u]


def run(
    con: sqlite3.Connection,
    transport: Transport,
    handle: Handler,
    *,
    stop: Callable[[], bool] | None = None,
    on_tick: Tick | None = None,
    timeout_s: int = DEFAULT_POLL_TIMEOUT_S,
    actor: str = "telegram",
    sleep: Callable[[float], None] = _sleep,
    failure_limit: int = POLL_FAILURE_LIMIT,
    backoff_s: float = 2.0,
) -> int:
    """Poll, handle, commit, repeat. Returns how many updates were handled.

    ``on_tick`` runs once per loop, poll or no poll: it is where due deliveries
    get claimed and presented, so a question raised while nobody is typing still
    reaches the phone within one poll timeout.

    A FAILED POLL IS NOT A FAILED CHANNEL. Nothing was confirmed, so the same
    updates come back on the next round trip; the loop backs off and tries again,
    and only gives up after ``failure_limit`` failures in a row. ``sleep`` is
    injected so that ladder is testable without a test that really waits.

    A handler that raises does NOT stop the loop and does NOT stop the cursor.
    That is deliberate and it is a trade: a poison update that is retried forever
    is a channel that has silently stopped answering, which is the exact failure
    this whole system exists to prevent. So the failure is published to the
    activity log — loudly, with the update id — and the loop moves on.
    """
    handled = 0
    failures = 0
    while stop is None or not stop():
        if on_tick is not None:
            try:
                on_tick(con)
            except Exception as e:  # noqa: BLE001 - same trade as a poison update
                # The tick presents due deliveries, and a presentation can fail
                # for reasons of its own (an unaddressable keyboard, a 500 from
                # sendMessage). Letting that out of the loop would take the whole
                # channel down over ONE undeliverable question, which is the
                # failure the handler below is already written to avoid.
                publish(
                    con,
                    "telegram.tick_failed",
                    actor,
                    {"error": f"{type(e).__name__}: {e}"},
                    poke_peers=False,
                )
        try:
            updates = poll_once(con, transport, timeout_s=timeout_s)
        except ScriptExhausted:
            return handled
        except (TelegramError, TransportError) as e:
            failures += 1
            publish(
                con,
                "telegram.poll_failed",
                actor,
                {"error": f"{type(e).__name__}: {e}", "consecutive": failures},
                poke_peers=False,
            )
            if failures >= failure_limit:
                return handled
            sleep(min(backoff_s * failures, 30.0))
            continue
        failures = 0
        for update in updates:
            update_id = int(update["update_id"])
            try:
                handle(con, update)
            except Exception as e:  # noqa: BLE001 - the log line IS the report
                publish(
                    con,
                    "telegram.update_failed",
                    actor,
                    {"update_id": update_id, "error": f"{type(e).__name__}: {e}"},
                    idem_key=f"tg:update_failed:{update_id}",
                    poke_peers=False,
                )
            finally:
                commit_offset(con, update_id)
            handled += 1
    return handled
