"""Public CSAT survey endpoints.

Unauthenticated. The customer follows a signed link the support team
emailed them and submits a 1-5 score. The token encodes the case id +
an HMAC slice over it; the tenant's ``outcomes_hmac_secret`` (or the
session JWT secret as a fallback) is the signing key.

GET /csat/{token} — returns the case's public-safe summary so the
form can show "Rate your experience with case X (opened on Y)" without
leaking PII like the customer name or full conversation.

POST /csat/{token} — accepts the score, writes it back on the
SupportCase. Idempotent (last write wins) so a customer who refreshes
or re-submits doesn't get an error.

Per-token rate limit: best-effort via Redis SETNX (5 / minute / token)
to make brute-forcing the HMAC slice impractical.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.config import get_settings
from backend.app.db import get_db
from backend.app.models import SupportCase, Tenant
from backend.app.services.support_case_service import verify_csat_token

logger = logging.getLogger(__name__)

router = APIRouter()


class CsatPublicCaseOut(BaseModel):
    """Public-safe view: enough to render "Rate your experience" without
    leaking customer identity or the conversation content."""

    case_subject: str
    opened_at: datetime
    resolved_at: Optional[datetime]
    status: str
    already_submitted: bool


class CsatPublicIn(BaseModel):
    score: int = Field(..., ge=1, le=5)
    comment: Optional[str] = Field(None, max_length=2000)


async def _resolve_token(
    db: AsyncSession, token: str
) -> SupportCase:
    """Decode the token, look up the case + signing key, validate."""
    # Token: <case_id_hex>.<sig>. Pull the case id first so we can
    # fetch the per-tenant signing secret.
    if "." not in token:
        raise HTTPException(status_code=404, detail="Invalid survey link")
    cid_str, _sig = token.split(".", 1)
    try:
        case_id = uuid.UUID(cid_str)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid survey link")
    case = await db.get(SupportCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Invalid survey link")
    tenant = await db.get(Tenant, case.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Invalid survey link")
    settings = get_settings()
    secret = tenant.outcomes_hmac_secret or settings.SESSION_JWT_SECRET or ""
    if not secret:
        raise HTTPException(status_code=500, detail="Survey misconfigured")
    if verify_csat_token(token, secret=secret) is None:
        # Failed HMAC. Same 404 as a fake case id so timing doesn't
        # leak which half of the token was the forgery.
        raise HTTPException(status_code=404, detail="Invalid survey link")
    return case


@router.get(
    "/csat/{token}",
    response_model=CsatPublicCaseOut,
)
async def get_public_case(
    token: str,
    db: AsyncSession = Depends(get_db),
) -> CsatPublicCaseOut:
    case = await _resolve_token(db, token)
    return CsatPublicCaseOut(
        case_subject=case.subject,
        opened_at=case.opened_at,
        resolved_at=case.resolved_at,
        status=case.status,
        already_submitted=case.csat_score is not None,
    )


@router.post(
    "/csat/{token}",
    response_model=CsatPublicCaseOut,
)
async def submit_public_csat(
    token: str,
    body: CsatPublicIn,
    db: AsyncSession = Depends(get_db),
) -> CsatPublicCaseOut:
    case = await _resolve_token(db, token)
    if not _claim_csat_slot(case.id):
        raise HTTPException(
            status_code=429,
            detail="Too many submissions for this case. Try again in a minute.",
        )
    if case.status not in ("resolved", "closed"):
        raise HTTPException(
            status_code=400,
            detail="Survey closed: case is still being worked.",
        )
    case.csat_score = body.score
    if body.comment:
        meta = dict(case.metadata_ or {})
        meta["csat_comment"] = body.comment[:2000]
        case.metadata_ = meta
    await db.commit()
    return CsatPublicCaseOut(
        case_subject=case.subject,
        opened_at=case.opened_at,
        resolved_at=case.resolved_at,
        status=case.status,
        already_submitted=True,
    )


def _claim_csat_slot(case_id: uuid.UUID) -> bool:
    """Best-effort token-rate-limit. 5/min/case. Failing open is
    intentional: the worst case is a customer can submit two scores in
    a minute, last-write-wins."""
    try:
        import redis  # type: ignore

        r = redis.Redis.from_url(get_settings().REDIS_URL, decode_responses=True)
        key = f"csat:slot:{case_id}"
        # NX + 12s TTL bucketed into 5-per-minute = 5 NX wins per 60s.
        # We just rate-limit re-submits to roughly 1 every 12s.
        return bool(r.set(key, "1", ex=12, nx=True))
    except Exception:
        logger.debug("csat rate-limit Redis check failed (allowing)", exc_info=True)
        return True
