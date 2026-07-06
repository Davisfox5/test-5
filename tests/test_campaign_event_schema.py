"""Schema tests for campaign event ingestion input.

``EventIn.occurred_at`` must always come out UTC-aware — a naive value
would be stored/serialized without an offset and re-parsed as local
time by consumers (same convention as ``OutcomeEvent.occurred_at``).
"""

from datetime import datetime, timezone

from backend.app.api.campaigns import EventIn


def test_event_in_normalizes_naive_occurred_at_to_utc():
    ev = EventIn(
        event_type="open",
        occurred_at=datetime(2026, 7, 1, 10, 0, 0),  # naive
    )
    assert ev.occurred_at.tzinfo is not None
    assert ev.occurred_at.utcoffset().total_seconds() == 0


def test_event_in_passes_aware_occurred_at_through():
    aware = datetime(2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc)
    ev = EventIn(event_type="click", occurred_at=aware)
    assert ev.occurred_at == aware


def test_event_in_occurred_at_optional():
    ev = EventIn(event_type="reply")
    assert ev.occurred_at is None
