"""Elasticsearch-backed search store.

Mapping decisions (full rationale in ARCHITECTURE.md):

* ``event_type``, ``user_id`` → ``keyword``. We never need a tokenized
  view of these; we filter and aggregate on exact values.
* ``timestamp`` → ``date``. Range filters + future date-histogram aggs.
* ``source_url`` → ``keyword`` with a ``text`` subfield. Exact-URL filter
  is the common case; the text subfield lets us match path fragments.
* ``metadata`` → dynamic object with strings mapped as ``text`` plus a
  ``.keyword`` subfield. Metadata is partner-controlled; dynamic mapping
  keeps the schema open while the multi-field gives us both full-text
  and exact-match without re-indexing.
"""

from __future__ import annotations

import logging
from dataclasses import field
from typing import Any

from elasticsearch import AsyncElasticsearch
from elasticsearch.helpers import async_bulk

from app.models.event import StoredEvent
from app.storage.protocols import EventSearchQuery

DEFAULT_SEARCH_FIELDS = ["metadata.*", "source_url.text"]

logger = logging.getLogger(__name__)


INDEX_SETTINGS: dict[str, Any] = {
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 0,
        "refresh_interval": "1s",
    },
    "mappings": {
        "dynamic_templates": [
            {
                "metadata_strings": {
                    "path_match": "metadata.*",
                    "match_mapping_type": "string",
                    "mapping": {
                        "type": "text",
                        "fields": {"keyword": {"type": "keyword", "ignore_above": 256}},
                    },
                }
            }
        ],
        "properties": {
            "event_id": {"type": "keyword"},
            "event_type": {"type": "keyword"},
            "user_id": {"type": "keyword"},
            "timestamp": {"type": "date"},
            "ingested_at": {"type": "date"},
            "source_url": {
                "type": "keyword",
                "fields": {"text": {"type": "text", "analyzer": "standard"}},
            },
            "metadata": {"type": "object", "dynamic": True},
        },
    },
}


class ESEventSearch:
    """:class:`EventSearchStore` backed by Elasticsearch.

    Secondary, rebuildable index. The worker writes here best-effort
    after a successful Mongo commit; on failure we log and ack the
    message anyway because Mongo remains authoritative and a reindex
    job can rebuild ES from there.

    Owns index creation (with the dynamic-template mapping defined in
    :data:`INDEX_SETTINGS`), bulk indexing via ``async_bulk``, and the
    ``simple_query_string`` search shape with ``lenient: true`` to
    survive mixed-type partner metadata.
    """

    def __init__(self, client: AsyncElasticsearch, index: str) -> None:
        """Wrap an async ES client and pin the target index name."""
        self._es = client
        self._index = index

    async def ensure_ready(self) -> None:
        """Wait for the cluster and idempotently create the index.

        Cluster health is waited on first so a misconfigured
        ``ELASTICSEARCH_URL`` fails the app at startup. If the index
        already exists, the call is a no-op; otherwise it is created
        with :data:`INDEX_SETTINGS` (single shard, zero replicas, the
        dynamic template that maps any metadata string as
        ``text`` + ``.keyword`` subfield).
        """
        await self._es.cluster.health(wait_for_status="yellow", timeout="10s")
        exists = await self._es.indices.exists(index=self._index)
        if not exists:
            await self._es.indices.create(index=self._index, body=INDEX_SETTINGS)
            logger.info("es.index_created", extra={"index": self._index})
        else:
            logger.info("es.index_ready", extra={"index": self._index})

    async def index_many(self, events: list[StoredEvent]) -> int:
        """Bulk-index events; return how many ES accepted.

        Uses ``raise_on_error=False`` so a partial failure logs a
        warning and the worker keeps moving — Mongo is the source of
        truth, ES can be rebuilt from there. ``_id`` is set to
        ``event_id`` so a redelivered event upserts deterministically
        rather than creating a duplicate document.
        """
        if not events:
            return 0
        actions = (
            {
                "_op_type": "index",
                "_index": self._index,
                "_id": e.event_id,
                "_source": e.model_dump(mode="json"),
            }
            for e in events
        )
        success, errors = await async_bulk(self._es, actions, raise_on_error=False)
        if errors:
            logger.warning("es.bulk_partial", extra={"errors": len(errors)})
        return success

    async def search(self, q: EventSearchQuery) -> list[dict[str, Any]]:
        """Full-text search over metadata + URL fragment.

        Builds a ``bool`` query: free-text via ``simple_query_string``
        (with ``lenient: true`` so a numeric ``metadata.amount`` does
        not break unrelated string searches), plus structured filters
        for ``event_type`` / ``user_id`` / time window. Results are
        sorted by ``(timestamp desc, event_id desc)`` for stable
        pagination. ``size`` is clamped to ``[1, 200]``.
        """
        must: list[dict[str, Any]] = []
        if q.q.strip():
            must.append(
                {
                    "simple_query_string": {
                        "query": q.q,
                        "fields": q.fields,
                        "default_operator": "and",
                        # Skip fields whose mapped type can't satisfy the
                        # query (e.g. searching "spring" against a numeric
                        # metadata.amount). Without this, partner-supplied
                        # numeric metadata breaks unrelated searches.
                        "lenient": True,
                    }
                }
            )
        filt: list[dict[str, Any]] = []
        if q.event_type:
            filt.append({"term": {"event_type": q.event_type}})
        if q.user_id:
            filt.append({"term": {"user_id": q.user_id}})
        if q.start or q.end:
            window: dict[str, Any] = {}
            if q.start:
                window["gte"] = q.start.isoformat()
            if q.end:
                window["lt"] = q.end.isoformat()
            filt.append({"range": {"timestamp": window}})

        body = {
            "size": min(max(q.size, 1), 200),
            "query": {"bool": {"must": must or [{"match_all": {}}], "filter": filt}},
            "sort": [{"timestamp": {"order": "desc"}}, {"event_id": {"order": "desc"}}],
        }
        resp = await self._es.search(index=self._index, body=body)
        return [hit["_source"] for hit in resp["hits"]["hits"]]

    async def close(self) -> None:
        """Release the underlying async ES client."""
        await self._es.close()
