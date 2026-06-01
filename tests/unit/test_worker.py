import asyncio
from datetime import datetime, timezone

import pytest

from app.models.event import StoredEvent
from app.queue import InMemoryQueue
from app.storage.fakes import InMemoryEventSearch, InMemoryEventStore
from app.workers import EventWorker


def _event(event_type: str = "click", event_id: str | None = None) -> dict:
    e = StoredEvent(
        event_type=event_type,
        user_id="u1",
        source_url="https://example.com",
        timestamp=datetime.now(timezone.utc),
        metadata={"k": "v"},
    )
    if event_id:
        e.event_id = event_id
    return e.model_dump(mode="json")


async def _run_worker(worker: EventWorker, queue: InMemoryQueue) -> None:
    await worker.start()
    await queue.join(timeout=2.0)
    await asyncio.sleep(0.05)
    await worker.stop(drain_timeout=1.0)


async def test_worker_persists_to_store_and_search():
    q, dlq = InMemoryQueue("e"), InMemoryQueue("d")
    store, search = InMemoryEventStore(), InMemoryEventSearch()
    worker = EventWorker(
        queue=q,
        dlq=dlq,
        store=store,
        search=search,
        concurrency=1,
        batch_size=5,
        batch_timeout=0.05,
        max_receive_count=2,
        base_retry_delay=0.01,
    )
    for _ in range(3):
        await q.send(_event())
    await _run_worker(worker, q)
    assert worker.processed_count == 3
    assert len(store._events) == 3
    assert len(search._events) == 3


async def test_worker_dedupes_repeated_event_ids():
    q, dlq = InMemoryQueue("e"), InMemoryQueue("d")
    store, search = InMemoryEventStore(), InMemoryEventSearch()
    worker = EventWorker(
        queue=q,
        dlq=dlq,
        store=store,
        search=search,
        concurrency=1,
        batch_size=5,
        batch_timeout=0.05,
        max_receive_count=2,
        base_retry_delay=0.01,
    )
    payload = _event(event_id="abc")
    await q.send(payload)
    await q.send(payload)
    await _run_worker(worker, q)
    assert worker.processed_count == 1
    assert worker.duplicate_count == 1
    assert len(store._events) == 1


async def test_worker_retries_then_dead_letters_on_store_failure():
    q, dlq = InMemoryQueue("e"), InMemoryQueue("d")
    search = InMemoryEventSearch()

    class FlakyStore:
        calls = 0

        async def ensure_ready(self):
            return None

        async def insert_many(self, events):
            type(self).calls += 1
            raise RuntimeError("mongo down")

    worker = EventWorker(
        queue=q,
        dlq=dlq,
        store=FlakyStore(),
        search=search,
        concurrency=1,
        batch_size=1,
        batch_timeout=0.01,
        max_receive_count=2,
        base_retry_delay=0.01,
    )
    await q.send(_event())
    await _run_worker(worker, q)
    assert worker.dead_letter_count == 1
    assert dlq.approximate_size == 1


async def test_worker_acks_when_es_fails_but_mongo_succeeds():
    q, dlq = InMemoryQueue("e"), InMemoryQueue("d")
    store = InMemoryEventStore()

    class BadSearch:
        async def ensure_ready(self):
            return None

        async def index_many(self, events):
            raise RuntimeError("es down")

        async def search(self, q):
            return []

        async def close(self):
            return None

    worker = EventWorker(
        queue=q,
        dlq=dlq,
        store=store,
        search=BadSearch(),
        concurrency=1,
        batch_size=2,
        batch_timeout=0.05,
        max_receive_count=2,
        base_retry_delay=0.01,
    )
    await q.send(_event())
    await _run_worker(worker, q)
    # Mongo is authoritative; failing ES should not re-queue or DLQ.
    assert worker.processed_count == 1
    assert worker.dead_letter_count == 0
    assert len(store._events) == 1


async def test_worker_handles_mixed_valid_and_invalid_payloads_in_batch():
    """A poison-pill payload must not block valid siblings in the same batch."""
    q, dlq = InMemoryQueue("e"), InMemoryQueue("d")
    store, search = InMemoryEventStore(), InMemoryEventSearch()
    worker = EventWorker(
        queue=q,
        dlq=dlq,
        store=store,
        search=search,
        concurrency=1,
        batch_size=4,
        batch_timeout=0.05,
        max_receive_count=2,
        base_retry_delay=0.01,
    )
    await q.send(_event(event_type="click"))
    await q.send({"this": "is", "not": "a valid event"})
    await q.send(_event(event_type="pageview"))
    await _run_worker(worker, q)
    assert worker.processed_count == 2
    assert worker.dead_letter_count == 1
    assert len(store._events) == 2
    assert dlq.approximate_size == 1


async def test_worker_dead_letters_invalid_payloads_immediately():
    q, dlq = InMemoryQueue("e"), InMemoryQueue("d")
    store, search = InMemoryEventStore(), InMemoryEventSearch()
    worker = EventWorker(
        queue=q,
        dlq=dlq,
        store=store,
        search=search,
        concurrency=1,
        batch_size=2,
        batch_timeout=0.05,
        max_receive_count=3,
        base_retry_delay=0.01,
    )
    await q.send({"not_a_valid": "event"})
    await _run_worker(worker, q)
    assert worker.dead_letter_count == 1
    assert dlq.approximate_size == 1
    assert len(store._events) == 0
