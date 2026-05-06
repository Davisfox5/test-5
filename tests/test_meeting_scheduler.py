"""Tests for the meeting scheduler — abstraction, stub, and dispatch."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from backend.app.services.meeting_scheduler.base import (
    MeetingParticipant,
    MeetingProvider,
    MeetingRequest,
    MeetingResult,
)
from backend.app.services.meeting_scheduler.participant_resolver import (
    _name_matches,
    _normalize,
)
from backend.app.services.meeting_scheduler.scheduler import (
    MeetingScheduler,
    _provider_class_by_name,
)
from backend.app.services.meeting_scheduler.stub import StubMeetingProvider


# ── Stub provider ────────────────────────────────────────────────────────


def _basic_request(**overrides) -> MeetingRequest:
    defaults = dict(
        subject="Test meeting",
        body="Discuss next steps.",
        organizer_email="rep@example.com",
        participants=[
            MeetingParticipant(name="Customer One", email="cust1@example.com",
                               role="champion", side="customer"),
            MeetingParticipant(name="Specialist", email="se@example.com",
                               role="specialist", side="vendor"),
        ],
        start=datetime(2026, 5, 10, 14, 0, tzinfo=timezone.utc),
        duration_minutes=45,
        conference_provider="google_meet",
    )
    defaults.update(overrides)
    return MeetingRequest(**defaults)


def test_stub_can_serve_always_true():
    tid = uuid.uuid4()
    uid = uuid.uuid4()
    assert asyncio.run(StubMeetingProvider.can_serve(None, tenant_id=tid, user_id=uid)) is True


def test_stub_create_meeting_returns_ics_payload():
    provider = StubMeetingProvider()
    result = asyncio.run(provider.create_meeting(_basic_request()))
    assert result.success is True
    assert result.provider == "stub"
    assert result.ics_payload is not None
    assert "BEGIN:VCALENDAR" in result.ics_payload
    assert "END:VCALENDAR" in result.ics_payload
    assert "Test meeting" in result.ics_payload
    assert "cust1@example.com" in result.ics_payload
    assert "se@example.com" in result.ics_payload
    assert result.note is not None
    assert "no calendar provider" in result.note.lower()


def test_stub_skips_participants_without_email():
    request = _basic_request(
        participants=[
            MeetingParticipant(name="No Email", email=None, side="customer"),
            MeetingParticipant(name="With Email", email="ok@x.com", side="customer"),
        ],
    )
    result = asyncio.run(StubMeetingProvider().create_meeting(request))
    assert "ok@x.com" in result.ics_payload
    assert "No Email" not in result.ics_payload  # no ATTENDEE line for missing email


def test_stub_handles_missing_start_time():
    request = _basic_request(start=None)
    result = asyncio.run(StubMeetingProvider().create_meeting(request))
    assert result.success is True
    # Should still produce a valid ICS with DTSTART/DTEND.
    assert "DTSTART:" in result.ics_payload
    assert "DTEND:" in result.ics_payload


def test_stub_includes_conference_provider_hint_in_body():
    result = asyncio.run(
        StubMeetingProvider().create_meeting(_basic_request(conference_provider="zoom"))
    )
    # The hint is escaped into the description; we just verify it
    # appears somewhere in the payload.
    assert "zoom" in result.ics_payload.lower()


# ── Scheduler dispatch ──────────────────────────────────────────────────


class _NeverServes(MeetingProvider):
    """Test double: a provider that's never available."""

    name = "never_serves"

    @classmethod
    async def can_serve(cls, db, *, tenant_id, user_id):
        return False

    async def create_meeting(self, request):
        return MeetingResult(success=False, provider=self.name, error="should not be called")


class _AlwaysServes(MeetingProvider):
    """Test double: a provider that's always available."""

    name = "always_serves"
    last_request: MeetingRequest = None

    def __init__(self, db=None, **kwargs):
        pass

    @classmethod
    async def can_serve(cls, db, *, tenant_id, user_id):
        return True

    async def create_meeting(self, request):
        _AlwaysServes.last_request = request
        return MeetingResult(
            success=True,
            provider=self.name,
            event_id="abc-123",
            join_url="https://meet.example/abc",
        )


