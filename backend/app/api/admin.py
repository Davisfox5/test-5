"""Admin-only endpoints. Not exposed to end users.

Auth gate reuses the standard API key dependency — in production these routes
should be restricted to admin tokens via an extra scope check, but for now any
tenant with an API key can inspect / edit their own signals.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
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
from backend.app.models import AuditLog, KBChunk, Tenant, TenantBriefSuggestion
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
    # The tenant's own organisation name — captured at onboarding so
    # entity_resolution knows which side of any call is "us" and stays
    # off it as a customer candidate. Editable later from /settings.
    # Free-form String; we don't enforce it has to match the Tenant.name
    # since brand vs legal entity often differ ("Beacon Software" vs
    # "Beacon Technologies, Inc.").
    own_org_name: Optional[str] = None


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
    {
        "key": "emotion_classification",
        "default": False,
        "label": "Emotion classification (beta)",
        "help": (
            "Run SpeechBrain's IEMOCAP wav2vec2 model on each voice "
            "interaction to label emotion (neutral/happy/sad/angry). "
            "Model is ~1 GB and downloads on first use; pre-warm on "
            "worker boot with prefetch_emotion_classifier() so the "
            "first call isn't slow."
        ),
    },
    {
        "key": "crm_writeback_notes",
        "default": False,
        "label": "CRM note write-back",
        "help": (
            "After each call, write a summary note back to the linked "
            "deal/contact in Pipedrive. Runs only when a matching open "
            "deal is found for the contact."
        ),
    },
    {
        "key": "crm_writeback_activities",
        "default": False,
        "label": "CRM activity write-back",
        "help": (
            "After each call, create a Pipedrive activity for every "
            "open action item the call produced. Due date, subject, "
            "and note are carried over."
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


# Sentinel — distinguishes "field omitted" from "field set to null". Using
# Pydantic's exclude_unset semantics on the patch model works for scalar
# fields generally, but we want callers to be able to PATCH a retention
# override back to null (= "fall back to platform default") explicitly,
# which exclude_none would swallow.
_UNSET: Any = object()


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
    # Retention overrides. Sending ``null`` clears the override (so the
    # tenant falls back to the platform default during the nightly
    # event_retention sweep); omitting the field leaves it untouched.
    audio_retention_hours_override: Optional[int] = None
    feedback_retention_days_override: Optional[int] = None


def _tenant_settings_payload(tenant: Tenant) -> Dict[str, Any]:
    from backend.app.plans import list_tiers, normalize_tier_key
    from backend.app.services.event_retention import (
        FEEDBACK_EVENT_RAW_RETENTION_DAYS,
    )

    features = dict(tenant.features_enabled or {})
    # Fill defaults for known flags so the UI always has something to render.
    for spec in _FEATURE_FLAG_SPEC:
        features.setdefault(spec["key"], spec["default"])
    # Audio retention is stored as a non-nullable column with a
    # platform-default of 168h (7 days). The "override" surface is purely
    # UX — anything other than 168 is treated as a custom value by the UI.
    audio_default_hours = 168
    audio_hours = (
        getattr(tenant, "audio_retention_hours", None) or audio_default_hours
    )
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
        # Retention surface — the UI renders two number inputs and shows
        # the platform defaults as placeholders.
        "audio_retention_hours": audio_hours,
        "audio_retention_hours_default": audio_default_hours,
        # ``retention_days_feedback_events`` is nullable; null means the
        # tenant inherits the platform default. The UI shows null as
        # "no override".
        "feedback_retention_days_override": getattr(
            tenant, "retention_days_feedback_events", None
        ),
        "feedback_retention_days_default": FEEDBACK_EVENT_RAW_RETENTION_DAYS,
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

    Retention overrides are special-cased: callers can PATCH them to
    ``null`` to clear the override (fall back to the platform default).
    Distinguishing "omitted" from "set to null" requires looking at
    ``model_fields_set`` rather than ``exclude_none``.
    """
    updates = body.model_dump(exclude_none=True)
    fields_set = body.model_fields_set

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
                # Forwards-compatible: keep accepting the request rather than
                # 400-ing newer UIs against older servers, but log a warning
                # so silent drops show up in operator logs instead of "the
                # toggle didn't stick" tickets.
                logger.warning(
                    "patch_tenant_settings: dropping unknown features_enabled key %r "
                    "(tenant=%s)",
                    k,
                    tenant.id,
                )
                continue
            merged[k] = bool(v) if isinstance(v, bool) else v
        tenant.features_enabled = merged

    # Retention overrides — explicitly check fields_set so PATCH'ing the
    # field to ``null`` clears the override (vs. omitting which leaves it).
    if "audio_retention_hours_override" in fields_set:
        raw = body.audio_retention_hours_override
        if raw is None:
            # Clear override — fall back to the platform default of 168h.
            tenant.audio_retention_hours = 168
        else:
            if not isinstance(raw, int) or raw < 1 or raw > 24 * 365:
                raise HTTPException(
                    status_code=400,
                    detail="audio_retention_hours_override must be 1..8760",
                )
            tenant.audio_retention_hours = int(raw)
    if "feedback_retention_days_override" in fields_set:
        raw = body.feedback_retention_days_override
        if raw is None:
            tenant.retention_days_feedback_events = None
        else:
            if not isinstance(raw, int) or raw < 1 or raw > 3650:
                raise HTTPException(
                    status_code=400,
                    detail="feedback_retention_days_override must be 1..3650",
                )
            tenant.retention_days_feedback_events = int(raw)

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


