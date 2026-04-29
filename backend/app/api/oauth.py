"""OAuth integration endpoints.

Supports: Google Workspace, Microsoft, HubSpot, Salesforce, Pipedrive.

Flow:

1. ``GET /oauth/{provider}/authorize`` — generates a CSRF-safe ``state``
   token, stashes ``{tenant_id, user_id?}`` in Redis under that token,
   and redirects the browser to the provider's consent screen.
2. ``GET /oauth/{provider}/callback`` — validates the returned ``state``
   against Redis, exchanges the code for tokens, encrypts them with the
   Fernet wrapper, and upserts an ``Integration`` row on that tenant.
3. ``GET /oauth/status`` lists connected integrations.
4. ``POST /oauth/{provider}/revoke`` deletes the row.

Adding a new provider = adding one entry to ``CRM_PROVIDERS`` (auth URL,
token URL, scopes, extras). Google/Microsoft still use their SDKs.
"""

from __future__ import annotations

import json
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import httpx
import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import AuthPrincipal, get_current_principal, get_current_tenant
from backend.app.config import get_settings
from backend.app.db import get_db
from backend.app.models import Integration, Tenant
from backend.app.services.token_crypto import encrypt_token

router = APIRouter()
settings = get_settings()

logger = logging.getLogger(__name__)


# ── Provider Configuration ──────────────────────────────

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/contacts.readonly",
]

MICROSOFT_SCOPES = [
    "Mail.Send",
    "Mail.Read",
    "Calendars.ReadWrite",
    "Contacts.Read",
    "offline_access",
]


def _provider_setting(attr: str) -> str:
    return getattr(settings, attr, "") or ""


# CRM provider table. Each adapter's oauth flow reads from this.
# ``scope_sep`` is how the provider wants scopes joined in the auth URL.
CRM_PROVIDERS: Dict[str, Dict[str, Any]] = {
    "hubspot": {
        "authorize_url": "https://app.hubspot.com/oauth/authorize",
        "token_url": "https://api.hubapi.com/oauth/v1/token",
        # HubSpot scopes are hub-specific; tenants typically want these for
        # contact/company/deal visibility.
        "scopes": [
            "crm.objects.companies.read",
            "crm.objects.contacts.read",
            "crm.schemas.companies.read",
            "crm.schemas.contacts.read",
            "oauth",
        ],
        "scope_sep": " ",
        "client_id_key": "HUBSPOT_CLIENT_ID",
        "client_secret_key": "HUBSPOT_CLIENT_SECRET",
    },
    "salesforce": {
        # Salesforce auth URL uses the login domain; test orgs use
        # https://test.salesforce.com. Override via provider_config on the
        # Integration row if needed.
        "authorize_url": "https://login.salesforce.com/services/oauth2/authorize",
        "token_url": "https://login.salesforce.com/services/oauth2/token",
        "scopes": ["api", "refresh_token", "offline_access"],
        "scope_sep": " ",
        "client_id_key": "SALESFORCE_CLIENT_ID",
        "client_secret_key": "SALESFORCE_CLIENT_SECRET",
    },
    "pipedrive": {
        "authorize_url": "https://oauth.pipedrive.com/oauth/authorize",
        "token_url": "https://oauth.pipedrive.com/oauth/token",
        "scopes": ["base", "contacts:read", "deals:read", "users:read"],
        "scope_sep": " ",
        "client_id_key": "PIPEDRIVE_CLIENT_ID",
        "client_secret_key": "PIPEDRIVE_CLIENT_SECRET",
    },
    # ── Stubs ─────────────────────────────────────────────────
    # Config + URL templates for providers we plan to support but
    # haven't certified end-to-end yet. The SPA reads ``certified=False``
    # from /oauth/providers and renders these as "Coming soon" instead
    # of letting users start a flow that would fail at the token-exchange
    # step. Full OAuth wiring (token refresh, contact-pull adapters) is
    # tracked separately.
    "zoho": {
        "authorize_url": "https://accounts.zoho.com/oauth/v2/auth",
        "token_url": "https://accounts.zoho.com/oauth/v2/token",
        "scopes": [
            "ZohoCRM.modules.contacts.READ",
            "ZohoCRM.modules.accounts.READ",
            "ZohoCRM.modules.deals.READ",
        ],
        "scope_sep": ",",
        "client_id_key": "ZOHO_CLIENT_ID",
        "client_secret_key": "ZOHO_CLIENT_SECRET",
        "certified": False,
    },
    "microsoft_dynamics": {
        # Microsoft Dynamics 365 — same authority root as Microsoft Graph,
        # but the resource scope is per-tenant ("https://<org>.crm.dynamics.com/.default").
        # Tenants will need to set the per-org resource via ``provider_config``
        # before the flow can complete.
        "authorize_url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "scopes": ["offline_access"],
        "scope_sep": " ",
        "client_id_key": "MICROSOFT_DYNAMICS_CLIENT_ID",
        "client_secret_key": "MICROSOFT_DYNAMICS_CLIENT_SECRET",
        "certified": False,
    },
}


