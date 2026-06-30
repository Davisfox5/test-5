"""Comped-account allowlist — the account(s) that get full, free access
regardless of plan tier, trial state, or billing.

Kept as a tiny leaf module (no imports from ``auth`` or ``plans``) so both the
auth layer and the plan/entitlement layer can use it without a circular
import. The auth layer detects a comped account once per request and stamps a
transient flag on the resolved ``Tenant``; the (synchronous) plan helpers read
that flag — so no extra DB I/O happens inside the hot entitlement checks.

Keep ``COMPED_ACCOUNT_EMAILS`` tiny: every entry is a deliberate revenue and
entitlement bypass.
"""

from __future__ import annotations

from typing import Optional

# Owner's own application account(s), lowercased for case-insensitive match.
# As of 2026-06-30 this is the ONLY comped account.
COMPED_ACCOUNT_EMAILS = frozenset({"davison@flexonline.net"})

# The tier a comped tenant is treated as — top tier: all features, the
# strongest model, no seat/usage caps. Must be a key in ``plans.PLANS``.
COMP_TIER = "enterprise"

# Transient attribute the auth layer sets on the resolved Tenant for the
# duration of a request; the sync plan helpers read it via tenant_is_comped().
_COMP_FLAG = "_comped"


def email_is_comped(email: Optional[str]) -> bool:
    """True if ``email`` is on the comp allowlist (case-insensitive)."""
    return bool(email) and email.strip().lower() in COMPED_ACCOUNT_EMAILS


def mark_tenant_comped(tenant, value: bool) -> None:
    """Stamp the per-request comp flag on a resolved Tenant instance."""
    try:
        setattr(tenant, _COMP_FLAG, bool(value))
    except Exception:  # pragma: no cover — never let this break auth
        pass


def tenant_is_comped(tenant) -> bool:
    """Read the per-request comp flag the auth layer set. Defaults False
    (e.g. background/Celery contexts that never went through auth)."""
    return bool(getattr(tenant, _COMP_FLAG, False))
