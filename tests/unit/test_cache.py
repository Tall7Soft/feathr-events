import asyncio

import fakeredis
import pytest
import pytest_asyncio

from app.cache import RealtimeStatsCache


@pytest_asyncio.fixture
async def cache():
    r = fakeredis.FakeAsyncRedis(decode_responses=False)
    try:
        yield RealtimeStatsCache(r, ttl_seconds=1)
    finally:
        await r.aclose()


async def test_set_then_get_roundtrip(cache: RealtimeStatsCache):
    await cache.set(60, {"total": 5, "by_type": {"click": 5}})
    got = await cache.get(60)
    assert got == {"total": 5, "by_type": {"click": 5}}


async def test_get_returns_none_for_missing_key(cache: RealtimeStatsCache):
    assert await cache.get(60) is None


async def test_get_or_compute_runs_once_under_concurrency(cache: RealtimeStatsCache):
    call_count = 0

    async def compute():
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.05)
        return {"total": 1, "by_type": {"click": 1}}

    results = await asyncio.gather(*(cache.get_or_compute(60, compute) for _ in range(10)))
    assert call_count == 1
    values = [v for v, _ in results]
    hits = [h for _, h in results]
    assert all(v == {"total": 1, "by_type": {"click": 1}} for v in values)
    assert hits.count(True) >= 1  # at least the post-lock readers


async def test_get_or_compute_reports_cache_hit_after_warm(cache: RealtimeStatsCache):
    async def compute():
        return {"total": 1, "by_type": {"click": 1}}

    _, miss = await cache.get_or_compute(60, compute)
    _, hit = await cache.get_or_compute(60, compute)
    assert miss is False
    assert hit is True


async def test_invalidate_clears_window(cache: RealtimeStatsCache):
    await cache.set(60, {"total": 5, "by_type": {}})
    await cache.invalidate(60)
    assert await cache.get(60) is None


async def test_datetimes_in_cached_payload_serialize_with_z_suffix(
    cache: RealtimeStatsCache,
):
    """Cached responses must match Pydantic's UTC ISO format.

    Without this, freshly-computed responses emit ``...Z`` while cached
    reads emit ``...+00:00`` — a subtle inconsistency that downstream
    parsers (especially Go / Java SDKs) trip on.
    """
    from datetime import datetime, timezone

    payload = {
        "total": 1,
        "by_type": {"click": 1},
        "generated_at": datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc),
    }
    await cache.set(60, payload)
    got = await cache.get(60)
    assert got["generated_at"].endswith("Z")
    assert "+00:00" not in got["generated_at"]