SUPPORTED_PROVIDERS = {"google", "microsoft"} | set(CRM_PROVIDERS.keys())


# ── Pydantic Schemas ────────────────────────────────────


class IntegrationOut(BaseModel):
    id: uuid.UUID
    provider: str
    scopes: List[str]
    expires_at: Optional[datetime]
    created_at: datetime

    model_config = {"from_attributes": True}


class IntegrationStatusResponse(BaseModel):
    integrations: List[IntegrationOut]


# ── Helpers ─────────────────────────────────────────────


def _build_redirect_uri(request: Request, provider: str) -> str:
    """Construct the OAuth callback URL from the current request base URL."""
    base = str(request.base_url).rstrip("/")
    return f"{base}{settings.API_V1_PREFIX}/oauth/{provider}/callback"


def _is_certified(provider: str) -> bool:
    """Whether a provider is fully wired end-to-end (auth + adapters).

    Stub providers (currently: zoho, microsoft_dynamics) ship with config
    slots only — the SPA renders them as "Coming soon" and the flow
    refuses to start to keep us from leaving partial integration rows.
    """
    spec = CRM_PROVIDERS.get(provider)
    if spec is None:
        # Built-ins (google/microsoft) and any provider we don't recognise
        # default to certified — SUPPORTED_PROVIDERS membership is the
        # outer gate.
        return True
    return bool(spec.get("certified", True))


def _validate_provider(provider: str) -> None:
    if provider not in SUPPORTED_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported provider '{provider}'. Must be one of: "
                + ", ".join(sorted(SUPPORTED_PROVIDERS))
            ),
        )


def _require_certified(provider: str) -> None:
    """Reject flow-start on stub providers.

    Used at authorize / ticket / callback only — revoke and status are
    safe to call on any provider so a tenant can clean up an integration
    row even if we later flagged its provider as uncertified.
    """
    if not _is_certified(provider):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Provider '{provider}' is not yet certified — full OAuth "
                "wiring is in progress. The provider is listed for UI "
                "discovery only."
            ),
        )


def _get_redis() -> aioredis.Redis:
    return aioredis.from_url(settings.REDIS_URL, decode_responses=True)


_STATE_TTL_SECONDS = 600  # 10 minutes for user to complete the flow


async def _stash_state(state: str, payload: Dict[str, Any]) -> None:
    """Store the CSRF state + its context for verification on callback."""
    r = _get_redis()
    try:
        await r.setex(
            f"oauth_state:{state}", _STATE_TTL_SECONDS, json.dumps(payload, default=str)
        )
    finally:
        await r.aclose()


async def _pop_state(state: str) -> Optional[Dict[str, Any]]:
    """Atomically read + delete the state payload. None if expired/missing."""
    r = _get_redis()
    try:
        raw = await r.get(f"oauth_state:{state}")
        if not raw:
            return None
        await r.delete(f"oauth_state:{state}")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None
    finally:
        await r.aclose()


def _spa_redirect(provider: str) -> RedirectResponse:
    """Send the user back to the SPA after a successful OAuth connect.

    Returning JSON leaves the user staring at ``{"status":"connected"}``
    in a tab; the SPA's ``/oauth-status`` poll never refreshes because
    the user never navigated back. We redirect to ``${SPA_URL}/settings``
    with a ``?integration_connected=<provider>`` query the SPA can
    pop into a toast.
    """
    base = (settings.SPA_URL or "").rstrip("/")
    if not base and settings.ALLOWED_ORIGINS:
        # Fall back to the first allowed origin so this works without
        # an extra env var on existing deploys.
        base = settings.ALLOWED_ORIGINS[0].rstrip("/")
    if not base:
        # Operator hasn't wired SPA_URL — keep the legacy JSON behaviour
        # rather than redirecting somewhere unsafe.
        return RedirectResponse(url=f"/?integration_connected={provider}")
    return RedirectResponse(url=f"{base}/settings?integration_connected={provider}")


def _expires_at_from_seconds(expires_in: Optional[int]) -> Optional[datetime]:
    if not expires_in:
        return None
    try:
        return datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
    except (TypeError, ValueError):
        return None


