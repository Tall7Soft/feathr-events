"""Redis-backed realtime stats cache.

Strategy (full rationale in ARCHITECTURE.md):

* TTL-only invalidation. The realtime endpoint advertises eventual
  consistency on the order of ``ttl`` seconds; this avoids the
  write-amplification of bursting cache busts on every event ingest.
* The cache key is derived from the requested window so distinct
  windows do not stomp each other's entries.
* A single-flight lock prevents the dogpile where N concurrent requests
  recompute the same window after expiry; only one holder runs the
  Mongo aggregation and the rest read the freshly written value.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Protocol

logger = logging.getLogger(__name__)


class RedisLike(Protocol):
    """Narrow Redis surface the cache actually depends on.

    Defining this lets tests pass a ``fakeredis`` instance without
    importing redis-py's full client interface.
    """

    async def get(self, key: str) -> bytes | str | None: ...
    async def set(
        self, key: str, value: str, ex: int | None = None
    ) -> Any: ...
    async def delete(self, *keys: str) -> int: ...


class RealtimeStatsCache:
    """Redis-backed cache for the ``/events/stats/realtime`` endpoint.

    Per-window keys (``stats:realtime:<window_seconds>``) so different
    requested windows do not stomp on each other. ``ttl_seconds``
    bounds staleness; an in-process ``asyncio.Lock`` per window
    single-flights the recompute on miss to prevent N concurrent
    callers from all running the Mongo aggregation.

    Cross-replica single-flighting would move the lock to Redis (see
    ``ARCHITECTURE.md`` §6); for v0.1 the single-replica deploy is
    correct as-is.
    """

    KEY_PREFIX = "stats:realtime"

    def __init__(self, redis: RedisLike, ttl_seconds: int) -> None:
        """Wrap a Redis client with the configured TTL."""
        self._redis = redis
        self._ttl = ttl_seconds
        self._locks: dict[str, asyncio.Lock] = {}

    def _key(self, window_seconds: int) -> str:
        """Compose the Redis key for a given window size."""
        return f"{self.KEY_PREFIX}:{window_seconds}"

    async def get(self, window_seconds: int) -> dict[str, Any] | None:
        """Return the cached payload for ``window_seconds``, or ``None``.

        A non-JSON payload (e.g. someone ran ``redis-cli SET`` by
        hand) is logged at WARNING and degraded to a cache miss
        rather than raising — the endpoint stays available while ops
        investigates.
        """
        raw = await self._redis.get(self._key(window_seconds))
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("cache.invalid_payload", extra={"key": self._key(window_seconds)})
            return None

    async def set(self, window_seconds: int, value: dict[str, Any]) -> None:
        """Write ``value`` to Redis under the window's key with the configured TTL.

        Datetimes inside ``value`` are serialized via :func:`_json_default`
        so the cached payload uses the same ``Z``-suffixed ISO format
        FastAPI emits on the fresh-compute path.
        """
        payload = json.dumps(value, default=_json_default)
        await self._redis.set(self._key(window_seconds), payload, ex=self._ttl)

    async def invalidate(self, window_seconds: int | None = None) -> None:
        """Drop the cached entry for one window, or no-op for the prefix bust.

        Called only by tests; the production ingest path is TTL-only
        (see ``ARCHITECTURE.md`` §6 for the rationale).
        """
        if window_seconds is None:
            # In production we'd SCAN by prefix; here we keep it explicit.
            return
        await self._redis.delete(self._key(window_seconds))

    async def get_or_compute(
        self,
        window_seconds: int,
        compute: Callable[[], Awaitable[dict[str, Any]]],
    ) -> tuple[dict[str, Any], bool]:
        """Return (value, cache_hit). Single-flighted per window key."""
        cached = await self.get(window_seconds)
        if cached is not None:
            return cached, True

        lock = self._locks.setdefault(self._key(window_seconds), asyncio.Lock())
        async with lock:
            cached = await self.get(window_seconds)
            if cached is not None:
                return cached, True
            value = await compute()
            await self.set(window_seconds, value)
            return value, False


def _json_default(o: Any) -> Any:
    """``json.dumps`` default for the cache payload.

    Serializes :class:`datetime` to the same ``Z``-suffixed ISO
    format Pydantic 2 / FastAPI emits, so the cached response and the
    freshly computed response are byte-identical. Anything else
    raises ``TypeError`` so a future caller does not silently lose a
    field.
    """
    if isinstance(o, datetime):
        if o.tzinfo is None:
            o = o.replace(tzinfo=timezone.utc)
        # Match Pydantic 2's UTC convention so cached and freshly-computed
        # responses serialize datetimes identically ("Z" suffix).
        return o.isoformat().replace("+00:00", "Z")
    raise TypeError(f"Object of type {type(o)!r} is not JSON serializable")
