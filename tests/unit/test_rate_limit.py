"""Token-bucket rate limiter behavior + /health bypass."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.middleware.rate_limit import RateLimitMiddleware


def _app(limit_per_minute: int) -> FastAPI:
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, requests_per_minute=limit_per_minute)

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/events")
    async def events():
        return {"items": []}

    return app


async def test_limit_returns_429_with_retry_after_header():
    transport = ASGITransport(app=_app(2))
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        ok = (await ac.get("/events")).status_code
        ok2 = (await ac.get("/events")).status_code
        denied = await ac.get("/events")
        assert ok == 200
        assert ok2 == 200
        assert denied.status_code == 429
        assert "retry-after" in {k.lower() for k in denied.headers.keys()}
        assert denied.json() == {"detail": "rate limit exceeded"}


async def test_health_bypasses_rate_limit():
    transport = ASGITransport(app=_app(1))
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Exhaust the limit on a different path
        await ac.get("/events")
        denied = await ac.get("/events")
        assert denied.status_code == 429
        # /health still works
        for _ in range(5):
            r = await ac.get("/health")
            assert r.status_code == 200
