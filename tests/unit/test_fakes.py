"""Tests for the in-memory storage fakes themselves.

The fakes (``InMemoryEventStore``, ``InMemoryEventSearch``) underpin
every integration test. If a filter predicate silently regresses inside
a fake, integration tests stay green while testing the wrong thing.
This module exercises each predicate directly so the fakes' guarantees
match the real Mongo / ES query semantics they stand in for.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.models.event import StoredEvent
from app.storage.fakes import (
    InMemoryEventSearch,
    InMemoryEventStore,
    _truncate,
)
from app.storage.protocols import EventQuery, EventSearchQuery


def _event(
    *,
    event_id: str = "x",
    event_type: str = "click",
    user_id: str = "u1",
    source_url: str = "https://feathr.co/a",
    timestamp: datetime | None = None,
    metadata: dict | None = None,
) -> StoredEvent:
    return StoredEvent(
        event_id=event_id,
        event_type=event_type,
        user_id=user_id,
        source_url=source_url,
        timestamp=timestamp or datetime(2026, 5, 31, 12, tzinfo=timezone.utc),
        metadata=metadata or {},
    )


# ---------------------------------------------------------------------------
# _truncate: week alignment
# ---------------------------------------------------------------------------


def test_truncate_week_aligns_to_monday_iso():
    """A Wednesday rounds down to the Monday of the same ISO week —
    matches the explicit Monday pin in MongoEventStore.stats."""
    wed = datetime(2026, 5, 27, 14, 30, tzinfo=timezone.utc)  # Wed
    out = _truncate(wed, "week")
    assert out.weekday() == 0  # Monday
    assert out == datetime(2026, 5, 25, tzinfo=timezone.utc)


def test_truncate_week_on_monday_is_identity():
    mon_noon = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)
    out = _truncate(mon_noon, "week")
    assert out == datetime(2026, 5, 25, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# InMemoryEventStore — lifecycle no-ops + dedupe
# ---------------------------------------------------------------------------


async def test_in_memory_store_ensure_ready_is_safe_noop():
    """Lets test fixtures call the same lifecycle hooks as production code."""
    store = InMemoryEventStore()
    assert await store.ensure_ready() is None


async def test_in_memory_store_close_is_safe_noop():
    store = InMemoryEventStore()
    assert await store.close() is None


async def test_in_memory_store_skips_duplicate_event_ids():
    """The fake mirrors the real Mongo unique-index semantic so worker
    dedupe tests see the same behavior under fakes as in production."""
    store = InMemoryEventStore()
    e = _event(event_id="dup")
    n1 = await store.insert_many([e])
    n2 = await store.insert_many([e])
    assert n1 == 1
    assert n2 == 0
    assert len(store._events) == 1


# ---------------------------------------------------------------------------
# InMemoryEventStore.find: every filter predicate independently
# ---------------------------------------------------------------------------


async def _seeded_store() -> InMemoryEventStore:
    store = InMemoryEventStore()
    await store.insert_many(
        [
            _event(
                event_id="a",
                event_type="click",
                user_id="alice",
                source_url="https://feathr.co/a",
                timestamp=datetime(2026, 5, 1, tzinfo=timezone.utc),
            ),
            _event(
                event_id="b",
                event_type="pageview",
                user_id="alice",
                source_url="https://feathr.co/b",
                timestamp=datetime(2026, 5, 15, tzinfo=timezone.utc),
            ),
            _event(
                event_id="c",
                event_type="click",
                user_id="bob",
                source_url="https://feathr.co/a",
                timestamp=datetime(2026, 5, 25, tzinfo=timezone.utc),
            ),
        ]
    )
    return store


async def test_in_memory_store_filters_by_event_type():
    store = await _seeded_store()
    rows = await store.find(EventQuery(event_type="click"))
    assert {r["event_id"] for r in rows} == {"a", "c"}


async def test_in_memory_store_filters_by_user_id():
    store = await _seeded_store()
    rows = await store.find(EventQuery(user_id="bob"))
    assert {r["event_id"] for r in rows} == {"c"}


async def test_in_memory_store_filters_by_source_url():
    store = await _seeded_store()
    rows = await store.find(EventQuery(source_url="https://feathr.co/b"))
    assert {r["event_id"] for r in rows} == {"b"}


async def test_in_memory_store_filters_by_start_lower_bound():
    store = await _seeded_store()
    rows = await store.find(
        EventQuery(start=datetime(2026, 5, 10, tzinfo=timezone.utc))
    )
    assert {r["event_id"] for r in rows} == {"b", "c"}


async def test_in_memory_store_filters_by_end_upper_bound_half_open():
    """``end`` is exclusive — matches the half-open window contract."""
    store = await _seeded_store()
    rows = await store.find(
        EventQuery(end=datetime(2026, 5, 15, tzinfo=timezone.utc))
    )
    assert {r["event_id"] for r in rows} == {"a"}


# ---------------------------------------------------------------------------
# InMemoryEventSearch — lifecycle no-ops + search filters
# ---------------------------------------------------------------------------


async def test_in_memory_search_ensure_ready_is_safe_noop():
    search = InMemoryEventSearch()
    assert await search.ensure_ready() is None


async def test_in_memory_search_close_is_safe_noop():
    search = InMemoryEventSearch()
    assert await search.close() is None


async def _seeded_search() -> InMemoryEventSearch:
    search = InMemoryEventSearch()
    await search.index_many(
        [
            _event(
                event_id="a",
                event_type="click",
                user_id="alice",
                source_url="https://feathr.co/spring-launch",
                timestamp=datetime(2026, 5, 1, tzinfo=timezone.utc),
                metadata={"campaign": "spring"},
            ),
            _event(
                event_id="b",
                event_type="pageview",
                user_id="bob",
                source_url="https://feathr.co/winter",
                timestamp=datetime(2026, 5, 15, tzinfo=timezone.utc),
                metadata={"campaign": "winter"},
            ),
        ]
    )
    return search


async def test_in_memory_search_finds_substring_in_source_url():
    search = await _seeded_search()
    rows = await search.search(EventSearchQuery(q="spring-launch"))
    assert {r["event_id"] for r in rows} == {"a"}


async def test_in_memory_search_finds_metadata_value_substring():
    search = await _seeded_search()
    rows = await search.search(EventSearchQuery(q="winter"))
    assert {r["event_id"] for r in rows} == {"b"}


async def test_in_memory_search_filters_by_event_type():
    search = await _seeded_search()
    rows = await search.search(
        EventSearchQuery(q="", event_type="pageview")
    )
    assert {r["event_id"] for r in rows} == {"b"}


async def test_in_memory_search_filters_by_user_id():
    search = await _seeded_search()
    rows = await search.search(EventSearchQuery(q="", user_id="alice"))
    assert {r["event_id"] for r in rows} == {"a"}


async def test_in_memory_search_filters_by_start_lower_bound():
    search = await _seeded_search()
    rows = await search.search(
        EventSearchQuery(q="", start=datetime(2026, 5, 10, tzinfo=timezone.utc))
    )
    assert {r["event_id"] for r in rows} == {"b"}


async def test_in_memory_search_filters_by_end_upper_bound_half_open():
    search = await _seeded_search()
    rows = await search.search(
        EventSearchQuery(q="", end=datetime(2026, 5, 15, tzinfo=timezone.utc))
    )
    assert {r["event_id"] for r in rows} == {"a"}
