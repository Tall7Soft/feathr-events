"""Application entrypoint.

Builds the FastAPI app, attaches the rate-limit middleware, mounts the
three public routers (health, ingestion, queries), and registers a
last-resort exception handler that prevents internal errors from
leaking to clients.

Production: ``uvicorn app.main:app`` (or via the Docker ``CMD``).
Tests: import :func:`create_app` directly — fixtures attach an
in-memory ``app.state`` via :func:`app.lifecycle.attach_state` instead
of running the real lifespan.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.health import router as health_router
from app.api.ingestion import router as ingestion_router
from app.api.queries import router as queries_router
from app.config import get_settings
from app.lifecycle import lifespan
from app.middleware.rate_limit import RateLimitMiddleware

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    """Construct and configure the FastAPI application.

    Wires up:

    1. Settings (via :func:`app.config.get_settings`).
    2. The :class:`RateLimitMiddleware` with the configured
       per-minute budget.
    3. The three routers — health, ingestion, queries.
    4. A global ``Exception`` handler that logs the traceback and
       returns a generic 500 so internal failure modes never leak
       implementation detail to the caller.

    The startup / shutdown sequence (Mongo / ES / Redis clients +
    worker pool) is owned by :func:`app.lifecycle.lifespan`, which
    FastAPI invokes via the ``lifespan=`` argument.
    """
    settings = get_settings()
    app = FastAPI(
        title="Distributed Event Processing Platform",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(RateLimitMiddleware, requests_per_minute=settings.rate_limit_per_minute)
    app.include_router(health_router)
    app.include_router(ingestion_router)
    app.include_router(queries_router)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """Last-resort handler for any uncaught exception.

        Logs the full traceback so on-call has something to
        investigate, and returns a generic 500 so internal error
        messages never reach the client.
        """
        logger.exception("api.unhandled", extra={"path": request.url.path})
        return JSONResponse(status_code=500, content={"detail": "internal server error"})

    return app


app = create_app()
