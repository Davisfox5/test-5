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

* ``POST /admin/stripe/link`` — admin clicks "Manage billing" in the
  SPA. Returns a Stripe-hosted billing portal URL for the tenant's
  Stripe customer (creating one on the fly if needed). The SPA opens
  the URL in a new tab.

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
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import AuthPrincipal, require_role
from backend.app.config import get_settings
from backend.app.db import get_db
from backend.app.models import Tenant
from backend.app.services.scorecard_entitlement import parse_price_catalog
from backend.app.services.stripe_billing import (
    price_id_to_tier,
    verify_stripe_signature_with_rotation,
)
from backend.app.services.seat_reconciliation import reconcile_seats
from backend.app.plans import apply_tier

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Admin: open Stripe billing portal ────────────────────────────────


# SPA's billing page POSTs here with no body and expects ``{url}``.
# We also surface ``portal_url`` for any other callers (the API spec
# names it that). Both keys point at the same URL.
STRIPE_PORTAL_RETURN_URL = "https://linda-staging-app.fly.dev/billing"


async def _stripe_post(
    api_key: str,
    path: str,
    form: Dict[str, str],
) -> Dict[str, Any]:
    """POST form-encoded to the Stripe REST API. Returns parsed JSON.

    Stripe's API is form-urlencoded, not JSON; using httpx keeps the
    dependency surface small (no Stripe SDK required).
    """
    url = f"https://api.stripe.com/v1{path}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(url, headers=headers, data=form)
    if resp.status_code >= 400:
        # Bubble Stripe's own error message up so the admin UI shows
        # something actionable instead of a bare 500.
        try:
            err = resp.json().get("error", {})
            msg = err.get("message") or resp.text
        except Exception:
            msg = resp.text
        raise HTTPException(
            status_code=502,
            detail=f"Stripe error: {msg}",
        )
    return resp.json()


@router.post("/admin/stripe/link")
async def open_stripe_billing_portal(
    principal: AuthPrincipal = Depends(require_role("admin")),
) -> Dict[str, Any]:
    """Return a Stripe-hosted billing portal URL for the tenant.

    If the tenant doesn't have a ``stripe_customer_id`` yet, create one
    on the fly and persist it before opening the portal session.
    Falls back to a 503 if Stripe isn't configured for the deployment
    (staging may not have ``STRIPE_API_KEY`` set).
    """
    settings = get_settings()
    api_key = (settings.STRIPE_API_KEY or "").strip()
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="Stripe is not configured for this tenant",
        )

    tenant = principal.tenant
    customer_id = (tenant.stripe_customer_id or "").strip()

    # Mint a fresh customer for tenants that haven't been linked yet so
    # the very first "Manage billing" click works without admin
    # intervention.  We tag the customer with our tenant id so a Stripe
    # support eyeball can reverse-map customers back to tenants.
    if not customer_id:
        form = {
            "name": tenant.name or f"tenant-{tenant.id}",
            "metadata[tenant_id]": str(tenant.id),
            "metadata[tenant_slug]": tenant.slug or "",
        }
        principal_user = principal.user
        if principal_user is not None and principal_user.email:
            form["email"] = principal_user.email
        created = await _stripe_post(api_key, "/customers", form)
        customer_id = str(created.get("id") or "")
        if not customer_id.startswith("cus_"):
            raise HTTPException(
                status_code=502,
                detail="Stripe did not return a customer id",
            )
        tenant.stripe_customer_id = customer_id

    session = await _stripe_post(
        api_key,
        "/billing_portal/sessions",
        {
            "customer": customer_id,
            "return_url": STRIPE_PORTAL_RETURN_URL,
        },
    )
    portal_url = str(session.get("url") or "")
    if not portal_url:
        raise HTTPException(
            status_code=502,
            detail="Stripe did not return a portal session url",
        )

    # Return both keys so the SPA (reads ``url``) and any other client
    # following the API spec (``portal_url``) see the right value.
    return {
        "tenant_id": str(tenant.id),
        "portal_url": portal_url,
        "url": portal_url,
    }


# ── Admin: create a fresh Stripe Checkout session ────────────────────


# The portal endpoint above handles existing-customer plan modifications
# (upgrade/downgrade/cancel). The Checkout endpoint below handles the
# *first* subscribe — minting a customer if needed and stamping the
# one-time onboarding fee onto the first invoice via Stripe's
# ``add_invoice_items`` parameter so the customer pays once at signup.


class CheckoutRequest(BaseModel):
    """Body for ``POST /admin/stripe/checkout``.

    All quantities default to zero so the SPA can call the endpoint
    with just ``tier`` + ``cycle`` for a vanilla subscribe; we add
    the optional add-on lines only when their counts are positive.
    """

    tier: Literal["starter", "growth", "enterprise"]
    cycle: Literal["monthly", "annual"] = "monthly"
    is_partner: bool = False
    addl_seats: int = Field(0, ge=0)
    live_coaching_seats: int = Field(0, ge=0)
    extra_scorecards: int = Field(0, ge=0)
    success_url: str
    cancel_url: str


