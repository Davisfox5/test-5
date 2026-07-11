"""Shared outbound-mailbox plumbing.

Extracted from api/emails.py so the cold-outreach scheduler (Celery, sync
session + asyncio.run) sends through exactly the same transport as the
interactive send-follow-up endpoint: same Integration row, same token
decrypt/refresh-and-re-persist behavior, same provider senders.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Union

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from backend.app.models import Integration
from backend.app.services.email.base import EmailError, EmailSender
from backend.app.services.email.gmail import GmailSender
from backend.app.services.email.outlook import OutlookSender
from backend.app.services.token_crypto import decrypt_token, encrypt_token

# Providers we can send email through, in fallback preference order
# (Google first — larger install base).
EMAIL_PROVIDERS: List[str] = ["google", "microsoft"]


def _integration_stmt(tenant_id: uuid.UUID, provider: str):
    return (
        select(Integration)
        .where(
            Integration.tenant_id == tenant_id,
            Integration.provider == provider,
        )
        .order_by(Integration.created_at.desc())
        .limit(1)
    )


async def resolve_email_integration(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    preferred: Optional[str] = None,
) -> Optional[Integration]:
    """Pick a connected email provider for the tenant (async sessions).

    When ``preferred`` is given, return only that provider's integration
    or None. Otherwise fall through EMAIL_PROVIDERS in order.
    """
    providers = [preferred] if preferred else EMAIL_PROVIDERS
    for p in providers:
        if p is None:
            continue
        integ = (await db.execute(_integration_stmt(tenant_id, p))).scalar_one_or_none()
        if integ is not None:
            return integ
    return None


def resolve_email_integration_sync(
    session: Session,
    tenant_id: uuid.UUID,
    preferred: Optional[str] = None,
) -> Optional[Integration]:
    """Sync twin of :func:`resolve_email_integration` for Celery tasks."""
    providers = [preferred] if preferred else EMAIL_PROVIDERS
    for p in providers:
        if p is None:
            continue
        integ = session.execute(_integration_stmt(tenant_id, p)).scalar_one_or_none()
        if integ is not None:
            return integ
    return None


def build_sender(
    integ: Integration, from_address_hint: Optional[str] = None
) -> EmailSender:
    """Decrypt the stored tokens and construct the provider sender.

    The ``on_token_refresh`` callback re-encrypts refreshed tokens onto the
    Integration row **in memory** — the caller owns the session and must
    commit for the refresh to stick (both the API request cycle and the
    scheduler tick commit after each send).

    Raises :class:`EmailError` on an unsupported provider — callers map
    that to their own error surface (HTTP 400 / failed member).
    """
    access = decrypt_token(integ.access_token) or ""
    refresh = decrypt_token(integ.refresh_token)

    async def _on_refresh(
        new_access: str,
        new_refresh: Optional[str],
        expires_in: Optional[int],
    ) -> None:
        integ.access_token = encrypt_token(new_access)
        if new_refresh:
            integ.refresh_token = encrypt_token(new_refresh)
        if expires_in:
            integ.expires_at = datetime.now(timezone.utc) + timedelta(
                seconds=int(expires_in)
            )

    if integ.provider == "google":
        return GmailSender(
            access_token=access,
            refresh_token=refresh,
            from_address=from_address_hint or "",
            on_token_refresh=_on_refresh,
        )
    if integ.provider == "microsoft":
        return OutlookSender(
            access_token=access,
            refresh_token=refresh,
            from_address=from_address_hint,
            on_token_refresh=_on_refresh,
        )
    raise EmailError(f"Unsupported email provider on integration: {integ.provider}")


async def close_sender(sender: Union[EmailSender, None]) -> None:
    if sender is None:
        return
    try:
        await sender.close()
    except Exception:  # pragma: no cover - close is best-effort
        pass
