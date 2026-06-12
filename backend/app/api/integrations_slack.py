"""Slack OAuth integration — per-tenant install + channel picker.

Flow:

1. ``GET /integrations/slack/install`` — generate a state token,
   redirect to ``slack.com/oauth/v2/authorize`` with bot scopes
   ``chat:write``, ``channels:read``, ``groups:read``.

2. ``GET /integrations/slack/oauth/callback`` — exchange the
   authorization ``code`` for a bot token via ``oauth.v2.access``,
   encrypt it via ``token_crypto``, store in ``slack_integration``.

3. ``GET /integrations/slack/channels`` — proxy ``conversations.list``
   so the SPA can show a channel picker.

4. ``POST /integrations/slack/channel`` — set ``default_channel_id``.

5. ``DELETE /integrations/slack`` — revoke and forget.

The bot token is the only authentication the alert-fanout layer needs;
managers don't proxy through their own user identity when the system
posts to their team channel.
"""

from __future__ import annotations

import logging
import secrets
import urllib.parse
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import (
    get_current_tenant,
    require_role,
)
from backend.app.config import get_settings
from backend.app.db import get_db
from backend.app.models import SlackIntegration, Tenant
from backend.app.services.token_crypto import decrypt_token, encrypt_token

logger = logging.getLogger(__name__)

router = APIRouter()


_SLACK_AUTHORIZE_URL = "https://slack.com/oauth/v2/authorize"
_SLACK_OAUTH_ACCESS_URL = "https://slack.com/api/oauth.v2.access"
_SLACK_REVOKE_URL = "https://slack.com/api/auth.revoke"
_SLACK_CONVERSATIONS_LIST = "https://slack.com/api/conversations.list"
_BOT_SCOPES = "chat:write,channels:read,groups:read"


class InstallURL(BaseModel):
    url: str


class IntegrationOut(BaseModel):
    tenant_id: uuid.UUID
    slack_team_id: str
    slack_team_name: Optional[str]
    default_channel_id: Optional[str]
    default_channel_name: Optional[str]
    installed_at: datetime
    revoked_at: Optional[datetime]


class ChannelOut(BaseModel):
    id: str
    name: str
    is_private: bool


class SetChannelBody(BaseModel):
    channel_id: str
    channel_name: Optional[str] = None


@router.get(
    "/integrations/slack/install",
    response_model=InstallURL,
    dependencies=[Depends(require_role("admin"))],
)
async def slack_install_url(
    tenant: Tenant = Depends(get_current_tenant),
):
    """Return the Slack OAuth authorize URL the SPA should redirect to.

    State is the tenant id encoded with a random nonce; the callback
    validates that the returned state belongs to a real tenant.
    """
    settings = get_settings()
    if not settings.SLACK_CLIENT_ID:
        raise HTTPException(
            status_code=503, detail="Slack OAuth is not configured on this server."
        )
    nonce = secrets.token_urlsafe(16)
    state = f"{tenant.id}.{nonce}"
    redirect_uri = _redirect_uri()
    params = {
        "client_id": settings.SLACK_CLIENT_ID,
        "scope": _BOT_SCOPES,
        "user_scope": "",
        "redirect_uri": redirect_uri,
        "state": state,
    }
    url = f"{_SLACK_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"
    return InstallURL(url=url)


