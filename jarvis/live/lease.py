"""How many Live sessions may be connected at once, as a resource rather than a hope.

CAPACITY IS ONE, AND IT IS A CONSTANT. The concurrent-session ceiling for
``gemini-3.8-live`` is UNVERIFIED and the sources conflict wildly — 3, 1000,
5000 — and the difference decides whether a desk conversation and an outbound
restaurant call can coexist at all. So v1 is single-flight under the pessimistic
reading, and because the sessions are separate objects with separate
connections, separate voices and separate handles from day one, the optimistic
reading costs exactly one number here rather than a refactor.

SINGLE-FLIGHT IS NOT "THE CALL FAILS". A higher-priority holder REVOKES the
lower one, which checkpoints its resumption handle and closes; when the call
ends, the desk reopens WITH that handle and the conversation continues. Losing
the handle at this hand-off is the reference build's exact defect, so the
checkpoint is part of the lease protocol rather than a thing each caller
remembers to do.

REVOCATION IS COOPERATIVE. The lease sets an event and waits for the holder to
release. It never grants over a live connection, because two connections is the
thing whose legality is unknown.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from jarvis.live.session import LiveSession

__all__ = [
    "CAPACITY",
    "DESK_PRIORITY",
    "HandleStore",
    "LeaseDenied",
    "LeaseGrant",
    "LiveLease",
    "MemoryHandleStore",
    "PHONE_PRIORITY",
    "SessionSupervisor",
]

#: v1. One connection. See the module docstring before changing it, and change
#: it only with the output of ``tools/probe_live.py`` in hand.
CAPACITY = 1

#: A ringing phone outranks the desk, because the desk can be resumed and a
#: caller cannot be asked to hold while a resumption handle is negotiated.
DESK_PRIORITY = 10
PHONE_PRIORITY = 20


class LeaseDenied(RuntimeError):
    """The lease could not be had in time. The caller must say so out loud."""


@dataclass(eq=False)
class LeaseGrant:
    """Permission for exactly one session to be connected.

    ``revoked`` is what :class:`jarvis.live.session.LiveSession` watches; setting
    it is how a call tells the desk to checkpoint and get off the line.
    """

    holder: str
    priority: int
    granted_at: float
    revoked: asyncio.Event = field(default_factory=asyncio.Event)
    revoke_reason: str = ""
    released: bool = False

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        state = "released" if self.released else ("revoked" if self.revoked.is_set() else "held")
        return f"<LeaseGrant {self.holder!r} prio={self.priority} {state}>"


@runtime_checkable
class HandleStore(Protocol):
    """Where a checkpointed resumption handle waits for the session to come back.

    A handle is a connection credential and it is NOT in the database: the
    schema is frozen, there is no column for it, and inventing one to hold a
    credential would be the wrong answer twice. In-process is enough for v1,
    where the hand-off is between two objects in one process; a phone worker in
    another process needs a real implementation of this protocol and that is a
    one-file change.
    """

    def save(self, holder: str, handle: str | None) -> None: ...

    def load(self, holder: str) -> str | None: ...


class MemoryHandleStore:
    """The v1 store: a dict, living as long as the process that owns the lease."""

    def __init__(self) -> None:
        self._handles: dict[str, str | None] = {}
        self.saves = 0

    def save(self, holder: str, handle: str | None) -> None:
        self.saves += 1
        self._handles[holder] = handle

    def load(self, holder: str) -> str | None:
        return self._handles.get(holder)


class LiveLease:
    """Who may be connected. Multi-instantiable; owns no globals."""

    def __init__(
        self, capacity: int = CAPACITY, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        if capacity < 1:
            raise ValueError(f"capacity must be at least 1, got {capacity}")
        self.capacity = capacity
        self._clock = clock
        self._held: list[LeaseGrant] = []
        self._cond = asyncio.Condition()

    @property
    def held(self) -> tuple[LeaseGrant, ...]:
        return tuple(self._held)

    @property
    def free(self) -> int:
        return self.capacity - len(self._held)

    async def acquire(
        self, holder: str, priority: int = DESK_PRIORITY, *, timeout: float | None = None
    ) -> LeaseGrant:
        """Wait for room, preempting a lower-priority holder if there is one."""
        try:
            async with asyncio.timeout(timeout):
                async with self._cond:
                    while self.free <= 0:
                        weakest = min(self._held, key=lambda g: g.priority)
                        if priority > weakest.priority and not weakest.revoked.is_set():
                            weakest.revoke_reason = f"preempted by {holder!r}"
                            weakest.revoked.set()
                            # NOT granted here: the holder still owns a live
                            # socket, and granting now would put two of them on
                            # an account whose ceiling nobody has measured.
                        await self._cond.wait()
                    grant = LeaseGrant(holder=holder, priority=priority, granted_at=self._clock())
                    self._held.append(grant)
                    return grant
        except TimeoutError as exc:
            raise LeaseDenied(
                f"{holder!r} waited {timeout}s for the live lease and the holder did not yield"
            ) from exc

    async def release(self, grant: LeaseGrant) -> None:
        """Give the slot back. Idempotent: a supervisor and a caller may race."""
        async with self._cond:
            if grant.released:
                return
            grant.released = True
            if grant in self._held:
                self._held.remove(grant)
            self._cond.notify_all()

    async def revoke(self, holder: str, reason: str) -> bool:
        """Ask a holder to checkpoint and get off. Returns whether one was asked."""
        async with self._cond:
            asked = False
            for grant in self._held:
                if grant.holder == holder and not grant.revoked.is_set():
                    grant.revoke_reason = reason
                    grant.revoked.set()
                    asked = True
            return asked


class SessionSupervisor:
    """One holder's session across suspensions: acquire, run, checkpoint, restore.

    The factory takes the handle to replay and the grant to watch, and returns a
    fresh :class:`~jarvis.live.session.LiveSession`. That is the shape it is,
    rather than a ``session.reconnect()``, because a suspended session is a
    CLOSED socket and a closed socket that pretends otherwise is how you end up
    with two of them.
    """

    def __init__(
        self,
        lease: LiveLease,
        holder: str,
        factory: Callable[[str | None, LeaseGrant], LiveSession],
        *,
        priority: int = DESK_PRIORITY,
        store: HandleStore | None = None,
    ) -> None:
        self._lease = lease
        self.holder = holder
        self._factory = factory
        self._priority = priority
        self._store: HandleStore = store or MemoryHandleStore()
        self.session: LiveSession | None = None
        self.grant: LeaseGrant | None = None
        self._task: asyncio.Task[None] | None = None
        self._watch: asyncio.Task[None] | None = None
        self.suspensions = 0

    @property
    def store(self) -> HandleStore:
        return self._store

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self, *, timeout: float | None = None) -> LiveSession:
        """Take the lease and open a session, replaying any checkpointed handle."""
        if self.running:
            raise RuntimeError(f"{self.holder!r} is already running")
        grant = await self._lease.acquire(self.holder, self._priority, timeout=timeout)
        # The session watches the grant itself: revocation closes it with
        # checkpoint=True, which is the whole hand-off in one line — so the
        # grant is a constructor argument, not something set on it afterwards.
        session = self._factory(self._store.load(self.holder), grant)
        self.session = session
        self.grant = grant
        self._task = asyncio.create_task(session.run())
        self._watch = asyncio.create_task(self._on_finish())
        return session

    async def _on_finish(self) -> None:
        """Whatever ends the session — revocation, close, a dead network — checkpoint."""
        assert self._task is not None
        try:
            await self._task
        except asyncio.CancelledError:
            raise
        except Exception:
            # A crashed session still has a handle worth keeping: the next start
            # resumes the conversation instead of greeting the user twice.
            pass
        finally:
            if self.session is not None:
                self._store.save(self.holder, self.session.resume_handle)
            if self.grant is not None:
                if self.grant.revoked.is_set():
                    self.suspensions += 1
                await self._lease.release(self.grant)

    async def stop(self, *, checkpoint: bool = True) -> None:
        """Close the session and give the lease back."""
        if self.session is not None:
            await self.session.close(checkpoint=checkpoint)
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
        if self._watch is not None:
            await asyncio.gather(self._watch, return_exceptions=True)
        self._task = None
        self._watch = None

    async def wait_suspended(self) -> None:
        """Block until a revocation has fully taken effect (closed and released)."""
        if self._watch is not None:
            await asyncio.gather(self._watch, return_exceptions=True)
        self._task = None
        self._watch = None
