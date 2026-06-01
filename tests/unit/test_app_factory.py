"""Tests for the top-level app factory + health endpoint.

These cover ``app/main.py`` which the integration test fixtures bypass
(they wire fakes via ``attach_state`` rather than running the real
lifespan). Without these tests, ``create_app()`` is silently uncovered.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.health import router as health_router
from app.main import create_app


def test_create_app_returns_configured_fastapi_instance():
    """The factory should produce a FastAPI app titled like the service
    and with the public endpoints wired in."""
    app = create_app()
    assert isinstance(app, FastAPI)
    assert app.title == "Distributed Event Processing Platform"

    paths = {getattr(r, "path", None) for r in app.routes}
    # All seven public endpoints should be on the app.
    expected = {
        "/health",
        "/health/queue",
        "/events",
        "/events/stats",
        "/events/search",
        "/events/stats/realtime",
    }
    assert expected.issubset(paths)


def test_create_app_installs_rate_limit_middleware():
    """Rate limiter must be on the middleware stack so a production deploy
    doesn't accidentally ship without abuse protection."""
    app = create_app()
    middleware_classes = [m.cls.__name__ for m in app.user_middleware]
    assert "RateLimitMiddleware" in middleware_classes


async def test_health_endpoint_returns_ok_status():
    """The /health body is trivial but contractual — uptime monitors
    poll it and parse the JSON shape."""
    app = FastAPI()
    app.include_router(health_router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_unhandled_exception_handler_returns_500_with_safe_body():
    """An unexpected exception from a route should surface as 500 with a
    generic detail (never leak internals to the client). The handler also
    logs via ``logger.exception``; that's verified by inspection, not asserted
    here."""
    app = create_app()
    # Inject a route that raises an uncaught exception so the global handler
    # fires. The route is added AFTER the handler is registered, which is
    # fine for FastAPI's resolution order.

    @app.get("/test/boom")
    async def boom():
        raise RuntimeError("simulated unhandled failure")

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/test/boom")
    assert resp.status_code == 500
    assert resp.json() == {"detail": "internal server error"}
