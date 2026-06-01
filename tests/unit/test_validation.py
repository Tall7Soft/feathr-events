import pytest
from pydantic import ValidationError

from app.models.event import EventIn, StoredEvent


def test_event_in_normalizes_event_type_case():
    e = EventIn(event_type="PageView", user_id="u1", source_url="https://example.com")
    assert e.event_type == "pageview"


def test_event_in_rejects_extra_fields():
    with pytest.raises(ValidationError):
        EventIn(
            event_type="click",
            user_id="u1",
            source_url="https://example.com",
            unexpected="x",
        )


def test_event_in_rejects_bad_url():
    with pytest.raises(ValidationError):
        EventIn(event_type="click", user_id="u1", source_url="not a url")


def test_event_in_caps_metadata_keys():
    big_meta = {f"k{i}": i for i in range(100)}
    with pytest.raises(ValidationError):
        EventIn(
            event_type="click",
            user_id="u1",
            source_url="https://example.com",
            metadata=big_meta,
        )


def test_stored_event_from_input_assigns_timestamp_and_id():
    e = StoredEvent.from_input(
        EventIn(event_type="click", user_id="u1", source_url="https://example.com")
    )
    assert e.event_id
    assert e.timestamp is not None
    assert e.ingested_at is not None
