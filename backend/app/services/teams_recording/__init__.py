"""Microsoft Teams Compliance Recording — Python control plane.

This package is the **scaffolding-only** half of the Teams compliance
recording integration. It owns four concerns:

* App-only Microsoft Graph authentication (``graph_app_auth``) — separate
  from the user-OAuth registry in ``backend/app/api/oauth.py`` because
  the compliance bot acts as itself, not as a delegated user.
* Graph change-notification subscription bookkeeping (``subscriptions``)
  — register, renew, validate.
* The abstract media-bot interface (``bot_interface``) plus a stub
  implementation that always reports "not deployed" until the .NET media
  bot is commissioned in a follow-on workstream.
* The customer-side PowerShell template helper (``policy``) for
  ``New-CsTeamsComplianceRecordingApplication``.

The actual media plane — joining a Teams call, mixing/muxing audio,
delivering frames to transcription — runs in a separate .NET stateful
media bot per Microsoft's ``Calls.AccessMedia.All`` certification
requirements. That bot, its Azure deployment, and Microsoft's
certification process are explicitly OUT OF SCOPE for this round.

See ``docs/integrations/stream-3-teams/CERTIFICATION_PATH.md`` for what
real production deployment requires, and ``USER_TODO.md`` for the
human-only checklist.
"""

from backend.app.services.teams_recording.bot_interface import (
    MediaBot,
    MediaBotStatus,
    StubMediaBot,
    get_media_bot,
)

__all__ = [
    "MediaBot",
    "MediaBotStatus",
    "StubMediaBot",
    "get_media_bot",
]