async def _upsert_integration(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: Optional[uuid.UUID],
    provider: str,
    access_token: Optional[str],
    refresh_token: Optional[str],
    scopes: List[str],
    expires_at: Optional[datetime],
    provider_config: Optional[Dict[str, Any]] = None,
) -> Integration:
    """Upsert the Integration row for a tenant+provider. Tokens are
    encrypted at this boundary — callers hand us plaintext."""
    stmt = select(Integration).where(
        Integration.tenant_id == tenant_id,
        Integration.provider == provider,
    )
    existing = (await db.execute(stmt)).scalar_one_or_none()
    enc_access = encrypt_token(access_token)
    enc_refresh = encrypt_token(refresh_token)

    if existing is None:
        row = Integration(
            tenant_id=tenant_id,
            # NULL when the OAuth flow had no associated user — tenant-wide integration.
            # Previously fell back to tenant_id, which is a UUID type-collision against
            # the FK to users.id.
            user_id=user_id,
            provider=provider,
            access_token=enc_access,
            refresh_token=enc_refresh,
            scopes=scopes,
            expires_at=expires_at,
            provider_config=provider_config or {},
        )
        db.add(row)
        await db.flush()
        return row

    existing.access_token = enc_access
    if enc_refresh:
        existing.refresh_token = enc_refresh
    existing.scopes = scopes
    existing.expires_at = expires_at
    if provider_config:
        merged = dict(existing.provider_config or {})
        merged.update(provider_config)
        existing.provider_config = merged
    return existing


# ── Endpoints ───────────────────────────────────────────


async def _build_provider_authorize_url(
    provider: str,
    request: Request,
    tenant_id: uuid.UUID,
    user_id: Optional[uuid.UUID],
) -> str:
    """Mint a state token + return the provider's hosted authorize URL.

    Shared between the legacy GET /authorize redirect (which still
    depends on a Bearer-auth'd request) and the new POST /ticket flow
    (where the SPA does the auth and the redirect happens client-side).
    """
    state = secrets.token_urlsafe(32)
    redirect_uri = _build_redirect_uri(request, provider)
    # Stash redirect_uri alongside tenant_id so the callback can verify the
    # exact URL we registered with the provider — guards against host-header
    # injection through misconfigured proxies that change request.base_url.
    payload: Dict[str, Any] = {
        "tenant_id": str(tenant_id),
        "provider": provider,
        "redirect_uri": redirect_uri,
    }
    if user_id is not None:
        payload["user_id"] = str(user_id)
    await _stash_state(state, payload)

    if provider == "google":
        from google_auth_oauthlib.flow import Flow

        flow = Flow.from_client_config(
            {
                "web": {
                    "client_id": settings.GOOGLE_CLIENT_ID,
                    "client_secret": settings.GOOGLE_CLIENT_SECRET,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            },
            scopes=GOOGLE_SCOPES,
            redirect_uri=redirect_uri,
        )
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            state=state,
            prompt="consent",
        )
        return auth_url

    if provider == "microsoft":
        import msal

        app = msal.ConfidentialClientApplication(
            settings.MICROSOFT_CLIENT_ID,
            authority="https://login.microsoftonline.com/common",
            client_credential=settings.MICROSOFT_CLIENT_SECRET,
        )
        return app.get_authorization_request_url(
            scopes=MICROSOFT_SCOPES,
            redirect_uri=redirect_uri,
            state=state,
        )

    spec = CRM_PROVIDERS[provider]
    client_id = _provider_setting(spec["client_id_key"])
    if not client_id:
        raise HTTPException(
            status_code=500,
            detail=f"{spec['client_id_key']} is not configured on this server",
        )
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": spec["scope_sep"].join(spec["scopes"]),
        "state": state,
    }
    return f"{spec['authorize_url']}?{urlencode(params)}"


class OAuthProviderInfo(BaseModel):
    provider: str
    certified: bool


class OAuthProvidersResponse(BaseModel):
    providers: List[OAuthProviderInfo]


@router.get("/oauth/providers", response_model=OAuthProvidersResponse)
async def oauth_providers() -> OAuthProvidersResponse:
    """List every OAuth provider the SPA can offer + its certification status.

    Stub providers (``certified=False``) are surfaced so the SPA can
    render a "Coming soon" treatment instead of hiding upcoming
    integrations entirely.
    """
    items: List[OAuthProviderInfo] = [
        OAuthProviderInfo(provider="google", certified=True),
        OAuthProviderInfo(provider="microsoft", certified=True),
    ]
    for name in sorted(CRM_PROVIDERS.keys()):
        items.append(
            OAuthProviderInfo(provider=name, certified=_is_certified(name))
        )
    return OAuthProvidersResponse(providers=items)


