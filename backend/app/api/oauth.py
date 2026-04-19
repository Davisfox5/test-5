"""OAuth integration endpoints — Google Workspace & Microsoft.

Authorize → redirect to provider → provider redirects to ``/callback``
with ``code`` and ``state``.  The state is a signed, time-limited token
minted by :mod:`backend.app.services.token_crypto`, so we can recover
the tenant/user without a Redis round trip and without trusting any
request header at callback time.

Tokens are AES-fernet-encrypted before they hit the database; callers
use :func:`get_provider_token` to get a decrypted access token (with
auto-refresh if expired).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import get_current_tenant
from backend.app.config import get_settings
from backend.app.db import get_db
from backend.app.models import Integration, Tenant, User
from backend.app.services.token_crypto import (
    decrypt_token,
    encrypt_token,
    sign_state,
    verify_state,
)

logger = logging.getLogger(__name__)

router = APIRouter()
settings = get_settings()

# ── Provider Configuration ──────────────────────────────

GOOGLE_SCOPES = [
    "openid",
    "email",
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

SUPPORTED_PROVIDERS = {"google", "microsoft"}


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
    base = str(request.base_url).rstrip("/")
    return f"{base}{settings.API_V1_PREFIX}/oauth/{provider}/callback"


def _validate_provider(provider: str) -> None:
    if provider not in SUPPORTED_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported provider '{provider}'. Must be one of: {', '.join(SUPPORTED_PROVIDERS)}",
        )


async def _resolve_user(
    db: AsyncSession, tenant_id: uuid.UUID, email: Optional[str]
) -> Optional[User]:
    """Find (or implicitly create) a User row by email within a tenant."""
    if not email:
        return None
    stmt = select(User).where(User.tenant_id == tenant_id, User.email == email)
    user = (await db.execute(stmt)).scalar_one_or_none()
    if user is None:
        user = User(tenant_id=tenant_id, email=email, role="agent")
        db.add(user)
        await db.flush()
    return user


# ── Authorize ───────────────────────────────────────────


@router.get("/oauth/{provider}/authorize")
async def oauth_authorize(
    provider: str,
    request: Request,
    tenant: Tenant = Depends(get_current_tenant),
):
    """Redirect the user to the provider's consent screen."""
    _validate_provider(provider)

    state = sign_state({"tenant_id": str(tenant.id), "provider": provider})
    redirect_uri = _build_redirect_uri(request, provider)

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

    # Microsoft
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


# ── Callback ────────────────────────────────────────────


@router.get("/oauth/{provider}/callback")
async def oauth_callback(
    provider: str,
    request: Request,
    code: str,
    state: str,
    db: AsyncSession = Depends(get_db),
):
    """Exchange the authorization code for tokens and persist them."""
    _validate_provider(provider)

    try:
        state_payload = verify_state(state)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid state: {exc}")

    if state_payload.get("provider") != provider:
        raise HTTPException(status_code=400, detail="State/provider mismatch")

    tenant_id = uuid.UUID(state_payload["tenant_id"])
    redirect_uri = _build_redirect_uri(request, provider)

    if provider == "google":
        access_token, refresh_token, scopes, expires_at, account_email = (
            await _google_exchange(code, redirect_uri)
        )
    else:
        access_token, refresh_token, scopes, expires_at, account_email = (
            await _microsoft_exchange(code, redirect_uri)
        )

    user = await _resolve_user(db, tenant_id, account_email)

    # Upsert: one Integration row per (tenant, user, provider).
    stmt = select(Integration).where(
        Integration.tenant_id == tenant_id,
        Integration.provider == provider,
        Integration.user_id == (user.id if user else None),
    )
    integration = (await db.execute(stmt)).scalar_one_or_none()
    if integration is None:
        integration = Integration(
            tenant_id=tenant_id,
            user_id=user.id if user else None,
            provider=provider,
        )
        db.add(integration)

    integration.access_token = encrypt_token(access_token)
    integration.refresh_token = encrypt_token(refresh_token) if refresh_token else integration.refresh_token
    integration.scopes = scopes
    integration.expires_at = expires_at
    await db.flush()

    return {"status": "connected", "provider": provider, "account": account_email}


