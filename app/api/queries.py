"""Read-only HTTP endpoints.

Four routes here, all GET, all thin: parse + validate the query
string, delegate to the store / search / cache, return the result.
Business logic lives behind the protocols, not in the route
handlers, so swapping storage implementations is invisible to the
HTTP surface.

* ``GET /events`` — list + filter against the primary store.
* ``GET /events/stats`` — aggregation pipeline grouped by time bucket.
* ``GET /events/search`` — full-text search via Elasticsearch.
* ``GET /events/stats/realtime`` — Redis-cached realtime summary.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import get_cache, get_search, get_settings, get_store
from app.cache import RealtimeStatsCache
from app.config import Settings
from app.models.event import Bucket
from app.storage.protocols import EventQuery, EventSearchQuery, EventSearchStore, EventStore

router = APIRouter(tags=["queries"])


@router.get("/events", summary="List events with filters")
async def list_events(
    event_type: str | None = Query(default=None, max_length=64),
    user_id: str | None = Query(default=None, max_length=128),
    source_url: str | None = Query(default=None, max_length=2048),
    start: datetime | None = None,
    end: datetime | None = None,
    skip: int = Query(default=0, ge=0, le=100_000),
    limit: int = Query(default=100, ge=1, le=1000),
    sort: Literal["asc", "desc"] = "desc",
    store: EventStore = Depends(get_store),
) -> dict[str, Any]:
    """List events matching the supplied filters.

    All filters AND-combine. Omitted filters mean "no constraint on
    this dimension". Returns ``{items, count, skip, limit}`` so a
    client can drive pagination without a separate count call.
    """
    _validate_window(start, end)
    rows = await store.find(
        EventQuery(
            event_type=event_type.lower() if event_type else None,
            user_id=user_id,
            source_url=source_url,
            start=start,
            end=end,
            skip=skip,
            limit=limit,
            sort=sort,
        )
    )
    return {"items": rows, "count": len(rows), "skip": skip, "limit": limit}


@router.get("/events/stats", summary="Aggregated event counts bucketed by time")
async def event_stats(
    start: datetime,
    end: datetime,
    bucket: Bucket = "day",
    store: EventStore = Depends(get_store),
) -> dict[str, Any]:
    """Counts grouped by ``event_type`` and the requested time bucket.

    ``start`` and ``end`` are both required and form a half-open
    window. ``bucket`` is one of ``hour`` / ``day`` / ``week`` —
    weekly buckets align to Monday (ISO 8601).
    """
    _validate_window(start, end, require_both=True)
    if (end - start).total_seconds() <= 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="end must be strictly greater than start",
        )
    rows = await store.stats(start=start, end=end, bucket=bucket)
    return {"bucket": bucket, "start": start, "end": end, "items": rows}


@router.get("/events/search", summary="Full-text search over event metadata")
async def search_events(
    q: str = Query(min_length=1, max_length=512),
    event_type: str | None = Query(default=None, max_length=64),
    user_id: str | None = Query(default=None, max_length=128),
    start: datetime | None = None,
    end: datetime | None = None,
    size: int = Query(default=20, ge=1, le=200),
    search: EventSearchStore = Depends(get_search),
) -> dict[str, Any]:
    """Full-text search over ``metadata.*`` and ``source_url.text``.

    ``q`` is required. Structured filters (``event_type``,
    ``user_id``, date window) compose with the free-text query.
    Returns ``{items, count}``.
    """
    _validate_window(start, end)
    rows = await search.search(
        EventSearchQuery(
            q=q,
            event_type=event_type.lower() if event_type else None,
            user_id=user_id,
            start=start,
            end=end,
            size=size,
        )
    )
    return {"items": rows, "count": len(rows)}


@router.get("/events/stats/realtime", summary="Recent-window stats served from Redis")
async def realtime_stats(
    window_seconds: int | None = Query(default=None, ge=10, le=86400),
    settings: Settings = Depends(get_settings),
    store: EventStore = Depends(get_store),
    cache: RealtimeStatsCache = Depends(get_cache),
) -> dict[str, Any]:
    """Lightweight realtime summary backed by a Redis cache.

    Returns ``{cache_hit, window_seconds, total, by_type,
    generated_at}``. The cache is single-flighted server-side so
    concurrent callers do not dogpile the Mongo aggregation on TTL
    expiry. Eventual consistency on the order of the configured TTL
    (default 10 s) is the contract.
    """
    window = window_seconds or settings.realtime_window_seconds

    async def compute() -> dict[str, Any]:
        """Recompute the summary from Mongo when the cache misses."""
        now = datetime.now(timezone.utc)
        since = now - timedelta(seconds=window)
        counts = await store.realtime_counts(since=since, until=now)
        return {
            "window_seconds": window,
            "total": sum(counts.values()),
            "by_type": counts,
            "generated_at": now,
        }

    payload, cache_hit = await cache.get_or_compute(window, compute)
    return {"cache_hit": cache_hit, **payload}


def _validate_window(
    start: datetime | None,
    end: datetime | None,
    *,
    require_both: bool = False,
) -> None:
    """Reject invalid time windows with a 422 before they hit storage.

    With ``require_both=True`` (used by ``/events/stats``), missing
    either bound is a 422. Whenever both bounds are present, an
    ``end <= start`` window is also a 422.
    """
    if require_both and (start is None or end is None):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="start and end are required",
        )
    if start and end and start >= end:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="end must be strictly greater than start",
        )
