"""Zoom meeting provider — creates Zoom meetings and returns join URLs.

Differs from Google Calendar / Microsoft Graph in one important way:
Zoom doesn't manage calendar events. ``create_meeting`` creates a
*Zoom meeting* and returns its ``join_url``. There's no associated
calendar event on Outlook/Google Calendar — the rep is expected to
either share the join URL directly OR pair Zoom with a calendar
provider (which we'll wire as a hybrid mode in a future PR).

For v1, the result includes both ``join_url`` (the Zoom join link)
and an ``ics_payload`` containing a calendar invite with the Zoom URL
embedded, so the rep can drop it into any calendar app even without
a calendar provider connected.

Uses the Integration row for ``provider='zoom'``; OAuth is wired in
``backend/app/api/oauth.py`` (the ``zoom`` entry in CRM_PROVIDERS).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import httpx
from sqlalchemy import select

from backend.app.models import Integration
from backend.app.services.meeting_scheduler.base import (
    MeetingProvider,
    MeetingRequest,
    MeetingResult,
)
from backend.app.services.meeting_scheduler.stub import StubMeetingProvider
from backend.app.services.token_crypto import decrypt_token

logger = logging.getLogger(__name__)


REQUIRED_SCOPE = "meeting:write:meeting"
ZOOM_API_BASE = "https://api.zoom.us/v2"


class ZoomMeetingProvider(MeetingProvider):
    """Zoom Meetings API — creates a meeting, returns the join URL."""

    name = "zoom"

    def __init__(
        self,
        db,
        *,
        tenant_id: uuid.UUID,
        user_id: Optional[uuid.UUID],
    ):
        self._db = db
        self._tenant_id = tenant_id
        self._user_id = user_id

    @classmethod
    async def can_serve(
        cls,
        db,
        *,
        tenant_id: uuid.UUID,
        user_id: Optional[uuid.UUID],
    ) -> bool:
        try:
            integration = await _async_load_integration(db, tenant_id, user_id)
            if integration is None:
                return False
            scopes = integration.scopes or []
            return REQUIRED_SCOPE in scopes
        except Exception:
            logger.exception("ZoomMeetingProvider.can_serve check failed")
            return False

    async def create_meeting(self, request: MeetingRequest) -> MeetingResult:
        try:
            integration = await _async_load_integration(
                self._db, self._tenant_id, self._user_id
            )
            if integration is None:
                return MeetingResult(
                    success=False,
                    provider=self.name,
                    error="no_zoom_integration: user has not connected Zoom",
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to load Zoom integration row")
            return MeetingResult(
                success=False,
                provider=self.name,
                error=f"integration_lookup_failed: {exc!r}",
            )

        access_token = decrypt_token(integration.access_token)
        if not access_token:
            return MeetingResult(
                success=False,
                provider=self.name,
                error="no_access_token",
            )

        body = self._build_meeting_body(request)
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.post(
                    f"{ZOOM_API_BASE}/users/me/meetings",
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                )
        except httpx.HTTPError as exc:
            logger.exception("Zoom meetings POST failed")
            return MeetingResult(
                success=False,
                provider=self.name,
                error=f"http_error: {exc!r}",
            )

        if response.status_code >= 400:
            body_excerpt = response.text[:300]
            logger.warning(
                "Zoom meetings POST returned %d: %s",
                response.status_code, body_excerpt,
            )
            return MeetingResult(
                success=False,
                provider=self.name,
                error=f"zoom_error_{response.status_code}: {body_excerpt}",
            )

        return self._result_from_meeting(response.json(), request)

    # ── Internal helpers ────────────────────────────────────────────

    def _build_meeting_body(self, request: MeetingRequest) -> Dict[str, Any]:
        """Construct the Zoom create-meeting body.

        ``type=2`` is a scheduled meeting at a specific time; ``type=1``
        is an instant meeting (we never want this for action items).
        """
        start = request.start or datetime.now(timezone.utc) + timedelta(hours=1)

        body: Dict[str, Any] = {
            "topic": request.subject[:200],  # Zoom caps topic length
            "type": 2,
            "start_time": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "duration": request.duration_minutes,
            "timezone": "UTC",
            "agenda": (request.body or "")[:2000],  # Zoom caps agenda
            "settings": {
                "host_video": True,
                "participant_video": True,
                "join_before_host": False,
                "mute_upon_entry": True,
                "waiting_room": False,
                "approval_type": 2,  # no registration
                "audio": "both",
            },
        }
        return body

    def _result_from_meeting(
        self, meeting: Dict[str, Any], request: MeetingRequest
    ) -> MeetingResult:
        """Wrap the Zoom response with an ICS payload for calendar import."""
        join_url = meeting.get("join_url")
        meeting_id = str(meeting.get("id")) if meeting.get("id") is not None else None

        # Embed the Zoom join URL into a calendar invite so the rep can
        # drop the .ics into any calendar app even without a separate
        # calendar provider connected. The stub provider handles ICS
        # generation cleanly; reuse it.
        ics_request = MeetingRequest(
            subject=request.subject,
            body=(
                f"Join Zoom Meeting:\n{join_url}\n\n"
                f"Meeting ID: {meeting_id}\n"
                f"Passcode: {meeting.get('password', '(none)')}\n\n"
                f"{request.body or ''}"
            ),
            organizer_email=request.organizer_email,
            participants=request.participants,
            start=request.start,
            duration_minutes=request.duration_minutes,
            location=join_url,  # Zoom URL goes in the LOCATION line per RFC convention
            conference_provider="zoom",
        )
        ics = StubMeetingProvider()._build_ics(ics_request)  # noqa: SLF001 — intentional reuse

        return MeetingResult(
            success=True,
            provider=self.name,
            event_id=meeting_id,
            join_url=join_url,
            html_link=meeting.get("start_url"),  # host start link as a stand-in
            ics_payload=ics,
            note=(
                "Zoom meeting created. The ICS payload includes the join "
                "URL — drop it into Google Calendar / Outlook to put the "
                "meeting on the rep's calendar."
            ),
        )


# ── internal helpers ────────────────────────────────────────────────────


async def _async_load_integration(
    db, tenant_id: uuid.UUID, user_id: Optional[uuid.UUID]
) -> Optional[Integration]:
    """Look up the Zoom integration row for this user / tenant."""
    stmt = (
        select(Integration)
        .where(
            Integration.tenant_id == tenant_id,
            Integration.provider == "zoom",
        )
        .order_by(Integration.created_at.desc())
    )
    if user_id is not None:
        stmt = stmt.where(Integration.user_id == user_id)
    result = await db.execute(stmt)
    return result.scalars().first()