class InternalOverrideIn(BaseModel):
    enabled: bool


@router.post("/admin/diag/repair-test")
async def diag_repair_test(
    body: Dict[str, str],
    principal: AuthPrincipal = Depends(get_current_principal),
) -> Dict[str, Any]:
    """Run the analysis-service JSON repair path against a payload.

    POST body: ``{"interaction_id": "<uuid>"}`` — uses that row's
    ``insights.summary`` (which the analysis service writes the raw
    LLM text into on parse-failure) and runs json-repair on it.
    Returns ``{recovered_keys: [...], topics_count: N, ...}`` or the
    raw exception so we can tell if repair is firing at all.
    """
    from sqlalchemy import select
    from backend.app.models import Interaction
    import uuid as _uuid, json as _json, re as _re

    iid = body.get("interaction_id")
    if not iid:
        raise HTTPException(400, "interaction_id required")
    stmt = select(Interaction).where(
        Interaction.id == _uuid.UUID(iid),
        Interaction.tenant_id == principal.tenant.id,
    )
    db_session = principal.db_session if hasattr(principal, "db_session") else None
    # No db session injected on principal; use the FastAPI dep instead.
    from backend.app.db import async_session
    async with async_session() as db:
        row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Not found")
    raw = ((row.insights or {}).get("summary") or "")
    cleaned = _re.sub(r"^```(?:json)?\s*", "", raw.strip())
    cleaned = _re.sub(r"\s*```$", "", cleaned)
    out: Dict[str, Any] = {
        "raw_len": len(raw),
        "cleaned_len": len(cleaned),
    }
    try:
        _json.loads(cleaned)
        out["json_loads"] = "OK (no repair needed)"
    except _json.JSONDecodeError as e:
        out["json_loads_error"] = str(e)
    try:
        from json_repair import repair_json
        repaired = repair_json(cleaned, return_objects=True)
        out["repair_type"] = type(repaired).__name__
        if isinstance(repaired, dict):
            out["repair_keys"] = sorted(repaired.keys())
            out["topics_count"] = len(repaired.get("topics") or [])
            out["sentiment_overall"] = repaired.get("sentiment_overall")
    except Exception as e:
        out["repair_error"] = f"{type(e).__name__}: {e}"
    return out


