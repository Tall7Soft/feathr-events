"""FastAPI dependency providers.

Keeping these in one place makes route handlers thin and lets tests
override individual components via ``app.dependency_overrides``.
"""

from __future__ import annotations

from fastapi import Request

from app.cache import RealtimeStatsCache
from app.config import Settings
from app.queue import InMemoryQueue
from app.storage.protocols import EventSearchStore, EventStore
from app.workers import EventWorker


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_queue(request: Request) -> InMemoryQueue:
    return request.app.state.queue


def get_dlq(request: Request) -> InMemoryQueue:
    return request.app.state.dlq


def get_store(request: Request) -> EventStore:
    return request.app.state.store


def get_search(request: Request) -> EventSearchStore:
    return request.app.state.search


def get_cache(request: Request) -> RealtimeStatsCache:
    return request.app.state.cache


def get_worker(request: Request) -> EventWorker:
    return request.app.state.worker
