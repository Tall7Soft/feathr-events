from __future__ import annotations

import asyncio
from typing import AsyncIterator

import fakeredis
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.health import router as health_router
from app.api.ingestion import router as ingestion_router
from app.api.queries import router as queries_router
from app.cache import RealtimeStatsCache
from app.config import Settings
from app.lifecycle import attach_state
from app.queue import InMemoryQueue
from app.storage.fakes import InMemoryEventSearch, InMemoryEventStore
from app.workers import EventWorker


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        mongo_url="mongodb://test",
        elasticsearch_url="http://test",
        redis_url="redis://test",
        worker_concurrency=2,
        worker_batch_size=10,
        worker_batch_timeout_seconds=0.05,
        queue_max_receive_count=3,
        queue_base_retry_delay_seconds=0.01,
        realtime_cache_ttl_seconds=2,
        realtime_window_seconds=60,
        rate_limit_per_minute=10_000,
    )


@pytest_asyncio.fixture
async def fake_redis() -> AsyncIterator[fakeredis.FakeAsyncRedis]:
    r = fakeredis.FakeAsyncRedis(decode_responses=False)
    try:
        yield r
    finally:
        await r.aclose()


@pytest_asyncio.fixture
async def stack(
    test_settings: Settings,
    fake_redis: fakeredis.FakeAsyncRedis,
) -> AsyncIterator[dict]:
    store = InMemoryEventStore()
    search = InMemoryEventSearch()
    cache = RealtimeStatsCache(fake_redis, ttl_seconds=test_settings.realtime_cache_ttl_seconds)
    queue = InMemoryQueue("events")
    dlq = InMemoryQueue("events-dlq")
    worker = EventWorker(
        queue=queue,
        dlq=dlq,
        store=store,
        search=search,
        concurrency=test_settings.worker_concurrency,
        batch_size=test_settings.worker_batch_size,
        batch_timeout=test_settings.worker_batch_timeout_seconds,
        max_receive_count=test_settings.queue_max_receive_count,
        base_retry_delay=test_settings.queue_base_retry_delay_seconds,
    )
    await worker.start()
    try:
        yield {
            "settings": test_settings,
            "store": store,
            "search": search,
            "cache": cache,
            "queue": queue,
            "dlq": dlq,
            "worker": worker,
        }
    finally:
        await worker.stop(drain_timeout=1.0)


@pytest_asyncio.fixture
async def app(stack: dict) -> FastAPI:
    application = FastAPI()
    application.include_router(health_router)
    application.include_router(ingestion_router)
    application.include_router(queries_router)
    attach_state(application, **stack)
    return application


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# Re-export drain_queue for tests that prefer importing from conftest.
from tests.helpers import drain_queue  # noqa: E402,F401