@router.get("/admin/diag/deps")
async def diag_deps(
    principal: AuthPrincipal = Depends(get_current_principal),
) -> Dict[str, Any]:
    """Confirm key runtime deps are importable in this image.

    Cheap to call; only verifies that ``import`` works (no I/O).
    Useful for checking whether a freshly-pinned package actually
    landed in the deployed Docker image after a CI build.
    """
    out: Dict[str, Any] = {}
    for name in ("json_repair", "deepgram", "anthropic", "boto3", "spacy"):
        try:
            mod = __import__(name)
            out[name] = getattr(mod, "__version__", "unknown")
        except Exception as exc:
            out[name] = f"ImportError: {exc}"
    return out


@router.get("/admin/diag/interaction/{interaction_id}")
async def diag_interaction(
    interaction_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(get_current_principal),
) -> Dict[str, Any]:
    """Dump every column on an interaction row — bypasses the
    InteractionOut serializer so we can see internal fields like
    ``audio_s3_key`` / ``audio_url`` that aren't normally exposed.
    Tenant-scoped so this is safe to leave on; it just answers
    "what's actually in the DB row?" for a given id.
    """
    from sqlalchemy import select
    from backend.app.models import Interaction

    stmt = select(Interaction).where(
        Interaction.id == interaction_id,
        Interaction.tenant_id == principal.tenant.id,
    )
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    out: Dict[str, Any] = {}
    for col in row.__table__.columns:
        v = getattr(row, col.name, None)
        if isinstance(v, (uuid.UUID,)):
            v = str(v)
        elif hasattr(v, "isoformat"):
            v = v.isoformat()
        elif isinstance(v, (list, dict)):
            v = v if len(str(v)) < 500 else f"<{type(v).__name__} len={len(v)}>"
        out[col.name] = v
    return out


class AnalysisTierOverrideIn(BaseModel):
    # ``None`` clears the override and lets triage pick per-call.
    tier: Optional[str] = None


@router.post(
    "/admin/tenant-settings/analysis-tier-override",
    response_model=TenantSettingsOut,
)
async def set_analysis_tier_override(
    body: AnalysisTierOverrideIn,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(get_current_principal),
) -> Dict[str, Any]:
    """Pin every analysis call to a specific model tier.

    Writes ``parameter_overrides.analysis.force_tier`` on the tenant's
    ``TenantPromptConfig``. The orchestrator already honors that key
    (``tasks.py`` line 898) — this is just the admin-facing way to set
    it without dropping into psql. ``tier=None`` clears the pin.
    """
    from backend.app.models import TenantPromptConfig
    from sqlalchemy import select

    if body.tier is not None and body.tier not in ("haiku", "sonnet", "opus"):
        raise HTTPException(
            status_code=400,
            detail="tier must be one of: haiku, sonnet, opus, or null to clear",
        )
    tenant = principal.tenant
    stmt = select(TenantPromptConfig).where(
        TenantPromptConfig.tenant_id == tenant.id
    )
    config = (await db.execute(stmt)).scalar_one_or_none()
    if config is None:
        config = TenantPromptConfig(tenant_id=tenant.id, parameter_overrides={})
        db.add(config)
        await db.flush()

    overrides = dict(config.parameter_overrides or {})
    analysis = dict(overrides.get("analysis") or {})
    if body.tier is None:
        analysis.pop("force_tier", None)
    else:
        analysis["force_tier"] = body.tier
    overrides["analysis"] = analysis

    # Persist via an explicit UPDATE — JSONB attribute reassignment on
    # an existing row was not reliably triggering SQLAlchemy's dirty
    # tracking in production, so the override silently failed to land.
    # Same lesson as the /interactions/upload race fix.
    from sqlalchemy import update as _sql_update
    await db.execute(
        _sql_update(TenantPromptConfig)
        .where(TenantPromptConfig.tenant_id == tenant.id)
        .values(parameter_overrides=overrides)
    )
    await db.commit()
    return _tenant_settings_payload(tenant)


