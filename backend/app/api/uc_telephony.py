"""HTTP routes for UC vendor (RingCentral / Webex Calling / Zoom Phone)
recording webhooks.

Three POST endpoints, one per provider:

* ``POST /uc/ringcentral/webhook/{tenant_id}`` — handles the RC
  Validation-Token handshake on first delivery + steady-state events.
* ``POST /uc/webex/webhook/{tenant_id}`` — verifies X-Spark-Signature
  and dispatches.
* ``POST /uc/zoom/webhook/{tenant_id}`` — handles Zoom's
  URL-validation challenge + signed steady-state events.

All three:

1. Resolve the tenant from the URL path.
2. Look up the tenant's ``Integration`` row (for the OAuth access
   token used during recording fetch).
3. Verify the inbound signature using the **vendor-wide** signing
   secret loaded from an env var (``RINGCENTRAL_WEBHOOK_SECRET``,
   ``WEBEX_WEBHOOK_SECRET``, ``ZOOM_PHONE_WEBHOOK_SECRET``). One
   secret per vendor — set once as a Fly secret. Tenant identity
   comes from the URL path (and double-checked against the payload's
   account/org id by the adapter). This matches how Stripe, Slack,
   Twilio and GitHub run multi-tenant webhook integrations.
4. Upsert a :class:`UcRecordingJob` row keyed on
   ``(provider, external_call_id)`` (idempotency).
5. Enqueue the Celery ``fetch_uc_recording`` task.

Vendor signatures are cryptographic auth — no Bearer token / API-key
scope is required on these routes.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Tuple

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db import get_db
from backend.app.models import Integration, Tenant, UcRecordingJob
from backend.app.services.telephony.uc.base import (
    UCWebhookEvent,
    WebhookVerificationError,
    get_provider,
)
from backend.app.services.telephony.uc.zoom_phone import ZoomPhoneProvider

logger = logging.getLogger(__name__)
router = APIRouter()


# Vendor-wide webhook signing secrets. One value per vendor, set as a Fly
# secret at deploy time — NOT per-tenant. Tenant identity comes from the
# URL path /uc/{vendor}/webhook/{tenant_id} and from the payload itself
# (accountId / orgId / account_id depending on vendor). Per-tenant
# secrets were dropped because they added operator burden on every
# customer onboarding without meaningful security benefit for the
# multi-tenant SaaS shape — the same pattern Stripe / Slack / Twilio /
# GitHub use for their webhook integrations.
_SIGNING_SECRET_ENV: dict[str, str] = {
    "ringcentral": "RINGCENTRAL_WEBHOOK_SECRET",
    "webex_calling": "WEBEX_WEBHOOK_SECRET",
    # Zoom's "Secret Token" comes from the Marketplace app config and is
    # already per-app (not per-tenant) by Zoom's own design.
    "zoom_phone": "ZOOM_PHONE_WEBHOOK_SECRET",
}


def _signing_secret(provider: str) -> str:
    env_key = _SIGNING_SECRET_ENV.get(provider, "")
    return os.environ.get(env_key, "")


async def _resolve_integration(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    provider: str,
) -> Tuple[Tenant, Integration]:
    """Find the ``Tenant`` + ``Integration`` for a webhook delivery.

    Raises 404 when the integration isn't connected — better than 401
    because vendor signatures are cryptographic, and a misrouted
    delivery (wrong tenant in the URL) should fail loudly enough that
    the operator notices.
    """
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Unknown tenant")
    integ = (
        await db.execute(
            select(Integration)
            .where(
                Integration.tenant_id == tenant_id,
                Integration.provider == provider,
            )
            .order_by(Integration.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if integ is None:
        raise HTTPException(
            status_code=404,
            detail=f"No {provider} integration connected for tenant",
        )
    return tenant, integ


async def _upsert_job_and_dispatch(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    integration_id: uuid.UUID,
    event: UCWebhookEvent,
) -> UcRecordingJob:
    """Idempotent UcRecordingJob upsert keyed on (provider, external_call_id).

    Late-arriving duplicates are no-ops: we keep the existing row
    (even when state is ``done``) and skip the Celery enqueue.
    """
    existing = (
        await db.execute(
            select(UcRecordingJob).where(
                UcRecordingJob.provider == event.provider,
                UcRecordingJob.external_call_id == event.external_call_id,
            )
        )
    ).scalar_one_or_none()

    payload_for_audit = dict(event.raw or {})
    # Don't persist the provider_config marker (it carries the secret).
    payload_for_audit.pop("__provider_config", None)

    if existing is not None:
        existing.recording_url = event.recording_url or existing.recording_url
        existing.duration_seconds = (
            event.duration_seconds or existing.duration_seconds
        )
        existing.payload = payload_for_audit
        await db.flush()
        if existing.state in ("done", "dispatched", "in_progress"):
            return existing
        _enqueue_fetch(existing.id)
        return existing

    job = UcRecordingJob(
        tenant_id=tenant_id,
        integration_id=integration_id,
        provider=event.provider,
        external_call_id=event.external_call_id,
        recording_id=event.recording_id,
        recording_url=event.recording_url,
        duration_seconds=event.duration_seconds,
        started_at_provider=event.started_at,
        direction=event.direction,
        caller_phone=event.caller_phone,
        callee_phone=event.callee_phone,
        payload=payload_for_audit,
        state="pending",
        attempts=0,
    )
    db.add(job)
    await db.flush()
    _enqueue_fetch(job.id)
    return job


def _enqueue_fetch(job_id: uuid.UUID) -> None:
    """Fire-and-forget Celery dispatch.

    Celery is optional in unit tests; suppress dispatch errors so the
    HTTP route still 200s when the broker is offline.
    """
    try:
        from backend.app.services.telephony.uc.fetch_task import fetch_uc_recording

        fetch_uc_recording.delay(str(job_id))
    except Exception:
        logger.exception(
            "fetch_uc_recording enqueue failed for job %s", job_id
        )


@router.post("/uc/ringcentral/webhook/{tenant_id}")
async def ringcentral_webhook(
    tenant_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """RC Validation-Token handshake + steady-state recording events."""
    headers = {k.lower(): v for k, v in request.headers.items()}
    validation_token = headers.get("validation-token")
    if validation_token and not headers.get("verification-token"):
        return Response(
            status_code=200,
            headers={"Validation-Token": validation_token},
        )

    _, integration = await _resolve_integration(
        db, tenant_id=tenant_id, provider="ringcentral"
    )
    body = await request.body()
    secret = _signing_secret("ringcentral")

    provider = get_provider("ringcentral")
    try:
        event = await provider.verify_webhook(
            headers=headers, body=body, signing_secret=secret
        )
    except WebhookVerificationError as exc:
        logger.warning("RingCentral webhook verification failed: %s", exc)
        raise HTTPException(status_code=401, detail=str(exc))

    job = await _upsert_job_and_dispatch(
        db,
        tenant_id=tenant_id,
        integration_id=integration.id,
        event=event,
    )
    await db.commit()
    return {"status": "queued", "job_id": str(job.id)}


@router.post("/uc/webex/webhook/{tenant_id}")
async def webex_webhook(
    tenant_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Webex Webhooks API delivery."""
    _, integration = await _resolve_integration(
        db, tenant_id=tenant_id, provider="webex_calling"
    )
    body = await request.body()
    secret = _signing_secret("webex_calling")
    headers = {k.lower(): v for k, v in request.headers.items()}

    provider = get_provider("webex_calling")
    try:
        event = await provider.verify_webhook(
            headers=headers, body=body, signing_secret=secret
        )
    except WebhookVerificationError as exc:
        logger.warning("Webex webhook verification failed: %s", exc)
        raise HTTPException(status_code=401, detail=str(exc))

    job = await _upsert_job_and_dispatch(
        db,
        tenant_id=tenant_id,
        integration_id=integration.id,
        event=event,
    )
    await db.commit()
    return {"status": "queued", "job_id": str(job.id)}


