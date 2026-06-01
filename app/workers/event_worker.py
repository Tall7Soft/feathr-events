"""Background worker that drains the ingestion queue into the stores.

Design notes:

* The worker batches messages with a max-size/max-wait trigger so we get
  the throughput win of bulk writes without sacrificing latency for
  low-volume periods.
* Persistence ordering is Mongo first, Elasticsearch second. Mongo is
  the source of truth; ES is a secondary index whose contents can be
  rebuilt from Mongo if it lags or loses data. A partial failure where
  Mongo succeeded but ES failed is logged and the message is acked —
  re-driving from Mongo is a recovery operation, not a per-message retry.
* Retries are bounded; messages that exhaust ``max_receive_count`` are
  forwarded to the DLQ for offline triage.
* A bounded in-memory dedupe set keys on ``event_id`` to swallow
  redeliveries from the queue itself; this is best-effort and only meant
  to harden against the at-least-once semantics SQS provides.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from typing import Protocol

from app.models.event import StoredEvent
from app.queue import InMemoryQueue, QueueMessage

logger = logging.getLogger(__name__)


class EventStoreLike(Protocol):
    async def insert_many(self, events: list[StoredEvent]) -> int: ...


class EventSearchLike(Protocol):
    async def index_many(self, events: list[StoredEvent]) -> int: ...


class _BoundedSet:
    """LRU-evicting set for cheap, bounded redelivery suppression."""

    def __init__(self, max_size: int = 10_000) -> None:
        self._items: OrderedDict[str, None] = OrderedDict()
        self._max = max_size

    def add(self, key: str) -> bool:
        if key in self._items:
            self._items.move_to_end(key)
            return False
        self._items[key] = None
        if len(self._items) > self._max:
            self._items.popitem(last=False)
        return True

    def __contains__(self, key: str) -> bool:
        return key in self._items


class EventWorker:
    """The background consumer that drains the queue into Mongo + ES.

    Owns four concerns kept in one place because they need to share
    state and a single asyncio loop:

    * **Batching** — accumulates up to ``batch_size`` messages or
      waits up to ``batch_timeout`` seconds, whichever comes first.
    * **Validation + dedupe** — invalid payloads are DLQ'd
      immediately, duplicates are suppressed against the bounded
      in-memory LRU set (committed keys only — see
      :meth:`_process_batch`).
    * **Two-stage write** — Mongo first (with retry+backoff on
      failure), Elasticsearch second (best-effort, ack the message
      regardless because Mongo is authoritative).
    * **Retry policy** — exponential backoff on Mongo failures, DLQ
      after ``max_receive_count`` attempts.

    Counters (``processed_count``, ``failed_count``,
    ``dead_letter_count``, ``duplicate_count``) are exposed via
    ``/health/queue`` for ops.
    """

    def __init__(
        self,
        *,
        queue: InMemoryQueue,
        dlq: InMemoryQueue,
        store: EventStoreLike,
        search: EventSearchLike,
        concurrency: int = 4,
        batch_size: int = 50,
        batch_timeout: float = 1.0,
        max_receive_count: int = 3,
        base_retry_delay: float = 1.0,
        dedupe_cache_size: int = 10_000,
    ) -> None:
        """Wire the worker up against its dependencies.

        Nothing starts running until :meth:`start` is called. All
        knobs default to the values used in production; tests tune
        them down for fast feedback.
        """
        self.queue = queue
        self.dlq = dlq
        self.store = store
        self.search = search
        self.concurrency = concurrency
        self.batch_size = batch_size
        self.batch_timeout = batch_timeout
        self.max_receive_count = max_receive_count
        self.base_retry_delay = base_retry_delay

        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._dedupe = _BoundedSet(dedupe_cache_size)
        self.processed_count = 0
        self.failed_count = 0
        self.dead_letter_count = 0
        self.duplicate_count = 0

    async def start(self) -> None:
        """Spawn the configured number of worker loops.

        Idempotent: calling ``start`` while workers are already
        running is a no-op, so the lifespan can call it without
        guarding.
        """
        if self._tasks:
            return
        self._stop.clear()
        for i in range(self.concurrency):
            self._tasks.append(asyncio.create_task(self._run_loop(i), name=f"worker-{i}"))
        logger.info("worker.started", extra={"concurrency": self.concurrency})

    async def stop(self, *, drain_timeout: float = 5.0) -> None:
        """Drain in-flight messages, then cancel the worker tasks.

        Idempotent. Called from the FastAPI lifespan shutdown so a
        graceful pod restart finishes the messages currently being
        processed instead of losing them.
        """
        if not self._tasks:
            return
        await self.queue.join(timeout=drain_timeout)
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("worker.stopped")

    async def _run_loop(self, worker_id: int) -> None:
        while not self._stop.is_set():
            batch = await self._collect_batch()
            if not batch:
                continue
            await self._process_batch(batch, worker_id=worker_id)

    async def _collect_batch(self) -> list[QueueMessage]:
        """Accumulate messages until ``batch_size`` or ``batch_timeout``.

        The first ``receive`` waits up to 500 ms so the loop wakes up
        periodically to honor a shutdown signal. Once a message is in
        hand, the rest of the batch is filled in within
        ``batch_timeout`` seconds or stops early on the next timeout.
        """
        batch: list[QueueMessage] = []
        try:
            first = await asyncio.wait_for(self.queue.receive(), timeout=0.5)
        except asyncio.TimeoutError:
            return batch
        batch.append(first)

        deadline = asyncio.get_running_loop().time() + self.batch_timeout
        while len(batch) < self.batch_size:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                msg = await asyncio.wait_for(self.queue.receive(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            batch.append(msg)
        return batch

    async def _process_batch(self, batch: list[QueueMessage], *, worker_id: int) -> None:
        """Validate, dedupe, persist, and ack/nack a batch.

        Stages, in order:

        1. **Validate** each payload against :class:`StoredEvent`.
           Bad payloads go to the DLQ immediately.
        2. **Dedupe** against the in-batch set and the committed-LRU
           set. Hits get ack'd and counted as duplicates.
        3. **Persist to Mongo** via ``insert_many(ordered=False)``.
           On failure, the whole batch is NACK'd with backoff or DLQ'd
           if retries are exhausted.
        4. **Mark dedupe** only after the Mongo commit succeeds, so a
           retry-after-failure is not falsely treated as a duplicate.
        5. **Best-effort ES index**. Failures are logged but the
           message is still ack'd because Mongo is authoritative and
           a reindex job can rebuild ES.
        6. **Ack** the surviving fresh messages.
        """
        fresh: list[tuple[QueueMessage, StoredEvent]] = []
        seen_in_batch: set[str] = set()
        for msg in batch:
            try:
                event = StoredEvent.model_validate(msg.body)
            except Exception as exc:
                # Bad payloads never become processable; ship to DLQ now.
                logger.exception(
                    "worker.invalid_payload",
                    extra={"worker_id": worker_id, "error": str(exc)},
                )
                await self._send_to_dlq(msg, reason="invalid_payload")
                await self.queue.ack(msg)
                continue

            key = event.dedupe_key()
            # Two dedupe layers: in-batch (same id appears twice within the
            # current flush) and cross-batch (we already committed this id
            # in a previous flush). Both layers only consult committed
            # state, so a retry after a failed insert is NOT suppressed.
            if key in seen_in_batch or key in self._dedupe:
                self.duplicate_count += 1
                await self.queue.ack(msg)
                continue
            seen_in_batch.add(key)

            fresh.append((msg, event))

        if not fresh:
            return

        events = [e for _, e in fresh]
        try:
            await self.store.insert_many(events)
        except Exception as exc:
            logger.exception(
                "worker.mongo_insert_failed",
                extra={"worker_id": worker_id, "batch_size": len(events), "error": str(exc)},
            )
            for msg, _ in fresh:
                await self._retry_or_dlq(msg, reason="mongo_failed")
            self.failed_count += len(fresh)
            return

        # Only record dedupe keys after a successful commit. If we marked
        # them eagerly, a retry of a failed batch would be mistaken for a
        # duplicate and silently dropped.
        for _, event in fresh:
            self._dedupe.add(event.dedupe_key())

        # ES is best-effort: if it fails, Mongo is still authoritative and
        # we can rebuild the index from there. We do NOT re-queue the
        # message — that would double-write to Mongo on retry.
        try:
            await self.search.index_many(events)
        except Exception as exc:
            logger.warning(
                "worker.es_index_failed",
                extra={
                    "worker_id": worker_id,
                    "batch_size": len(events),
                    "error": str(exc),
                    "recovery": "rebuild_from_mongo",
                },
            )

        for msg, _ in fresh:
            await self.queue.ack(msg)
        self.processed_count += len(fresh)

    async def _retry_or_dlq(self, msg: QueueMessage, *, reason: str) -> None:
        """NACK with exponential backoff, or DLQ if retries are exhausted.

        Delay grows as ``base_retry_delay * 2^(receive_count - 1)``,
        so attempts at default settings happen at ~1 s, ~2 s, ~4 s.
        After ``max_receive_count`` attempts the message lands in the
        DLQ for ops to inspect.
        """
        if msg.receive_count >= self.max_receive_count:
            await self._send_to_dlq(msg, reason=reason)
            await self.queue.ack(msg)
            return
        delay = self.base_retry_delay * (2 ** (msg.receive_count - 1))
        await self.queue.nack(msg, delay=delay)

    async def _send_to_dlq(self, msg: QueueMessage, *, reason: str) -> None:
        """Forward a message to the dead-letter queue with provenance.

        The DLQ payload includes ``reason`` (e.g.
        ``"invalid_payload"``, ``"mongo_failed"``) so an operator
        replaying the DLQ can route to the right runbook.
        """
        await self.dlq.send(
            {
                "body": msg.body,
                "message_id": msg.message_id,
                "receive_count": msg.receive_count,
                "reason": reason,
            }
        )
        self.dead_letter_count += 1
        logger.error(
            "worker.dead_lettered",
            extra={"message_id": msg.message_id, "reason": reason},
        )
