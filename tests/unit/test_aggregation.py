from datetime import datetime, timedelta, timezone

import pytest

from app.models.event import StoredEvent
from app.storage.fakes import InMemoryEventStore


def _make(event_type: str, ts: datetime) -> StoredEvent:
    return StoredEvent(
        event_type=event_type,
        user_id="u",
        source_url="https://example.com",
        timestamp=ts,
        metadata={},
    )


async def test_stats_buckets_by_hour():
    store = InMemoryEventStore()
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    await store.insert_many(
        [
            _make("click", base + timedelta(minutes=5)),
            _make("click", base + timedelta(minutes=30)),
            _make("click", base + timedelta(hours=1, minutes=10)),
            _make("pageview", base + timedelta(minutes=10)),
        ]
    )
    rows = await store.stats(
        start=base, end=base + timedelta(hours=3), bucket="hour"
    )
    by_key = {(r["event_type"], r["bucket_start"].hour): r["count"] for r in rows}
    assert by_key[("click", 12)] == 2
    assert by_key[("click", 13)] == 1
    assert by_key[("pageview", 12)] == 1


async def test_stats_excludes_events_outside_window():
    store = InMemoryEventStore()
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    await store.insert_many(
        [
            _make("click", base - timedelta(hours=1)),
            _make("click", base + timedelta(minutes=5)),
        ]
    )
    rows = await store.stats(
        start=base, end=base + timedelta(hours=1), bucket="hour"
    )
    assert sum(r["count"] for r in rows) == 1


async def test_realtime_counts_groups_by_type():
    store = InMemoryEventStore()
    now = datetime.now(timezone.utc)
    await store.insert_many(
        [
            _make("click", now - timedelta(seconds=10)),
            _make("click", now - timedelta(seconds=20)),
            _make("pageview", now - timedelta(seconds=30)),
        ]
    )
    counts = await store.realtime_counts(
        since=now - timedelta(minutes=1), until=now
    )
    assert counts == {"click": 2, "pageview": 1}
