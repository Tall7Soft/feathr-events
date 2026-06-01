"""Tests for the Mongo-specific layer.

These exercise the parts of ``MongoEventStore`` that are easy to get wrong
and were caught by manual QA (silent data loss on NetworkTimeout, naive
timestamps, week bucket alignment). Real Mongo lives behind a docker
container; here we mock at the pymongo/Motor boundary to keep the suite
fast and runnable without infra.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import BulkWriteError, NetworkTimeout

from app.models.event import StoredEvent
from app.storage.mongo import MongoEventStore
from app.storage.protocols import EventQuery


def _event() -> StoredEvent:
    return StoredEvent(
        event_type="click",
        user_id="u1",
        source_url="https://feathr.co",
        timestamp=datetime(2026, 5, 31, 10, 0, 0, tzinfo=timezone.utc),
        metadata={},
    )


def _store_with_mock_collection() -> tuple[MongoEventStore, MagicMock]:
    client = MagicMock()
    collection = MagicMock()
    db = MagicMock()
    db.__getitem__.return_value = collection
    client.__getitem__.return_value = db
    store = MongoEventStore(client, "events")
    return store, collection


class _AsyncIterCursor:
    """Async iterable mock for cursors / aggregate results."""

    def __init__(self, items):
        self._items = list(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._items:
            raise StopAsyncIteration
        return self._items.pop(0)


def _store_with_mock_chainable_cursor(
    docs: list | None = None,
) -> tuple[MongoEventStore, MagicMock, MagicMock]:
    """Mock the Motor cursor chain: find().sort().skip().limit() → async iter."""
    store, collection = _store_with_mock_collection()
    cursor = MagicMock()
    cursor.sort.return_value = cursor
    cursor.skip.return_value = cursor
    items = docs if docs is not None else []
    cursor.limit.return_value = _AsyncIterCursor(items)
    collection.find = MagicMock(return_value=cursor)
    return store, collection, cursor


async def test_insert_many_propagates_network_timeout():
    """A socket / network timeout MUST NOT be swallowed.

    The original implementation had a catch-all that interpreted any
    exception with no ``writeErrors`` as "all duplicates absorbed",
    which silently reported success and lost the event. This guards
    against that regression.
    """
    store, collection = _store_with_mock_collection()
    collection.insert_many = AsyncMock(
        side_effect=NetworkTimeout("mongo:27017: timed out")
    )
    with pytest.raises(NetworkTimeout):
        await store.insert_many([_event()])


async def test_insert_many_absorbs_only_duplicate_bulk_errors():
    store, collection = _store_with_mock_collection()
    err = BulkWriteError(
        {
            "writeErrors": [
                {"code": 11000, "errmsg": "duplicate key"},
                {"code": 11000, "errmsg": "duplicate key"},
            ]
        }
    )
    collection.insert_many = AsyncMock(side_effect=err)
    inserted = await store.insert_many([_event(), _event()])
    assert inserted == 0  # both were duplicates


async def test_insert_many_reraises_when_bulk_has_non_duplicate_errors():
    store, collection = _store_with_mock_collection()
    err = BulkWriteError(
        {
            "writeErrors": [
                {"code": 11000, "errmsg": "duplicate key"},
                {"code": 121, "errmsg": "document validation failure"},
            ]
        }
    )
    collection.insert_many = AsyncMock(side_effect=err)
    with pytest.raises(BulkWriteError):
        await store.insert_many([_event(), _event()])


async def test_insert_many_returns_inserted_count_on_clean_write():
    store, collection = _store_with_mock_collection()
    result = SimpleNamespace(inserted_ids=["a", "b", "c"])
    collection.insert_many = AsyncMock(return_value=result)
    inserted = await store.insert_many([_event(), _event(), _event()])
    assert inserted == 3


async def test_insert_many_no_op_on_empty_batch():
    store, collection = _store_with_mock_collection()
    collection.insert_many = AsyncMock()
    assert await store.insert_many([]) == 0
    collection.insert_many.assert_not_awaited()


def test_from_doc_tags_naive_datetimes_as_utc():
    """Mongo returns naive UTC datetimes; clients need a Z-suffixed ISO
    string. ``_from_doc`` coerces tzinfo=UTC on the relevant fields."""
    doc = {
        "event_id": "x",
        "timestamp": datetime(2026, 5, 31, 12, 0, 0),
        "ingested_at": datetime(2026, 5, 31, 12, 0, 1),
        "bucket_start": datetime(2026, 5, 31, 0, 0, 0),
    }
    out = MongoEventStore._from_doc(doc)
    assert out["timestamp"].tzinfo == timezone.utc
    assert out["ingested_at"].tzinfo == timezone.utc
    assert out["bucket_start"].tzinfo == timezone.utc


def test_from_doc_leaves_aware_datetimes_alone():
    aware = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)
    out = MongoEventStore._from_doc({"timestamp": aware})
    assert out["timestamp"] is aware


# ---------------------------------------------------------------------------
# ensure_ready: pings + creates the four indexes the worker / queries depend
# on. If a future refactor drops the unique index on event_id, idempotency
# under at-least-once delivery silently breaks.
# ---------------------------------------------------------------------------


async def test_ensure_ready_pings_then_creates_four_indexes():
    store, collection = _store_with_mock_collection()
    store._client.admin.command = AsyncMock(return_value={"ok": 1})
    collection.create_index = AsyncMock()
    await store.ensure_ready()
    store._client.admin.command.assert_awaited_once_with("ping")
    assert collection.create_index.await_count == 4
    index_names = [call.kwargs.get("name") for call in collection.create_index.await_args_list]
    assert set(index_names) == {"evt_type_ts", "user_ts", "ts", "event_id_unique"}


async def test_ensure_ready_marks_event_id_index_unique():
    """The unique constraint on event_id is the idempotency keystone."""
    store, collection = _store_with_mock_collection()
    store._client.admin.command = AsyncMock(return_value={"ok": 1})
    collection.create_index = AsyncMock()
    await store.ensure_ready()
    event_id_call = next(
        call
        for call in collection.create_index.await_args_list
        if call.kwargs.get("name") == "event_id_unique"
    )
    assert event_id_call.kwargs.get("unique") is True


# ---------------------------------------------------------------------------
# find: filter construction + cursor chaining + tz coercion of results
# ---------------------------------------------------------------------------


async def test_find_returns_results_through_from_doc():
    """Naive datetimes returned by Mongo must be tagged UTC before reaching the client."""
    naive_ts = datetime(2026, 5, 31, 10, 0, 0)
    docs = [
        {"event_id": "a", "event_type": "click", "timestamp": naive_ts},
        {"event_id": "b", "event_type": "click", "timestamp": naive_ts},
    ]
    store, collection, _ = _store_with_mock_chainable_cursor(docs)
    result = await store.find(EventQuery())
    assert len(result) == 2
    assert all(r["timestamp"].tzinfo == timezone.utc for r in result)


async def test_find_default_sort_is_descending():
    store, collection, cursor = _store_with_mock_chainable_cursor()
    await store.find(EventQuery())
    sort_args = cursor.sort.call_args
    assert sort_args.args == ("timestamp", DESCENDING)


async def test_find_sort_ascending_flips_direction():
    store, collection, cursor = _store_with_mock_chainable_cursor()
    await store.find(EventQuery(sort="asc"))
    assert cursor.sort.call_args.args == ("timestamp", ASCENDING)


async def test_find_clamps_limit_to_1000_max():
    """Excessive limit values would force Mongo into unbounded cursors."""
    store, collection, cursor = _store_with_mock_chainable_cursor()
    await store.find(EventQuery(limit=99_999))
    assert cursor.limit.call_args.args == (1000,)


async def test_find_clamps_negative_skip_to_zero():
    store, collection, cursor = _store_with_mock_chainable_cursor()
    await store.find(EventQuery(skip=-50))
    assert cursor.skip.call_args.args == (0,)


async def test_find_builds_filter_combining_all_predicates():
    store, collection, _ = _store_with_mock_chainable_cursor()
    start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    end = datetime(2026, 6, 1, tzinfo=timezone.utc)
    await store.find(
        EventQuery(
            event_type="click",
            user_id="u1",
            source_url="https://feathr.co",
            start=start,
            end=end,
        )
    )
    filt = collection.find.call_args.args[0]
    assert filt == {
        "event_type": "click",
        "user_id": "u1",
        "source_url": "https://feathr.co",
        "timestamp": {"$gte": start, "$lt": end},
    }


async def test_find_projects_out_internal_id():
    """``_id`` is internal to Mongo; the API surface only exposes ``event_id``."""
    store, collection, _ = _store_with_mock_chainable_cursor()
    await store.find(EventQuery())
    assert collection.find.call_args.kwargs["projection"] == {"_id": 0}


# ---------------------------------------------------------------------------
# realtime_counts: pipeline shape + dict assembly
# ---------------------------------------------------------------------------


async def test_realtime_counts_returns_dict_keyed_by_event_type():
    store, collection = _store_with_mock_collection()
    collection.aggregate = MagicMock(
        return_value=_AsyncIterCursor(
            [{"_id": "click", "count": 12}, {"_id": "pageview", "count": 5}]
        )
    )
    since = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)
    until = datetime(2026, 5, 31, 12, 5, 0, tzinfo=timezone.utc)
    result = await store.realtime_counts(since=since, until=until)
    assert result == {"click": 12, "pageview": 5}


async def test_realtime_counts_empty_aggregation_returns_empty_dict():
    store, collection = _store_with_mock_collection()
    collection.aggregate = MagicMock(return_value=_AsyncIterCursor([]))
    since = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)
    until = datetime(2026, 5, 31, 12, 5, 0, tzinfo=timezone.utc)
    assert await store.realtime_counts(since=since, until=until) == {}


async def test_realtime_counts_uses_half_open_window():
    """Same composition guarantee as the stats pipeline (§A.5)."""
    store, collection = _store_with_mock_collection()
    captured: list[list[dict]] = []

    def aggregate(pipeline):
        captured.append(pipeline)
        return _AsyncIterCursor([])

    collection.aggregate = aggregate
    since = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)
    until = datetime(2026, 5, 31, 12, 5, 0, tzinfo=timezone.utc)
    await store.realtime_counts(since=since, until=until)
    assert captured[0][0]["$match"]["timestamp"] == {"$gte": since, "$lt": until}


# ---------------------------------------------------------------------------
# stats: results pass through _from_doc (tz coercion on bucket_start)
# ---------------------------------------------------------------------------


async def test_stats_coerces_naive_bucket_start_to_utc():
    """The aggregation returns ``bucket_start`` naive; _from_doc must tag it."""
    store, collection = _store_with_mock_collection()
    collection.aggregate = MagicMock(
        return_value=_AsyncIterCursor(
            [
                {
                    "event_type": "click",
                    "bucket_start": datetime(2026, 5, 31, 0, 0, 0),
                    "count": 4,
                }
            ]
        )
    )
    start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    end = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = await store.stats(start=start, end=end, bucket="day")
    assert rows[0]["bucket_start"].tzinfo == timezone.utc


# ---------------------------------------------------------------------------
# close: cleanup
# ---------------------------------------------------------------------------


async def test_close_invokes_client_close():
    store, _ = _store_with_mock_collection()
    store._client.close = MagicMock()
    await store.close()
    store._client.close.assert_called_once()
