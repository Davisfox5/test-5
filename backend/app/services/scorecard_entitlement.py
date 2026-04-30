"""Scorecard entitlement helpers.

Linda's pricing rule: each tenant gets one *included* scorecard per
admin seat, where 1 admin per 10 paid seats is the default ratio
(rounded up; a single-seat tenant still gets 1 admin = 1 scorecard).
Tenants who need more analyst surface area buy *Extra Scorecard*
add-on subscription items via Stripe (per-tier prices live in
``Settings.STRIPE_PRICE_CATALOG``).

Three pure helpers here, plus the async ``compute_entitlement``
coroutine the API calls before creating a scorecard:

* :func:`included_scorecards_for_seats` — math only, no I/O.
* :func:`count_paid_extra_scorecards` — sums quantities across the
  Stripe subscription's items whose price IDs match any
  ``extra_scorecard`` SKU in the price catalog (any tier counts).
* :func:`compute_entitlement` — counts seats + scorecards in the DB,
  fetches the live subscription from Stripe iff the tenant has a
  ``stripe_subscription_id``, and returns ``EntitlementInfo``.

Design notes
------------

* We deliberately do not cache the Stripe subscription. Add-on
  quantity changes need to take effect on the very next create
  attempt; a stale cache would show "cap reached" after the customer
  bought more capacity. The endpoint that calls this helper runs once
  per scorecard creation — fine to spend an http round-trip there.
* When ``STRIPE_API_KEY`` is unset (e.g. local dev), we treat
  ``paid_extra`` as zero. Same when the Stripe call fails — better to
  let admins create scorecards under the included cap than to take
  the whole feature down because Stripe is flaky.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import ScorecardTemplate, Tenant, User

logger = logging.getLogger(__name__)


@dataclass
class EntitlementInfo:
    """Snapshot of a tenant's scorecard entitlement at the moment of
    ``compute_entitlement``."""

    included: int
    paid_extra: int
    total: int
    used: int


# ── Pure helpers ─────────────────────────────────────────────────────


def included_scorecards_for_seats(seats: int) -> int:
    """One included scorecard per admin; one admin per 10 paid seats.

    ``seats`` is the count of *active* user seats on the tenant. We
    floor at 1 so a brand-new sandbox tenant still gets a single
    scorecard to play with.

    Examples::

        >>> included_scorecards_for_seats(0)
        1
        >>> included_scorecards_for_seats(10)
        1
        >>> included_scorecards_for_seats(11)
        2
        >>> included_scorecards_for_seats(50)
        5
    """
    if seats <= 0:
        return 1
    return max(1, math.ceil(seats / 10))


def count_paid_extra_scorecards(
    subscription_obj: Dict[str, Any],
    settings: Any,
) -> int:
    """Sum quantities across subscription items whose price ID is one
    of the ``extra_scorecard`` SKUs in the catalog.

    The catalog is per-tier (Starter/Growth/Enterprise each have their
    own monthly + annual price IDs), so we collect every extra-scorecard
    price ID and check membership rather than caring about which tier
    the customer is on.
    """
    extra_price_ids = _extra_scorecard_price_ids(settings)
    if not extra_price_ids:
        return 0

    items = ((subscription_obj or {}).get("items") or {}).get("data") or []
    total = 0
    for item in items:
        price = item.get("price") or {}
        price_id = price.get("id")
        if price_id and price_id in extra_price_ids:
            total += int(item.get("quantity") or 0)
    return total


def _extra_scorecard_price_ids(settings: Any) -> set[str]:
    """Collect every extra-scorecard price ID across all tiers.

    Empty set when the catalog is unset or malformed — caller treats
    that as "no paid extras configured", so tenants only get the
    included entitlement.
    """
    catalog = parse_price_catalog(getattr(settings, "STRIPE_PRICE_CATALOG", "") or "")
    out: set[str] = set()
    for tier in ("starter", "growth", "enterprise"):
        tier_block = catalog.get(tier) or {}
        sku = tier_block.get("extra_scorecard") or {}
        for cycle in ("monthly", "annual"):
            pid = sku.get(cycle)
            if isinstance(pid, str) and pid:
                out.add(pid)
    return out


def parse_price_catalog(raw: str) -> Dict[str, Any]:
    """Parse the ``STRIPE_PRICE_CATALOG`` JSON env var.

    Returns ``{}`` when missing or malformed. Logs a single warning on
    parse failure so a typo in fly secrets shows up at startup.
    """
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        logger.warning(
            "STRIPE_PRICE_CATALOG is set but is not valid JSON (%s); "
            "checkout + scorecard entitlement will treat it as empty.",
            exc,
        )
        return {}
    if not isinstance(parsed, dict):
        logger.warning(
            "STRIPE_PRICE_CATALOG must decode to a JSON object; got %s.",
            type(parsed).__name__,
        )
        return {}
    return parsed


# ── Async entitlement compute ────────────────────────────────────────


async def compute_entitlement(
    db: AsyncSession,
    tenant: Tenant,
    *,
    settings: Optional[Any] = None,
    http_client: Optional[httpx.AsyncClient] = None,
) -> EntitlementInfo:
    """Materialise a tenant's current scorecard entitlement.

    Reads:
    * Active user count → ``included``.
    * ``ScorecardTemplate`` count → ``used``.
    * Live Stripe subscription (only if the tenant has one and Stripe
      is configured) → ``paid_extra``.
    """
    if settings is None:
        from backend.app.config import get_settings

        settings = get_settings()

    seats = await _active_user_count(db, tenant.id)
    used = await _scorecard_count(db, tenant.id)
    included = included_scorecards_for_seats(seats)

    paid_extra = 0
    sub_id = (tenant.stripe_subscription_id or "").strip()
    api_key = (getattr(settings, "STRIPE_API_KEY", "") or "").strip()
    if sub_id and api_key:
        sub = await _fetch_subscription(api_key, sub_id, client=http_client)
        if sub is not None:
            paid_extra = count_paid_extra_scorecards(sub, settings)

    total = included + paid_extra
    return EntitlementInfo(
        included=included,
        paid_extra=paid_extra,
        total=total,
        used=used,
    )


async def _active_user_count(db: AsyncSession, tenant_id) -> int:
    stmt = (
        select(func.count())
        .select_from(User)
        .where(User.tenant_id == tenant_id, User.is_active.is_(True))
    )
    return int((await db.execute(stmt)).scalar_one())


async def _scorecard_count(db: AsyncSession, tenant_id) -> int:
    stmt = (
        select(func.count())
        .select_from(ScorecardTemplate)
        .where(ScorecardTemplate.tenant_id == tenant_id)
    )
    return int((await db.execute(stmt)).scalar_one())


async def _fetch_subscription(
    api_key: str,
    subscription_id: str,
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> Optional[Dict[str, Any]]:
    """GET /subscriptions/{id} from Stripe. Returns parsed JSON or None.

    We swallow network/HTTP errors and return None — a flaky Stripe
    must not block scorecard creation under the included cap. Logs at
    warning so the on-call sees it.
    """
    url = f"https://api.stripe.com/v1/subscriptions/{subscription_id}"
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=15.0) as c:
                resp = await c.get(url, headers=headers)
        else:
            resp = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:  # network failure / timeout
        logger.warning("Stripe subscription fetch failed: %s", exc)
        return None
    if resp.status_code >= 400:
        logger.warning(
            "Stripe subscription fetch returned %s: %s",
            resp.status_code,
            resp.text[:200],
        )
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def _iter_subscription_items(
    subscription_obj: Dict[str, Any],
) -> Iterable[Dict[str, Any]]:
    return ((subscription_obj or {}).get("items") or {}).get("data") or []
