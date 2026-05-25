"""External / CRM context fan-out for the Action Plan synthesizer.

The existing :mod:`backend.app.services.kb.customer_brief_builder` builds
a long-cycle dossier from internal data (interactions, lifecycle events,
agent notes). For each plan synthesis we want something fresher and
more transactional: the customer's current deal stage, owner, recent
activities, open cases. That comes from the connected CRMs.

Per the locked freshness decision: use ``CrmDealRecord`` cache if it
was last_synced_at within the last 15 minutes; otherwise fetch live
from the adapter. On live-fetch failure (5xx, rate-limit, auth fail)
fall through to whatever cache exists (even if older than 15min) and
flag the snapshot as stale so the plan header can show it.

The result block is what gets interpolated into Call A/B's
``customer_brief_block`` placeholder.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import CrmDealRecord, Customer, Integration

logger = logging.getLogger(__name__)


# Locked: <15min cache, else fresh fetch. Beyond 15min we still
# attempt a live fetch; if that fails, we fall back to the stale
# cache and mark stale_at.
CRM_CACHE_FRESHNESS_SECONDS = 15 * 60


@dataclass
class CrmCustomerSnapshot:
    """Per-provider snapshot of a customer's CRM state."""

    provider: str  # 'hubspot' | 'salesforce' | 'pipedrive'
    deals: List[Dict[str, Any]] = field(default_factory=list)
    last_synced_at: Optional[datetime] = None
    # True when the data is older than CRM_CACHE_FRESHNESS_SECONDS and
    # the live-refresh attempt failed; the UI surfaces this.
    is_stale: bool = False
    error_reason: Optional[str] = None


@dataclass
class ExternalContextResult:
    """All external context surfaces gathered for one plan synthesis."""

    snapshots: List[CrmCustomerSnapshot] = field(default_factory=list)
    # Connected provider names (always populated, even when no snapshots
    # were retrievable). Drives the tenant_capabilities block too.
    connected_providers: List[str] = field(default_factory=list)

    def to_brief_block(self) -> str:
        """Render as plain text for prompt injection."""
        if not self.snapshots:
            return "(no CRM data available for this customer)"
        lines: List[str] = []
        for snap in self.snapshots:
            head = f"{snap.provider}"
            if snap.is_stale:
                head += (
                    f" (stale cache, last synced "
                    f"{_iso_or_unknown(snap.last_synced_at)}; live fetch "
                    f"failed: {snap.error_reason or 'unknown'})"
                )
            elif snap.last_synced_at:
                head += f" (synced {_iso_or_unknown(snap.last_synced_at)})"
            lines.append(f"- {head}:")
            if not snap.deals:
                lines.append("  - no open deals on record")
            for d in snap.deals[:5]:  # cap to keep prompt tight
                summary = _format_deal(d)
                lines.append(f"  - {summary}")
        return "\n".join(lines)


def _iso_or_unknown(dt: Optional[datetime]) -> str:
    return dt.isoformat() if dt else "unknown"


def _format_deal(d: Dict[str, Any]) -> str:
    parts = [str(d.get("title") or "(untitled deal)")]
    if d.get("stage"):
        parts.append(f"stage={d['stage']}")
    if d.get("amount") is not None:
        currency = d.get("currency") or ""
        parts.append(f"amount={currency}{d['amount']}")
    if d.get("close_date"):
        parts.append(f"close={d['close_date']}")
    if d.get("owner_name"):
        parts.append(f"owner={d['owner_name']}")
    return " | ".join(parts)


