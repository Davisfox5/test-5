"""Admin-only endpoints. Not exposed to end users.

Auth gate reuses the standard API key dependency — in production these routes
should be restricted to admin tokens via an extra scope check, but for now any
tenant with an API key can inspect / edit their own signals.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import (
    AuthPrincipal,
    get_current_principal,
    get_current_tenant,
    require_role,
)
from backend.app.config import get_settings
from backend.app.db import get_db
from backend.app.models import KBChunk, Tenant, TenantBriefSuggestion
from backend.app.services.kb import ContextBuilderService, format_brief_for_prompt
from backend.app.services.kb.context_builder import _validate_brief
from backend.app.services.kb.context_dispatch import schedule_context_rebuild
from backend.app.services.kb.infer_from_sources import (
    InferFromSources,
    apply_suggestion,
    reject_suggestion,
)
from backend.app.services.kb.vector_health import current_metrics, streak_days

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/admin/tenant-context")
async def get_tenant_context(
    tenant: Tenant = Depends(get_current_tenant),
) -> Dict[str, Any]:
    """Return LINDA's current per-tenant operating brief plus a rendered
    preview of how it lands in the system prompt."""
    brief = dict(tenant.tenant_context or {})
    return {
        "tenant_id": str(tenant.id),
        "brief": brief,
        "prompt_preview": format_brief_for_prompt(brief),
    }


class TenantContextFields(BaseModel):
    """Subset of the tenant brief that the tenant owns directly.

    These come from the onboarding interview or later explicit instruction.
    The ContextBuilder (KB agent) and TenantBriefRefiner (outcomes agent)
    both leave these sections alone when they run.
    """

    goals: Optional[List[str]] = None
    kpis: Optional[List[Dict[str, Any]]] = None
    strategies: Optional[List[str]] = None
    org_structure: Optional[Dict[str, Any]] = None
    personal_touches: Optional[Dict[str, Any]] = None


@router.put("/admin/tenant-context/fields")
async def set_tenant_context_fields(
    body: TenantContextFields,
    tenant: Tenant = Depends(get_current_tenant),
) -> Dict[str, Any]:
    """Set the onboarding-owned sections of LINDA's tenant brief.

    Merges provided fields into ``tenant.tenant_context`` — only keys present
    in the request body are updated; omitted keys are left as-is. Use this
    during onboarding (when the tenant answers the structured interview),
    or later to push explicit overrides ("actually, we no longer do handwritten
    notes, change that to a Slack shout-out").
    """
    brief = _validate_brief(tenant.tenant_context or {})
    updates = body.model_dump(exclude_none=True)
    brief.update(updates)
    tenant.tenant_context = brief
    return {
        "tenant_id": str(tenant.id),
        "updated_keys": list(updates.keys()),
        "brief": brief,
    }


@router.post("/admin/tenant-context/rebuild", status_code=202)
async def rebuild_tenant_context(
    mode: str = "full",
    sync: bool = False,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
) -> Dict[str, Any]:
    """Force a rebuild of the tenant-context brief.

    * ``mode=full`` (default) — stream every KB doc through the merger.
    * ``mode=debounce`` — just bump the debounce timer so an incremental
      merge runs shortly after the last KB write.
    * ``sync=true`` — run inline and return the new brief (blocks until done).
      Use for admin-driven rebuilds that want immediate feedback; leave false
      to offload to Celery.
    """
    if mode not in {"full", "debounce"}:
        mode = "full"

    if sync and mode == "full":
        builder = ContextBuilderService()
        brief = await builder.rebuild_all(db, tenant.id)
        return {"tenant_id": str(tenant.id), "mode": mode, "brief": brief}

    await schedule_context_rebuild(tenant.id, full=(mode == "full"))
    return {"tenant_id": str(tenant.id), "mode": mode, "scheduled": True}


@router.get("/admin/vector-health")
async def vector_health(
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
) -> Dict[str, Any]:
    """Vector store health snapshot for the developer.

    Returns the configured backend, chunk counts, query latency percentiles
    (rolling 24h), and the current alert streak so we can see at a glance
    whether pgvector is keeping up.
    """
    settings = get_settings()

    total_chunks = (await db.execute(select(func.count()).select_from(KBChunk))).scalar_one()
    tenant_chunks = (
        await db.execute(
            select(func.count()).select_from(KBChunk).where(KBChunk.tenant_id == tenant.id)
        )
    ).scalar_one()

    metrics = await current_metrics(total_chunks=int(total_chunks))
    streak = await streak_days()

    return {
        "backend": settings.VECTOR_BACKEND,
        "embed_model": settings.VOYAGE_EMBED_MODEL,
        "embed_dim": settings.VOYAGE_EMBED_DIM,
        "total_chunks": int(total_chunks),
        "tenant_chunks": int(tenant_chunks),
        "latency": {
            "p50_ms": metrics["p50_ms"],
            "p95_ms": metrics["p95_ms"],
            "p99_ms": metrics["p99_ms"],
            "samples_24h": int(metrics["samples_24h"]),
        },
        "thresholds": {
            "p95_ms": settings.VECTOR_HEALTH_P95_MS,
            "alert_days": settings.VECTOR_HEALTH_ALERT_DAYS,
            "size_milestones": settings.VECTOR_HEALTH_SIZE_MILESTONES,
        },
        "alert_streak_days": streak,
    }


# ── Infer-From-Sources: tenant brief suggestions ─────────────────────


class BriefSuggestionOut(BaseModel):
    id: str
    section: str
    path: Optional[str]
    proposed_value: Any
    rationale: str
    confidence: Optional[float]
    evidence_refs: list
    status: str
    created_at: str

    @classmethod
    def from_row(cls, row: TenantBriefSuggestion) -> "BriefSuggestionOut":
        return cls(
            id=str(row.id),
            section=row.section,
            path=row.path,
            proposed_value=row.proposed_value,
            rationale=row.rationale,
            confidence=row.confidence,
            evidence_refs=list(row.evidence_refs or []),
            status=row.status,
            created_at=row.created_at.isoformat() if row.created_at else "",
        )


@router.get("/admin/tenant-context/suggestions")
async def list_suggestions(
    status: str = "pending",
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
) -> Dict[str, Any]:
    """List pending (or approved/rejected) suggestions from the
    Infer-From-Sources agent for this tenant."""
    stmt = (
        select(TenantBriefSuggestion)
        .where(
            TenantBriefSuggestion.tenant_id == tenant.id,
            TenantBriefSuggestion.status == status,
        )
        .order_by(TenantBriefSuggestion.created_at.desc())
    )
    rows = (await db.execute(stmt)).scalars().all()
    return {
        "tenant_id": str(tenant.id),
        "status": status,
        "suggestions": [BriefSuggestionOut.from_row(r).model_dump() for r in rows],
    }


@router.post("/admin/tenant-context/suggestions/{suggestion_id}/approve")
async def approve_suggestion(
    suggestion_id: str,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(get_current_principal),
) -> Dict[str, Any]:
    """Apply a suggestion to the tenant brief and mark it approved."""
    import uuid as _uuid

    try:
        sid = _uuid.UUID(suggestion_id)
    except ValueError:
        return {"error": "invalid id"}
    row = await db.get(TenantBriefSuggestion, sid)
    if row is None or row.tenant_id != principal.tenant.id:
        return {"error": "not found"}
    if row.status != "pending":
        return {"error": f"already {row.status}"}
    brief = await apply_suggestion(db, row, reviewed_by_user_id=principal.user_id)
    return {"status": "approved", "brief": brief}


@router.post("/admin/tenant-context/suggestions/{suggestion_id}/reject")
async def reject_suggestion_endpoint(
    suggestion_id: str,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(get_current_principal),
) -> Dict[str, Any]:
    import uuid as _uuid

    try:
        sid = _uuid.UUID(suggestion_id)
    except ValueError:
        return {"error": "invalid id"}
    row = await db.get(TenantBriefSuggestion, sid)
    if row is None or row.tenant_id != principal.tenant.id:
        return {"error": "not found"}
    if row.status != "pending":
        return {"error": f"already {row.status}"}
    await reject_suggestion(db, row, reviewed_by_user_id=principal.user_id)
    return {"status": "rejected"}


@router.post("/admin/tenant-context/infer-now", status_code=202)
async def trigger_infer_now(
    sync: bool = False,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
) -> Dict[str, Any]:
    """Trigger the Infer-From-Sources agent immediately for this tenant.

    ``sync=true`` runs inline and returns the number of new suggestions;
    otherwise the work is enqueued as a Celery task.
    """
    if sync:
        agent = InferFromSources()
        rows = await agent.run(db, tenant.id)
        return {
            "tenant_id": str(tenant.id),
            "new_suggestions": len(rows),
            "ids": [str(r.id) for r in rows],
        }

    try:
        from backend.app.tasks import infer_from_sources_weekly

        infer_from_sources_weekly.delay(str(tenant.id))
    except Exception:
        logger.exception("Failed to enqueue infer-from-sources task")
    return {"tenant_id": str(tenant.id), "scheduled": True}


# ── Tenant settings (admin UI) ────────────────────────────────────────

# Allowlist of flags the UI can flip on `features_enabled`. Each entry is
# (key, default, human label, help text). Keep in sync with the frontend
# rendering in website/js/linda-insights.js / preferences-settings.js.
_FEATURE_FLAG_SPEC: List[Dict[str, Any]] = [
    {
        "key": "live_sentiment",
        "default": False,
        "label": "Live sentiment updates",
        "help": "Stream numeric sentiment to the agent during calls. Paid tier.",
    },
    {
        "key": "live_kb_retrieval",
        "default": True,
        "label": "Live KB retrieval",
        "help": "Surface answer cards from the KB when callers ask questions.",
    },
    {
        "key": "keyterm_prompting",
        "default": False,
        "label": "Deepgram keyterm prompting",
        "help": "Boost transcription accuracy for tenant-configured phrases. $0.0013/min add-on.",
    },
    {
        "key": "infer_from_sources_autorun",
        "default": True,
        "label": "Weekly Infer-From-Sources runs",
        "help": "Run the passive agent every week to propose tenant-brief updates.",
    },
    {
        "key": "crm_sync_autorun",
        "default": False,
        "label": "Daily CRM sync",
        "help": "Pull customers + contacts from connected CRMs overnight.",
    },
    {
        "key": "paralinguistic_analysis",
        "default": True,
        "label": "Post-call voice analysis",
        "help": (
            "Extract pitch, pace, pauses, and voice-stress markers from each "
            "call. Adds acoustic signals to sentiment + churn scoring."
        ),
    },
    {
        "key": "paralinguistic_live",
        "default": False,
        "label": "Live voice coaching",
        "help": (
            "Show real-time monotone, pace, and stress alerts during calls. "
            "Costs ~20% of one CPU per concurrent call — opt in only for "
            "high-touch teams."
        ),
    },
]


class TenantSettingsOut(BaseModel):
    tenant_id: str
    transcription_engine: str
    automation_level: str
    pii_redaction_enabled: bool
    translation_enabled: bool
    default_language: str
    keyterm_boost_list: List[str]
    question_keyterms: List[str]
    features_enabled: Dict[str, Any]
    feature_flag_spec: List[Dict[str, Any]]


class TenantSettingsPatch(BaseModel):
    transcription_engine: Optional[str] = None
    automation_level: Optional[str] = None
    pii_redaction_enabled: Optional[bool] = None
    translation_enabled: Optional[bool] = None
    default_language: Optional[str] = None
    keyterm_boost_list: Optional[List[str]] = None
    question_keyterms: Optional[List[str]] = None
    # Partial merge — only keys present here update features_enabled.
    features_enabled: Optional[Dict[str, Any]] = None


def _tenant_settings_payload(tenant: Tenant) -> Dict[str, Any]:
    from backend.app.plans import list_tiers, normalize_tier_key

    features = dict(tenant.features_enabled or {})
    # Fill defaults for known flags so the UI always has something to render.
    for spec in _FEATURE_FLAG_SPEC:
        features.setdefault(spec["key"], spec["default"])
    return {
        "tenant_id": str(tenant.id),
        "transcription_engine": tenant.transcription_engine or "deepgram",
        "automation_level": tenant.automation_level or "approval",
        "pii_redaction_enabled": bool(tenant.pii_redaction_enabled),
        "translation_enabled": bool(tenant.translation_enabled),
        "default_language": tenant.default_language or "en",
        "keyterm_boost_list": list(tenant.keyterm_boost_list or []),
        "question_keyterms": list(tenant.question_keyterms or []),
        "features_enabled": features,
        "feature_flag_spec": _FEATURE_FLAG_SPEC,
        # Plan surface. Seats are controlled by the tier; the UI
        # shows the tier picker + the current limits as read-only.
        "plan_tier": normalize_tier_key(getattr(tenant, "plan_tier", None)),
        "seat_limit": tenant.seat_limit,
        "admin_seat_limit": tenant.admin_seat_limit,
        "tier_catalog": list_tiers(),
    }


@router.get("/admin/tenant-settings", response_model=TenantSettingsOut)
async def get_tenant_settings(
    tenant: Tenant = Depends(get_current_tenant),
) -> Dict[str, Any]:
    """Return the tenant-level configuration surfaced in the admin UI.

    Includes a ``feature_flag_spec`` list so the frontend can render toggle
    rows without hard-coding labels / defaults in two places.
    """
    return _tenant_settings_payload(tenant)


@router.patch("/admin/tenant-settings", response_model=TenantSettingsOut)
async def patch_tenant_settings(
    body: TenantSettingsPatch,
    tenant: Tenant = Depends(get_current_tenant),
) -> Dict[str, Any]:
    """Merge updates into the tenant record. Allowlisted fields only.

    ``features_enabled`` is merged key-by-key so unspecified flags keep
    their current value. All other scalar fields replace when present.
    """
    updates = body.model_dump(exclude_none=True)

    if "transcription_engine" in updates:
        val = str(updates["transcription_engine"])
        if val not in ("deepgram", "whisper"):
            raise HTTPException(status_code=400, detail="invalid transcription_engine")
        tenant.transcription_engine = val
    if "automation_level" in updates:
        val = str(updates["automation_level"])
        if val not in ("approval", "auto", "shadow"):
            raise HTTPException(status_code=400, detail="invalid automation_level")
        tenant.automation_level = val
    if "pii_redaction_enabled" in updates:
        tenant.pii_redaction_enabled = bool(updates["pii_redaction_enabled"])
    if "translation_enabled" in updates:
        tenant.translation_enabled = bool(updates["translation_enabled"])
    if "default_language" in updates:
        tenant.default_language = str(updates["default_language"])[:8]
    if "keyterm_boost_list" in updates:
        tenant.keyterm_boost_list = [
            str(s).strip() for s in (updates["keyterm_boost_list"] or []) if str(s).strip()
        ][:100]
    if "question_keyterms" in updates:
        tenant.question_keyterms = [
            str(s).strip() for s in (updates["question_keyterms"] or []) if str(s).strip()
        ][:50]
    if "features_enabled" in updates:
        merged = dict(tenant.features_enabled or {})
        allowed = {spec["key"] for spec in _FEATURE_FLAG_SPEC}
        for k, v in (updates["features_enabled"] or {}).items():
            if k not in allowed:
                # Ignore unknown flags rather than 400 — keeps the API
                # forwards-compatible with newer UIs calling older servers.
                continue
            merged[k] = bool(v) if isinstance(v, bool) else v
        tenant.features_enabled = merged

    return _tenant_settings_payload(tenant)


# ── Subscription tier ─────────────────────────────────────────────────


class TierChangeIn(BaseModel):
    tier: str


@router.post("/admin/tenant-settings/tier", response_model=TenantSettingsOut)
async def change_plan_tier(
    body: TierChangeIn,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(get_current_principal),
) -> Dict[str, Any]:
    """Change the tenant's plan tier.

    Applies the tier's seat limits + feature flag defaults, then runs
    seat reconciliation: if the new cap is below the current active
    count, the newest excess users are auto-suspended with
    ``suspension_reason="tier_downgrade"`` and the tenant is flagged
    ``pending_seat_reconciliation``. The acting admin is *protected*
    from suspension so they never kick themselves out mid-downgrade.

    Admins then pick who stays active via ``/admin/seat-reconciliation``
    and ``POST /users/{id}/reactivate``. Suspended users can't log in.
    """
    from backend.app.plans import PLANS, apply_tier, normalize_tier_key
    from backend.app.services.seat_reconciliation import reconcile_seats

    tenant = principal.tenant
    normalized = normalize_tier_key(body.tier)
    # Accept legacy keys (solo/team/pro) transparently but reject outright
    # unknown inputs so typos don't silently downgrade to the default.
    if body.tier not in PLANS and normalized != body.tier and body.tier not in (
        "solo",
        "team",
        "pro",
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown tier: {body.tier}. Supported: "
                + ", ".join(PLANS.keys())
            ),
        )
    apply_tier(tenant, normalized)
    await reconcile_seats(db, tenant, protect_user_id=principal.user_id)
    return _tenant_settings_payload(tenant)