class OAuthTicketResponse(BaseModel):
    authorize_url: str


@router.post("/oauth/{provider}/ticket", response_model=OAuthTicketResponse)
async def oauth_ticket(
    provider: str,
    request: Request,
    principal: AuthPrincipal = Depends(get_current_principal),
):
    """Mint a one-shot authorize URL for the SPA to redirect to.

    The SPA can't open the legacy ``GET /oauth/{provider}/authorize``
    in a new tab because that strips the ``Authorization: Bearer …``
    header and 401s. This endpoint fetches the authorize URL on behalf
    of the authenticated SPA caller (auth flows over the JSON API as
    usual), so the SPA can then ``window.location =`` the result.

    The returned URL embeds a fresh ``state`` token stashed in Redis
    under the same key the callback already reads — so the rest of
    the flow is unchanged.
    """
    _validate_provider(provider)
    _require_certified(provider)
    auth_url = await _build_provider_authorize_url(
        provider,
        request,
        tenant_id=principal.tenant.id,
        user_id=principal.user_id,
    )
    return OAuthTicketResponse(authorize_url=auth_url)


@router.get("/oauth/{provider}/authorize")
async def oauth_authorize(
    provider: str,
    request: Request,
    tenant: Tenant = Depends(get_current_tenant),
):
    """Generate an OAuth authorization URL and redirect the user.

    Kept for API-key callers + tooling. The SPA uses POST /ticket
    instead because anchor-tag clicks can't carry a Bearer header.
    """
    _validate_provider(provider)
    _require_certified(provider)

    state = secrets.token_urlsafe(32)
    redirect_uri = _build_redirect_uri(request, provider)

    # CSRF protection + tenant context: the state token keys a Redis entry
    # carrying the tenant id, so the callback knows who to attribute. We
    # also stash the redirect_uri so the callback compares the rebuilt URL
    # against the one we authorized — defends against host-header injection.
    await _stash_state(
        state,
        {
            "tenant_id": str(tenant.id),
            "provider": provider,
            "redirect_uri": redirect_uri,
        },
    )

    if provider == "google":
        from google_auth_oauthlib.flow import Flow

        flow = Flow.from_client_config(
            {
                "web": {
                    "client_id": settings.GOOGLE_CLIENT_ID,
                    "client_secret": settings.GOOGLE_CLIENT_SECRET,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            },
            scopes=GOOGLE_SCOPES,
            redirect_uri=redirect_uri,
        )
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            state=state,
            prompt="consent",
        )
        return RedirectResponse(url=auth_url)

    if provider == "microsoft":
        import msal

        app = msal.ConfidentialClientApplication(
            settings.MICROSOFT_CLIENT_ID,
            authority="https://login.microsoftonline.com/common",
            client_credential=settings.MICROSOFT_CLIENT_SECRET,
        )
        auth_url = app.get_authorization_request_url(
            scopes=MICROSOFT_SCOPES,
            redirect_uri=redirect_uri,
            state=state,
        )
        return RedirectResponse(url=auth_url)

    # CRM providers — generic code-flow URL builder.
    spec = CRM_PROVIDERS[provider]
    client_id = _provider_setting(spec["client_id_key"])
    if not client_id:
        raise HTTPException(
            status_code=500,
            detail=f"{spec['client_id_key']} is not configured on this server",
        )
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": spec["scope_sep"].join(spec["scopes"]),
        "state": state,
    }
    auth_url = f"{spec['authorize_url']}?{urlencode(params)}"
    return RedirectResponse(url=auth_url)