@router.post(
    "/admin/tenant-settings/internal-override",
    response_model=TenantSettingsOut,
)
async def set_internal_override(
    body: InternalOverrideIn,
    principal: AuthPrincipal = Depends(get_current_principal),
) -> Dict[str, Any]:
    """Mark a tenant as internal / staging-test — bypasses the subscription gate.

    Sets a sentinel ``stripe_subscription_id='internal_test'`` so that
    ``require_active_subscription`` (which only null-checks the field)
    treats the tenant as paid without an actual Stripe link. Used for
    internal QA and end-to-end staging tests where we want unlimited
    access on an enterprise tier without wiring real billing.

    Reversible: ``{"enabled": false}`` clears the sentinel so the gate
    re-asserts.
    """
    tenant = principal.tenant
    tenant.stripe_subscription_id = "internal_test" if body.enabled else None
    return _tenant_settings_payload(tenant)


@router.post(
    "/admin/tenant-settings/reset-features",
    response_model=TenantSettingsOut,
)
async def reset_features_to_tier(
    principal: AuthPrincipal = Depends(get_current_principal),
) -> Dict[str, Any]:
    """Reset ``features_enabled`` to the current tier's defaults.

    ``apply_tier`` only *merges* the tier's flags so manual overrides
    survive an upgrade by design. This endpoint surfaces a way to flip
    that switch — admins click it when they want to forget every prior
    toggle and start clean from the tier catalog.
    """
    from backend.app.plans import get_tier

    tenant = principal.tenant
    spec = get_tier(getattr(tenant, "plan_tier", None))
    tenant.features_enabled = dict(spec.features)
    return _tenant_settings_payload(tenant)


# ── Demo data seeder ──────────────────────────────────────────────────


class SeedDemoDataOut(BaseModel):
    tenant_id: str
    created: Dict[str, int]


@router.post("/admin/seed-demo-data", response_model=SeedDemoDataOut)
async def seed_demo_data_endpoint(
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(get_current_principal),
) -> SeedDemoDataOut:
    """Populate the calling tenant with sample dashboards / data.

    Idempotent: re-running on a tenant that already has interactions
    skips the interactions section but tops up missing scorecards / KB
    docs / webhooks. Returns the per-resource created counts.
    """
    from backend.app.services.demo_seeder import seed_demo_data

    counts = await seed_demo_data(
        db, tenant=principal.tenant, admin_user=principal.user
    )
    return SeedDemoDataOut(tenant_id=str(principal.tenant.id), created=counts)


# ── Audit log ────────────────────────────────────────────


class AuditLogOut(BaseModel):
    """One row from ``audit_log`` for the admin UI.

    Sensitive fields (request_id, IP, user-agent) live under ``meta`` and
    are admin-only — non-admins never see this endpoint at all because
    every ``/admin/*`` path is gated by ``require_role("admin")`` at the
    router level.
    """

    id: str
    tenant_id: str
    actor_user_id: Optional[str]
    actor_principal: str
    action: str
    resource_type: str
    resource_id: Optional[str]
    before: Optional[Dict[str, Any]]
    after: Optional[Dict[str, Any]]
    meta: Dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


class AuditLogPage(BaseModel):
    items: List[AuditLogOut]
    total: int
    limit: int
    offset: int