async def fetch_external_context(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    customer_id: Optional[uuid.UUID],
) -> ExternalContextResult:
    """Build the per-customer CRM snapshot for plan synthesis.

    ``customer_id`` may be None for calls where the resolver couldn't
    identify a known customer; in that case we return a result with
    just the connected_providers populated.
    """
    connected_providers = await _connected_provider_set(db, tenant_id)
    result = ExternalContextResult(connected_providers=sorted(connected_providers))

    if customer_id is None:
        return result

    # We currently surface deal records (the most decision-relevant CRM
    # surface for plan composition). Activities + cases follow when the
    # CrmAdapter protocol grows the corresponding read methods; the
    # synthesizer prompts already account for the shape via
    # customer_brief_block.
    cached_deals = await _load_cached_deals(db, tenant_id, customer_id)
    by_provider: Dict[str, List[CrmDealRecord]] = {}
    for d in cached_deals:
        by_provider.setdefault(d.provider, []).append(d)

    # Per the freshness decision: live-fetch when *any* provider's most
    # recent sync is older than the threshold. We don't fan out to
    # providers we never had cached data for either (no live-fetch
    # purely to find out we have nothing — adapters are paginated and
    # expensive).
    for provider in connected_providers:
        rows = by_provider.get(provider, [])
        latest_sync = _latest_sync(rows)
        snap = CrmCustomerSnapshot(
            provider=provider,
            deals=[_deal_record_to_dict(r) for r in rows],
            last_synced_at=latest_sync,
        )
        if _needs_refresh(latest_sync):
            # The live-fetch path is intentionally a soft attempt -
            # we don't block plan synthesis on a flaky CRM. The
            # sync_service / writeback layer already maintains the
            # cache; here we just nudge it and accept whatever lands.
            await _attempt_live_refresh(
                db=db,
                tenant_id=tenant_id,
                customer_id=customer_id,
                provider=provider,
                snap=snap,
            )
        result.snapshots.append(snap)
    return result


async def _connected_provider_set(
    db: AsyncSession, tenant_id: uuid.UUID,
) -> set:
    rows = await db.execute(
        select(Integration.provider).where(Integration.tenant_id == tenant_id)
    )
    return {row[0] for row in rows.all() if row[0]}


async def _load_cached_deals(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    customer_id: uuid.UUID,
) -> List[CrmDealRecord]:
    rows = await db.execute(
        select(CrmDealRecord).where(
            CrmDealRecord.tenant_id == tenant_id,
            CrmDealRecord.customer_id == customer_id,
        )
    )
    return list(rows.scalars())


def _latest_sync(rows: List[CrmDealRecord]) -> Optional[datetime]:
    timestamps = [r.last_synced_at for r in rows if r.last_synced_at]
    return max(timestamps) if timestamps else None


def _needs_refresh(latest_sync: Optional[datetime]) -> bool:
    if latest_sync is None:
        return True
    now = datetime.now(timezone.utc)
    # Tolerate naive timestamps (some legacy rows are naive UTC).
    if latest_sync.tzinfo is None:
        latest_sync = latest_sync.replace(tzinfo=timezone.utc)
    return (now - latest_sync) > timedelta(seconds=CRM_CACHE_FRESHNESS_SECONDS)


def _deal_record_to_dict(r: CrmDealRecord) -> Dict[str, Any]:
    return {
        "external_id": r.external_id,
        "title": r.title,
        "stage": r.stage,
        "status": r.status,
        "amount": r.amount,
        "currency": r.currency,
        "probability": r.probability,
        "close_date": r.close_date,
        "owner_name": r.owner_name,
    }