async def _google_exchange(
    code: str, redirect_uri: str
) -> Tuple[str, Optional[str], List[str], Optional[datetime], Optional[str]]:
    from google_auth_oauthlib.flow import Flow
    from googleapiclient.discovery import build

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

    # Resolve the authenticated account's email so we can tie the
    # Integration to the right User row.
    account_email: Optional[str] = None
    try:
        svc = build("oauth2", "v2", credentials=creds, cache_discovery=False)
        account_email = svc.userinfo().get().execute().get("email")
    except Exception:  # pragma: no cover — best effort
        logger.exception("Failed to resolve Google userinfo")

    scopes = list(creds.scopes) if creds.scopes else GOOGLE_SCOPES
    return creds.token, creds.refresh_token, scopes, creds.expiry, account_email


async def _microsoft_exchange(
    code: str, redirect_uri: str
) -> Tuple[str, Optional[str], List[str], Optional[datetime], Optional[str]]:
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

    expires_in = result.get("expires_in")
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
        if expires_in
        else None
    )
    account_email = None
    id_claims = result.get("id_token_claims") or {}
    account_email = (
        id_claims.get("preferred_username")
        or id_claims.get("email")
        or id_claims.get("upn")
    )

    return (
        result["access_token"],
        result.get("refresh_token"),
        MICROSOFT_SCOPES,
        expires_at,
        account_email,
    )


# ── Status / revoke ─────────────────────────────────────


@router.get("/oauth/status", response_model=IntegrationStatusResponse)
async def oauth_status(
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
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
    _validate_provider(provider)

    stmt = select(Integration).where(
        Integration.tenant_id == tenant.id,
        Integration.provider == provider,
    )
    result = await db.execute(stmt)
    integration = result.scalar_one_or_none()
    if integration is None:
        raise HTTPException(status_code=404, detail=f"No {provider} integration found")

    await db.delete(integration)


# ── Token accessor (used by ingestion + send) ───────────


async def get_provider_token(
    db: AsyncSession, integration: Integration
) -> str:
    """Return a valid, decrypted access token, refreshing if necessary.

    The synchronous counterpart lives in
    :func:`get_provider_token_sync` for use from Celery tasks.
    """
    token = decrypt_token(integration.access_token)
    refresh = decrypt_token(integration.refresh_token)
    if integration.expires_at and integration.expires_at > datetime.now(timezone.utc):
        return token

    # Expired or unknown — refresh.
    if not refresh:
        raise HTTPException(status_code=401, detail="Integration token expired and no refresh token")

    new_access, new_refresh, new_expiry = _refresh_tokens(
        integration.provider, refresh
    )
    integration.access_token = encrypt_token(new_access)
    if new_refresh:
        integration.refresh_token = encrypt_token(new_refresh)
    integration.expires_at = new_expiry
    await db.flush()
    return new_access


def _refresh_tokens(
    provider: str, refresh_token: str
) -> Tuple[str, Optional[str], Optional[datetime]]:
    """Provider-specific refresh. Returns (access, refresh, expires_at)."""
    if provider == "google":
        import requests  # google-auth pulls requests in

        resp = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=10,
        )
        resp.raise_for_status()
        j = resp.json()
        return (
            j["access_token"],
            j.get("refresh_token"),
            datetime.now(timezone.utc) + timedelta(seconds=int(j.get("expires_in", 3600))),
        )

    # Microsoft
    import msal

    app = msal.ConfidentialClientApplication(
        settings.MICROSOFT_CLIENT_ID,
        authority="https://login.microsoftonline.com/common",
        client_credential=settings.MICROSOFT_CLIENT_SECRET,
    )
    result = app.acquire_token_by_refresh_token(refresh_token, scopes=MICROSOFT_SCOPES)
    if "error" in result:
        raise HTTPException(status_code=401, detail=f"Microsoft refresh failed: {result['error']}")
    return (
        result["access_token"],
        result.get("refresh_token"),
        datetime.now(timezone.utc) + timedelta(seconds=int(result.get("expires_in", 3600))),
    )
