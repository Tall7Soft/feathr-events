"""In-process queue modeled after SQS semantics.

This deliberately mirrors the slice of SQS we actually use in the worker
so that a future swap to real SQS is a transport change, not a redesign:

* ``send`` is non-blocking from the producer's perspective and supports
  a per-message delivery delay (SQS ``DelaySeconds``).
* ``receive`` returns a :class:`QueueMessage` carrying a ``receive_count``
  (SQS ``ApproximateReceiveCount``) used by the worker to drive retries
  and dead-letter routing.
* ``ack`` and ``nack`` close the lifecycle of a message; ``nack`` accepts
  a delay so the worker can implement exponential backoff without leaking
  ``asyncio.sleep`` into business logic.

What we do NOT simulate (and why it's fine here):

* Visibility timeouts. The in-process worker holds the message in memory
  for the duration of ``process``; there is no separate consumer that
  could double-deliver. Real SQS needs this because consumers are remote
  and may crash mid-batch.
* Cross-process durability. Messages live in an ``asyncio.PriorityQueue``;
  a process restart loses anything in flight. ARCHITECTURE.md discusses
  the durable upgrade path.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class QueueMessage:
    body: dict[str, Any]
    message_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    receive_count: int = 0
    enqueued_at: float = field(default_factory=time.time)


class InMemoryQueue:
    """SQS-shaped in-process queue.

    Backed by :class:`asyncio.PriorityQueue` keyed on
    ``(visible_at, seq)`` so delayed redelivery and ordinary
    enqueueing share one mechanism. Tracks an ``in_flight`` count and
    a ``_drained`` event so callers (workers, tests) can wait for the
    queue to be both empty and quiescent via :meth:`join`.
    """

    def __init__(self, name: str) -> None:
        """Create a named queue. The name is used in logs and the DLQ shape."""
        self.name = name
        # PriorityQueue keyed on (visible_at, seq) lets us implement
        # delayed delivery cheaply without spawning per-message tasks.
        self._queue: asyncio.PriorityQueue[tuple[float, int, QueueMessage]] = (
            asyncio.PriorityQueue()
        )
        self._seq = 0
        self._inflight = 0
        self._inflight_lock = asyncio.Lock()
        self._drained = asyncio.Event()
        self._drained.set()

    async def send(self, body: dict[str, Any], delay: float = 0.0) -> QueueMessage:
        """Wrap ``body`` in a fresh :class:`QueueMessage` and enqueue it.

        ``delay`` (seconds) is honored as a per-message visibility
        delay — equivalent to SQS ``DelaySeconds``.
        """
        msg = QueueMessage(body=body)
        return await self.send_message(msg, delay=delay)

    async def send_message(self, msg: QueueMessage, delay: float = 0.0) -> QueueMessage:
        """Enqueue an existing message (used by the worker's NACK path).

        Preserves the message's ``receive_count`` so a retried message
        still hits the DLQ after ``max_receive_count`` attempts.
        """
        self._seq += 1
        visible_at = time.time() + max(delay, 0.0)
        await self._queue.put((visible_at, self._seq, msg))
        self._drained.clear()
        return msg

    async def receive(self, *, poll_interval: float = 0.05) -> QueueMessage:
        """Block until a message is visible, then return it.

        Visibility is enforced cooperatively: if the head message is
        not yet visible, we put it back and sleep until it should be.
        This keeps the queue single-reader-friendly while honoring
        delays. Increments the in-flight counter so :meth:`join` can
        block until acked.
        """
        while True:
            visible_at, seq, msg = await self._queue.get()
            now = time.time()
            if visible_at <= now:
                msg.receive_count += 1
                async with self._inflight_lock:
                    self._inflight += 1
                    self._drained.clear()
                return msg
            # Not yet visible: put it back and wait.
            await self._queue.put((visible_at, seq, msg))
            await asyncio.sleep(min(visible_at - now, poll_interval))

    async def ack(self, _msg: QueueMessage) -> None:
        """Mark a received message as processed.

        Decrements the in-flight counter; if the queue is then both
        empty and quiescent, sets the drained event so :meth:`join`
        can return.
        """
        await self._mark_done()

    async def nack(self, msg: QueueMessage, *, delay: float = 0.0) -> None:
        """Re-enqueue a message with an optional visibility delay.

        Used by the worker's retry path. The message carries its
        incremented ``receive_count`` so the worker can decide to DLQ
        on the next attempt.
        """
        await self.send_message(msg, delay=delay)
        await self._mark_done()

    async def _mark_done(self) -> None:
        """Decrement the in-flight counter and set ``_drained`` if quiescent."""
        async with self._inflight_lock:
            self._inflight -= 1
            if self._inflight <= 0 and self._queue.empty():
                self._drained.set()

    @property
    def approximate_size(self) -> int:
        return self._queue.qsize()

    @property
    def in_flight(self) -> int:
        return self._inflight

    async def join(self, *, timeout: float | None = None) -> bool:
        """Wait for the queue to be both empty and quiescent.

        Returns ``True`` if the drain completed within the timeout,
        ``False`` on timeout. Tests use this to wait for the worker
        to fully process before asserting.
        """
        try:
            if timeout is None:
                await self._drained.wait()
            else:
                await asyncio.wait_for(self._drained.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        return True
