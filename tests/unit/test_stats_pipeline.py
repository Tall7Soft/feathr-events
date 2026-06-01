"""Guard the Mongo aggregation pipeline structure.

We can't exercise real ``$dateTrunc`` here without a Mongo container,
but we can pin the pipeline shape so a future refactor doesn't silently
drop the explicit ``startOfWeek`` (which would shift weekly bucket
boundaries between deploys and break dashboards).
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.storage.mongo import MongoEventStore


class _AsyncIterCursor:
    def __init__(self, items):
        self._items = list(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._items:
            raise StopAsyncIteration
        return self._items.pop(0)


def _store_capturing_pipeline():
    client = MagicMock()
    collection = MagicMock()
    db = MagicMock()
    db.__getitem__.return_value = collection
    client.__getitem__.return_value = db
    captured: list[list[dict]] = []

    def aggregate(pipeline):
        captured.append(pipeline)
        return _AsyncIterCursor([])

    collection.aggregate = aggregate
    return MongoEventStore(client, "events"), captured


async def test_stats_pipeline_pins_startofweek_for_week_bucket():
    store, captured = _store_capturing_pipeline()
    start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    end = datetime(2026, 6, 1, tzinfo=timezone.utc)
    await store.stats(start=start, end=end, bucket="week")
    pipeline = captured[0]
    date_trunc = pipeline[1]["$group"]["_id"]["bucket_start"]["$dateTrunc"]
    assert date_trunc["unit"] == "week"
    assert date_trunc["startOfWeek"] == "monday", (
        "Mongo's default week boundary is Sunday; we pin Monday so the "
        "fake store and real Mongo agree, and so dashboard semantics "
        "don't shift between Mongo upgrades."
    )


async def test_stats_pipeline_omits_startofweek_for_hour_and_day():
    store, captured = _store_capturing_pipeline()
    start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    end = datetime(2026, 6, 1, tzinfo=timezone.utc)
    for bucket in ("hour", "day"):
        captured.clear()
        await store.stats(start=start, end=end, bucket=bucket)
        date_trunc = captured[0][1]["$group"]["_id"]["bucket_start"]["$dateTrunc"]
        assert "startOfWeek" not in date_trunc, (
            f"startOfWeek is meaningless on bucket={bucket} and would "
            "make Mongo reject the pipeline."
        )


async def test_stats_pipeline_uses_half_open_window():
    store, captured = _store_capturing_pipeline()
    start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    end = datetime(2026, 6, 1, tzinfo=timezone.utc)
    await store.stats(start=start, end=end, bucket="day")
    match_stage = captured[0][0]["$match"]["timestamp"]
    assert match_stage == {"$gte": start, "$lt": end}, (
        "Half-open windows compose cleanly when chaining buckets; "
        "swapping to $lte would double-count boundary events."
    )
