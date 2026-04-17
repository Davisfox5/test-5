"""OAuth integration endpoints — Google Workspace & Microsoft."""

from __future__ import annotations

import json
import secrets
import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import get_current_tenant
from backend.app.config import get_settings
from backend.app.db import get_db
from backend.app.models import Integration, Tenant

router = APIRouter()
settings = get_settings()

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
    """Construct the OAuth callback URL from the current request base URL."""
    base = str(request.base_url).rstrip("/")
    return f"{base}{settings.API_V1_PREFIX}/oauth/{provider}/callback"


def _validate_provider(provider: str) -> None:
    if provider not in SUPPORTED_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported provider '{provider}'. Must be one of: {', '.join(SUPPORTED_PROVIDERS)}",
        )


# ── Endpoints ───────────────────────────────────────────


@router.get("/oauth/{provider}/authorize")
async def oauth_authorize(
    provider: str,
    request: Request,
    tenant: Tenant = Depends(get_current_tenant),
):
    """Generate an OAuth authorization URL and redirect the user."""
    _validate_provider(provider)

    state = secrets.token_urlsafe(32)
    redirect_uri = _build_redirect_uri(request, provider)

    # TODO: Store state token in Redis for CSRF protection
    # redis = await get_redis()
    # await redis.setex(f"oauth_state:{state}", 600, json.dumps({
    #     "tenant_id": str(tenant.id),
    #     "provider": provider,
    # }))

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

    elif provider == "microsoft":
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


@router.get("/oauth/{provider}/callback")
async def oauth_callback(
    provider: str,
    request: Request,
    code: str,
    state: str,
    db: AsyncSession = Depends(get_db),
):
    """Handle OAuth callback — exchange code for tokens and store them."""
    _validate_provider(provider)

    # TODO: Validate state token from Redis for CSRF protection
    # redis = await get_redis()
    # stored = await redis.get(f"oauth_state:{state}")
    # if not stored:
    #     raise HTTPException(status_code=400, detail="Invalid or expired state token")
    # state_data = json.loads(stored)
    # tenant_id = state_data["tenant_id"]
    # await redis.delete(f"oauth_state:{state}")

    # Placeholder tenant_id — in production, extract from validated state token
    tenant_id: Optional[str] = None  # Will come from state validation above

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
        flow.fetch_token(code=code)
        credentials = flow.credentials

        # TODO: Encrypt tokens with AES-256 before storing
        access_token = credentials.token
        refresh_token = credentials.refresh_token
        scopes = list(credentials.scopes) if credentials.scopes else GOOGLE_SCOPES
        expires_at = credentials.expiry

    elif provider == "microsoft":
        import msal

        app = msal.ConfidentialClientApplication(
            settings.MICROSOFT_CLIENT_ID,
            authority="https://login.microsoftonline.com/common",
            client_credential=settings.MICROSOFT_CLIENT_SECRET,
        )
        result = app.acquire_token_by_authorization_code(
            code,
            scopes=MICROSOFT_SCOPES,
            redirect_uri=redirect_uri,
        )
        if "error" in result:
            raise HTTPException(
                status_code=400,
                detail=f"Token exchange failed: {result.get('error_description', result['error'])}",
            )

        # TODO: Encrypt tokens with AES-256 before storing
        access_token = result.get("access_token")
        refresh_token = result.get("refresh_token")
        scopes = MICROSOFT_SCOPES
        expires_at = None  # Microsoft tokens typically expire in 1 hour

    # Upsert integration record
    # NOTE: tenant_id and user_id should come from validated state in production
    integration = Integration(
        provider=provider,
        access_token=access_token,
        refresh_token=refresh_token,
        scopes=scopes,
        expires_at=expires_at,
        # tenant_id and user_id will be set from state token validation
    )
    db.add(integration)
    await db.flush()

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
        raise HTTPException(status_code=404, detail=f"No {provider} integration found")

    await db.delete(integration)
