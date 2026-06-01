"""Tests for the Elasticsearch repo's non-query surface.

``test_es_query.py`` covers query construction (lenient flag, default
fields, filter combinations). This module covers everything else: index
lifecycle (``ensure_ready``), bulk indexing, and connection cleanup —
all the code paths that would otherwise require a real ES container.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.event import StoredEvent
from app.storage.elasticsearch_repo import (
    INDEX_SETTINGS,
    ESEventSearch,
)


def _event(event_id: str = "abc") -> StoredEvent:
    return StoredEvent(
        event_id=event_id,
        event_type="click",
        user_id="u1",
        source_url="https://feathr.co",
        timestamp=datetime(2026, 5, 31, 10, 0, 0, tzinfo=timezone.utc),
        metadata={},
    )


def _es_with_mocked_apis() -> tuple[MagicMock, ESEventSearch]:
    es = MagicMock()
    es.cluster = MagicMock()
    es.cluster.health = AsyncMock(return_value={"status": "yellow"})
    es.indices = MagicMock()
    es.indices.exists = AsyncMock(return_value=False)
    es.indices.create = AsyncMock(return_value={"acknowledged": True})
    es.close = AsyncMock()
    es.search = AsyncMock(return_value={"hits": {"hits": []}})
    return es, ESEventSearch(es, "events")


# ---------------------------------------------------------------------------
# ensure_ready
# ---------------------------------------------------------------------------


async def test_ensure_ready_waits_for_cluster_health_first():
    """Cluster must be at least yellow before we attempt index operations."""
    es, store = _es_with_mocked_apis()
    await store.ensure_ready()
    es.cluster.health.assert_awaited_once()
    call_kwargs = es.cluster.health.await_args.kwargs
    assert call_kwargs["wait_for_status"] == "yellow"


async def test_ensure_ready_creates_index_with_full_mapping_when_missing():
    """Missing index → create with the dynamic-template mapping."""
    es, store = _es_with_mocked_apis()
    es.indices.exists = AsyncMock(return_value=False)
    await store.ensure_ready()
    es.indices.create.assert_awaited_once()
    create_kwargs = es.indices.create.await_args.kwargs
    assert create_kwargs["index"] == "events"
    assert create_kwargs["body"] is INDEX_SETTINGS


async def test_ensure_ready_skips_create_when_index_exists():
    """Re-running ensure_ready on an existing index is a safe no-op."""
    es, store = _es_with_mocked_apis()
    es.indices.exists = AsyncMock(return_value=True)
    await store.ensure_ready()
    es.indices.create.assert_not_awaited()


# ---------------------------------------------------------------------------
# index_many
# ---------------------------------------------------------------------------


async def test_index_many_no_op_on_empty_batch():
    """Empty batch must not touch the network — the bulk helper is allowed
    to throw on an empty action stream in some ES client versions."""
    es, store = _es_with_mocked_apis()
    with patch(
        "app.storage.elasticsearch_repo.async_bulk", new_callable=AsyncMock
    ) as bulk:
        result = await store.index_many([])
    assert result == 0
    bulk.assert_not_awaited()


async def test_index_many_returns_success_count_from_async_bulk():
    es, store = _es_with_mocked_apis()
    with patch(
        "app.storage.elasticsearch_repo.async_bulk",
        new=AsyncMock(return_value=(3, [])),
    ):
        result = await store.index_many([_event(), _event("b"), _event("c")])
    assert result == 3


async def test_index_many_passes_index_actions_keyed_on_event_id():
    """The bulk action shape is what ES expects: ``_op_type: index`` with
    ``_id`` set to ``event_id`` so redeliveries upsert deterministically."""
    es, store = _es_with_mocked_apis()
    captured: list[dict] = []

    async def fake_bulk(es_client, actions, raise_on_error=True):
        captured.extend(actions)
        return len(captured), []

    with patch(
        "app.storage.elasticsearch_repo.async_bulk", new=fake_bulk
    ):
        await store.index_many([_event("first"), _event("second")])

    assert len(captured) == 2
    assert all(a["_op_type"] == "index" for a in captured)
    assert all(a["_index"] == "events" for a in captured)
    assert [a["_id"] for a in captured] == ["first", "second"]
    assert captured[0]["_source"]["event_type"] == "click"


async def test_index_many_does_not_raise_on_partial_failures():
    """``raise_on_error=False`` means partial failures degrade gracefully —
    the worker logs and moves on, Mongo remains source of truth."""
    es, store = _es_with_mocked_apis()
    partial_errors = [{"index": {"_id": "x", "status": 500}}]
    with patch(
        "app.storage.elasticsearch_repo.async_bulk",
        new=AsyncMock(return_value=(1, partial_errors)),
    ):
        # No raise — returns whatever count the bulk helper reported.
        result = await store.index_many([_event(), _event("b")])
    assert result == 1


async def test_index_many_passes_raise_on_error_false_to_helper():
    """Pin the call shape so a future refactor doesn't accidentally
    re-enable raise_on_error and turn ES outages into ingest failures."""
    es, store = _es_with_mocked_apis()
    with patch(
        "app.storage.elasticsearch_repo.async_bulk",
        new_callable=AsyncMock,
        return_value=(1, []),
    ) as bulk:
        await store.index_many([_event()])
    assert bulk.await_args.kwargs.get("raise_on_error") is False


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


async def test_close_closes_underlying_client():
    es, store = _es_with_mocked_apis()
    await store.close()
    es.close.assert_awaited_once()
