"""Observability endpoints.

Two routes, both exempt from the rate limiter so uptime monitors and
the container healthcheck cannot be locked out under load.

* ``GET /health`` — process liveness, used by docker-compose and
  external monitors.
* ``GET /health/queue`` — queue depth, worker counters, DLQ depth.
  This is the ops-facing diagnostic surface; pinned in
  :class:`tests.integration.test_search_and_realtime` so a future
  refactor that drops a counter is caught immediately.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_dlq, get_queue, get_worker
from app.queue import InMemoryQueue
from app.workers import EventWorker

router = APIRouter(tags=["health"])


@router.get("/health", summary="Liveness probe")
async def health() -> dict[str, str]:
    """Always-on liveness probe used by container + external monitors."""
    return {"status": "ok"}


@router.get("/health/queue", summary="Queue + worker observability snapshot")
async def queue_health(
    queue: InMemoryQueue = Depends(get_queue),
    dlq: InMemoryQueue = Depends(get_dlq),
    worker: EventWorker = Depends(get_worker),
) -> dict[str, int | str]:
    """Snapshot of queue + worker state for dashboards and on-call.

    Returns counters from the worker (processed / failed /
    dead_lettered / duplicates_suppressed) alongside live queue depth
    + in-flight + DLQ depth. Cheap enough to poll once a second.
    """
    return {
        "queue_depth": queue.approximate_size,
        "queue_in_flight": queue.in_flight,
        "dlq_depth": dlq.approximate_size,
        "processed": worker.processed_count,
        "failed": worker.failed_count,
        "dead_lettered": worker.dead_letter_count,
        "duplicates_suppressed": worker.duplicate_count,
    }
