"""End-to-end: search lifecycle + realtime cache behavior."""

import pytest

from tests.helpers import drain_queue

pytestmark = pytest.mark.integration


async def test_ingest_then_full_text_search(client, stack):
    payloads = [
        {
            "event_type": "click",
            "user_id": "u1",
            "source_url": "https://feathr.co/pricing",
            "metadata": {"campaign": "spring-launch", "label": "hero-cta"},
        },
        {
            "event_type": "click",
            "user_id": "u2",
            "source_url": "https://feathr.co/blog",
            "metadata": {"campaign": "winter-retention", "label": "footer-cta"},
        },
    ]
    for p in payloads:
        r = await client.post("/events", json=p)
        assert r.status_code == 202
    await drain_queue(stack["queue"])

    resp = await client.get("/events/search", params={"q": "spring-launch"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["items"][0]["metadata"]["campaign"] == "spring-launch"


async def test_realtime_stats_caches_subsequent_calls(client, stack):
    for _ in range(2):
        r = await client.post(
            "/events",
            json={
                "event_type": "click",
                "user_id": "u1",
                "source_url": "https://example.com",
            },
        )
        assert r.status_code == 202
    await drain_queue(stack["queue"])

    first = await client.get("/events/stats/realtime", params={"window_seconds": 60})
    second = await client.get("/events/stats/realtime", params={"window_seconds": 60})
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["cache_hit"] is False
    assert second.json()["cache_hit"] is True
    assert first.json()["total"] == 2
    assert second.json()["total"] == 2


async def test_queue_health_reports_processed_events(client, stack):
    await client.post(
        "/events",
        json={
            "event_type": "click",
            "user_id": "u1",
            "source_url": "https://example.com",
        },
    )
    await drain_queue(stack["queue"])
    resp = await client.get("/health/queue")
    body = resp.json()
    assert body["processed"] == 1
    assert body["dead_lettered"] == 0
