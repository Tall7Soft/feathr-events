"""Token-bucket rate limiter keyed on client IP.

In production this would live in Redis (shared across replicas) — see
ARCHITECTURE.md. The in-process implementation here is enough to
demonstrate the API contract and protect a single-node deployment from
trivial abuse.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from typing import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-client-IP token bucket installed on the FastAPI app.

    Each IP gets a bucket of ``requests_per_minute`` tokens that
    refills linearly. A request consumes one token; an empty bucket
    returns 429 with ``Retry-After`` based on how long the caller
    must wait for the next token. ``/health*`` paths bypass the
    limiter so uptime monitors cannot be locked out.
    """

    def __init__(self, app, *, requests_per_minute: int) -> None:
        """Configure the bucket capacity + refill rate."""
        super().__init__(app)
        self._capacity = requests_per_minute
        self._refill_per_second = requests_per_minute / 60.0
        self._buckets: dict[str, tuple[float, float]] = defaultdict(
            lambda: (float(self._capacity), time.monotonic())
        )
        self._lock = asyncio.Lock()

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Refill the caller's bucket, consume a token, forward or reject."""
        if request.url.path.startswith("/health"):
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"
        async with self._lock:
            tokens, last = self._buckets[client_ip]
            now = time.monotonic()
            tokens = min(self._capacity, tokens + (now - last) * self._refill_per_second)
            if tokens < 1.0:
                self._buckets[client_ip] = (tokens, now)
                retry_after = max(int((1.0 - tokens) / self._refill_per_second), 1)
                return JSONResponse(
                    status_code=429,
                    headers={"Retry-After": str(retry_after)},
                    content={"detail": "rate limit exceeded"},
                )
            self._buckets[client_ip] = (tokens - 1.0, now)

        return await call_next(request)