@router.get("/admin/audit-logs", response_model=AuditLogPage)
async def list_audit_logs(
    action: Optional[str] = None,
    resource_type: Optional[str] = None,
    actor: Optional[str] = None,
    from_: Optional[datetime] = Query(None, alias="from"),
    to: Optional[datetime] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(get_current_principal),
) -> AuditLogPage:
    """Tenant-scoped audit log feed for the admin UI.

    Filters:

    * ``action`` — exact match against the dot-namespaced verb
      (``"interaction.deleted"``).
    * ``resource_type`` — exact match (``"webhook"``, ``"user"`` …).
    * ``actor`` — UUID of the user that performed the action; pass
      ``"api_key"`` to filter to API-key calls (rows where
      ``actor_principal = 'api_key'``) and ``"system"`` for cron writes.
    * ``from`` / ``to`` — inclusive bounds on ``created_at``.

    Pagination is offset/limit. Newest first.
    """
    from datetime import datetime as _dt  # local alias avoids shadowing
    from sqlalchemy import func as _func

    base_filters = [AuditLog.tenant_id == principal.tenant.id]
    if action:
        base_filters.append(AuditLog.action == action)
    if resource_type:
        base_filters.append(AuditLog.resource_type == resource_type)
    if actor:
        if actor in {"api_key", "user", "system"}:
            base_filters.append(AuditLog.actor_principal == actor)
        else:
            try:
                actor_uuid = uuid.UUID(actor)
            except (ValueError, TypeError):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "actor must be a user UUID or one of: "
                        "'user', 'api_key', 'system'"
                    ),
                )
            base_filters.append(AuditLog.actor_user_id == actor_uuid)
    if from_ is not None:
        base_filters.append(AuditLog.created_at >= from_)
    if to is not None:
        base_filters.append(AuditLog.created_at <= to)

    count_stmt = select(_func.count()).select_from(AuditLog).where(*base_filters)
    total = int((await db.execute(count_stmt)).scalar_one())

    stmt = (
        select(AuditLog)
        .where(*base_filters)
        .order_by(AuditLog.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = list((await db.execute(stmt)).scalars().all())

    items = [
        AuditLogOut(
            id=str(r.id),
            tenant_id=str(r.tenant_id),
            actor_user_id=str(r.actor_user_id) if r.actor_user_id else None,
            actor_principal=r.actor_principal,
            action=r.action,
            resource_type=r.resource_type,
            resource_id=r.resource_id,
            before=r.before,
            after=r.after,
            meta=r.meta or {},
            created_at=r.created_at,
        )
        for r in rows
    ]
    return AuditLogPage(items=items, total=total, limit=limit, offset=offset)


# ── Celery worker introspection ──────────────────────────────────────────
# Admin-only ops endpoint that round-trips through the Celery control
# plane (broadcast over Redis) to ask every connected worker for its
# stats / active / reserved task lists. If no worker responds within the
# timeout, the result keys are empty — definitive signal that the worker
# fleet is offline even though the API is healthy. Also peeks at the
# Redis broker queue length so a "no workers + tasks piling up" state
# is obvious without leaving the JSON.


@router.get("/admin/celery/inspect")
async def celery_inspect(
    _principal: AuthPrincipal = Depends(require_role("admin")),
) -> Dict[str, Any]:
    from backend.app.tasks import celery_app

    insp = celery_app.control.inspect(timeout=2.5)
    stats = insp.stats()
    active = insp.active()
    reserved = insp.reserved()

    queue_depth: Optional[int] = None
    queue_error: Optional[str] = None
    try:
        import redis as _redis

        r = _redis.from_url(get_settings().REDIS_URL, decode_responses=True)
        try:
            queue_depth = int(r.llen("celery"))
        finally:
            try:
                r.close()
            except Exception:
                pass
    except Exception as exc:  # noqa: BLE001 — debug-only
        queue_error = str(exc)[:200]

    return {
        "workers_online": list(stats.keys()) if stats else [],
        "stats": stats,
        "active": active,
        "reserved": reserved,
        "default_queue_depth": queue_depth,
        "default_queue_error": queue_error,
    }


# ── Phase 4 backfill: re-run warnings/commitments on existing analyzed rows ──
#
# Re-drive isn't valid for ``analyzed`` interactions (it would re-pay the
# Sonnet analysis cost). This endpoint runs only the cheap Phase 4 step
# (warnings_commitments.detect_and_persist) against the rows we already
# analyzed pre-#76, so the customer detail pages light up with warnings
# + commitments without re-ingesting transcripts.
#
# One-shot, idempotent: warnings dedupe on (customer_id, kind) so re-runs
# upsert; commitments insert per-call (a second run on the same call
# would create duplicate commitment rows, so we skip interactions whose
# ``insights.warnings_commitments_debug`` already has a non-zero
# ``commitments_created``).


class BackfillResultRow(BaseModel):
    interaction_id: uuid.UUID
    skipped: bool
    reason: Optional[str] = None
    warnings_upserted: int = 0
    commitments_created: int = 0
    # Phase 2: when ``paralinguistic_reanalysis=true``, this records
    # whether we actually re-enqueued the pipeline for that
    # interaction. Skip rows leave it False and explain via ``reason``.
    paralinguistic_applied: bool = False


class BackfillResponse(BaseModel):
    processed: int
    rows: List[BackfillResultRow]


@router.post(
    "/admin/backfill-warnings-commitments",
    response_model=BackfillResponse,
)
async def backfill_warnings_commitments(
    interaction_ids: Optional[List[uuid.UUID]] = None,
    limit: int = Query(50, le=200),
    paralinguistic_reanalysis: bool = Query(
        False,
        description=(
            "Phase 2: also re-enqueue the analysis pipeline for each row "
            "so the new paralinguistic block lands on already-analyzed "
            "interactions. Skips rows where audio is no longer "
            "accessible."
        ),
    ),
    _principal: AuthPrincipal = Depends(require_role("admin")),
    tenant: Tenant = Depends(get_current_tenant),
) -> BackfillResponse:
    """Run Phase 4 detection on already-analyzed interactions.

    Without this, a tenant analyzed before PR #76 would only get
    warnings + commitments on interactions ingested after the deploy.

    Phase 2 (``paralinguistic_reanalysis=true``): additionally
    re-enqueue the full analysis pipeline for each interaction so the
    new paralinguistic prompt block + LSM rapport gauge land on
    historical calls. Skips rows where ``audio_s3_key`` and
    ``audio_url`` are both null (audio retention has expired).
    """
    from backend.app.models import Interaction
    from backend.app.services.warnings_commitments import detect_and_persist
    from backend.app.tasks import _get_sync_session

    session = _get_sync_session()
    try:
        q = (
            session.query(Interaction)
            .filter(
                Interaction.tenant_id == tenant.id,
                Interaction.status == "analyzed",
            )
            .order_by(Interaction.created_at.desc())
        )
        if interaction_ids:
            q = q.filter(Interaction.id.in_(interaction_ids))
        rows = q.limit(limit).all()

        out: List[BackfillResultRow] = []
        for ix in rows:
            insights = ix.insights or {}
            prior = (insights.get("warnings_commitments_debug") or {})
            already_backfilled = int(prior.get("commitments_created") or 0) > 0
            if already_backfilled and not paralinguistic_reanalysis:
                out.append(
                    BackfillResultRow(
                        interaction_id=ix.id,
                        skipped=True,
                        reason="already backfilled",
                    )
                )
                continue
            if ix.customer_id is None and not paralinguistic_reanalysis:
                out.append(
                    BackfillResultRow(
                        interaction_id=ix.id,
                        skipped=True,
                        reason="no customer linked",
                    )
                )
                continue
            transcript = (
                "\n".join(
                    seg.get("text", "")
                    for seg in (ix.transcript or [])
                    if isinstance(seg, dict)
                )
                if isinstance(ix.transcript, list)
                else (ix.transcript or "")
            )
            row = BackfillResultRow(interaction_id=ix.id, skipped=False)
            try:
                if not already_backfilled and ix.customer_id is not None:
                    outcome = await detect_and_persist(
                        session=session,
                        interaction=ix,
                        tenant=tenant,
                        insights=insights,
                        compressed_transcript=transcript[:18_000],
                    )
                    row.warnings_upserted = outcome.warnings_upserted
                    row.commitments_created = outcome.commitments_created
                    session.commit()
                if paralinguistic_reanalysis:
                    if not (ix.audio_s3_key or ix.audio_url):
                        # No audio path on this interaction → can't
                        # extract paralinguistics. Mark skipped on
                        # this dimension while keeping any warnings
                        # / commitments work that already ran.
                        row.skipped = True
                        row.reason = "audio_unavailable"
                    else:
                        # Re-enqueue the pipeline. Step 7.5 is now
                        # part of the standard run, so this lands the
                        # new paralinguistic block + bumps
                        # ``analysis_prompt_version`` automatically.
                        from backend.app.tasks import process_voice_interaction
                        process_voice_interaction.delay(str(ix.id))
                        row.paralinguistic_applied = True
                out.append(row)
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                logger.exception(
                    "backfill failed for %s", ix.id
                )
                out.append(
                    BackfillResultRow(
                        interaction_id=ix.id,
                        skipped=True,
                        reason=f"error: {type(exc).__name__}: {str(exc)[:120]}",
                    )
                )
        return BackfillResponse(processed=len(out), rows=out)
    finally:
        session.close()


# ── Re-segment legacy single-segment transcripts ────────────────────
#
# Pre-fix-transcript-speaker-tags, the text-ingest path wrapped
# ``raw_text`` in a single segment with start=0/end=0/speaker_id=None,
# so every analyzed call rendered as one paragraph attributed to
# "Speaker 1" at 0:00. This endpoint re-parses ``raw_text`` through
# the new ``text_segmenter`` and writes a proper ``transcript`` array
# without re-running analysis (no Sonnet cost).


class TranscriptResegmentRow(BaseModel):
    interaction_id: uuid.UUID
    skipped: bool
    reason: Optional[str] = None
    segment_count: int = 0
    speaker_count: int = 0


class TranscriptResegmentResponse(BaseModel):
    processed: int
    rows: List[TranscriptResegmentRow]


@router.post(
    "/admin/resegment-transcripts",
    response_model=TranscriptResegmentResponse,
)
async def resegment_transcripts(
    interaction_ids: Optional[List[uuid.UUID]] = None,
    limit: int = Query(50, le=200),
    _principal: AuthPrincipal = Depends(require_role("admin")),
    tenant: Tenant = Depends(get_current_tenant),
) -> TranscriptResegmentResponse:
    """Re-segment legacy single-segment transcripts using ``raw_text``.

    Skips rows whose stored transcript already has more than one
    segment (so re-running is safe).
    """
    from backend.app.models import Interaction
    from backend.app.services.text_segmenter import segments_from_text
    from backend.app.tasks import _get_sync_session

    session = _get_sync_session()
    try:
        q = session.query(Interaction).filter(
            Interaction.tenant_id == tenant.id,
            Interaction.status == "analyzed",
        )
        if interaction_ids:
            q = q.filter(Interaction.id.in_(interaction_ids))
        rows = q.order_by(Interaction.created_at.desc()).limit(limit).all()

        out: List[TranscriptResegmentRow] = []
        for ix in rows:
            if not ix.raw_text:
                out.append(
                    TranscriptResegmentRow(
                        interaction_id=ix.id,
                        skipped=True,
                        reason="no raw_text — would lose data to re-segment",
                    )
                )
                continue
            existing = ix.transcript or []
            if isinstance(existing, list) and len(existing) > 1:
                out.append(
                    TranscriptResegmentRow(
                        interaction_id=ix.id,
                        skipped=True,
                        reason="already multi-segment",
                        segment_count=len(existing),
                    )
                )
                continue
            segments = segments_from_text(
                ix.raw_text,
                duration_seconds=ix.duration_seconds,
            )
            if not segments:
                out.append(
                    TranscriptResegmentRow(
                        interaction_id=ix.id,
                        skipped=True,
                        reason="segmenter returned empty",
                    )
                )
                continue
            speakers = {
                s.get("speaker_label") for s in segments if isinstance(s, dict)
            }
            speakers.discard(None)
            ix.transcript = segments
            session.commit()
            out.append(
                TranscriptResegmentRow(
                    interaction_id=ix.id,
                    skipped=False,
                    segment_count=len(segments),
                    speaker_count=len(speakers),
                )
            )
        return TranscriptResegmentResponse(processed=len(out), rows=out)
    finally:
        session.close()


# ── Phase 4 classifier training ─────────────────────────────────────


class Phase4TrainRequest(BaseModel):
    target: str = "churn"  # "churn" | "upsell"
    label_horizon_days: int = 90  # 30 | 90 | 180 | 365


class Phase4TrainResponse(BaseModel):
    tenant_id: uuid.UUID
    target: str
    status: str  # "ok" | "learning" | "insufficient_data"
    n_total: int
    n_events: int
    model_version: Optional[str] = None
    log_loss: Optional[float] = None
    metrics: dict = {}


class Phase4StatusRow(BaseModel):
    target: str
    has_active_model: bool
    model_version: Optional[str] = None
    n_train: Optional[int] = None
    n_events: Optional[int] = None
    learning_mode: Optional[bool] = None
    metrics: dict = {}


class Phase4StatusResponse(BaseModel):
    tenant_id: uuid.UUID
    rows: List[Phase4StatusRow]


@router.post(
    "/admin/classifier/train",
    response_model=Phase4TrainResponse,
)
async def classifier_train(
    body: Phase4TrainRequest,
    _principal: AuthPrincipal = Depends(require_role("admin")),
    tenant: Tenant = Depends(get_current_tenant),
) -> Phase4TrainResponse:
    """Train (or retrain) the Phase 4 binary classifier for this tenant.

    Inline / synchronous: the LR fit is fast (pure Python, ~1k rows in
    well under a second). Status comes back as ``"ok"`` once
    ``RELIABLE_TRAIN_EVENTS`` is crossed; ``"learning"`` between
    ``MIN_TRAIN_EVENTS`` and that threshold; ``"insufficient_data"``
    when there isn't enough labeled outcome data yet — in which case
    the rubric / bucket numerics stay the source of truth on every
    interaction page.
    """
    from backend.app.services.phase4_classifier import (
        SUPPORTED_LABEL_HORIZONS,
        train_for_tenant,
    )
    from backend.app.tasks import _get_sync_session

    if body.target not in {"churn", "upsell"}:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported target: {body.target!r}",
        )
    if body.label_horizon_days not in SUPPORTED_LABEL_HORIZONS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported label_horizon_days: {body.label_horizon_days!r} "
                f"(allowed: {SUPPORTED_LABEL_HORIZONS})"
            ),
        )

    session = _get_sync_session()
    try:
        result = train_for_tenant(
            session,
            tenant.id,
            target=body.target,
            label_horizon_days=body.label_horizon_days,
        )
        return Phase4TrainResponse(
            tenant_id=tenant.id,
            target=result.target,
            status=result.status,
            n_total=result.n_total,
            n_events=result.n_events,
            model_version=result.model_version,
            log_loss=result.log_loss,
            metrics=result.metrics,
        )
    finally:
        session.close()


