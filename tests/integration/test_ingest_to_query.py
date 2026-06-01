"""End-to-end: POST /events → worker → GET /events round-trips a row."""

import pytest

from tests.helpers import drain_queue

pytestmark = pytest.mark.integration


async def test_ingest_then_list_returns_event(client, stack):
    payload = {
        "event_type": "Click",
        "user_id": "user-42",
        "source_url": "https://feathr.co/blog/post-1",
        "metadata": {"browser": "chrome", "campaign": "spring-2026"},
    }
    resp = await client.post("/events", json=payload)
    assert resp.status_code == 202
    event_id = resp.json()["event_id"]
    assert event_id

    await drain_queue(stack["queue"])

    resp = await client.get("/events", params={"user_id": "user-42", "limit": 10})
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    item = body["items"][0]
    assert item["event_id"] == event_id
    assert item["event_type"] == "click"
    assert item["metadata"]["campaign"] == "spring-2026"


async def test_ingest_then_stats_aggregates_count(client, stack):
    base_payload = {
        "event_type": "click",
        "user_id": "user-7",
        "source_url": "https://example.com",
    }
    for _ in range(3):
        r = await client.post("/events", json=base_payload)
        assert r.status_code == 202
    await drain_queue(stack["queue"])

    # Use a wide range to avoid clock-skew flakiness.
    resp = await client.get(
        "/events/stats",
        params={
            "start": "2000-01-01T00:00:00Z",
            "end": "2100-01-01T00:00:00Z",
            "bucket": "day",
        },
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    total = sum(i["count"] for i in items if i["event_type"] == "click")
    assert total == 3


async def test_ingest_rejects_invalid_payload(client):
    resp = await client.post(
        "/events",
        json={"event_type": "click", "user_id": "u1", "source_url": "not a url"},
    )
    assert resp.status_code == 422


async def test_stats_requires_window(client):
    resp = await client.get("/events/stats")
    assert resp.status_code == 422
