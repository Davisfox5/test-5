"""Microsoft Graph calendar provider — creates events with embedded
Microsoft Teams meeting links.

Uses the existing ``Integration`` row for ``provider='microsoft'``,
which the OAuth flow in ``backend/app/api/oauth.py`` populates with
the ``Calendars.ReadWrite`` scope already granted at first connect.
The Graph API endpoint POST /me/events accepts an ``isOnlineMeeting``
+ ``onlineMeetingProvider='teamsForBusiness'`` pair to auto-generate
a Teams join URL on event creation.

Tokens are AES-encrypted at rest. We decrypt and call the API directly
via httpx (async). Token refresh on 401 is left to a future PR — for
the launch case, freshly-connected tokens stay valid for ~1 hour and
re-authentication via the OAuth flow is the recovery path.
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
from backend.app.services.token_crypto import decrypt_token

logger = logging.getLogger(__name__)


REQUIRED_SCOPE = "Calendars.ReadWrite"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"


class MicrosoftGraphProvider(MeetingProvider):
    """Microsoft Graph calendar with Teams meeting links."""

    name = "microsoft_graph"

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
            # Some users may have granted a narrower scope set than the
            # OAuth flow requested. Without Calendars.ReadWrite we can't
            # create events.
            return REQUIRED_SCOPE in scopes
        except Exception:
            logger.exception("MicrosoftGraphProvider.can_serve check failed")
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
                    error="no_microsoft_integration: user has not connected Microsoft 365",
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to load Microsoft integration row")
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

        body = self._build_event_body(request)
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.post(
                    f"{GRAPH_BASE}/me/events",
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                )
        except httpx.HTTPError as exc:
            logger.exception("Microsoft Graph events POST failed")
            return MeetingResult(
                success=False,
                provider=self.name,
                error=f"http_error: {exc!r}",
            )

        if response.status_code >= 400:
            # Surface Graph's error message when present so the rep
            # sees a useful failure ("user has not consented" etc).
            body_excerpt = response.text[:300]
            logger.warning(
                "Graph events POST returned %d: %s",
                response.status_code, body_excerpt,
            )
            return MeetingResult(
                success=False,
                provider=self.name,
                error=f"graph_error_{response.status_code}: {body_excerpt}",
            )

        return self._result_from_event(response.json())

    # ── Internal helpers ────────────────────────────────────────────

    def _build_event_body(self, request: MeetingRequest) -> Dict[str, Any]:
        """Construct the Graph event body, including the online-meeting
        flags that auto-generate a Teams link."""
        attendees = []
        for p in request.participants:
            if not p.email:
                continue
            attendees.append(
                {
                    "emailAddress": {"address": p.email, "name": p.name},
                    "type": (
                        "optional" if (p.role or "").lower() == "optional"
                        else "required"
                    ),
                }
            )

        start = request.start or datetime.now(timezone.utc) + timedelta(hours=1)
        end = start + timedelta(minutes=request.duration_minutes)

        body: Dict[str, Any] = {
            "subject": request.subject,
            "body": {"contentType": "HTML", "content": request.body or ""},
            "start": {"dateTime": start.isoformat(), "timeZone": "UTC"},
            "end": {"dateTime": end.isoformat(), "timeZone": "UTC"},
            "attendees": attendees,
        }
        if request.location:
            body["location"] = {"displayName": request.location}

        # Auto-create a Teams meeting unless the caller opted out.
        # ``teamsForBusiness`` is the modern provider; ``skypeForBusiness``
        # is legacy and not worth supporting in 2026.
        if request.conference_provider != "none":
            body["isOnlineMeeting"] = True
            body["onlineMeetingProvider"] = "teamsForBusiness"
        return body

    def _result_from_event(self, event: Dict[str, Any]) -> MeetingResult:
        join_url: Optional[str] = None
        online_meeting = event.get("onlineMeeting") or {}
        join_url = online_meeting.get("joinUrl") or event.get("onlineMeetingUrl")

        return MeetingResult(
            success=True,
            provider=self.name,
            event_id=event.get("id"),
            join_url=join_url,
            html_link=event.get("webLink"),
        )


# ── internal helpers ────────────────────────────────────────────────────


async def _async_load_integration(
    db, tenant_id: uuid.UUID, user_id: Optional[uuid.UUID]
) -> Optional[Integration]:
    """Look up the Microsoft integration row for this user / tenant."""
    stmt = (
        select(Integration)
        .where(
            Integration.tenant_id == tenant_id,
            Integration.provider == "microsoft",
        )
        .order_by(Integration.created_at.desc())
    )
    if user_id is not None:
        stmt = stmt.where(Integration.user_id == user_id)
    result = await db.execute(stmt)
    return result.scalars().first()