def _resolve_price(
    catalog: Dict[str, Any],
    *path: str,
) -> Optional[str]:
    """Walk the catalog dict by keys, returning the leaf string price ID.

    Returns None for any missing intermediate key or non-string leaf.
    """
    node: Any = catalog
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node if isinstance(node, str) and node else None


@router.post("/admin/stripe/checkout")
async def create_stripe_checkout_session(
    body: CheckoutRequest,
    principal: AuthPrincipal = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Create a Stripe Checkout Session for a first-time subscriber.

    Includes the recurring tier base price, optional add-on lines
    (additional seats, live coaching seats on Starter, extra
    scorecards), and the one-time onboarding fee as
    ``add_invoice_items`` so it appears on the first invoice.

    Returns 503 if Stripe / the price catalog isn't configured, 409 if
    the tenant already has an active subscription (they should use the
    portal instead).
    """
    settings = get_settings()
    api_key = (settings.STRIPE_API_KEY or "").strip()
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="Stripe is not configured for this tenant",
        )

    catalog = parse_price_catalog(getattr(settings, "STRIPE_PRICE_CATALOG", "") or "")
    if not catalog:
        raise HTTPException(
            status_code=503,
            detail="Stripe price catalog is not configured",
        )

    tenant = principal.tenant

    # Already-subscribed tenants must use the billing portal — Checkout
    # would create a *second* subscription which we don't support.
    if (tenant.stripe_subscription_id or "").strip():
        raise HTTPException(
            status_code=409,
            detail=(
                "Tenant already has an active subscription; "
                "use the billing portal to make changes."
            ),
        )

    # Resolve every price ID up front so we can fail fast with a
    # descriptive 503 rather than letting Stripe reject the create call
    # with a less helpful error.
    base_price = _resolve_price(catalog, body.tier, "base", body.cycle)
    onboarding_audience = "partner" if body.is_partner else "direct"
    onboarding_price = _resolve_price(
        catalog, body.tier, "onboarding", onboarding_audience
    )

    missing: List[str] = []
    if not base_price:
        missing.append(f"{body.tier}.base.{body.cycle}")
    if not onboarding_price:
        missing.append(f"{body.tier}.onboarding.{onboarding_audience}")

    addl_seat_price: Optional[str] = None
    if body.addl_seats > 0:
        addl_seat_price = _resolve_price(catalog, body.tier, "addl_seat", body.cycle)
        if not addl_seat_price:
            missing.append(f"{body.tier}.addl_seat.{body.cycle}")

    extra_scorecard_price: Optional[str] = None
    if body.extra_scorecards > 0:
        extra_scorecard_price = _resolve_price(
            catalog, body.tier, "extra_scorecard", body.cycle
        )
        if not extra_scorecard_price:
            missing.append(f"{body.tier}.extra_scorecard.{body.cycle}")

    # Live coaching is bundled into Growth + Enterprise — silently drop
    # the line for those tiers. Only Starter pays per-seat for it.
    coaching_price: Optional[str] = None
    include_coaching = body.live_coaching_seats > 0 and body.tier == "starter"
    if include_coaching:
        coaching_price = _resolve_price(
            catalog, "starter_addons", "live_coaching", body.cycle
        )
        if not coaching_price:
            missing.append(f"starter_addons.live_coaching.{body.cycle}")

    if missing:
        raise HTTPException(
            status_code=503,
            detail=(
                "Stripe price catalog is missing required entries: "
                + ", ".join(missing)
            ),
        )

    # Mint a customer if the tenant doesn't have one yet so the very
    # first signup click works without an admin pre-step. Mirror the
    # tagging strategy from ``open_stripe_billing_portal`` so the
    # ``customer.created`` webhook can reverse-link tenant → customer.
    customer_id = (tenant.stripe_customer_id or "").strip()
    if not customer_id:
        cust_form: Dict[str, str] = {
            "name": tenant.name or f"tenant-{tenant.id}",
            "metadata[tenant_id]": str(tenant.id),
            "metadata[tenant_slug]": tenant.slug or "",
        }
        principal_user = principal.user
        if principal_user is not None and principal_user.email:
            cust_form["email"] = principal_user.email
        created = await _stripe_post(api_key, "/customers", cust_form)
        customer_id = str(created.get("id") or "")
        if not customer_id.startswith("cus_"):
            raise HTTPException(
                status_code=502,
                detail="Stripe did not return a customer id",
            )
        tenant.stripe_customer_id = customer_id

    # Build form-encoded payload. Stripe Checkout takes line_items as
    # an indexed array (line_items[0][price]=…) — we lay them out in a
    # stable, human-readable order: base → addl seat → coaching →
    # extra scorecards → onboarding (one-time, on add_invoice_items).
    form: Dict[str, str] = {
        "mode": "subscription",
        "customer": customer_id,
        "success_url": body.success_url,
        "cancel_url": body.cancel_url,
        # Tag the resulting subscription so the webhook handler's
        # tenant lookup works even if customer.created arrived late.
        "subscription_data[metadata][tenant_id]": str(tenant.id),
        "subscription_data[metadata][tier]": body.tier,
        "subscription_data[metadata][cycle]": body.cycle,
        "subscription_data[metadata][audience]": onboarding_audience,
    }

    line_index = 0

    def _add_line(price_id: str, quantity: int) -> None:
        nonlocal line_index
        form[f"line_items[{line_index}][price]"] = price_id
        form[f"line_items[{line_index}][quantity]"] = str(quantity)
        line_index += 1

    _add_line(base_price, 1)  # type: ignore[arg-type]
    if body.addl_seats > 0 and addl_seat_price:
        _add_line(addl_seat_price, body.addl_seats)
    if include_coaching and coaching_price:
        _add_line(coaching_price, body.live_coaching_seats)
    if body.extra_scorecards > 0 and extra_scorecard_price:
        _add_line(extra_scorecard_price, body.extra_scorecards)

    # One-time onboarding fee. Stripe Checkout in ``mode=subscription``
    # does not accept ``add_invoice_items`` at the top level *or* nested
    # inside ``subscription_data`` (verified empirically — both raise
    # "Received unknown parameter"). Stripe *does* accept a one-time
    # Price alongside recurring Prices in the same ``line_items`` block;
    # the resulting Subscription bills the one-time line on its first
    # invoice and never again.
    _add_line(onboarding_price, 1)  # type: ignore[arg-type]

    session = await _stripe_post(api_key, "/checkout/sessions", form)
    session_url = str(session.get("url") or "")
    if not session_url:
        raise HTTPException(
            status_code=502,
            detail="Stripe did not return a checkout session url",
        )

    return {
        "tenant_id": str(tenant.id),
        "url": session_url,
        "expires_at": session.get("expires_at"),
        "session_id": session.get("id"),
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

    # Accept either the current or the staged-next secret so we can
    # rotate the signing secret with no traffic interruption (see
    # ``services/stripe_billing`` module docstring for the full procedure).
    secrets = [settings.STRIPE_WEBHOOK_SECRET, settings.STRIPE_WEBHOOK_SECRET_NEXT]
    if not any(secrets):
        # Prod must set at least one secret. Refuse to accept unverified
        # webhooks rather than silently trust them.
        raise HTTPException(
            status_code=503,
            detail="Stripe webhook secret is not configured on the server.",
        )

    if not verify_stripe_signature_with_rotation(
        payload_bytes=raw,
        signature_header=signature,
        secrets=secrets,
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

    if event_type == "customer.created":
        return await _handle_customer_created(db, obj)

    if event_type == "invoice.payment_failed":
        return await _handle_payment_failed(db, obj)

    if event_type == "invoice.payment_succeeded":
        # Reset the failure streak so a single payment hiccup doesn't
        # accumulate forever and silently downgrade a healthy tenant.
        return await _handle_payment_succeeded(db, obj)

    # Unhandled events: return 200 so Stripe doesn't retry forever.
    return {"handled": False, "event_type": event_type}


# ── New event handlers ───────────────────────────────────────────────


# Stash payment-failure state on tenant.features_enabled rather than
# a fresh column — keeps the migration surface small and the field is
# already JSONB. Underscore-prefixed keys are reserved for system use.
_PAYMENT_FAILURE_KEY = "_billing_payment_failure_count"
_PAYMENT_FAILURE_AT_KEY = "_billing_payment_failure_at"
_PAYMENT_FAILURE_DOWNGRADE_THRESHOLD = 3


async def _handle_customer_created(
    db: AsyncSession, obj: Dict[str, Any]
) -> Dict[str, Any]:
    """Persist the Stripe customer id on a tenant whose metadata names it.

    The Stripe-hosted checkout includes ``client_reference_id`` /
    ``metadata.tenant_id``; mirroring it back via the customer.created
    event lets later subscription events resolve to the tenant by
    ``stripe_customer_id`` without a manual link step.
    """
    metadata = obj.get("metadata") or {}
    tenant_id_raw = (
        metadata.get("tenant_id")
        or metadata.get("linda_tenant_id")
        or obj.get("client_reference_id")
    )
    if not tenant_id_raw:
        return {"handled": False, "reason": "no_tenant_metadata"}
    try:
        tenant_id = uuid.UUID(str(tenant_id_raw))
    except (TypeError, ValueError):
        return {"handled": False, "reason": "bad_tenant_metadata"}

    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        return {"handled": False, "reason": "unknown_tenant"}

    customer_id = str(obj.get("id") or "")
    if not customer_id:
        return {"handled": False, "reason": "no_customer_id"}

    # Only set if blank — never overwrite a tenant's existing link from
    # a stray customer.created (e.g. duplicate Stripe accounts).
    if not tenant.stripe_customer_id:
        tenant.stripe_customer_id = customer_id
        return {
            "handled": True,
            "tenant_id": str(tenant.id),
            "stripe_customer_id": customer_id,
            "linked": True,
        }
    return {
        "handled": True,
        "tenant_id": str(tenant.id),
        "linked": False,
        "reason": "already_linked",
    }


async def _handle_payment_failed(
    db: AsyncSession, obj: Dict[str, Any]
) -> Dict[str, Any]:
    """Increment the consecutive-failure counter and downgrade on streak.

    Stripe retries failed invoices on a smart-retry cadence; each retry
    re-fires ``invoice.payment_failed``. We count those into a small
    counter on ``features_enabled``. After
    ``_PAYMENT_FAILURE_DOWNGRADE_THRESHOLD`` consecutive failures we
    drop the tenant to ``sandbox`` (same path the cancellation handler
    takes), which gates revenue endpoints behind ``require_active_
    subscription`` until billing is healthy again.
    """
    customer_id = str(obj.get("customer") or "")
    tenant = await _tenant_by_customer(db, customer_id)
    if tenant is None:
        return {"handled": False, "reason": "unknown_customer", "customer": customer_id}

    features = dict(tenant.features_enabled or {})
    count = int(features.get(_PAYMENT_FAILURE_KEY, 0)) + 1
    features[_PAYMENT_FAILURE_KEY] = count
    features[_PAYMENT_FAILURE_AT_KEY] = datetime.now(timezone.utc).isoformat()
    tenant.features_enabled = features

    downgraded = False
    if count >= _PAYMENT_FAILURE_DOWNGRADE_THRESHOLD:
        # Already on sandbox? Keep counting, but no further state change.
        if tenant.plan_tier != "sandbox":
            apply_tier(tenant, "sandbox")
            await reconcile_seats(db, tenant)
            downgraded = True

    return {
        "handled": True,
        "tenant_id": str(tenant.id),
        "consecutive_failures": count,
        "downgraded": downgraded,
    }


async def _handle_payment_succeeded(
    db: AsyncSession, obj: Dict[str, Any]
) -> Dict[str, Any]:
    """Clear the failure streak on a successful invoice."""
    customer_id = str(obj.get("customer") or "")
    tenant = await _tenant_by_customer(db, customer_id)
    if tenant is None:
        return {"handled": False, "reason": "unknown_customer"}

    features = dict(tenant.features_enabled or {})
    if _PAYMENT_FAILURE_KEY in features or _PAYMENT_FAILURE_AT_KEY in features:
        features.pop(_PAYMENT_FAILURE_KEY, None)
        features.pop(_PAYMENT_FAILURE_AT_KEY, None)
        tenant.features_enabled = features
    return {"handled": True, "tenant_id": str(tenant.id), "cleared_failure_streak": True}


async def _tenant_by_customer(
    db: AsyncSession, customer_id: str
) -> Optional[Tenant]:
    if not customer_id:
        return None
    stmt = select(Tenant).where(Tenant.stripe_customer_id == customer_id).limit(1)
    return (await db.execute(stmt)).scalar_one_or_none()


def _active_price_id(subscription_obj: Dict[str, Any]) -> Optional[str]:
    """Find the subscription item whose price maps to a known tier.

    Subscriptions are now multi-item: a tier "base" line (Starter/Growth/
    Enterprise pack) plus optional per-seat overage and à-la-carte add-ons
    (Scorecards, Live Coaching). Iterate every item and return the first
    price_id that resolves through ``price_id_to_tier`` so add-ons and
    overage lines don't disturb tier resolution. Falls back to the first
    item if nothing maps — preserves the legacy single-item flow for any
    tenant on the old SKU layout.
    """
    items = (subscription_obj.get("items") or {}).get("data") or []
    for item in items:
        price = item.get("price") or {}
        price_id = price.get("id")
        if price_id and price_id_to_tier(str(price_id)):
            return str(price_id)
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
    apply_tier(tenant, "sandbox")
    reconcile = await reconcile_seats(db, tenant)
    return {
        "handled": True,
        "tenant_id": str(tenant.id),
        "tier": "sandbox",
        "reason": "canceled_or_expired",
        "suspended_user_count": len(reconcile.suspended_user_ids)
        + len(reconcile.suspended_admin_ids),
        "pending_seat_reconciliation": reconcile.pending,
    }
