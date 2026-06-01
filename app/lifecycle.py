"""Application startup / shutdown orchestration.

Kept separate from ``main.py`` so tests can build a tailored ``app.state``
(with in-memory fakes) without standing up real Mongo / ES / Redis.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

import redis.asyncio as redis
from elasticsearch import AsyncElasticsearch
from fastapi import FastAPI
from motor.motor_asyncio import AsyncIOMotorClient

from app.cache import RealtimeStatsCache
from app.config import Settings, get_settings
from app.logging_config import configure_logging
from app.queue import InMemoryQueue
from app.storage.elasticsearch_repo import ESEventSearch
from app.storage.mongo import MongoEventStore
from app.workers import EventWorker

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Build everything the running app needs, then tear it down cleanly.

    On startup:

    1. Load + log settings.
    2. Open the three external clients (Mongo via Motor with both
       timeouts pinned, async Elasticsearch, async Redis).
    3. Ping Mongo and create indexes via ``store.ensure_ready()``;
       check cluster health and create the ES index mapping.
    4. Build the queue, DLQ, and worker pool, then start the worker.
    5. Stash everything on ``app.state`` so the DI providers in
       :mod:`app.api.deps` can hand each route what it needs.

    On shutdown:

    * Stop the worker (drains in-flight messages within
      ``drain_timeout`` before cancelling tasks).
    * Close the ES + Redis clients.
    """
    settings = get_settings()
    configure_logging(settings.log_level)
    logger.info("app.starting", extra={"mongo_db": settings.mongo_db})

    # Fast-fail server selection AND socket reads so a Mongo outage
    # triggers the worker's retry/DLQ paths within seconds instead of
    # blocking on the default 30 s timeout. socketTimeoutMS is the one
    # that matters when a previously-healthy server stops responding
    # mid-operation (e.g. SIGSTOP / network partition).
    mongo_client = AsyncIOMotorClient(
        settings.mongo_url,
        uuidRepresentation="standard",
        serverSelectionTimeoutMS=settings.mongo_server_selection_timeout_ms,
        socketTimeoutMS=settings.mongo_socket_timeout_ms,
    )
    es_client = AsyncElasticsearch(settings.elasticsearch_url, request_timeout=10)
    redis_client = redis.from_url(settings.redis_url, decode_responses=False)

    store = MongoEventStore(mongo_client, settings.mongo_db)
    search = ESEventSearch(es_client, settings.elasticsearch_index)
    cache = RealtimeStatsCache(redis_client, ttl_seconds=settings.realtime_cache_ttl_seconds)

    await store.ensure_ready()
    await search.ensure_ready()

    queue = InMemoryQueue("events")
    dlq = InMemoryQueue("events-dlq")
    worker = EventWorker(
        queue=queue,
        dlq=dlq,
        store=store,
        search=search,
        concurrency=settings.worker_concurrency,
        batch_size=settings.worker_batch_size,
        batch_timeout=settings.worker_batch_timeout_seconds,
        max_receive_count=settings.queue_max_receive_count,
        base_retry_delay=settings.queue_base_retry_delay_seconds,
    )
    await worker.start()

    app.state.settings = settings
    app.state.queue = queue
    app.state.dlq = dlq
    app.state.store = store
    app.state.search = search
    app.state.cache = cache
    app.state.worker = worker
    app.state.mongo_client = mongo_client
    app.state.es_client = es_client
    app.state.redis_client = redis_client

    try:
        yield
    finally:
        logger.info("app.stopping")
        await worker.stop()
        await search.close()
        await store.close()
        await redis_client.aclose()


def attach_state(
    app: FastAPI,
    *,
    settings: Settings,
    queue: InMemoryQueue,
    dlq: InMemoryQueue,
    store,
    search,
    cache: RealtimeStatsCache,
    worker: EventWorker,
) -> None:
    """Test helper: wire fakes into ``app.state`` without the real lifespan.

    Mirrors what :func:`lifespan` does on startup, but bypasses the
    real Mongo / ES / Redis clients. Test fixtures call this to drive
    the FastAPI app through ``httpx.ASGITransport`` against in-memory
    fakes.
    """
    app.state.settings = settings
    app.state.queue = queue
    app.state.dlq = dlq
    app.state.store = store
    app.state.search = search
    app.state.cache = cache
    app.state.worker = worker