@router.post("/uc/zoom/webhook/{tenant_id}")
async def zoom_phone_webhook(
    tenant_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Zoom Phone URL-validation challenge + steady-state events."""
    body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}

    try:
        peek = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        peek = {}

    event_name = peek.get("event")

    _, integration = await _resolve_integration(
        db, tenant_id=tenant_id, provider="zoom_phone"
    )
    secret = _signing_secret("zoom_phone")

    if event_name == "endpoint.url_validation":
        plain_token = (peek.get("payload") or {}).get("plainToken") or ""
        if not (plain_token and secret):
            raise HTTPException(
                status_code=400,
                detail="Cannot answer URL validation without ZOOM_PHONE_WEBHOOK_SECRET",
            )
        return JSONResponse(
            ZoomPhoneProvider.url_validation_response(plain_token, secret)
        )

    provider = get_provider("zoom_phone")
    try:
        event = await provider.verify_webhook(
            headers=headers, body=body, signing_secret=secret
        )
    except WebhookVerificationError as exc:
        logger.warning("Zoom Phone webhook verification failed: %s", exc)
        raise HTTPException(status_code=401, detail=str(exc))

    job = await _upsert_job_and_dispatch(
        db,
        tenant_id=tenant_id,
        integration_id=integration.id,
        event=event,
    )
    await db.commit()
    return {"status": "queued", "job_id": str(job.id)}


__all__ = ["router"]
