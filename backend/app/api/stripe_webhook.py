"""Stripe billing endpoints.

Two surfaces:

* ``POST /webhooks/stripe`` — Stripe's outbound webhook. We verify the
  ``Stripe-Signature`` header, then handle:

  - ``customer.subscription.created`` / ``customer.subscription.updated``
    → look up the tenant by ``stripe_customer_id``, map the active
    price to a tier, call ``apply_tier``.
  - ``customer.subscription.deleted`` (cancellation) → drop the tenant
    back to ``solo`` and clear ``stripe_subscription_id``.
  - Anything else → 200 OK, ignored. Stripe retries on non-2xx.

* ``POST /admin/stripe/link`` — admin pins a ``stripe_customer_id`` on
  their tenant. Required before the first webhook arrives, since
  Stripe doesn't know about our tenant ids.

Design notes:

* The webhook endpoint is **unauthenticated** (standard for Stripe —
  we prove it's Stripe by signature). We don't gate it by tenant
  because one endpoint serves every tenant; the lookup by
  ``stripe_customer_id`` scopes work.
* We never deactivate users on downgrade. The new tier's ``seat_limit``
  is enforced the next time someone *creates* a user — surplus
  existing users keep working until deliberately removed. That's the
  non-retroactive policy we set when we built ``apply_tier``.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import AuthPrincipal, require_role
from backend.app.config import get_settings
from backend.app.db import get_db
from backend.app.models import Tenant
from backend.app.services.stripe_billing import (
    price_id_to_tier,
    verify_stripe_signature,
)
from backend.app.services.seat_reconciliation import reconcile_seats
from backend.app.services.subscription_tiers import apply_tier

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Admin: link Stripe customer ──────────────────────────────────────


class StripeLinkIn(BaseModel):
    stripe_customer_id: str


@router.post("/admin/stripe/link")
async def link_stripe_customer(
    body: StripeLinkIn,
    principal: AuthPrincipal = Depends(require_role("admin")),
) -> Dict[str, Any]:
    """Pin a Stripe customer id onto the tenant.

    Call this once after creating the Stripe Customer (either manually
    via the Stripe dashboard, or automatically in a billing flow).
    Subsequent ``customer.subscription.*`` webhooks for that customer
    id will resolve to this tenant.
    """
    customer_id = (body.stripe_customer_id or "").strip()
    if not customer_id.startswith("cus_"):
        raise HTTPException(
            status_code=400,
            detail="stripe_customer_id should start with 'cus_'",
        )
    principal.tenant.stripe_customer_id = customer_id
    return {
        "tenant_id": str(principal.tenant.id),
        "stripe_customer_id": customer_id,
    }


# ── Webhook ──────────────────────────────────────────────────────────


@router.post("/webhooks/stripe")
async def stripe_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Stripe outbound webhook receiver."""
    settings = get_settings()
    raw = await request.body()
    signature = request.headers.get("Stripe-Signature", "")

    secret = settings.STRIPE_WEBHOOK_SECRET
    if not secret:
        # Prod must set the secret. Refuse to accept unverified webhooks
        # rather than silently trust them.
        raise HTTPException(
            status_code=503,
            detail="Stripe webhook secret is not configured on the server.",
        )

    if not verify_stripe_signature(
        payload_bytes=raw,
        signature_header=signature,
        secret=secret,
    ):
        raise HTTPException(status_code=400, detail="Invalid Stripe signature")

    import json as _json

    try:
        event = _json.loads(raw)
    except _json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event_type = event.get("type", "")
    data_object = (event.get("data") or {}).get("object") or {}

    result = await _handle_event(db, event_type, data_object)
    return {"received": True, **result}


