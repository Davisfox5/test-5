"""Stub meeting provider — always works, produces an ICS payload.

Used as the universal fallback when a tenant has no calendar provider
connected, or when the chosen provider fails. The rep gets a
copy-pasteable invite text + an ``.ics`` blob they can drop into any
calendar app.

Conference provider hint is included in the body as a flag for the
rep ("this should be a Google Meet / Teams / Zoom call") but no real
video link is generated.
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from typing import Optional

from backend.app.services.meeting_scheduler.base import (
    MeetingProvider,
    MeetingRequest,
    MeetingResult,
)

logger = logging.getLogger(__name__)


def _ics_escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace(",", "\\,")
        .replace(";", "\\;")
    )


def _format_dt(dt) -> str:
    """ICS DTSTART/DTEND format: YYYYMMDDTHHMMSSZ (UTC)."""
    return dt.strftime("%Y%m%dT%H%M%SZ")


class StubMeetingProvider(MeetingProvider):
    name = "stub"

    @classmethod
    async def can_serve(
        cls, db, *, tenant_id: uuid.UUID, user_id: Optional[uuid.UUID]
    ) -> bool:
        return True  # Always available — that's the point.

    async def create_meeting(self, request: MeetingRequest) -> MeetingResult:
        ics = self._build_ics(request)
        note_parts = [
            "No calendar provider connected — returning ICS payload "
            "and copy-paste invite text for the rep to send manually."
        ]
        if request.conference_provider:
            note_parts.append(
                f"Suggested video platform: {request.conference_provider}."
            )
        return MeetingResult(
            success=True,
            provider=self.name,
            ics_payload=ics,
            note=" ".join(note_parts),
        )

    def _build_ics(self, request: MeetingRequest) -> str:
        # When start is missing, anchor to "in 1 hour" so the ICS validates;
        # the rep edits the time on import. Better than producing an
        # invalid ICS that some calendar apps reject.
        from datetime import datetime, timezone
        start = request.start or datetime.now(timezone.utc) + timedelta(hours=1)
        end = start + timedelta(minutes=request.duration_minutes)
        uid = f"linda-{uuid.uuid4()}@calendar"

        attendee_lines = []
        for p in request.participants:
            if not p.email:
                continue
            cn = _ics_escape(p.name)
            attendee_lines.append(
                f"ATTENDEE;CN={cn};RSVP=TRUE:mailto:{p.email}"
            )

        location_line = (
            f"LOCATION:{_ics_escape(request.location)}\n"
            if request.location else ""
        )
        body = _ics_escape(request.body)
        if request.conference_provider:
            body = f"{request.conference_provider}.\\n\\n{body}"

        ics = (
            "BEGIN:VCALENDAR\n"
            "VERSION:2.0\n"
            "PRODID:-//Linda//Meeting Scheduler//EN\n"
            "METHOD:REQUEST\n"
            "BEGIN:VEVENT\n"
            f"UID:{uid}\n"
            f"DTSTAMP:{_format_dt(datetime.now(timezone.utc))}\n"
            f"DTSTART:{_format_dt(start)}\n"
            f"DTEND:{_format_dt(end)}\n"
            f"ORGANIZER:mailto:{request.organizer_email}\n"
            f"SUMMARY:{_ics_escape(request.subject)}\n"
            f"DESCRIPTION:{body}\n"
            f"{location_line}"
            + "\n".join(attendee_lines) + ("\n" if attendee_lines else "")
            + "END:VEVENT\n"
            "END:VCALENDAR\n"
        )
        return ics
