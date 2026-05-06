"""Cal.com meeting provider — creates bookings via Cal.com's API.

Cal.com fundamentally differs from Google / Microsoft / Zoom: its
primary mode is *customer self-booking* against an event type, not
rep-initiated immediate meeting creation. We support the
rep-initiated path here using Cal.com's admin booking API.

Setup, per-user (tenants typically have many reps each with their
own Cal.com account):

1. The rep generates a Cal.com API key in their Cal.com account
   settings (or the tenant admin generates a service-level key for
   self-hosted Cal.com).
2. The rep picks an event type (e.g. "30-min discovery call") to
   use as the default for action-item-driven meetings.
3. Both get stored in the ``Integration`` row:
   ``access_token`` = encrypted API key, ``provider_config`` =
   ``{"event_type_id": int, "base_url": str}``.

When ``create_meeting`` is called, we POST to /v2/bookings with the
configured event type and the resolved customer email. Cal.com handles
the calendar event + video link on its side (Cal.com integrates with
Google Meet / Zoom / Daily.co / etc. depending on the user's Cal.com
setup), so we don't need a separate calendar provider.

API-key auth, not OAuth. Self-hosted tenants can override ``base_url``
to point at their Cal.com instance.
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


DEFAULT_BASE_URL = "https://api.cal.com/v2"


class CalcomProvider(MeetingProvider):
    """Cal.com booking creator (rep-initiated, not customer self-book)."""

    name = "cal_com"

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
            cfg = integration.provider_config or {}
            # Cal.com requires both an API key and an event type ID
            # configured. Without both, ``create_meeting`` would fail
            # at the API call — better to skip and fall through to the
            # next provider.
            if not integration.access_token:
                return False
            if not cfg.get("event_type_id"):
                return False
            return True
        except Exception:
            logger.exception("CalcomProvider.can_serve check failed")
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
                    error="no_calcom_integration: user has not connected Cal.com",
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to load Cal.com integration row")
            return MeetingResult(
                success=False,
                provider=self.name,
                error=f"integration_lookup_failed: {exc!r}",
            )

        api_key = decrypt_token(integration.access_token)
        if not api_key:
            return MeetingResult(
                success=False,
                provider=self.name,
                error="no_api_key",
            )
        cfg = integration.provider_config or {}
        event_type_id = cfg.get("event_type_id")
        if not event_type_id:
            return MeetingResult(
                success=False,
                provider=self.name,
                error="no_event_type_id_configured",
            )

        customer_attendee = self._pick_customer_attendee(request)
        if customer_attendee is None:
            return MeetingResult(
                success=False,
                provider=self.name,
                error=(
                    "no_customer_attendee_with_email: Cal.com requires at "
                    "least one attendee with an email address"
                ),
            )

        base_url = cfg.get("base_url") or DEFAULT_BASE_URL
        body = self._build_booking_body(event_type_id, request, customer_attendee)

        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.post(
                    f"{base_url}/bookings",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                        # Cal.com v2 requires a version header; pin to a
                        # known-good date so the contract doesn't shift
                        # under us.
                        "cal-api-version": "2024-08-13",
                    },
                    json=body,
                )
        except httpx.HTTPError as exc:
            logger.exception("Cal.com bookings POST failed")
            return MeetingResult(
                success=False,
                provider=self.name,
                error=f"http_error: {exc!r}",
            )

        if response.status_code >= 400:
            body_excerpt = response.text[:300]
            logger.warning(
                "Cal.com bookings POST returned %d: %s",
                response.status_code, body_excerpt,
            )
            return MeetingResult(
                success=False,
                provider=self.name,
                error=f"calcom_error_{response.status_code}: {body_excerpt}",
            )

        return self._result_from_booking(response.json())

    # ── Internal helpers ────────────────────────────────────────────

    def _pick_customer_attendee(self, request: MeetingRequest):
        """Cal.com accepts a single primary attendee per booking. Pick
        the first customer-side participant with an email; fall back to
        any participant with an email when no side is set."""
        for p in request.participants:
            if p.email and (p.side or "").lower() == "customer":
                return p
        for p in request.participants:
            if p.email:
                return p
        return None

    def _build_booking_body(
        self, event_type_id: int, request: MeetingRequest, attendee
    ) -> Dict[str, Any]:
        start = request.start or datetime.now(timezone.utc) + timedelta(hours=1)
        return {
            "start": start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "eventTypeId": int(event_type_id),
            "attendee": {
                "name": attendee.name,
                "email": attendee.email,
                "timeZone": "UTC",
                "language": "en",
            },
            "lengthInMinutes": request.duration_minutes,
            "metadata": {
                # ``source`` lets Cal.com tag bookings created by Linda
                # so tenants can filter their Cal.com dashboard.
                "source": "linda_action_item",
                "subject": request.subject,
            },
        }

    def _result_from_booking(self, payload: Dict[str, Any]) -> MeetingResult:
        # Cal.com v2 wraps booking data under ``data``; v1 returned it
        # at the top level. Tolerate both shapes.
        booking = payload.get("data") or payload
        booking_id = booking.get("uid") or booking.get("id")
        booking_id = str(booking_id) if booking_id is not None else None

        # The video URL Cal.com selects depends on the event type's
        # configured location (Cal Video / Google Meet / Zoom / etc).
        # Try common shapes in order.
        join_url = (
            booking.get("meetingUrl")
            or booking.get("location")
            or _extract_video_link(booking.get("references") or [])
        )

        return MeetingResult(
            success=True,
            provider=self.name,
            event_id=booking_id,
            join_url=join_url,
            html_link=booking.get("htmlLink") or booking.get("rescheduleLink"),
        )


def _extract_video_link(references) -> Optional[str]:
    """Walk Cal.com's ``references`` array (Google Meet / Zoom / Daily
    integration outputs land here on some plans) and return the first
    video URL found."""
    for ref in references or []:
        if not isinstance(ref, dict):
            continue
        url = ref.get("meetingUrl") or ref.get("url")
        if url:
            return url
    return None


# ── internal helpers ────────────────────────────────────────────────────


async def _async_load_integration(
    db, tenant_id: uuid.UUID, user_id: Optional[uuid.UUID]
) -> Optional[Integration]:
    """Look up the Cal.com integration row for this user / tenant."""
    stmt = (
        select(Integration)
        .where(
            Integration.tenant_id == tenant_id,
            Integration.provider == "cal_com",
        )
        .order_by(Integration.created_at.desc())
    )
    if user_id is not None:
        stmt = stmt.where(Integration.user_id == user_id)
    result = await db.execute(stmt)
    return result.scalars().first()
