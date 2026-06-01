"""Shared test helpers (imported by both unit and integration tests)."""

import asyncio

from app.queue import InMemoryQueue


async def drain_queue(queue: InMemoryQueue, *, timeout: float = 2.0) -> None:
    drained = await queue.join(timeout=timeout)
    if not drained:
        raise AssertionError("queue did not drain within timeout")
    # Give the worker an extra tick to finish post-ack bookkeeping
    # (counter updates, search index writes, etc.).
    await asyncio.sleep(0.05)
