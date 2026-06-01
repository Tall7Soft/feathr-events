"""MongoDB-backed event store.

Index strategy (rationale lives in ARCHITECTURE.md; summary here):

* ``(event_type, timestamp desc)`` — supports the common "filter by type
  within a time window" query and powers the stats aggregation's
  ``$match`` + ``$group`` stages with an index-only scan on the match.
* ``(user_id, timestamp desc)`` — supports per-user history queries.
* ``(timestamp desc)`` — supports bare time-range scans and the realtime
  summary, which always slices the most recent window.

We deliberately do NOT create a multikey index on ``metadata.*``. Metadata
is open-ended and indexing arbitrary keys would explode index size for
unbounded analytic benefit; Elasticsearch handles ad-hoc metadata search.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import BulkWriteError

from app.models.event import Bucket, StoredEvent
from app.storage.protocols import EventQuery

# Fields on which we tag tzinfo=UTC after reading from Mongo. BSON Date
# is timezone-naive on the wire and Motor surfaces it that way; without
# this coercion API responses would emit ambiguous naive ISO strings and
# clients would have to guess the zone.
_DATETIME_FIELDS = ("timestamp", "ingested_at", "bucket_start")

logger = logging.getLogger(__name__)

_BUCKET_TO_MONGO_UNIT = {"hour": "hour", "day": "day", "week": "week"}


class MongoEventStore:
    """:class:`EventStore` backed by MongoDB via Motor.

    Source of truth for events. Owns four indexes (see
    :meth:`ensure_ready`), one aggregation pipeline (:meth:`stats`),
    one realtime sum (:meth:`realtime_counts`), and the bulk insert
    path with explicit idempotency handling on duplicate ``event_id``.

    Reads pass through :meth:`_from_doc` which tags BSON-naive
    datetimes as UTC so API responses always serialize with a ``Z``
    suffix.
    """

    COLLECTION = "events"

    def __init__(self, client: AsyncIOMotorClient, db_name: str) -> None:
        """Wrap a Motor client and pin the target database name.

        ``client`` lifecycle (connect / close) is owned by the caller
        (the FastAPI lifespan); the store does not open or close it.
        """
        self._client = client
        self._db = client[db_name]

    @property
    def _collection(self) -> AsyncIOMotorCollection:
        """The single ``events`` collection. Defined as a property so
        the underlying database object can be hot-swapped in tests."""
        return self._db[self.COLLECTION]

    async def ensure_ready(self) -> None:
        """Verify connectivity and idempotently create indexes.

        Called from the FastAPI lifespan at startup. Pings the admin
        DB first so a misconfigured ``MONGO_URL`` fails the app rather
        than silently degrading. The four ``create_index`` calls are
        no-ops if the indexes already exist.
        """
        await self._client.admin.command("ping")
        await self._collection.create_index(
            [("event_type", ASCENDING), ("timestamp", DESCENDING)],
            name="evt_type_ts",
        )
        await self._collection.create_index(
            [("user_id", ASCENDING), ("timestamp", DESCENDING)],
            name="user_ts",
        )
        await self._collection.create_index(
            [("timestamp", DESCENDING)],
            name="ts",
        )
        await self._collection.create_index(
            [("event_id", ASCENDING)],
            name="event_id_unique",
            unique=True,
        )
        logger.info("mongo.indexes_ready")

    async def insert_many(self, events: list[StoredEvent]) -> int:
        """Persist a batch of events; return how many were newly inserted.

        Uses ``ordered=False`` so a single duplicate ``event_id`` does
        not block its batch siblings. The ``BulkWriteError`` branch is
        load-bearing: under at-least-once delivery, redelivered events
        will collide on the unique ``event_id`` index, and we treat
        the "all duplicates" case as a successful no-op. Any other
        kind of failure (``NetworkTimeout``, mixed errors, etc.)
        propagates so the worker can retry or DLQ.

        Returns 0 on empty batch.
        """
        if not events:
            return 0
        docs = [self._to_doc(e) for e in events]
        # ordered=False keeps the partial batch durable when a single doc
        # collides on event_id (idempotency under at-least-once delivery).
        try:
            result = await self._collection.insert_many(docs, ordered=False)
            return len(result.inserted_ids)
        except BulkWriteError as exc:
            # Duplicates on event_id are expected under at-least-once
            # redelivery; treat them as already-persisted and only let
            # genuinely failed writes escalate.
            details = exc.details or {}
            write_errors = details.get("writeErrors", [])
            duplicates = sum(1 for e in write_errors if e.get("code") == 11000)
            if write_errors and duplicates == len(write_errors):
                logger.info(
                    "mongo.duplicate_writes_absorbed",
                    extra={"duplicates": duplicates, "batch_size": len(docs)},
                )
                return len(docs) - duplicates
            raise

    async def find(self, q: EventQuery) -> list[dict[str, Any]]:
        """List events matching the filter, ordered by timestamp.

        Filters AND-combine. ``skip`` is clamped non-negative; ``limit``
        is clamped to ``[1, 1000]`` so a buggy caller cannot force an
        unbounded cursor. Results pass through :meth:`_from_doc` so
        their datetimes are UTC-aware.
        """
        filt = self._build_filter(q)
        sort_dir = DESCENDING if q.sort == "desc" else ASCENDING
        cursor = (
            self._collection.find(filt, projection={"_id": 0})
            .sort("timestamp", sort_dir)
            .skip(max(q.skip, 0))
            .limit(min(max(q.limit, 1), 1000))
        )
        return [self._from_doc(d) async for d in cursor]

    async def stats(
        self, *, start: datetime, end: datetime, bucket: Bucket
    ) -> list[dict[str, Any]]:
        """Aggregation pipeline returning counts per ``(event_type, bucket)``.

        Window is half-open ``[start, end)`` so callers can chain
        adjacent windows without double-counting boundary events.
        Buckets are produced via ``$dateTrunc``; week buckets pin
        ``startOfWeek: monday`` so the boundary survives Mongo
        upgrades that might shift the default.
        """
        unit = _BUCKET_TO_MONGO_UNIT[bucket]
        # ``$dateTrunc`` defaults week boundaries to Sunday. We pin Monday
        # explicitly so weekly buckets follow the ISO convention and stay
        # consistent with the in-memory fake used in tests.
        date_trunc: dict[str, Any] = {"date": "$timestamp", "unit": unit}
        if unit == "week":
            date_trunc["startOfWeek"] = "monday"
        pipeline: list[dict[str, Any]] = [
            {"$match": {"timestamp": {"$gte": start, "$lt": end}}},
            {
                "$group": {
                    "_id": {
                        "event_type": "$event_type",
                        "bucket_start": {"$dateTrunc": date_trunc},
                    },
                    "count": {"$sum": 1},
                }
            },
            {"$sort": {"_id.bucket_start": 1, "_id.event_type": 1}},
            {
                "$project": {
                    "_id": 0,
                    "event_type": "$_id.event_type",
                    "bucket_start": "$_id.bucket_start",
                    "count": 1,
                }
            },
        ]
        return [self._from_doc(doc) async for doc in self._collection.aggregate(pipeline)]

    async def realtime_counts(
        self, *, since: datetime, until: datetime
    ) -> dict[str, int]:
        """Per-event-type counts over a recent window, keyed by event_type.

        Backs ``/events/stats/realtime`` on cache miss. Half-open
        window like :meth:`stats`. The result dict is small (one entry
        per event_type) so it round-trips through Redis cheaply.
        """
        pipeline = [
            {"$match": {"timestamp": {"$gte": since, "$lt": until}}},
            {"$group": {"_id": "$event_type", "count": {"$sum": 1}}},
        ]
        out: dict[str, int] = {}
        async for doc in self._collection.aggregate(pipeline):
            out[doc["_id"]] = doc["count"]
        return out

    async def close(self) -> None:
        """Release the underlying Motor client. Called from lifespan shutdown."""
        self._client.close()

    @staticmethod
    def _to_doc(event: StoredEvent) -> dict[str, Any]:
        """Convert a :class:`StoredEvent` to a BSON-safe dict."""
        return event.model_dump()

    @staticmethod
    def _from_doc(doc: dict[str, Any]) -> dict[str, Any]:
        """Tag BSON-naive datetimes as UTC.

        BSON ``Date`` is timezone-naive on the wire and Motor surfaces
        it that way. Without this coercion, API responses would emit
        ambiguous naive ISO strings and clients would have to guess
        the zone. Aware datetimes pass through untouched.
        """
        for key in _DATETIME_FIELDS:
            v = doc.get(key)
            if isinstance(v, datetime) and v.tzinfo is None:
                doc[key] = v.replace(tzinfo=timezone.utc)
        return doc

    @staticmethod
    def _build_filter(q: EventQuery) -> dict[str, Any]:
        """Translate an :class:`EventQuery` into a Mongo ``find`` filter.

        Filters AND-combine. A time window uses ``$gte`` / ``$lt`` to
        match the half-open contract used everywhere else in the
        service.
        """
        filt: dict[str, Any] = {}
        if q.event_type:
            filt["event_type"] = q.event_type
        if q.user_id:
            filt["user_id"] = q.user_id
        if q.source_url:
            filt["source_url"] = q.source_url
        if q.start or q.end:
            window: dict[str, Any] = {}
            if q.start:
                window["$gte"] = q.start
            if q.end:
                window["$lt"] = q.end
            filt["timestamp"] = window
        return filt