@router.get(
    "/admin/classifier/status",
    response_model=Phase4StatusResponse,
)
async def classifier_status(
    _principal: AuthPrincipal = Depends(require_role("admin")),
    tenant: Tenant = Depends(get_current_tenant),
) -> Phase4StatusResponse:
    """Active model + calibration metrics per target.

    Returns one row per supported target. ``has_active_model=False``
    means cold-start (rubric is the source of truth). Use the
    ``metrics`` dict to surface a calibration-curve UI.
    """
    from backend.app.models import ScorerVersion
    from backend.app.tasks import _get_sync_session

    session = _get_sync_session()
    try:
        rows: List[Phase4StatusRow] = []
        for target in ("churn", "upsell"):
            scorer_name = f"{target}_lr_phase4"
            sv = (
                session.query(ScorerVersion)
                .filter(
                    ScorerVersion.tenant_id == tenant.id,
                    ScorerVersion.scorer_name == scorer_name,
                    ScorerVersion.is_active.is_(True),
                )
                .order_by(ScorerVersion.created_at.desc())
                .first()
            )
            if sv is None:
                rows.append(
                    Phase4StatusRow(
                        target=target,
                        has_active_model=False,
                    )
                )
                continue
            params = sv.parameters or {}
            calibration = sv.calibration or {}
            rows.append(
                Phase4StatusRow(
                    target=target,
                    has_active_model=True,
                    model_version=sv.version,
                    n_train=int(params.get("n_train", 0)) or None,
                    n_events=int(params.get("n_events", 0)) or None,
                    learning_mode=bool(params.get("learning_mode", False)),
                    metrics=(calibration.get("metrics_inline") or {}),
                )
            )
        return Phase4StatusResponse(tenant_id=tenant.id, rows=rows)
    finally:
        session.close()
