"""Public click-redirect endpoint for tracked outreach links.

Unauthenticated by design — the hits come from prospects' mail clients.
The opaque token is the only input; it resolves to a stored destination
and the response is always a 302. The destination is NEVER taken from
the request (no open redirect), and an unknown token bounces to a safe
default rather than erroring — a stale tracking link should never
strand a prospect on a 404.

Registered at the ROOT path (``/t/{token}``, not under /api/v1): the
rewritten links live inside emails as ``https://<host>/t/<token>`` and
are kept short and unversioned on purpose.

RLS: ``outreach_links`` is in ``rls.AUTH_BOOTSTRAP_TABLES`` — the token
lookup runs before any tenant is known (same posture as the webhook
correlation tables); ``record_click`` binds the tenant immediately
after for the event write.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db import get_db
from backend.app.models import OutreachLink
from backend.app.services.outreach.links import fallback_redirect_url, record_click

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/t/{token}", include_in_schema=False)
async def follow_tracked_link(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    link = (
        await db.execute(select(OutreachLink).where(OutreachLink.token == token))
    ).scalar_one_or_none()
    if link is None:
        return RedirectResponse(url=fallback_redirect_url(), status_code=302)

    # Capture before the write attempt — a rollback would expire the ORM row.
    destination = link.original_url
    try:
        await record_click(
            db,
            link,
            user_agent=request.headers.get("User-Agent"),
            client_ip=(request.client.host if request.client else None),
        )
    except Exception:
        # Recording is best-effort; the prospect always gets their page.
        logger.warning(
            "outreach click recording failed link=%s", link.id, exc_info=True
        )
        try:
            await db.rollback()
        except Exception:
            pass
    return RedirectResponse(url=destination, status_code=302)
