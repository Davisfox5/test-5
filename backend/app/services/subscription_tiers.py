"""Subscription tier catalog.

Tier definitions live in code (not the DB) so they're version-controlled,
reviewable, and easy to adjust without migrations. A tenant picks exactly
one tier; that tier decides:

* ``seat_limit`` — total active users the tenant may have.
* ``admin_seat_limit`` — active admins.
* ``features`` — overrides merged into ``Tenant.features_enabled`` when
  the tier is applied. We only touch keys defined here, so a tenant's
  manual flag overrides survive a tier change as long as they don't
  collide with a tier-owned flag.

Changing a tenant's tier is a **non-retroactive** operation: we never
deactivate users even if the new tier's seat_limit is below the current
active count. The UI surfaces that mismatch so the admin can decide who
to off-board.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass(frozen=True)
class TierSpec:
    key: str
    label: str
    seat_limit: int
    admin_seat_limit: int
    # Feature flags this tier sets. Merged into Tenant.features_enabled
    # on apply. Flags not listed here are not touched.
    features: Dict[str, Any] = field(default_factory=dict)
    # Human description for the UI.
    description: str = ""


# Order matters — ordered from cheapest to most expensive for UI rendering.
SUBSCRIPTION_TIERS: Dict[str, TierSpec] = {
    "solo": TierSpec(
        key="solo",
        label="Solo",
        seat_limit=1,
        admin_seat_limit=1,
        features={
            "live_sentiment": False,
            "live_kb_retrieval": True,
            "keyterm_prompting": False,
            "infer_from_sources_autorun": True,
            "crm_sync_autorun": False,
        },
        description="One admin seat. KB retrieval + post-call analysis.",
    ),
    "team": TierSpec(
        key="team",
        label="Team",
        seat_limit=10,
        admin_seat_limit=1,
        features={
            "live_sentiment": False,
            "live_kb_retrieval": True,
            "keyterm_prompting": False,
            "infer_from_sources_autorun": True,
            "crm_sync_autorun": True,
        },
        description="Up to 10 seats (1 admin). Adds daily CRM sync.",
    ),
    "pro": TierSpec(
        key="pro",
        label="Pro",
        seat_limit=50,
        admin_seat_limit=3,
        features={
            "live_sentiment": True,
            "live_kb_retrieval": True,
            "keyterm_prompting": True,
            "infer_from_sources_autorun": True,
            "crm_sync_autorun": True,
        },
        description=(
            "Up to 50 seats (3 admins). Adds live sentiment + Deepgram "
            "keyterm prompting."
        ),
    ),
    "enterprise": TierSpec(
        key="enterprise",
        label="Enterprise",
        seat_limit=500,
        admin_seat_limit=20,
        features={
            "live_sentiment": True,
            "live_kb_retrieval": True,
            "keyterm_prompting": True,
            "infer_from_sources_autorun": True,
            "crm_sync_autorun": True,
        },
        description="500 seats, 20 admins. Full feature set.",
    ),
}


_DEFAULT_TIER = "solo"


def default_tier() -> TierSpec:
    return SUBSCRIPTION_TIERS[_DEFAULT_TIER]


def get_tier(key: str) -> TierSpec:
    """Return the tier spec for ``key``. Falls back to the default tier
    with a clear label so the caller can surface 'unknown tier, reverted
    to solo' without crashing."""
    return SUBSCRIPTION_TIERS.get(key) or SUBSCRIPTION_TIERS[_DEFAULT_TIER]


def list_tiers() -> List[Dict[str, Any]]:
    """Return the ordered tier catalog for the admin UI picker."""
    return [
        {
            "key": spec.key,
            "label": spec.label,
            "seat_limit": spec.seat_limit,
            "admin_seat_limit": spec.admin_seat_limit,
            "features": dict(spec.features),
            "description": spec.description,
        }
        for spec in SUBSCRIPTION_TIERS.values()
    ]


def apply_tier(tenant, tier_key: str) -> TierSpec:
    """Apply a tier to a tenant in place. Returns the resolved spec.

    Side effects on ``tenant`` (caller must flush/commit):

    - ``subscription_tier`` = resolved key (i.e. falls back to solo for
      unknown keys).
    - ``seat_limit`` / ``admin_seat_limit`` replaced with the tier's values.
    - ``features_enabled`` gets the tier's feature flags merged in
      (non-tier keys preserved).

    Intentionally does NOT touch existing users — a downgrade below the
    current headcount is the admin's problem to resolve via deactivations.
    """
    spec = get_tier(tier_key)
    tenant.subscription_tier = spec.key
    tenant.seat_limit = spec.seat_limit
    tenant.admin_seat_limit = spec.admin_seat_limit
    merged = dict(tenant.features_enabled or {})
    for k, v in spec.features.items():
        merged[k] = v
    tenant.features_enabled = merged
    return spec
