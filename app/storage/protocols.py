"""Storage abstractions.

Defining protocols here gives the API layer and worker something to type
against, and lets tests substitute in-memory fakes without touching real
infrastructure. The concrete Mongo / ES implementations live alongside.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

from app.models.event import Bucket, StoredEvent


@dataclass
class EventQuery:
    """Filter + paging parameters for :meth:`EventStore.find`.

    Empty fields mean "no constraint on this dimension". Time bounds
    use the half-open ``[start, end)`` contract.
    """

    event_type: str | None = None
    user_id: str | None = None
    source_url: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    skip: int = 0
    limit: int = 100
    sort: Literal["asc", "desc"] = "desc"


@dataclass
class EventSearchQuery:
    """Free-text + structured filter parameters for :meth:`EventSearchStore.search`.

    ``q`` is the free-text term. ``fields`` defaults to
    ``metadata.*`` plus the URL's ``text`` subfield, which is what the
    public API exposes; callers can override for internal tools.
    """

    q: str
    event_type: str | None = None
    user_id: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    size: int = 20
    fields: list[str] = field(
        default_factory=lambda: ["metadata.*", "source_url.text"]
    )


class EventStore(Protocol):
    """The persistence contract the worker and query API depend on.

    Both :class:`app.storage.mongo.MongoEventStore` and
    :class:`app.storage.fakes.InMemoryEventStore` implement this
    surface; tests swap between them via dependency injection.
    """

    async def insert_many(self, events: list[StoredEvent]) -> int: ...
    async def find(self, q: EventQuery) -> list[dict[str, Any]]: ...
    async def stats(
        self, *, start: datetime, end: datetime, bucket: Bucket
    ) -> list[dict[str, Any]]: ...
    async def realtime_counts(
        self, *, since: datetime, until: datetime
    ) -> dict[str, int]: ...
    async def ensure_ready(self) -> None: ...
    async def close(self) -> None: ...


class EventSearchStore(Protocol):
    """The full-text search contract.

    A narrower surface than :class:`EventStore` because the search
    layer is best-effort and rebuildable; it does not own aggregations
    or realtime counts.
    """

    async def index_many(self, events: list[StoredEvent]) -> int: ...
    async def search(self, q: EventSearchQuery) -> list[dict[str, Any]]: ...
    async def ensure_ready(self) -> None: ...
    async def close(self) -> None: ...