async def _handle_event(
    db: AsyncSession,
    event_type: str,
    obj: Dict[str, Any],
) -> Dict[str, Any]:
    """Route one verified event to its handler.

    Returns a small summary dict so the HTTP response is informative
    during incident debugging (Stripe dashboard shows the body).
    """
    if event_type in (
        "customer.subscription.created",
        "customer.subscription.updated",
    ):
        return await _apply_subscription_to_tenant(db, obj)

    if event_type == "customer.subscription.deleted":
        return await _cancel_subscription(db, obj)

    # Unhandled events: return 200 so Stripe doesn't retry forever.
    return {"handled": False, "event_type": event_type}


async def _tenant_by_customer(
    db: AsyncSession, customer_id: str
) -> Optional[Tenant]:
    if not customer_id:
        return None
    stmt = select(Tenant).where(Tenant.stripe_customer_id == customer_id).limit(1)
    return (await db.execute(stmt)).scalar_one_or_none()


def _active_price_id(subscription_obj: Dict[str, Any]) -> Optional[str]:
    """Pull the first active item's price id out of a subscription object.

    Stripe subscriptions can have multiple items (pro with add-ons, etc.),
    but for our single-tier model we just grab the first one.
    """
    items = (subscription_obj.get("items") or {}).get("data") or []
    for item in items:
        price = item.get("price") or {}
        price_id = price.get("id")
        if price_id:
            return str(price_id)
    return None


async def _apply_subscription_to_tenant(
    db: AsyncSession, subscription_obj: Dict[str, Any]
) -> Dict[str, Any]:
    customer_id = str(subscription_obj.get("customer") or "")
    tenant = await _tenant_by_customer(db, customer_id)
    if tenant is None:
        logger.warning(
            "Stripe subscription event for unknown customer %s — ignoring",
            customer_id,
        )
        return {"handled": False, "reason": "unknown_customer", "customer": customer_id}

    status = str(subscription_obj.get("status") or "")
    # Only "active" / "trialing" / "past_due" subscriptions count as
    # granting access. "canceled" / "unpaid" / "incomplete_expired" fall
    # through to the cancel path.
    if status in ("canceled", "unpaid", "incomplete_expired"):
        return await _cancel_subscription(db, subscription_obj, tenant=tenant)

    price_id = _active_price_id(subscription_obj)
    tier_key = price_id_to_tier(price_id) if price_id else None
    if tier_key is None:
        logger.warning(
            "Stripe subscription %s has no mappable price_id (%s); keeping existing tier",
            subscription_obj.get("id"),
            price_id,
        )
        return {
            "handled": False,
            "reason": "unknown_price",
            "price_id": price_id,
            "tenant_id": str(tenant.id),
        }

    tenant.stripe_subscription_id = str(subscription_obj.get("id") or "")
    apply_tier(tenant, tier_key)
    # Enforce the new caps — auto-suspend excess users with
    # suspension_reason="tier_downgrade". Admin must reconcile via the
    # seat-reconciliation UI before the banner clears.
    reconcile = await reconcile_seats(db, tenant)
    return {
        "handled": True,
        "tenant_id": str(tenant.id),
        "tier": tier_key,
        "price_id": price_id,
        "subscription_id": tenant.stripe_subscription_id,
        "suspended_user_count": len(reconcile.suspended_user_ids)
        + len(reconcile.suspended_admin_ids),
        "pending_seat_reconciliation": reconcile.pending,
    }


async def _cancel_subscription(
    db: AsyncSession,
    subscription_obj: Dict[str, Any],
    *,
    tenant: Optional[Tenant] = None,
) -> Dict[str, Any]:
    if tenant is None:
        tenant = await _tenant_by_customer(
            db, str(subscription_obj.get("customer") or "")
        )
    if tenant is None:
        return {"handled": False, "reason": "unknown_customer"}

    tenant.stripe_subscription_id = None
    apply_tier(tenant, "solo")
    reconcile = await reconcile_seats(db, tenant)
    return {
        "handled": True,
        "tenant_id": str(tenant.id),
        "tier": "solo",
        "reason": "canceled_or_expired",
        "suspended_user_count": len(reconcile.suspended_user_ids)
        + len(reconcile.suspended_admin_ids),
        "pending_seat_reconciliation": reconcile.pending,
    }
