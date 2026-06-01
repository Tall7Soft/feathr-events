"""In-memory implementations of the storage protocols.

Used by tests (and the in-process docker-less path) to exercise the full
ingest pipeline without standing up Mongo or Elasticsearch. The behavior
is intentionally a simplified-but-faithful subset: same query semantics
for the filters we actually use, so tests catch real logic bugs rather
than mock-shaped ones.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from app.models.event import Bucket, StoredEvent
from app.storage.protocols import EventQuery, EventSearchQuery


def _truncate(ts: datetime, bucket: Bucket) -> datetime:
    """Return the start of the bucket window that ``ts`` falls into.

    Mirrors Mongo's ``$dateTrunc`` semantics including the explicit
    Monday-start week boundary used by :class:`MongoEventStore`.
    """
    ts = ts.astimezone(timezone.utc)
    if bucket == "hour":
        return ts.replace(minute=0, second=0, microsecond=0)
    if bucket == "day":
        return ts.replace(hour=0, minute=0, second=0, microsecond=0)
    # week starts on Monday (ISO)
    start_of_day = ts.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_of_day - timedelta(days=start_of_day.weekday())


class InMemoryEventStore:
    """In-memory :class:`EventStore`. Mirrors :class:`MongoEventStore`
    semantics (half-open windows, dedupe on ``event_id``, sort by
    timestamp) so integration tests can swap it in without changing a
    single assertion.

    Not thread-safe by design — tests drive it from a single asyncio
    loop, same as the production worker.
    """

    def __init__(self) -> None:
        self._events: list[dict[str, Any]] = []
        self._seen_ids: set[str] = set()

    async def ensure_ready(self) -> None:
        return None

    async def insert_many(self, events: list[StoredEvent]) -> int:
        inserted = 0
        for e in events:
            if e.event_id in self._seen_ids:
                continue
            self._seen_ids.add(e.event_id)
            self._events.append(e.model_dump())
            inserted += 1
        return inserted

    async def find(self, q: EventQuery) -> list[dict[str, Any]]:
        results = [e for e in self._events if self._matches(e, q)]
        results.sort(key=lambda e: e["timestamp"], reverse=(q.sort == "desc"))
        return results[q.skip : q.skip + q.limit]

    async def stats(
        self, *, start: datetime, end: datetime, bucket: Bucket
    ) -> list[dict[str, Any]]:
        buckets: dict[tuple[datetime, str], int] = defaultdict(int)
        for e in self._events:
            ts = e["timestamp"]
            if not (start <= ts < end):
                continue
            key = (_truncate(ts, bucket), e["event_type"])
            buckets[key] += 1
        rows = [
            {"bucket_start": bs, "event_type": et, "count": c}
            for (bs, et), c in buckets.items()
        ]
        rows.sort(key=lambda r: (r["bucket_start"], r["event_type"]))
        return rows

    async def realtime_counts(
        self, *, since: datetime, until: datetime
    ) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for e in self._events:
            if since <= e["timestamp"] < until:
                counts[e["event_type"]] += 1
        return dict(counts)

    async def close(self) -> None:
        return None

    @staticmethod
    def _matches(doc: dict[str, Any], q: EventQuery) -> bool:
        if q.event_type and doc["event_type"] != q.event_type:
            return False
        if q.user_id and doc["user_id"] != q.user_id:
            return False
        if q.source_url and doc["source_url"] != q.source_url:
            return False
        if q.start and doc["timestamp"] < q.start:
            return False
        if q.end and doc["timestamp"] >= q.end:
            return False
        return True


class InMemoryEventSearch:
    """In-memory :class:`EventSearchStore`.

    Substring match over ``source_url`` plus metadata string values.
    Coarser than real ES, but faithful enough that the worker's
    "ack on ES failure" path and the search endpoint's filter
    combinations get exercised in integration tests without standing
    up a container.
    """

    def __init__(self) -> None:
        self._events: dict[str, dict[str, Any]] = {}

    async def ensure_ready(self) -> None:
        return None

    async def index_many(self, events: list[StoredEvent]) -> int:
        for e in events:
            self._events[e.event_id] = e.model_dump()
        return len(events)

    async def search(self, q: EventSearchQuery) -> list[dict[str, Any]]:
        needle = q.q.strip().lower()
        results: list[dict[str, Any]] = []
        for doc in self._events.values():
            if q.event_type and doc["event_type"] != q.event_type:
                continue
            if q.user_id and doc["user_id"] != q.user_id:
                continue
            if q.start and doc["timestamp"] < q.start:
                continue
            if q.end and doc["timestamp"] >= q.end:
                continue
            if needle and not self._contains(doc, needle):
                continue
            results.append(doc)
        results.sort(key=lambda d: d["timestamp"], reverse=True)
        return results[: q.size]

    async def close(self) -> None:
        return None

    @staticmethod
    def _contains(doc: dict[str, Any], needle: str) -> bool:
        if needle in doc["source_url"].lower():
            return True
        for v in doc.get("metadata", {}).values():
            if isinstance(v, str) and needle in v.lower():
                return True
        return False
