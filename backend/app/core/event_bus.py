"""In-process thread-safe EventBus for real-time task event streaming (P4).

Publishers (sync FastAPI handlers, review/validation worker threads) publish
committed ``Event`` objects from any OS thread. Subscribers (async SSE stream
generators) consume from bounded buffers woken via coalesced loop callbacks.

Slow-consumer protection:
- Each subscriber buffer is strictly bounded (``DEFAULT_QUEUE_MAXSIZE``).
- At most ONE wake callback is ever pending on a subscriber's event loop at a
  time, even if thousands of events arrive while the loop is stalled.
- Publishers NEVER block when a subscriber falls behind.
- When a subscriber's buffer fills, ``overflowed`` is latched and the buffer
  is cleared; the SSE generator detects overflow and resyncs directly from
  SQLite starting after ``last_sent_seq`` so zero events are lost or duplicated
  while both queue and callback memory remain strictly O(1).
"""
from __future__ import annotations

import asyncio
from collections import deque
import threading

from app.core.models import Event

DEFAULT_QUEUE_MAXSIZE = 256


class EventSubscription:
    """Handle for one async subscriber watching a single ``task_id``."""

    def __init__(
        self,
        *,
        task_id: str,
        loop: asyncio.AbstractEventLoop,
        maxsize: int = DEFAULT_QUEUE_MAXSIZE,
    ):
        self.task_id = task_id
        self.loop = loop
        self._maxsize = max(1, maxsize)
        self._lock = threading.Lock()
        self._buffer: deque[Event] = deque()
        self._overflowed = False
        self._shutdown = False
        self._closed = False
        self._wake_scheduled = False
        self._signal = asyncio.Event()

    def _wake_on_loop(self) -> None:
        with self._lock:
            self._wake_scheduled = False
        self._signal.set()

    def _schedule_wake_locked(self) -> bool:
        """Schedule at most one coalesced wake callback on ``self.loop``."""
        if self.loop.is_closed():
            return False
        if self._wake_scheduled:
            return True
        self._wake_scheduled = True
        try:
            self.loop.call_soon_threadsafe(self._wake_on_loop)
            return True
        except RuntimeError:
            self._wake_scheduled = False
            return False

    def notify(self, item: Event | None) -> bool:
        """Thread-safely enqueue ``item`` (or shutdown sentinel ``None``).

        Returns False if the subscriber is closed or its event loop is closed.
        """
        with self._lock:
            if item is None:
                self._shutdown = True
                self._closed = True
                self._buffer.clear()
                return self._schedule_wake_locked()
            if self._closed:
                return False
            if self._overflowed:
                # Already latched for DB resync; schedule wake only if not pending.
                return self._schedule_wake_locked()
            if len(self._buffer) >= self._maxsize:
                self._overflowed = True
                self._buffer.clear()
                return self._schedule_wake_locked()
            self._buffer.append(item)
            return self._schedule_wake_locked()

    @property
    def overflowed(self) -> bool:
        with self._lock:
            return self._overflowed

    def consume_overflow(self) -> bool:
        """Clear the overflow flag and drain buffered items so caller can resync from DB."""
        with self._lock:
            if not self._overflowed:
                return False
            self._overflowed = False
            self._buffer.clear()
            return True

    async def wait_next(self) -> tuple[str, Event | None]:
        """Await the next item: ``('event', Event)``, ``('overflow', None)``, or ``('shutdown', None)``."""
        while True:
            with self._lock:
                if self._shutdown:
                    return 'shutdown', None
                if self._overflowed:
                    self._overflowed = False
                    self._buffer.clear()
                    return 'overflow', None
                if self._buffer:
                    return 'event', self._buffer.popleft()
                # Buffer empty & not overflowed/shutdown: arm signal while
                # holding `_lock` so a concurrent `notify()` cannot slip through.
                self._signal.clear()
            await self._signal.wait()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._buffer.clear()


class EventBus:
    """Thread-safe in-process pub/sub keyed by ``task_id``."""

    def __init__(self, *, max_queue_size: int = DEFAULT_QUEUE_MAXSIZE):
        self._lock = threading.Lock()
        self._max_queue_size = max(1, max_queue_size)
        self._subscribers: dict[str, set[EventSubscription]] = {}
        self._closed = False

    def subscribe(
        self,
        task_id: str,
        *,
        loop: asyncio.AbstractEventLoop | None = None,
        maxsize: int | None = None,
    ) -> EventSubscription:
        target_loop = loop if loop is not None else asyncio.get_running_loop()
        sub = EventSubscription(
            task_id=task_id,
            loop=target_loop,
            maxsize=self._max_queue_size if maxsize is None else maxsize,
        )
        with self._lock:
            if self._closed:
                sub.notify(None)
                return sub
            self._subscribers.setdefault(task_id, set()).add(sub)
        return sub

    def unsubscribe(self, sub: EventSubscription) -> None:
        sub.close()
        with self._lock:
            subs = self._subscribers.get(sub.task_id)
            if subs is not None:
                subs.discard(sub)
                if not subs:
                    self._subscribers.pop(sub.task_id, None)

    def publish(self, event: Event) -> int:
        """Publish a committed event to all subscribers of ``event.task_id``.

        Returns the number of live subscribers notified.
        """
        with self._lock:
            subs = list(self._subscribers.get(event.task_id, ()))
        if not subs:
            return 0
        dead: list[EventSubscription] = []
        delivered = 0
        for sub in subs:
            if sub.notify(event):
                delivered += 1
            else:
                dead.append(sub)
        if dead:
            with self._lock:
                current = self._subscribers.get(event.task_id)
                if current is not None:
                    for sub in dead:
                        current.discard(sub)
                    if not current:
                        self._subscribers.pop(event.task_id, None)
        return delivered

    def subscriber_count(self, task_id: str | None = None) -> int:
        with self._lock:
            if task_id is not None:
                return len(self._subscribers.get(task_id, ()))
            return sum(len(s) for s in self._subscribers.values())

    def close_all(self) -> None:
        """Signal all active subscribers to close cleanly (e.g. server shutdown)."""
        with self._lock:
            self._closed = True
            all_subs = [sub for subs in self._subscribers.values() for sub in subs]
            self._subscribers.clear()
        for sub in all_subs:
            sub.notify(None)

    def reopen(self) -> None:
        """Allow new subscriptions again (mainly for tests reusing a bus)."""
        with self._lock:
            self._closed = False
