"""In-process thread-safe EventBus for real-time task event streaming (P4).

Publishers (sync FastAPI handlers, review/validation worker threads) publish
committed ``Event`` objects from any OS thread. Subscribers (async SSE stream
generators) consume from bounded ``asyncio.Queue`` instances on their own event
loop via ``call_soon_threadsafe``.

Slow-consumer protection:
- Each subscriber queue is bounded (``DEFAULT_QUEUE_MAXSIZE``).
- Publishers NEVER block when a subscriber falls behind.
- When a subscriber's queue is full, ``overflowed`` is latched on that
  subscriber; the SSE generator detects overflow, drains stale buffered items,
  and resyncs directly from SQLite starting after ``last_sent_seq`` so zero
  events are lost or duplicated while memory remains strictly bounded.
"""
from __future__ import annotations

import asyncio
import threading
from typing import Callable

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
        self.queue: asyncio.Queue[Event | None] = asyncio.Queue(maxsize=max(1, maxsize))
        self._overflowed = False
        self._closed = False

    def _offer_on_loop(self, item: Event | None) -> None:
        """Enqueue an event on the subscriber's owning event loop."""
        if self._closed:
            return
        if item is None:
            # Shutdown sentinel: make room if full so the consumer always wakes up.
            self._closed = True
            while self.queue.full():
                try:
                    self.queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            try:
                self.queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
            return
        if self._overflowed:
            # Already awaiting a DB resync; avoid pointless queue churn.
            return
        if self.queue.full():
            self._overflowed = True
            return
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            self._overflowed = True

    def notify(self, item: Event | None) -> bool:
        """Thread-safely schedule delivery of ``item``. Returns False if loop is closed."""
        if self._closed and item is not None:
            return False
        if self.loop.is_closed():
            return False
        try:
            self.loop.call_soon_threadsafe(self._offer_on_loop, item)
            return True
        except RuntimeError:
            return False

    @property
    def overflowed(self) -> bool:
        return self._overflowed

    def consume_overflow(self) -> bool:
        """Clear the overflow flag and drain buffered items so caller can resync from DB."""
        if not self._overflowed:
            return False
        self._overflowed = False
        while True:
            try:
                item = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if item is None:
                # Preserve shutdown sentinel if it arrived concurrently.
                try:
                    self.queue.put_nowait(None)
                except asyncio.QueueFull:
                    pass
                break
        return True

    async def get(self) -> Event | None:
        return await self.queue.get()

    def close(self) -> None:
        self._closed = True


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