def test_scheduler_falls_back_to_stub_when_nothing_serves(monkeypatch):
    from backend.app.services.meeting_scheduler import scheduler as scheduler_mod

    monkeypatch.setattr(
        scheduler_mod,
        "_PROVIDER_ORDER",
        [_NeverServes, StubMeetingProvider],
    )

    sched = MeetingScheduler(db=None, tenant_id=uuid.uuid4(), user_id=uuid.uuid4())
    result = asyncio.run(sched.create_meeting(_basic_request()))
    assert result.provider == "stub"
    assert result.success is True


def test_scheduler_picks_first_available_real_provider(monkeypatch):
    from backend.app.services.meeting_scheduler import scheduler as scheduler_mod

    monkeypatch.setattr(
        scheduler_mod,
        "_PROVIDER_ORDER",
        [_NeverServes, _AlwaysServes, StubMeetingProvider],
    )

    sched = MeetingScheduler(db=None, tenant_id=uuid.uuid4(), user_id=uuid.uuid4())
    result = asyncio.run(sched.create_meeting(_basic_request()))
    assert result.provider == "always_serves"
    assert result.event_id == "abc-123"


def test_scheduler_honors_preferred_provider_when_servable(monkeypatch):
    from backend.app.services.meeting_scheduler import scheduler as scheduler_mod

    monkeypatch.setattr(
        scheduler_mod,
        "_PROVIDER_ORDER",
        [_AlwaysServes, StubMeetingProvider],
    )

    sched = MeetingScheduler(
        db=None,
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        preferred_provider="always_serves",
    )
    result = asyncio.run(sched.create_meeting(_basic_request()))
    assert result.provider == "always_serves"


def test_scheduler_falls_through_when_preferred_cannot_serve(monkeypatch):
    from backend.app.services.meeting_scheduler import scheduler as scheduler_mod

    monkeypatch.setattr(
        scheduler_mod,
        "_PROVIDER_ORDER",
        [_NeverServes, StubMeetingProvider],
    )

    sched = MeetingScheduler(
        db=None,
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        preferred_provider="never_serves",  # preferred but can't serve
    )
    result = asyncio.run(sched.create_meeting(_basic_request()))
    assert result.provider == "stub"


def test_provider_class_by_name_finds_known():
    # Stub is always in the default order.
    assert _provider_class_by_name("stub") is StubMeetingProvider


def test_provider_class_by_name_returns_none_for_unknown():
    assert _provider_class_by_name("totally_made_up") is None


# ── Participant resolver helpers ────────────────────────────────────────


def test_name_normalize_lowercases_and_collapses():
    assert _normalize("  Sarah   Chen  ") == "sarah chen"


def test_name_match_substring():
    assert _name_matches("Sarah Chen", "Sarah") is True
    assert _name_matches("Sarah", "Sarah Chen") is True


def test_name_match_token_share():
    # Different first-name spellings, shared last name.
    assert _name_matches("Sarah Chen", "Sara Chen") is True
    # Initialism + token share.
    assert _name_matches("Sarah Chen", "S Chen") is True


def test_name_match_no_overlap():
    assert _name_matches("Sarah Chen", "Mark Patel") is False


def test_name_match_handles_none():
    assert _name_matches(None, "Anything") is False


def test_name_match_handles_empty():
    assert _name_matches("", "Sarah") is False
    assert _name_matches("Sarah", "") is False


# ── Microsoft Graph provider — body shape ──────────────────────────────


