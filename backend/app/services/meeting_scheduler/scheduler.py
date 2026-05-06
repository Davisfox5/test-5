"""MeetingScheduler — picks the right provider for the user.

Resolution order, first match wins:

1. Tenant has explicitly preferred a provider in
   ``Tenant.features_enabled.calendar_provider`` AND that provider can
   serve the user (integration row + scopes present).
2. The user has any connected provider that can serve — preference for
   Google Calendar > Microsoft Graph > Zoom > Cal.com (Meet/Teams give
   us video links automatically; Zoom and Cal.com require pairing).
3. Stub provider — always available, returns ICS + copy-paste payload.

The dispatcher resolves the provider on each call (no caching) so a
user who connects Google after their first attempt sees the upgrade
on their next "Schedule meeting" click without restart.
"""

from __future__ import annotations

import logging
import uuid
from typing import List, Optional, Type

from backend.app.services.meeting_scheduler.base import (
    MeetingProvider,
    MeetingRequest,
    MeetingResult,
)
from backend.app.services.meeting_scheduler.google_calendar import (
    GoogleCalendarProvider,
)
from backend.app.services.meeting_scheduler.microsoft_graph import (
    MicrosoftGraphProvider,
)
from backend.app.services.meeting_scheduler.stub import StubMeetingProvider
from backend.app.services.meeting_scheduler.zoom import ZoomMeetingProvider

logger = logging.getLogger(__name__)


# Ordered preference. Real providers are tried first; stub is last.
# When PR #85+ land, MicrosoftGraphProvider, ZoomProvider, CalcomProvider
# slot in here.
_PROVIDER_ORDER: List[Type[MeetingProvider]] = [
    GoogleCalendarProvider,
    MicrosoftGraphProvider,
    ZoomMeetingProvider,
    StubMeetingProvider,
]


def _provider_class_by_name(name: str) -> Optional[Type[MeetingProvider]]:
    for cls in _PROVIDER_ORDER:
        if cls.name == name:
            return cls
    return None


class MeetingScheduler:
    """Front door for the action items "Schedule meeting" action."""

    def __init__(
        self,
        db,
        *,
        tenant_id: uuid.UUID,
        user_id: Optional[uuid.UUID],
        preferred_provider: Optional[str] = None,
    ):
        self._db = db
        self._tenant_id = tenant_id
        self._user_id = user_id
        self._preferred_provider = preferred_provider

    async def create_meeting(self, request: MeetingRequest) -> MeetingResult:
        provider = await self._pick_provider()
        return await provider.create_meeting(request)

    async def _pick_provider(self) -> MeetingProvider:
        # 1. Honor tenant preference when set AND functional.
        if self._preferred_provider:
            preferred_cls = _provider_class_by_name(self._preferred_provider)
            if preferred_cls is not None:
                try:
                    if await preferred_cls.can_serve(
                        self._db,
                        tenant_id=self._tenant_id,
                        user_id=self._user_id,
                    ):
                        return self._instantiate(preferred_cls)
                except Exception:
                    logger.exception(
                        "preferred provider %s can_serve check failed",
                        self._preferred_provider,
                    )

        # 2. Try each real provider in preference order.
        for cls in _PROVIDER_ORDER:
            if cls is StubMeetingProvider:
                continue
            try:
                if await cls.can_serve(
                    self._db,
                    tenant_id=self._tenant_id,
                    user_id=self._user_id,
                ):
                    return self._instantiate(cls)
            except Exception:
                logger.exception("provider %s can_serve check failed", cls.name)

        # 3. Stub fallback — always works.
        return self._instantiate(StubMeetingProvider)

    def _instantiate(self, cls: Type[MeetingProvider]) -> MeetingProvider:
        # Providers that need DB/tenant/user context get them at construction.
        # The stub doesn't, but accepting kwargs anyway keeps the call site uniform.
        if cls is StubMeetingProvider:
            return StubMeetingProvider()
        # Real providers all take the same shape today.
        return cls(
            self._db,
            tenant_id=self._tenant_id,
            user_id=self._user_id,
        )
