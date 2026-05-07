"""App-only Microsoft Graph authentication (client-credentials flow).

Teams compliance recording uses *application* permissions, not delegated
ones. The compliance bot identity is the app itself; there is no signed-in
user. That's a different code path from ``backend/app/api/oauth.py`` —
that file (Stream 2 owns it) handles user-OAuth for Microsoft Mail/
Calendar scopes. We must not reuse it for the compliance bot, because:

* The user-OAuth registry stores ``access_token`` / ``refresh_token`` per
  ``Integration`` row. App-only auth has no refresh token — MSAL caches
  the bearer in-process and re-mints from the client secret on expiry.
* The scope list is different (``https://graph.microsoft.com/.default``
  resolves to the application permissions granted in tenant admin
  consent — ``Calls.AccessMedia.All``, ``Calls.JoinGroupCallAsGuest.All``,
  ``OnlineMeetingArtifact.Read.All``).
* The authorization endpoint is per-tenant
  (``https://login.microsoftonline.com/{TEAMS_TENANT_ID}/...``) — the
  user-OAuth registry uses the multi-tenant ``common`` endpoint.

Configuration flows through the standard ``backend.app.config.Settings``
object. Three env vars: ``TEAMS_BOT_APP_ID``, ``TEAMS_BOT_APP_SECRET``,
``TEAMS_TENANT_ID``. The first two identify the Azure AD app
registration of the bot; the third is the customer's Microsoft 365
tenant. Multi-tenant deployments will eventually move
``TEAMS_TENANT_ID`` onto ``Integration.provider_config`` (one row per
customer); for the scaffold, a single env var is sufficient.

This module is intentionally thin. We import ``msal`` lazily so the rest
of the app can boot without it installed (msal lands in
``requirements.txt`` under the ``# stream-3/teams:`` block). Test code
patches ``_acquire_token`` — see ``tests/test_teams_*.py``.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, List, Optional

from backend.app.config import get_settings

logger = logging.getLogger(__name__)


# The Graph scope literal used by client-credentials flow. The
# application-permission consents (``Calls.AccessMedia.All`` etc.) are
# granted by the tenant admin in Azure Portal; ``.default`` tells AAD
# "issue a token for whichever app permissions this app already has".
GRAPH_DEFAULT_SCOPE = "https://graph.microsoft.com/.default"


class GraphAppAuthError(RuntimeError):
    """Raised when token acquisition fails for any reason — missing
    config, MSAL not installed, AAD rejection, etc. Callers should
    treat this as terminal for the current request."""


@dataclass
class GraphToken:
    """A bearer access token plus the wall-clock at which it expires.

    We add a one-minute safety margin so that the cached token is
    refreshed before it expires under network latency. ``raw`` is the
    full MSAL response payload, retained mostly for diagnostic logging
    — production callers should only read ``access_token``."""

    access_token: str
    expires_at: float
    raw: dict

    def is_expired(self, *, now: Optional[float] = None) -> bool:
        return (now or time.time()) >= self.expires_at


class GraphAppAuth:
    """Lazy MSAL ``ConfidentialClientApplication`` wrapper.

    Holds one MSAL app instance per process, lazily constructed on
    first use. Tokens are cached in-process by MSAL itself; the public
    ``acquire_token`` always asks MSAL first, which returns the cached
    bearer until it's near expiry.

    The class is async-friendly: ``acquire_token`` is sync but cheap
    (cache hit) on the hot path, so callers may invoke it directly from
    async code without a thread pool. On cache miss MSAL makes a
    blocking HTTPS call — wrap with ``asyncio.to_thread`` if that
    matters for your call path.
    """

    def __init__(
        self,
        *,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        tenant_id: Optional[str] = None,
        scopes: Optional[List[str]] = None,
    ) -> None:
        settings = get_settings()
        self._client_id = client_id or getattr(settings, "TEAMS_BOT_APP_ID", "") or ""
        self._client_secret = (
            client_secret or getattr(settings, "TEAMS_BOT_APP_SECRET", "") or ""
        )
        self._tenant_id = tenant_id or getattr(settings, "TEAMS_TENANT_ID", "") or ""
        self._scopes = scopes or [GRAPH_DEFAULT_SCOPE]
        self._app: Any = None  # msal.ConfidentialClientApplication
        self._lock = threading.Lock()
        self._cached: Optional[GraphToken] = None

    # ── Public API ────────────────────────────────────────────────

    def is_configured(self) -> bool:
        """Whether enough env config is present to attempt a token call.

        This is what API handlers use to decide between "auth ready" and
        "scaffold-only" responses. False is the expected state until
        the user runs the Azure setup steps in ``USER_TODO.md``."""

        return bool(self._client_id and self._client_secret and self._tenant_id)

    def acquire_token(self) -> GraphToken:
        """Return a fresh (or cached) bearer token.

        Raises :class:`GraphAppAuthError` if config is missing, MSAL is
        not installed, or AAD rejects the request.
        """

        if not self.is_configured():
            raise GraphAppAuthError(
                "Teams app-only auth not configured: set TEAMS_BOT_APP_ID, "
                "TEAMS_BOT_APP_SECRET, TEAMS_TENANT_ID."
            )

        with self._lock:
            if self._cached is not None and not self._cached.is_expired():
                return self._cached
            self._cached = self._acquire_token()
            return self._cached

    def authorization_header(self) -> str:
        """Convenience for ``Authorization: Bearer ...`` headers."""

        return f"Bearer {self.acquire_token().access_token}"

    def reset_cache(self) -> None:
        """Drop the in-process token cache. Tests use this; production
        rarely needs to (MSAL's own cache handles expiry)."""

        with self._lock:
            self._cached = None
            self._app = None

    # ── Internals ─────────────────────────────────────────────────

    def _build_app(self) -> Any:
        try:
            import msal  # type: ignore
        except ImportError as exc:  # pragma: no cover - exercised via patching
            raise GraphAppAuthError(
                "msal is not installed; add it to requirements.txt under "
                "the '# stream-3/teams:' block."
            ) from exc

        authority = f"https://login.microsoftonline.com/{self._tenant_id}"
        return msal.ConfidentialClientApplication(
            client_id=self._client_id,
            client_credential=self._client_secret,
            authority=authority,
        )

    def _acquire_token(self) -> GraphToken:
        if self._app is None:
            self._app = self._build_app()

        # MSAL serves from its own cache when a non-expired token exists,
        # so we don't need to call acquire_token_silent first; the
        # client-credentials helper does the right thing.
        result = self._app.acquire_token_for_client(scopes=self._scopes)
        if not isinstance(result, dict) or "access_token" not in result:
            err = (result or {}).get("error_description") or str(result)
            raise GraphAppAuthError(f"MSAL token acquisition failed: {err}")

        # ``expires_in`` is seconds-from-now per the OAuth spec. Subtract
        # a 60 s safety margin so the next caller doesn't race expiry.
        expires_in = int(result.get("expires_in", 3600))
        expires_at = time.time() + max(0, expires_in - 60)
        return GraphToken(
            access_token=result["access_token"],
            expires_at=expires_at,
            raw=result,
        )


# ── Module-level singleton helper ─────────────────────────────────────

_default: Optional[GraphAppAuth] = None
_default_lock = threading.Lock()


def get_graph_app_auth() -> GraphAppAuth:
    """Process-wide default ``GraphAppAuth`` instance.

    Tests construct their own (with patched MSAL) instead of using this.
    """

    global _default
    with _default_lock:
        if _default is None:
            _default = GraphAppAuth()
        return _default


def reset_default_for_tests() -> None:
    """Drop the module-level singleton. Test-only."""

    global _default
    with _default_lock:
        _default = None