def test_microsoft_graph_event_body_includes_teams_meeting_flags():
    from backend.app.services.meeting_scheduler.microsoft_graph import (
        MicrosoftGraphProvider,
    )

    provider = MicrosoftGraphProvider(
        db=None, tenant_id=uuid.uuid4(), user_id=uuid.uuid4()
    )
    body = provider._build_event_body(_basic_request())
    assert body["subject"] == "Test meeting"
    assert body["isOnlineMeeting"] is True
    assert body["onlineMeetingProvider"] == "teamsForBusiness"
    assert body["start"]["timeZone"] == "UTC"
    assert body["end"]["timeZone"] == "UTC"
    # Both participants with emails should land as required attendees.
    emails = {a["emailAddress"]["address"] for a in body["attendees"]}
    assert emails == {"cust1@example.com", "se@example.com"}


def test_microsoft_graph_event_body_omits_teams_when_conference_none():
    from backend.app.services.meeting_scheduler.microsoft_graph import (
        MicrosoftGraphProvider,
    )

    provider = MicrosoftGraphProvider(
        db=None, tenant_id=uuid.uuid4(), user_id=uuid.uuid4()
    )
    body = provider._build_event_body(_basic_request(conference_provider="none"))
    assert "isOnlineMeeting" not in body
    assert "onlineMeetingProvider" not in body


def test_microsoft_graph_event_body_skips_participants_without_email():
    from backend.app.services.meeting_scheduler.microsoft_graph import (
        MicrosoftGraphProvider,
    )

    request = _basic_request(
        participants=[
            MeetingParticipant(name="No Email", email=None, side="customer"),
            MeetingParticipant(name="Has Email", email="ok@x.com", side="customer"),
        ],
    )
    provider = MicrosoftGraphProvider(
        db=None, tenant_id=uuid.uuid4(), user_id=uuid.uuid4()
    )
    body = provider._build_event_body(request)
    assert len(body["attendees"]) == 1
    assert body["attendees"][0]["emailAddress"]["address"] == "ok@x.com"


def test_microsoft_graph_event_body_marks_optional_attendees():
    from backend.app.services.meeting_scheduler.microsoft_graph import (
        MicrosoftGraphProvider,
    )

    request = _basic_request(
        participants=[
            MeetingParticipant(name="Required", email="r@x.com", role="required"),
            MeetingParticipant(name="Optional", email="o@x.com", role="optional"),
        ],
    )
    provider = MicrosoftGraphProvider(
        db=None, tenant_id=uuid.uuid4(), user_id=uuid.uuid4()
    )
    body = provider._build_event_body(request)
    by_email = {a["emailAddress"]["address"]: a["type"] for a in body["attendees"]}
    assert by_email["o@x.com"] == "optional"
    assert by_email["r@x.com"] == "required"


def test_microsoft_graph_result_pulls_join_url_from_online_meeting():
    from backend.app.services.meeting_scheduler.microsoft_graph import (
        MicrosoftGraphProvider,
    )

    provider = MicrosoftGraphProvider(
        db=None, tenant_id=uuid.uuid4(), user_id=uuid.uuid4()
    )
    event = {
        "id": "AAMkADX==",
        "webLink": "https://outlook.office.com/calendar/...",
        "onlineMeeting": {"joinUrl": "https://teams.microsoft.com/l/meetup-join/abc"},
    }
    result = provider._result_from_event(event)
    assert result.success is True
    assert result.event_id == "AAMkADX=="
    assert result.join_url == "https://teams.microsoft.com/l/meetup-join/abc"
    assert result.html_link == "https://outlook.office.com/calendar/..."


def test_microsoft_graph_result_falls_back_to_legacy_field():
    from backend.app.services.meeting_scheduler.microsoft_graph import (
        MicrosoftGraphProvider,
    )

    provider = MicrosoftGraphProvider(
        db=None, tenant_id=uuid.uuid4(), user_id=uuid.uuid4()
    )
    # Older Graph responses surface ``onlineMeetingUrl`` instead of
    # the nested ``onlineMeeting.joinUrl``.
    event = {"id": "x", "onlineMeetingUrl": "https://teams.microsoft.com/legacy"}
    result = provider._result_from_event(event)
    assert result.join_url == "https://teams.microsoft.com/legacy"
