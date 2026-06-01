"""Event domain models.

This module defines the two shapes an event takes in the system:

* :class:`EventIn` — the wire format. Strict, rejects unknown fields,
  validates URL + size + character bounds. Owns the API contract.
* :class:`StoredEvent` — the internal format. Adds server-generated
  ``event_id`` and ``ingested_at``. Everything downstream of the worker
  (Mongo, ES, dedupe set) sees this shape.

Aggregation result types (:class:`StatsBucket`, :class:`RealtimeStats`)
live here too so they share the same module as the inputs they
summarize.
"""

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

EventType = Literal["pageview", "click", "conversion", "form_submit", "custom"]


def _utcnow() -> datetime:
    """Return the current time tagged as UTC.

    Always tz-aware so downstream code (Mongo writes, response
    serialization, comparisons) never has to guess the zone.
    """
    return datetime.now(timezone.utc)


class EventIn(BaseModel):
    """Payload accepted by POST /events.

    Kept permissive on ``event_type`` (free string) so partners can emit
    custom event types without redeploys; downstream queries treat it as
    an opaque keyword.
    """

    model_config = ConfigDict(extra="forbid")

    event_type: str = Field(min_length=1, max_length=64)
    user_id: str = Field(min_length=1, max_length=128)
    source_url: str = Field(min_length=1, max_length=2048)
    timestamp: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("event_type")
    @classmethod
    def _normalize_event_type(cls, v: str) -> str:
        return v.strip().lower()

    @field_validator("source_url")
    @classmethod
    def _validate_url(cls, v: str) -> str:
        # Use pydantic's URL parser as a validator without forcing the
        # storage type to HttpUrl (which serializes oddly into Mongo).
        HttpUrl(v)
        return v

    @field_validator("metadata")
    @classmethod
    def _bound_metadata(cls, v: dict[str, Any]) -> dict[str, Any]:
        # Defense-in-depth: cap metadata size to keep ES mappings sane and
        # prevent abusive payloads from blowing out the worker.
        if len(v) > 64:
            raise ValueError("metadata must have at most 64 top-level keys")
        return v


class StoredEvent(BaseModel):
    """Internal representation after enrichment, before persistence."""

    event_id: str = Field(default_factory=lambda: uuid4().hex)
    event_type: str
    user_id: str
    source_url: str
    timestamp: datetime
    metadata: dict[str, Any]
    ingested_at: datetime = Field(default_factory=_utcnow)

    @classmethod
    def from_input(cls, payload: EventIn) -> "StoredEvent":
        """Build a ``StoredEvent`` from a validated wire payload.

        Adds the two server-controlled fields:

        * ``event_id`` — a fresh UUID. Producers cannot supply this in
          v0.1 (idempotency at the producer boundary is on the §12
          follow-up list).
        * ``timestamp`` — defaults to ``_utcnow()`` if the producer
          didn't provide one, so events without an explicit time still
          land on a deterministic instant.
        """
        return cls(
            event_type=payload.event_type,
            user_id=payload.user_id,
            source_url=payload.source_url,
            timestamp=payload.timestamp or _utcnow(),
            metadata=payload.metadata,
        )

    def dedupe_key(self) -> str:
        """Stable signature used by the worker's dedupe set."""
        return f"{self.event_id}"


Bucket = Literal["hour", "day", "week"]


class StatsBucket(BaseModel):
    """One row in the ``/events/stats`` aggregation result.

    A bucket is uniquely identified by ``(bucket_start, event_type)``.
    The ``count`` is the number of events of that type whose timestamp
    falls inside that bucket window.
    """

    bucket_start: datetime
    event_type: str
    count: int


class RealtimeStats(BaseModel):
    """Cached summary returned by ``/events/stats/realtime``.

    Always reflects a rolling ``window_seconds`` window ending at
    ``generated_at``. ``by_type`` is the breakdown per event_type;
    ``total`` is the sum across types and is duplicated for cheap
    client-side use.
    """

    window_seconds: int
    total: int
    by_type: dict[str, int]
    generated_at: datetime
