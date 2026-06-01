"""Guards around the Elasticsearch query construction.

These pin two production-affecting decisions:

1. ``lenient: true`` on ``simple_query_string``. Without it, a numeric
   metadata field (e.g. ``metadata.amount``) breaks unrelated text
   searches with ``failed to create query: For input string: "spring"``.
2. ``source_url.text`` is the field actually searched (the URL is
   indexed as ``keyword`` with a ``.text`` subfield; querying the
   ``keyword`` parent is exact-match only).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.storage.elasticsearch_repo import DEFAULT_SEARCH_FIELDS, ESEventSearch
from app.storage.protocols import EventSearchQuery


def _es_with_capture():
    es = MagicMock()
    es.search = AsyncMock(return_value={"hits": {"hits": []}})
    return es, ESEventSearch(es, "events")


def test_default_search_fields_include_text_subfield():
    assert "source_url.text" in DEFAULT_SEARCH_FIELDS
    assert "metadata.*" in DEFAULT_SEARCH_FIELDS


async def test_search_query_sets_lenient_true():
    es, store = _es_with_capture()
    await store.search(EventSearchQuery(q="spring"))
    es.search.assert_awaited_once()
    body = es.search.await_args.kwargs["body"]
    sqs = body["query"]["bool"]["must"][0]["simple_query_string"]
    assert sqs["lenient"] is True
    assert sqs["default_operator"] == "and"


async def test_search_query_passes_through_filters():
    es, store = _es_with_capture()
    from datetime import datetime, timezone

    await store.search(
        EventSearchQuery(
            q="spring",
            event_type="click",
            user_id="u1",
            start=datetime(2026, 5, 1, tzinfo=timezone.utc),
            end=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )
    )
    filt = es.search.await_args.kwargs["body"]["query"]["bool"]["filter"]
    terms = [next(iter(f.values())) for f in filt]
    assert {"event_type": "click"} in terms
    assert {"user_id": "u1"} in terms
    assert any("timestamp" in f.get("range", {}) for f in filt)


async def test_search_with_empty_query_matches_all():
    es, store = _es_with_capture()
    await store.search(EventSearchQuery(q="   "))
    body = es.search.await_args.kwargs["body"]
    assert body["query"]["bool"]["must"] == [{"match_all": {}}]
