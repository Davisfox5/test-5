"""Per-integration poller — runs inside a Celery task.

Scans every active Integration, refreshes its token if needed, pulls
recent messages via the provider-specific fetcher, and hands each one
to :func:`ingest_email`.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from backend.app.models import EmailSyncCursor, Integration, Tenant, User
from backend.app.services.email_ingest import gmail as gmail_fetcher
from backend.app.services.email_ingest import graph as graph_fetcher
from backend.app.services.email_ingest.ingest import ingest_email
from backend.app.services.email_classifier import EmailClassifier
from backend.app.services.token_crypto import decrypt_token, encrypt_token

logger = logging.getLogger(__name__)


def _refresh_if_expired_sync(session: Session, integration: Integration) -> str:
    """Return a valid access token, refreshing via provider API if needed.

    Mirrors the async version in api/oauth.py but runs inside Celery's
    synchronous world.  Updates the Integration row in place; caller
    commits.
    """
    token = decrypt_token(integration.access_token)
    now = datetime.now(timezone.utc)
    if integration.expires_at and integration.expires_at > now + timedelta(seconds=30):
        return token

    refresh = decrypt_token(integration.refresh_token)
    if not refresh:
        raise RuntimeError(
            f"Integration {integration.id} expired and has no refresh token"
        )

    if integration.provider == "google":
        import requests

        from backend.app.config import get_settings

        s = get_settings()
        resp = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": s.GOOGLE_CLIENT_ID,
                "client_secret": s.GOOGLE_CLIENT_SECRET,
                "refresh_token": refresh,
                "grant_type": "refresh_token",
            },
            timeout=10,
        )
        resp.raise_for_status()
        j = resp.json()
        new_access = j["access_token"]
        new_refresh = j.get("refresh_token")
        expires_at = now + timedelta(seconds=int(j.get("expires_in", 3600)))
    else:
        import msal

        from backend.app.config import get_settings

        s = get_settings()
        app = msal.ConfidentialClientApplication(
            s.MICROSOFT_CLIENT_ID,
            authority="https://login.microsoftonline.com/common",
            client_credential=s.MICROSOFT_CLIENT_SECRET,
        )
        result = app.acquire_token_by_refresh_token(
            refresh,
            scopes=[
                "Mail.Send", "Mail.Read", "Calendars.ReadWrite",
                "Contacts.Read", "offline_access",
            ],
        )
        if "error" in result:
            raise RuntimeError(f"Microsoft refresh failed: {result['error']}")
        new_access = result["access_token"]
        new_refresh = result.get("refresh_token")
        expires_at = now + timedelta(seconds=int(result.get("expires_in", 3600)))

    integration.access_token = encrypt_token(new_access)
    if new_refresh:
        integration.refresh_token = encrypt_token(new_refresh)
    integration.expires_at = expires_at
    return new_access


def poll_integration(session: Session, integration: Integration) -> int:
    """Poll a single integration.  Returns the number of emails ingested."""
    tenant = session.query(Tenant).filter(Tenant.id == integration.tenant_id).first()
    if tenant is None:
        logger.warning("Integration %s has no tenant; skipping", integration.id)
        return 0

    # Agent email = the User attached to the integration (set on OAuth callback).
    user = (
        session.query(User).filter(User.id == integration.user_id).first()
        if integration.user_id else None
    )
    agent_email = user.email if user else None

    cursor = (
        session.query(EmailSyncCursor)
        .filter(EmailSyncCursor.integration_id == integration.id)
        .first()
    )
    if cursor is None:
        cursor = EmailSyncCursor(
            integration_id=integration.id,
            tenant_id=integration.tenant_id,
            provider=integration.provider,
        )
        session.add(cursor)
        session.flush()

    access_token = _refresh_if_expired_sync(session, integration)

    if integration.provider == "google":
        stream = gmail_fetcher.fetch_recent(integration, cursor, access_token, agent_email)
    elif integration.provider == "microsoft":
        stream = graph_fetcher.fetch_recent(integration, cursor, access_token, agent_email)
    else:
        logger.info("Skipping non-email integration %s (%s)", integration.id, integration.provider)
        return 0

    classifier = EmailClassifier()
    ingested = 0

    async def _run(msgs):
        nonlocal ingested
        for msg in msgs:
            if await ingest_email(session, tenant, msg, classifier) is not None:
                ingested += 1

    asyncio.run(_run(stream))
    session.commit()
    return ingested


def poll_all(session: Session) -> dict:
    """Poll every email-capable integration *that doesn't have push configured*.

    Real-time delivery comes from Gmail Pub/Sub + Microsoft Graph push
    webhooks. When those are configured globally (``GMAIL_PUBSUB_TOPIC`` /
    ``GRAPH_CLIENT_STATE``) we skip the corresponding provider here — the
    poll is only a safety net for environments that can't receive
    webhooks (local dev, air-gapped installs, pre-verification tenants).

    To force a full poll regardless of push config (smoke tests), pass
    ``settings.EMAIL_POLL_FORCE_ALL = True`` in the env.
    """
    from backend.app.config import get_settings

    settings = get_settings()
    force_all = bool(getattr(settings, "EMAIL_POLL_FORCE_ALL", False))

    providers: list[str] = []
    if force_all or not getattr(settings, "GMAIL_PUBSUB_TOPIC", ""):
        providers.append("google")
    if force_all or not getattr(settings, "GRAPH_CLIENT_STATE", ""):
        providers.append("microsoft")

    if not providers:
        return {
            "integrations": 0,
            "emails_ingested": 0,
            "skipped_reason": "push_configured_for_all_providers",
        }

    integrations = (
        session.query(Integration)
        .filter(Integration.provider.in_(providers))
        .all()
    )
    summary = {"integrations": 0, "emails_ingested": 0, "polled_providers": providers}
    for integ in integrations:
        try:
            count = poll_integration(session, integ)
        except Exception:
            logger.exception("Poll failed for integration %s", integ.id)
            session.rollback()
            continue
        summary["integrations"] += 1
        summary["emails_ingested"] += count
    return summary