async def _attempt_live_refresh(
    *,
    db: AsyncSession,
    tenant_id: uuid.UUID,
    customer_id: uuid.UUID,
    provider: str,
    snap: CrmCustomerSnapshot,
) -> None:
    """Try a live CRM fetch; on failure, mark the snapshot stale.

    We deliberately do NOT raise - failures during synthesis must not
    block plan creation. The locked decision: "use stale cache if
    present, else proceed without CRM data". If we successfully refresh,
    the persisted CrmDealRecord rows are updated by the adapter's sync
    path; if not, we keep what we have and flag stale_at.
    """
    try:
        # Lazy import to avoid pulling provider SDKs at module load.
        from backend.app.services.crm import sync_service

        await sync_service.refresh_customer_deals(
            db=db,
            tenant_id=tenant_id,
            customer_id=customer_id,
            provider=provider,
        )
        # Re-load post-refresh.
        refreshed = await db.execute(
            select(CrmDealRecord).where(
                CrmDealRecord.tenant_id == tenant_id,
                CrmDealRecord.customer_id == customer_id,
                CrmDealRecord.provider == provider,
            )
        )
        rows = list(refreshed.scalars())
        snap.deals = [_deal_record_to_dict(r) for r in rows]
        snap.last_synced_at = _latest_sync(rows)
        snap.is_stale = False
    except ImportError:
        # Hard-stop: sync_service should always be importable. If it
        # isn't, something is fundamentally wrong with the deployment;
        # surface stale + reason so the agent at least sees the data
        # they have plus a clear signal that something's broken.
        snap.is_stale = snap.last_synced_at is not None and _needs_refresh(
            snap.last_synced_at
        )
        snap.error_reason = "sync_service import failed"
        logger.error(
            "sync_service import failed during plan synthesis for "
            "%s/%s — deployment issue, not a CRM outage",
            provider, customer_id,
        )
    except Exception as exc:  # noqa: BLE001 - we deliberately tolerate any error
        snap.is_stale = True
        snap.error_reason = str(exc)[:200]
        logger.warning(
            "Live CRM refresh failed for %s/%s: %s; falling back to cache",
            provider, customer_id, exc,
        )


# ──────────────────────────────────────────────────────────
# Tenant capabilities — drives the system_write step gating + the
# {tenant_capabilities_block} interpolation in Call A.
# ──────────────────────────────────────────────────────────


# Static map of provider -> {read: [scopes], write: [ops]}. The Action
# Plan synthesizer surfaces these to the LLM so it knows which
# system_write operations it can recommend. Conservative on purpose -
# we only list operations the corresponding adapter actually supports;
# adding a new op requires both the adapter method and an entry here.
PROVIDER_CAPABILITIES: Dict[str, Dict[str, List[str]]] = {
    "hubspot": {
        "read": ["deals", "contacts", "activities"],
        "write": ["create_task", "log_activity", "create_note", "update_deal_stage"],
    },
    "salesforce": {
        "read": ["opportunities", "accounts", "contacts"],
        "write": ["create_task", "log_activity", "create_note", "update_deal_stage"],
    },
    "pipedrive": {
        "read": ["deals", "contacts", "activities"],
        "write": ["create_activity", "create_note", "update_deal_stage"],
    },
    "gmail": {
        "read": ["threads"],
        "write": ["send_email"],
    },
    "outlook": {
        "read": ["threads"],
        "write": ["send_email"],
    },
    "google_calendar": {
        "read": ["events"],
        "write": ["create_event", "update_event"],
    },
}


def build_capabilities_block(connected_providers: List[str]) -> str:
    """Render the {tenant_capabilities_block} prompt fragment.

    Only lists providers the tenant has actually connected; the
    synthesizer reads this as the closed set of system_write targets
    it may emit.
    """
    if not connected_providers:
        return (
            "(No CRM, email, or calendar integrations are currently "
            "connected. Do NOT emit any system_write steps; emit "
            "'log manually in <system>' as a note step when a procedure "
            "would otherwise require integration writes.)"
        )
    lines: List[str] = []
    for provider in connected_providers:
        caps = PROVIDER_CAPABILITIES.get(provider)
        if not caps:
            lines.append(f"- {provider} (connected; capabilities unknown)")
            continue
        read = ", ".join(caps.get("read", [])) or "(none)"
        write = ", ".join(caps.get("write", [])) or "(none)"
        lines.append(
            f"- {provider} (read: {read} | write: {write})"
        )
    return "\n".join(lines)


__all__ = [
    "CrmCustomerSnapshot",
    "ExternalContextResult",
    "fetch_external_context",
    "build_capabilities_block",
    "PROVIDER_CAPABILITIES",
    "CRM_CACHE_FRESHNESS_SECONDS",
]