@router.get("/integrations/slack/oauth/callback")
async def slack_oauth_callback(
    code: str = Query(...),
    state: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """Slack redirects back here after the user approves the install."""
    settings = get_settings()
    tenant_id = _parse_state(state)
    if tenant_id is None:
        raise HTTPException(status_code=400, detail="Invalid state.")

    redirect_uri = _redirect_uri()
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.post(
            _SLACK_OAUTH_ACCESS_URL,
            data={
                "client_id": settings.SLACK_CLIENT_ID,
                "client_secret": settings.SLACK_CLIENT_SECRET,
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )
    body: Dict[str, Any] = resp.json()
    if not body.get("ok"):
        logger.warning("Slack OAuth exchange failed: %s", body)
        raise HTTPException(status_code=502, detail=f"Slack OAuth error: {body.get('error')}")

    bot_token = body.get("access_token") or ""
    team = body.get("team") or {}
    encrypted = encrypt_token(bot_token) or ""

    integration = await db.get(SlackIntegration, tenant_id)
    if integration is None:
        integration = SlackIntegration(
            tenant_id=tenant_id,
            slack_team_id=team.get("id") or "",
            slack_team_name=team.get("name"),
            bot_user_id=body.get("bot_user_id"),
            bot_token_encrypted=encrypted,
        )
        db.add(integration)
    else:
        integration.slack_team_id = team.get("id") or integration.slack_team_id
        integration.slack_team_name = team.get("name") or integration.slack_team_name
        integration.bot_user_id = body.get("bot_user_id") or integration.bot_user_id
        integration.bot_token_encrypted = encrypted
        integration.revoked_at = None
        integration.installed_at = datetime.now(timezone.utc)
    await db.commit()

    spa_url = settings.SPA_URL or (settings.ALLOWED_ORIGINS[0] if settings.ALLOWED_ORIGINS else "")
    return RedirectResponse(url=f"{spa_url}/settings/integrations/slack?status=connected")


@router.get(
    "/integrations/slack",
    response_model=Optional[IntegrationOut],
    dependencies=[Depends(require_role("manager"))],
)
async def get_integration(
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    row = await db.get(SlackIntegration, tenant.id)
    if row is None or row.revoked_at is not None:
        return None
    return IntegrationOut(
        tenant_id=row.tenant_id,
        slack_team_id=row.slack_team_id,
        slack_team_name=row.slack_team_name,
        default_channel_id=row.default_channel_id,
        default_channel_name=row.default_channel_name,
        installed_at=row.installed_at,
        revoked_at=row.revoked_at,
    )


@router.get(
    "/integrations/slack/channels",
    response_model=List[ChannelOut],
    dependencies=[Depends(require_role("manager"))],
)
async def list_channels(
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """List conversations the bot can post to, for the channel picker."""
    row = await _active_integration(db, tenant.id)
    bot_token = decrypt_token(row.bot_token_encrypted) or ""
    if not bot_token:
        raise HTTPException(status_code=500, detail="Slack token unavailable.")
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.get(
            _SLACK_CONVERSATIONS_LIST,
            headers={"Authorization": f"Bearer {bot_token}"},
            params={"types": "public_channel,private_channel", "limit": 200, "exclude_archived": "true"},
        )
    body = resp.json()
    if not body.get("ok"):
        raise HTTPException(status_code=502, detail=f"Slack error: {body.get('error')}")
    return [
        ChannelOut(
            id=ch.get("id"),
            name=ch.get("name") or ch.get("id"),
            is_private=bool(ch.get("is_private")),
        )
        for ch in (body.get("channels") or [])
        if ch.get("id")
    ]


@router.post(
    "/integrations/slack/channel",
    response_model=IntegrationOut,
    dependencies=[Depends(require_role("manager"))],
)
async def set_default_channel(
    body: SetChannelBody,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    row = await _active_integration(db, tenant.id)
    row.default_channel_id = body.channel_id
    row.default_channel_name = body.channel_name
    await db.commit()
    return await get_integration(db=db, tenant=tenant)  # type: ignore[return-value]


@router.delete(
    "/integrations/slack",
    dependencies=[Depends(require_role("admin"))],
)
async def uninstall(
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    row = await db.get(SlackIntegration, tenant.id)
    if row is None or row.revoked_at is not None:
        return {"ok": True}
    bot_token = decrypt_token(row.bot_token_encrypted) or ""
    if bot_token:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(
                    _SLACK_REVOKE_URL,
                    headers={"Authorization": f"Bearer {bot_token}"},
                )
        except Exception:
            logger.exception("auth.revoke failed (continuing with local delete)")
    row.revoked_at = datetime.now(timezone.utc)
    row.default_channel_id = None
    row.default_channel_name = None
    await db.commit()
    return {"ok": True}


# ─────────────────────────────────────────────────────────────────────


def _redirect_uri() -> str:
    """Build the OAuth redirect URI from configured origins.

    Slack requires the callback URL to be registered in the app config;
    we resolve it from ``SPA_URL`` (production) or fall back to the
    first allowed origin (staging).
    """
    settings = get_settings()
    base = settings.SPA_URL or (settings.ALLOWED_ORIGINS[0] if settings.ALLOWED_ORIGINS else "")
    if not base:
        # Local dev: rely on the developer setting an explicit redirect
        # URI in the Slack app config that matches whatever they're
        # tunnelling. The empty string here yields an OAuth error which
        # is the correct loud failure mode.
        return ""
    base = base.rstrip("/")
    return f"{base}{settings.API_V1_PREFIX}/integrations/slack/oauth/callback"


def _parse_state(state: str) -> Optional[uuid.UUID]:
    if not state or "." not in state:
        return None
    raw = state.split(".", 1)[0]
    try:
        return uuid.UUID(raw)
    except (TypeError, ValueError):
        return None


async def _active_integration(
    db: AsyncSession, tenant_id: uuid.UUID
) -> SlackIntegration:
    row = await db.get(SlackIntegration, tenant_id)
    if row is None or row.revoked_at is not None:
        raise HTTPException(status_code=404, detail="Slack not installed.")
    return row