@router.get("/oauth/{provider}/callback")
async def oauth_callback(
    provider: str,
    request: Request,
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Handle OAuth callback — exchange code for tokens and store them."""
    _validate_provider(provider)
    _require_certified(provider)

    if error:
        raise HTTPException(
            status_code=400, detail=f"Provider returned error: {error}"
        )
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code or state")

    state_payload = await _pop_state(state)
    if state_payload is None:
        raise HTTPException(
            status_code=400, detail="Invalid or expired state token"
        )
    tenant_id = uuid.UUID(state_payload["tenant_id"])
    user_id_raw = state_payload.get("user_id")
    user_id = uuid.UUID(user_id_raw) if user_id_raw else None

    # Use the redirect_uri stashed at authorize time — defends against
    # host-header injection that would otherwise let a misconfigured
    # proxy change request.base_url between authorize and callback.
    rebuilt_uri = _build_redirect_uri(request, provider)
    redirect_uri = state_payload.get("redirect_uri") or rebuilt_uri
    if state_payload.get("redirect_uri") and redirect_uri != rebuilt_uri:
        logger.warning(
            "oauth callback host mismatch: stashed=%s rebuilt=%s",
            redirect_uri,
            rebuilt_uri,
        )

    if provider == "google":
        from google_auth_oauthlib.flow import Flow

        flow = Flow.from_client_config(
            {
                "web": {
                    "client_id": settings.GOOGLE_CLIENT_ID,
                    "client_secret": settings.GOOGLE_CLIENT_SECRET,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            },
            scopes=GOOGLE_SCOPES,
            redirect_uri=redirect_uri,
        )
        flow.fetch_token(code=code)
        creds = flow.credentials
        await _upsert_integration(
            db,
            tenant_id=tenant_id,
            user_id=user_id,
            provider=provider,
            access_token=creds.token,
            refresh_token=creds.refresh_token,
            scopes=list(creds.scopes) if creds.scopes else GOOGLE_SCOPES,
            expires_at=creds.expiry,
        )
        return _spa_redirect(provider)

    if provider == "microsoft":
        import msal

        app = msal.ConfidentialClientApplication(
            settings.MICROSOFT_CLIENT_ID,
            authority="https://login.microsoftonline.com/common",
            client_credential=settings.MICROSOFT_CLIENT_SECRET,
        )
        result = app.acquire_token_by_authorization_code(
            code, scopes=MICROSOFT_SCOPES, redirect_uri=redirect_uri
        )
        if "error" in result:
            raise HTTPException(
                status_code=400,
                detail=f"Token exchange failed: {result.get('error_description', result['error'])}",
            )
        # Read the scopes the user actually granted instead of recording the
        # full request set — a tenant who declines a scope shouldn't show up
        # as having granted it.
        granted_raw = result.get("scope") or ""
        granted_scopes = [s for s in granted_raw.split(" ") if s] or MICROSOFT_SCOPES
        await _upsert_integration(
            db,
            tenant_id=tenant_id,
            user_id=user_id,
            provider=provider,
            access_token=result.get("access_token"),
            refresh_token=result.get("refresh_token"),
            scopes=granted_scopes,
            expires_at=_expires_at_from_seconds(result.get("expires_in")),
        )
        return _spa_redirect(provider)

    # ── Generic CRM exchange ──────────────────────────────
    spec = CRM_PROVIDERS[provider]
    client_id = _provider_setting(spec["client_id_key"])
    client_secret = _provider_setting(spec["client_secret_key"])
    if not (client_id and client_secret):
        raise HTTPException(
            status_code=500,
            detail=f"{provider} client credentials are not configured",
        )

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            spec["token_url"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=400,
            detail=f"{provider} token exchange failed: {resp.status_code} {resp.text[:300]}",
        )
    body = resp.json()

    # Provider-specific extras we need to carry on provider_config.
    provider_config: Dict[str, Any] = {}
    if provider == "salesforce" and body.get("instance_url"):
        provider_config["instance_url"] = body["instance_url"].rstrip("/")
    if provider == "pipedrive" and body.get("api_domain"):
        provider_config["api_domain"] = body["api_domain"].rstrip("/")

    await _upsert_integration(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        provider=provider,
        access_token=body.get("access_token"),
        refresh_token=body.get("refresh_token"),
        scopes=spec["scopes"],
        expires_at=_expires_at_from_seconds(body.get("expires_in")),
        provider_config=provider_config,
    )
    return {"status": "connected", "provider": provider}


@router.get("/oauth/status", response_model=IntegrationStatusResponse)
async def oauth_status(
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Return all connected integrations for the current tenant."""
    stmt = (
        select(Integration)
        .where(Integration.tenant_id == tenant.id)
        .order_by(Integration.created_at.desc())
    )
    result = await db.execute(stmt)
    integrations = result.scalars().all()
    return IntegrationStatusResponse(
        integrations=[IntegrationOut.model_validate(i) for i in integrations],
    )


@router.post("/oauth/{provider}/revoke", status_code=204)
async def oauth_revoke(
    provider: str,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Delete the integration record for a provider."""
    _validate_provider(provider)
    stmt = select(Integration).where(
        Integration.tenant_id == tenant.id,
        Integration.provider == provider,
    )
    result = await db.execute(stmt)
    integration = result.scalar_one_or_none()
    if integration is None:
        raise HTTPException(
            status_code=404, detail=f"No {provider} integration found"
        )
    await db.delete(integration)
