"""Meeting scheduler — provider-agnostic interface for creating
calendar events with embedded video conference links.

Resolves at runtime which provider to use based on tenant configuration
and which integrations the user has connected. Falls back to a stub
provider that returns an ICS payload + copy-paste invite text so the
"Schedule meeting" UI works for everyone, including tenants who haven't
connected any provider yet.
"""

from backend.app.services.meeting_scheduler.base import (
    MeetingParticipant,
    MeetingProvider,
    MeetingRequest,
    MeetingResult,
)
from backend.app.services.meeting_scheduler.scheduler import MeetingScheduler

__all__ = [
    "MeetingParticipant",
    "MeetingProvider",
    "MeetingRequest",
    "MeetingResult",
    "MeetingScheduler",
]
