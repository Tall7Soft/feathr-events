"""Targeted tests closing remaining coverage gaps.

These exercise small but important branches that the integration tests
sweep past: idempotency of ``start``/``stop``, the LRU eviction inside
the worker's dedupe set, cache JSON corruption paths, query-window
validation helpers, and the trivial /health handler.

Each test pins a specific branch — when a future refactor accidentally
drops one, this file is what catches it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import fakeredis
import pytest
import pytest_asyncio
from fastapi import HTTPException

from app.api.queries import _validate_window
from app.cache.realtime import RealtimeStatsCache, _json_default
from app.queue import InMemoryQueue
from app.storage.fakes import InMemoryEventSearch, InMemoryEventStore
from app.workers.event_worker import EventWorker, _BoundedSet


# ---------------------------------------------------------------------------
# _BoundedSet — the worker's dedupe LRU
# ---------------------------------------------------------------------------


def test_bounded_set_returns_false_on_duplicate_add():
    s = _BoundedSet(max_size=10)
    assert s.add("x") is True
    assert s.add("x") is False  # already present
    assert "x" in s


def test_bounded_set_evicts_oldest_when_over_capacity():
    s = _BoundedSet(max_size=3)
    s.add("a")
    s.add("b")
    s.add("c")
    s.add("d")  # forces eviction of "a"
    assert "a" not in s
    assert "d" in s


def test_bounded_set_readd_promotes_to_most_recent():
    """Re-adding an existing key should move it to the end (LRU semantics)."""
    s = _BoundedSet(max_size=3)
    s.add("a")
    s.add("b")
    s.add("c")
    s.add("a")  # re-add: a moves to end, no eviction yet
    s.add("d")  # eviction now removes "b", the new oldest
    assert "b" not in s
    assert "a" in s


# ---------------------------------------------------------------------------
# Worker start/stop idempotency
# ---------------------------------------------------------------------------


def _make_worker() -> EventWorker:
    return EventWorker(
        queue=InMemoryQueue("e"),
        dlq=InMemoryQueue("d"),
        store=InMemoryEventStore(),
        search=InMemoryEventSearch(),
        concurrency=1,
        batch_size=1,
        batch_timeout=0.01,
        max_receive_count=2,
        base_retry_delay=0.01,
    )


async def test_worker_start_is_idempotent():
    """Calling start twice must not spawn a second pool of tasks."""
    worker = _make_worker()
    await worker.start()
    first_task_count = len(worker._tasks)
    await worker.start()  # should early-return
    assert len(worker._tasks) == first_task_count
    await worker.stop(drain_timeout=0.5)


async def test_worker_stop_is_safe_when_never_started():
    worker = _make_worker()
    # No exception, no hang — stop should early-return.
    await worker.stop(drain_timeout=0.5)


# ---------------------------------------------------------------------------
# RealtimeStatsCache — error and edge paths
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def cache():
    r = fakeredis.FakeAsyncRedis(decode_responses=False)
    try:
        yield RealtimeStatsCache(r, ttl_seconds=2)
    finally:
        await r.aclose()


async def test_cache_get_returns_none_on_corrupt_json(cache: RealtimeStatsCache):
    """A Redis value that isn't valid JSON should degrade to a cache miss,
    not raise. This shields the API from operational footguns (manual
    redis-cli SET, version downgrades, etc.)."""
    await cache._redis.set(cache._key(60), b"this is not json")
    assert await cache.get(60) is None


async def test_cache_invalidate_with_no_window_is_a_noop(
    cache: RealtimeStatsCache,
):
    """``invalidate(None)`` is the prefix-wide bust; we deliberately
    don't implement it on a single node — see ARCHITECTURE.md §6."""
    await cache.set(60, {"total": 1, "by_type": {"x": 1}})
    await cache.invalidate(None)
    assert await cache.get(60) == {"total": 1, "by_type": {"x": 1}}


def test_json_default_serializes_naive_datetime_as_utc_z():
    """Naive datetimes round-trip as if they were UTC, with the ``Z``
    suffix so they match Pydantic's output."""
    naive = datetime(2026, 5, 31, 12, 0, 0)
    out = _json_default(naive)
    assert out == "2026-05-31T12:00:00Z"


def test_json_default_serializes_aware_datetime_as_z():
    aware = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)
    assert _json_default(aware) == "2026-05-31T12:00:00Z"


def test_json_default_raises_typeerror_for_unsupported_types():
    """Anything other than datetime should raise TypeError so json.dumps
    surfaces the bug instead of silently dropping a field."""
    with pytest.raises(TypeError):
        _json_default(object())


def test_json_default_integrates_with_json_dumps():
    """End-to-end: dumping a payload with a datetime works with our default."""
    payload = {"generated_at": datetime(2026, 5, 31, 12, tzinfo=timezone.utc)}
    out = json.loads(json.dumps(payload, default=_json_default))
    assert out["generated_at"].endswith("Z")


# ---------------------------------------------------------------------------
# _validate_window — query window validation helper
# ---------------------------------------------------------------------------


def test_validate_window_requires_both_when_flag_set():
    """``/events/stats`` mandates both start and end; the helper raises 422."""
    with pytest.raises(HTTPException) as exc_info:
        _validate_window(None, None, require_both=True)
    assert exc_info.value.status_code == 422
    assert "required" in exc_info.value.detail.lower()


def test_validate_window_rejects_start_after_end():
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    end = datetime(2026, 5, 1, tzinfo=timezone.utc)
    with pytest.raises(HTTPException) as exc_info:
        _validate_window(start, end)
    assert exc_info.value.status_code == 422
    assert "greater than start" in exc_info.value.detail


def test_validate_window_rejects_equal_endpoints():
    same = datetime(2026, 5, 1, tzinfo=timezone.utc)
    with pytest.raises(HTTPException):
        _validate_window(same, same)


def test_validate_window_accepts_none_when_not_required():
    """The list endpoint allows missing window bounds — helper returns None."""
    assert _validate_window(None, None) is None
    assert _validate_window(datetime(2026, 5, 1, tzinfo=timezone.utc), None) is None
