"""Single-flight, and the hand-off that makes single-flight survivable.

The lease is only interesting because of what happens at the moment a call
starts: the desk session must checkpoint its resumption handle and get off the
line, and when the call ends the desk must come back to the SAME conversation.
Losing the handle there is the reference build's exact defect, so most of this
file is that one cycle, measured from both ends.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from jarvis.live import fake
from jarvis.live.fake import Pause, ScriptedConnector
from jarvis.live.lease import (
    CAPACITY,
    DESK_PRIORITY,
    PHONE_PRIORITY,
    LeaseDenied,
    LeaseGrant,
    LiveLease,
    MemoryHandleStore,
    SessionSupervisor,
)
from jarvis.live.profiles import AGENT_CALL, DESK
from jarvis.live.session import LiveSession


class Sleeper:
    async def __call__(self, seconds: float) -> None:
        await asyncio.sleep(0)


async def until(predicate: Any, tries: int = 2000) -> None:
    for _ in range(tries):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never became true")


def desk_factory(connector: ScriptedConnector, profile: Any = DESK) -> Any:
    def make(handle: str | None, grant: LeaseGrant) -> LiveSession:
        return LiveSession(
            profile,
            None,
            fake.BytesSink(),
            connector=connector,
            grant=grant,
            handle=handle,
            sleep=Sleeper(),
            idle_s=0.0,
        )

    return make


# ───────────────────────────── the constant ─────────────────────────────


def test_v1_is_single_flight_and_the_limit_is_a_constant() -> None:
    assert CAPACITY == 1
    assert LiveLease().capacity == 1
    # The optimistic reading of the quota costs exactly this much to adopt.
    assert LiveLease(capacity=3).capacity == 3
    with pytest.raises(ValueError, match="at least 1"):
        LiveLease(capacity=0)


async def test_a_second_holder_waits_and_is_let_in_on_release() -> None:
    lease = LiveLease()
    first = await lease.acquire("desk", DESK_PRIORITY)
    waiter = asyncio.create_task(lease.acquire("briefing", DESK_PRIORITY))
    await asyncio.sleep(0)
    assert not waiter.done()
    assert lease.free == 0

    await lease.release(first)
    second = await waiter
    assert second.holder == "briefing"
    assert lease.held == (second,)


async def test_a_higher_priority_holder_preempts_but_does_not_double_up() -> None:
    lease = LiveLease()
    desk = await lease.acquire("desk", DESK_PRIORITY)
    call = asyncio.create_task(lease.acquire("phone", PHONE_PRIORITY))
    await until(lambda: desk.revoked.is_set())

    # Revoked, but NOT yet replaced: the desk still owns a live socket, and two
    # of those is the thing whose legality nobody has measured.
    assert lease.free == 0
    assert not call.done()
    assert "phone" in desk.revoke_reason

    await lease.release(desk)
    grant = await call
    assert grant.holder == "phone"
    assert lease.held == (grant,)


async def test_a_lower_priority_holder_never_preempts() -> None:
    lease = LiveLease()
    call = await lease.acquire("phone", PHONE_PRIORITY)
    desk = asyncio.create_task(lease.acquire("desk", DESK_PRIORITY))
    await asyncio.sleep(0)
    assert not call.revoked.is_set()
    assert not desk.done()
    await lease.release(call)
    assert (await desk).holder == "desk"


async def test_waiting_forever_is_not_an_option() -> None:
    lease = LiveLease()
    await lease.acquire("phone", PHONE_PRIORITY)
    with pytest.raises(LeaseDenied, match="did not yield"):
        await lease.acquire("desk", DESK_PRIORITY, timeout=0.01)


async def test_release_is_idempotent_and_revoke_reports_what_it_found() -> None:
    lease = LiveLease()
    grant = await lease.acquire("desk", DESK_PRIORITY)
    assert await lease.revoke("desk", "kill switch") is True
    assert await lease.revoke("desk", "again") is False  # already asked
    assert await lease.revoke("nobody", "x") is False
    await lease.release(grant)
    await lease.release(grant)
    assert lease.free == 1


async def test_capacity_two_is_a_config_change_not_a_refactor() -> None:
    lease = LiveLease(capacity=2)
    a = await lease.acquire("desk", DESK_PRIORITY)
    b = await lease.acquire("phone", PHONE_PRIORITY)
    assert len(lease.held) == 2
    assert not a.revoked.is_set()
    await lease.release(a)
    await lease.release(b)


# ────────────────── the hand-off: checkpoint, call, restore ──────────────────


async def test_a_call_suspends_the_desk_and_the_desk_comes_back_to_the_same_conversation() -> None:
    lease = LiveLease()
    store = MemoryHandleStore()
    desk_conn = ScriptedConnector(
        scripts=[[fake.handle("h-desk"), Pause()], [fake.handle("h-desk-2"), Pause()]]
    )
    desk = SessionSupervisor(
        lease, "desk", desk_factory(desk_conn), priority=DESK_PRIORITY, store=store
    )
    session = await desk.start()
    await until(lambda: session.resume_handle == "h-desk")

    # The call arrives. Acquiring the lease is the ONLY thing it does: the desk
    # checkpoints and closes itself because its grant was revoked.
    call_grant = await lease.acquire("phone", PHONE_PRIORITY)
    assert store.load("desk") == "h-desk"
    assert desk.suspensions == 1
    assert not desk.running

    call_conn = ScriptedConnector(scripts=[[Pause()]])
    call_session = LiveSession(
        AGENT_CALL,
        None,
        fake.BytesSink(),
        connector=call_conn,
        grant=call_grant,
        sleep=Sleeper(),
        idle_s=0.0,
    )
    call_task = asyncio.create_task(call_session.run())
    await until(lambda: call_session.connected)
    assert call_conn.voices == ["Charon"]

    await call_session.close()
    await call_task
    await lease.release(call_grant)

    # The desk comes back WITH the handle. That line is the whole design.
    resumed = await desk.start()
    await until(lambda: resumed.connected)
    assert desk_conn.handles == [None, "h-desk"]
    await desk.stop()


async def test_a_session_that_crashes_still_leaves_its_handle_behind() -> None:
    lease = LiveLease()
    store = MemoryHandleStore()
    # One script, then the connector runs out: run() raises ScriptExhausted out
    # of the reconnect loop, which is a crash as far as the supervisor knows.
    connector = ScriptedConnector(scripts=[[fake.handle("h-crash"), fake.Hangup()]])
    desk = SessionSupervisor(lease, "desk", desk_factory(connector), store=store)
    await desk.start()
    await desk.wait_suspended()
    assert store.load("desk") == "h-crash"
    assert lease.free == 1


async def test_stopping_without_a_checkpoint_forgets_the_conversation() -> None:
    lease = LiveLease()
    store = MemoryHandleStore()
    connector = ScriptedConnector(scripts=[[fake.handle("h-gone"), Pause()]])
    desk = SessionSupervisor(lease, "desk", desk_factory(connector), store=store)
    session = await desk.start()
    await until(lambda: session.resume_handle == "h-gone")
    await desk.stop(checkpoint=False)
    assert store.load("desk") is None
    assert lease.free == 1


async def test_a_supervisor_refuses_to_run_twice() -> None:
    lease = LiveLease()
    connector = ScriptedConnector(scripts=[[Pause()]], repeat_last=True)
    desk = SessionSupervisor(lease, "desk", desk_factory(connector))
    await desk.start()
    with pytest.raises(RuntimeError, match="already running"):
        await desk.start()
    await desk.stop()
