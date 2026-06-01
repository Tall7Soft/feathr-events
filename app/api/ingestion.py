"""Write-side HTTP endpoint.

One route: ``POST /events``. Validates the payload, enriches it with a
server-generated ``event_id`` + timestamp, enqueues for async
processing, and returns ``202 Accepted``. Persistence is the worker's
job — this route never touches Mongo or ES directly.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from app.api.deps import get_queue
from app.models.event import EventIn, StoredEvent
from app.queue import InMemoryQueue

router = APIRouter(tags=["ingestion"])


@router.post(
    "/events",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Enqueue an event for async processing",
)
async def ingest_event(
    payload: EventIn,
    queue: InMemoryQueue = Depends(get_queue),
) -> dict[str, str]:
    """The HTTP layer's job is validation + enqueue.

    The 202 contract is intentional: callers receive an opaque event_id
    they can use to correlate downstream, but the persistence happens in
    the worker. This keeps p99 request latency bounded by validation +
    queue put, not by Mongo + ES write latency.
    """
    event = StoredEvent.from_input(payload)
    await queue.send(event.model_dump(mode="json"))
    return {"status": "accepted", "event_id": event.event_id}
