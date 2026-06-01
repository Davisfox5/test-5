"""Public contract for the meeting scheduler.

Every provider (Google Calendar, Microsoft Graph, Zoom, Cal.com, stub)
implements ``MeetingProvider``. The ``MeetingScheduler`` service in
``scheduler.py`` resolves which provider to use per request based on
tenant config + which integrations the user has connected, and falls
back to the stub when nothing is wired.
"""

from __future__ import annotations

import abc
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional


# ── Data shapes ─────────────────────────────────────────────────────────


@dataclass
class MeetingParticipant:
    """One person to invite. ``email`` may be None when we couldn't
    resolve it from contacts/users — the stub provider falls back to
    name-only in that case. ``phone`` is populated for customer-side
    participants when a matching Contact row has a phone number; used
    by the SPA's click-to-call link on phone_call steps."""

    name: str
    email: Optional[str] = None
    role: Optional[str] = None  # 'organizer' | 'required' | 'optional' | 'specialist'
    side: Optional[str] = None  # 'customer' | 'vendor'
    phone: Optional[str] = None


@dataclass
class MeetingRequest:
    subject: str
    body: str  # plain text or simple HTML
    organizer_email: str
    participants: List[MeetingParticipant] = field(default_factory=list)
    start: Optional[datetime] = None  # tz-aware; None → "TBD" in the invite
    duration_minutes: int = 30
    # ``conference_provider`` hints which video platform we want embedded
    # in the calendar event. Each provider implementation interprets it:
    #   - GoogleCalendarProvider: 'google_meet' (default), 'none'
    #   - MicrosoftGraphProvider: 'teams' (default), 'none'
    #   - ZoomProvider: ignored — always Zoom
    #   - StubProvider: included as a hint in the ICS payload only
    conference_provider: Optional[str] = None
    location: Optional[str] = None  # physical location for in-person meetings


@dataclass
class MeetingResult:
    """Outcome of a meeting creation attempt.

    ``success=True`` means a real calendar event was created OR a
    fallback ICS payload was produced; either way, the rep has
    something they can act on. ``success=False`` is reserved for
    provider errors that left no usable artifact (network failure,
    permission denied) — surface ``error`` to the user when this
    happens.
    """

    success: bool
    provider: str
    event_id: Optional[str] = None
    join_url: Optional[str] = None  # Google Meet / Teams / Zoom join link
    html_link: Optional[str] = None  # link to view in calendar app
    ics_payload: Optional[str] = None  # populated when stub or as fallback
    note: Optional[str] = None  # explanation, e.g. "no provider connected"
    error: Optional[str] = None


# ── Provider interface ──────────────────────────────────────────────────


class MeetingProvider(abc.ABC):
    """Implemented per platform.

    Lifecycle: ``can_serve`` is called by the scheduler to decide
    whether this provider has working credentials for the user.
    ``create_meeting`` is the actual API call.

    Both methods are async — DB lookups use ``AsyncSession``; the
    blocking provider SDK calls run in ``asyncio.to_thread``.
    """

    name: str = "abstract"

    @classmethod
    @abc.abstractmethod
    async def can_serve(
        cls,
        db,
        *,
        tenant_id: uuid.UUID,
        user_id: Optional[uuid.UUID],
    ) -> bool:
        """Return True when this provider has the integration + scopes
        needed to satisfy a meeting request from this user."""

    @abc.abstractmethod
    async def create_meeting(self, request: MeetingRequest) -> MeetingResult:
        """Create the meeting. Must not raise — return a result with
        ``success=False`` and ``error`` set when something fails."""
