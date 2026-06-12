"""Tests for the meeting scheduler — abstraction, stub, and dispatch."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone


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


# ── Zoom provider ───────────────────────────────────────────────────────


def test_zoom_meeting_body_basic_shape():
    from backend.app.services.meeting_scheduler.zoom import ZoomMeetingProvider

    provider = ZoomMeetingProvider(
        db=None, tenant_id=uuid.uuid4(), user_id=uuid.uuid4()
    )
    body = provider._build_meeting_body(_basic_request())
    assert body["topic"] == "Test meeting"
    assert body["type"] == 2  # scheduled
    assert body["duration"] == 45
    assert body["timezone"] == "UTC"
    assert "agenda" in body
    # Settings defaults — host video on, no waiting room, audio both.
    assert body["settings"]["host_video"] is True
    assert body["settings"]["audio"] == "both"


def test_zoom_meeting_body_caps_topic_and_agenda_length():
    from backend.app.services.meeting_scheduler.zoom import ZoomMeetingProvider

    long_subject = "x" * 500
    long_body = "y" * 5000
    request = _basic_request()
    request.subject = long_subject
    request.body = long_body
    provider = ZoomMeetingProvider(
        db=None, tenant_id=uuid.uuid4(), user_id=uuid.uuid4()
    )
    body = provider._build_meeting_body(request)
    assert len(body["topic"]) <= 200
    assert len(body["agenda"]) <= 2000


def test_zoom_result_includes_join_url_and_ics():
    from backend.app.services.meeting_scheduler.zoom import ZoomMeetingProvider

    provider = ZoomMeetingProvider(
        db=None, tenant_id=uuid.uuid4(), user_id=uuid.uuid4()
    )
    zoom_response = {
        "id": 8123456789,
        "join_url": "https://us05web.zoom.us/j/8123456789",
        "start_url": "https://us05web.zoom.us/s/8123456789?zak=...",
        "password": "abc123",
    }
    result = provider._result_from_meeting(zoom_response, _basic_request())
    assert result.success is True
    assert result.event_id == "8123456789"
    assert result.join_url == "https://us05web.zoom.us/j/8123456789"
    assert result.html_link == zoom_response["start_url"]
    # ICS payload is generated for calendar import.
    assert result.ics_payload is not None
    assert "BEGIN:VCALENDAR" in result.ics_payload
    assert "8123456789" in result.ics_payload  # join URL embedded


def test_zoom_result_handles_missing_password():
    from backend.app.services.meeting_scheduler.zoom import ZoomMeetingProvider

    provider = ZoomMeetingProvider(
        db=None, tenant_id=uuid.uuid4(), user_id=uuid.uuid4()
    )
    zoom_response = {
        "id": 1,
        "join_url": "https://zoom.us/j/1",
        "start_url": "https://zoom.us/s/1",
        # password missing
    }
    result = provider._result_from_meeting(zoom_response, _basic_request())
    assert result.success is True


def test_oauth_zoom_provider_registered():
    """Smoke test: Zoom is registered in the OAuth provider table so
    the connect flow can resolve its authorize URL."""
    from backend.app.api.oauth import CRM_PROVIDERS, SUPPORTED_PROVIDERS

    assert "zoom" in CRM_PROVIDERS
    cfg = CRM_PROVIDERS["zoom"]
    assert "authorize_url" in cfg
    assert "token_url" in cfg
    assert "meeting:write:meeting" in cfg["scopes"]
    assert "zoom" in SUPPORTED_PROVIDERS


# ── Cal.com provider ────────────────────────────────────────────────────


def test_calcom_picks_customer_attendee_over_vendor():
    from backend.app.services.meeting_scheduler.cal_com import CalcomProvider

    provider = CalcomProvider(
        db=None, tenant_id=uuid.uuid4(), user_id=uuid.uuid4()
    )
    request = _basic_request(
        participants=[
            MeetingParticipant(name="Vendor Person", email="v@x.com", side="vendor"),
            MeetingParticipant(name="Customer Person", email="c@x.com", side="customer"),
        ],
    )
    attendee = provider._pick_customer_attendee(request)
    assert attendee.email == "c@x.com"


def test_calcom_falls_back_to_any_email_when_no_customer():
    from backend.app.services.meeting_scheduler.cal_com import CalcomProvider

    provider = CalcomProvider(
        db=None, tenant_id=uuid.uuid4(), user_id=uuid.uuid4()
    )
    request = _basic_request(
        participants=[
            MeetingParticipant(name="Vendor Only", email="v@x.com", side="vendor"),
        ],
    )
    attendee = provider._pick_customer_attendee(request)
    assert attendee.email == "v@x.com"


def test_calcom_returns_none_when_no_emails():
    from backend.app.services.meeting_scheduler.cal_com import CalcomProvider

    provider = CalcomProvider(
        db=None, tenant_id=uuid.uuid4(), user_id=uuid.uuid4()
    )
    request = _basic_request(
        participants=[
            MeetingParticipant(name="No Email", email=None, side="customer"),
        ],
    )
    assert provider._pick_customer_attendee(request) is None


def test_calcom_booking_body_shape():
    from backend.app.services.meeting_scheduler.cal_com import CalcomProvider

    provider = CalcomProvider(
        db=None, tenant_id=uuid.uuid4(), user_id=uuid.uuid4()
    )
    attendee = MeetingParticipant(name="Customer", email="c@x.com", side="customer")
    body = provider._build_booking_body(
        event_type_id=42,
        request=_basic_request(),
        attendee=attendee,
    )
    assert body["eventTypeId"] == 42
    assert body["attendee"]["email"] == "c@x.com"
    assert body["attendee"]["timeZone"] == "UTC"
    assert body["lengthInMinutes"] == 45
    assert body["metadata"]["source"] == "linda_action_item"


def test_calcom_result_from_booking_v2_shape():
    from backend.app.services.meeting_scheduler.cal_com import CalcomProvider

    provider = CalcomProvider(
        db=None, tenant_id=uuid.uuid4(), user_id=uuid.uuid4()
    )
    payload = {
        "data": {
            "uid": "abc-uid-123",
            "meetingUrl": "https://meet.example.com/xyz",
            "rescheduleLink": "https://cal.com/reschedule/abc",
        }
    }
    result = provider._result_from_booking(payload)
    assert result.success is True
    assert result.event_id == "abc-uid-123"
    assert result.join_url == "https://meet.example.com/xyz"
    assert result.html_link == "https://cal.com/reschedule/abc"


def test_calcom_result_from_booking_v1_shape():
    """Older Cal.com v1 returns booking data at the top level."""
    from backend.app.services.meeting_scheduler.cal_com import CalcomProvider

    provider = CalcomProvider(
        db=None, tenant_id=uuid.uuid4(), user_id=uuid.uuid4()
    )
    payload = {
        "id": 99,
        "location": "https://zoom.us/j/123456",
    }
    result = provider._result_from_booking(payload)
    assert result.event_id == "99"
    assert result.join_url == "https://zoom.us/j/123456"


def test_calcom_result_extracts_video_url_from_references():
    """Cal.com surfaces Google Meet / Zoom / Daily URLs in references."""
    from backend.app.services.meeting_scheduler.cal_com import CalcomProvider

    provider = CalcomProvider(
        db=None, tenant_id=uuid.uuid4(), user_id=uuid.uuid4()
    )
    payload = {
        "data": {
            "uid": "x",
            "references": [
                {"type": "daily_video", "meetingUrl": "https://daily.co/abc"},
            ],
        }
    }
    result = provider._result_from_booking(payload)
    assert result.join_url == "https://daily.co/abc"
