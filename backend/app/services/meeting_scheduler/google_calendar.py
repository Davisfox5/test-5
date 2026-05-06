"""Google Calendar provider — creates events with auto-generated
Google Meet links via the Calendar API's ``conferenceData`` mechanism.

Uses the existing ``Integration`` row for ``provider='google'``,
which the OAuth flow in ``backend/app/api/oauth.py`` populates with
the ``calendar.events`` scope already granted at first connect.
Tokens are AES-encrypted at rest; we decrypt + refresh on each call
via the ``google-auth`` library, which handles expiry transparently.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from backend.app.models import Integration
from backend.app.services.meeting_scheduler.base import (
    MeetingProvider,
    MeetingRequest,
    MeetingResult,
)
from backend.app.services.token_crypto import decrypt_token

logger = logging.getLogger(__name__)


REQUIRED_SCOPE = "https://www.googleapis.com/auth/calendar.events"


class GoogleCalendarProvider(MeetingProvider):
    """Real Google Calendar API integration. Auto-generates Meet links."""

    name = "google_calendar"

    def __init__(self, db, *, tenant_id: uuid.UUID, user_id: Optional[uuid.UUID]):
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
        """Confirm the user has a Google integration with calendar scope."""
        try:
            integration = await _async_load_integration(db, tenant_id, user_id)
            if integration is None:
                return False
            scopes = integration.scopes or []
            # Some Google OAuth flows return granted_scopes shorter than
            # the requested set if the user de-selected a box. Without
            # the calendar scope, we can't create events.
            return REQUIRED_SCOPE in scopes
        except Exception:
            logger.exception("GoogleCalendarProvider.can_serve check failed")
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
                    error="no_google_integration: user has not connected Google",
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to load Google integration row")
            return MeetingResult(
                success=False,
                provider=self.name,
                error=f"integration_lookup_failed: {exc!r}",
            )

        # The google-api-python-client + google-auth libraries are sync.
        # Run the blocking work in a thread so we don't stall the event
        # loop for the duration of the API call.
        return await asyncio.to_thread(
            self._create_sync, integration, request
        )

    def _create_sync(self, integration: Integration, request: MeetingRequest) -> MeetingResult:

        try:
            credentials = self._build_credentials(integration)
        except _CredentialError as exc:
            return MeetingResult(
                success=False,
                provider=self.name,
                error=str(exc),
            )

        try:
            from googleapiclient.discovery import build  # type: ignore
            service = build("calendar", "v3", credentials=credentials, cache_discovery=False)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to build Google Calendar service")
            return MeetingResult(
                success=False,
                provider=self.name,
                error=f"calendar_client_init_failed: {exc!r}",
            )

        body = self._build_event_body(request)
        try:
            event = (
                service.events()
                .insert(
                    calendarId="primary",
                    body=body,
                    conferenceDataVersion=1,  # required to honor conferenceData
                    sendUpdates="all",
                )
                .execute()
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Google Calendar event insert failed")
            return MeetingResult(
                success=False,
                provider=self.name,
                error=f"calendar_insert_failed: {exc!r}",
            )

        return self._result_from_event(event)

    # ── internal helpers ────────────────────────────────────────────

    def _build_credentials(self, integration: Integration):
        try:
            from google.oauth2.credentials import Credentials  # type: ignore
        except ImportError as exc:
            raise _CredentialError(
                f"google-auth library missing: {exc!r}"
            ) from exc

        access = decrypt_token(integration.access_token)
        refresh = decrypt_token(integration.refresh_token)
        client_id = os.environ.get("GOOGLE_CLIENT_ID")
        client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
        if not access:
            raise _CredentialError("no_access_token")
        return Credentials(
            token=access,
            refresh_token=refresh,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=list(integration.scopes or [REQUIRED_SCOPE]),
        )

    def _build_event_body(self, request: MeetingRequest) -> Dict[str, Any]:
        """Construct the events.insert body, including a conferenceData
        request that triggers Google Meet link generation."""
        attendees = [
            {"email": p.email, "displayName": p.name}
            for p in request.participants
            if p.email
        ]

        body: Dict[str, Any] = {
            "summary": request.subject,
            "description": request.body,
            "attendees": attendees,
            "guestsCanModify": False,
            "reminders": {"useDefault": True},
        }
        if request.location:
            body["location"] = request.location

        # When start is provided, build precise start/end. When it isn't,
        # Google requires a start time; default to "in 1 hour" so the
        # event lands somewhere reasonable for the rep to adjust.
        from datetime import datetime, timedelta, timezone
        start = request.start or datetime.now(timezone.utc) + timedelta(hours=1)
        end = start + timedelta(minutes=request.duration_minutes)
        body["start"] = {"dateTime": start.isoformat(), "timeZone": "UTC"}
        body["end"] = {"dateTime": end.isoformat(), "timeZone": "UTC"}

        # Auto-generate a Google Meet link unless the caller explicitly
        # opted out. ``conferenceDataVersion=1`` on the insert call is
        # required for this to take effect.
        if request.conference_provider != "none":
            body["conferenceData"] = {
                "createRequest": {
                    "requestId": f"linda-{uuid.uuid4()}",
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                }
            }
        return body

    def _result_from_event(self, event: Dict[str, Any]) -> MeetingResult:
        """Pull join URL + event id out of the API response."""
        join_url: Optional[str] = None
        conference = event.get("conferenceData") or {}
        for entry in conference.get("entryPoints", []) or []:
            if entry.get("entryPointType") == "video" and entry.get("uri"):
                join_url = entry["uri"]
                break
        # Fallback: hangoutLink is the legacy field that older API
        # versions return when entryPoints is empty.
        if not join_url:
            join_url = event.get("hangoutLink")

        return MeetingResult(
            success=True,
            provider=self.name,
            event_id=event.get("id"),
            join_url=join_url,
            html_link=event.get("htmlLink"),
        )


# ── internal helpers ────────────────────────────────────────────────────


async def _async_load_integration(
    db, tenant_id: uuid.UUID, user_id: Optional[uuid.UUID]
) -> Optional[Integration]:
    """Look up the Google integration row for this user (or tenant-level
    when ``user_id`` is None). Most-recently-created wins when several
    rows exist for the same tenant + user."""
    stmt = (
        select(Integration)
        .where(
            Integration.tenant_id == tenant_id,
            Integration.provider == "google",
        )
        .order_by(Integration.created_at.desc())
    )
    if user_id is not None:
        stmt = stmt.where(Integration.user_id == user_id)
    result = await db.execute(stmt)
    return result.scalars().first()


class _CredentialError(RuntimeError):
    pass
