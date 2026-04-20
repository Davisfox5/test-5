"""Orchestrator — Opus-powered maintenance of the four profile trees.

Contract (see ``docs/SCORING_ARCHITECTURE.md``):

1. Real-time: when an interaction reaches ``analyzed``, Sonnet writes one
   :class:`DeltaReport` scoped to every entity the call touched
   (client, agent, manager, business).
2. Daily (Celery Beat): Opus consumes unconsumed delta reports per
   tenant and produces a new *version* of every affected profile, with
   updated ``top_factors`` and ``recommendations``.
3. Weekly: Opus reads the last four daily versions against observed
   proxy outcomes (renewals, cancellations, action-item closures) and
   re-scores calibration weights.

All profiles are append-only.  The latest version per entity is the
working truth; prior versions are retained for audit and rollback.

To keep Opus cost bounded, it never sees raw transcripts at the
orchestrator layer.  It receives structured deltas + the prior profile
payload only.  The tokens in play are on the order of tens-of-thousands
per tenant-day, not per-call, because we consolidate.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import and_, desc, select, update
from sqlalchemy.orm import Session

from backend.app.services.model_router import (
    CacheableBlock,
    LLMRequest,
    ModelRouter,
    TaskType,
    get_router,
    tenant_context_block,
)

logger = logging.getLogger(__name__)


# ── Entity scopes ────────────────────────────────────────────────────────


ENTITY_CLIENT = "client"
ENTITY_AGENT = "agent"
ENTITY_MANAGER = "manager"
ENTITY_BUSINESS = "business"


@dataclass
class EntityScope:
    entity_type: str
    entity_id: str


# ── Sonnet delta-report generation (real-time) ───────────────────────────


_DELTA_REPORT_SYSTEM = (
    "You are the real-time observer for a conversation intelligence "
    "platform. Given one interaction's deterministic features and "
    "structured LLM analysis, produce a compact delta report that the "
    "daily orchestrator will consolidate into per-entity profiles.\n\n"
    "Return ONLY valid JSON with the following keys:\n"
    "  client_delta, agent_delta, manager_delta, business_delta.\n"
    "Each section is optional; omit a section entirely if nothing "
    "notable changed.  Keep every field grounded in evidence from the "
    "features you were given — do not invent.\n\n"
    "client_delta: {sentiment_shift: float, new_objections: [str], "
    "resolved_objections: [str], champion_health_change: float, "
    "buying_signals_detected: [str], churn_risk_factors_added: [str]}\n"
    "agent_delta: {strengths_observed: [str], gaps_observed: [str], "
    "skill_exercised: [str], metric_snapshots: {<metric>: <value>}}\n"
    "manager_delta: {escalations_to_flag: [str], "
    "coaching_priority_hint: str}\n"
    "business_delta: {trend_evidence: {topic: str, direction: "
    "'up'|'down'|'flat'}, content_gaps: [str], competitive_threat: str}"
)


class DeltaReportWriter:
    """Produce delta reports after each interaction analysis."""

    def __init__(self, router: Optional[ModelRouter] = None) -> None:
        self._router = router or get_router()

    async def write(
        self,
        *,
        tenant: Any,
        interaction: Any,
        features: Dict[str, Any],
        scopes: Sequence[EntityScope],
    ) -> Dict[str, Any]:
        """Generate the structured delta.  Returns the parsed JSON."""
        payload = {
            "interaction_id": str(interaction.id),
            "channel": interaction.channel,
            "duration_seconds": interaction.duration_seconds,
            "deterministic": features.get("deterministic", {}),
            "llm_structured": _condense_llm(features.get("llm_structured", {})),
            "scopes": [{"type": s.entity_type, "id": s.entity_id} for s in scopes],
        }
        req = LLMRequest(
            task_type=TaskType.DELTA_REPORT,
            user_message=json.dumps(payload),
            system_blocks=[
                CacheableBlock(text=_DELTA_REPORT_SYSTEM),
                tenant_context_block(tenant),
            ],
            max_tokens=1024,
            temperature=0.0,
            metadata={"interaction_id": str(interaction.id)},
        )
        resp = await self._router.ainvoke(req)
        try:
            return resp.parse_json()
        except Exception:
            logger.exception("Delta report JSON parse failed for interaction %s", interaction.id)
            return {}


def _condense_llm(llm: Dict[str, Any]) -> Dict[str, Any]:
    """Shrink the llm_structured blob to the fields delta-writers need.

    Keeps token counts low by dropping the long-form summary / transcripts
    and keeping only the structured signals.
    """
    keep_keys = {
        "sentiment_overall", "sentiment_score", "topics",
        "competitor_mentions", "product_feedback", "action_items",
        "churn_risk_signal", "churn_risk", "upsell_signal",
        "upsell_score", "coaching", "commitment_language_count",
        "change_talk_count", "sustain_talk_count", "next_step_specific",
        "objections_raised", "objections_resolved",
    }
    return {k: llm[k] for k in keep_keys if k in llm}


# ── Profile persistence ──────────────────────────────────────────────────


@dataclass
class ProfileEntityRef:
    entity_type: str  # ENTITY_CLIENT / ENTITY_AGENT / ENTITY_MANAGER / ENTITY_BUSINESS
    entity_id: uuid.UUID


class ProfileStore:
    """Append-only versioned store over the four profile tables."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def latest(self, ref: ProfileEntityRef) -> Optional[Dict[str, Any]]:
        model, fk = self._model_and_fk(ref.entity_type)
        stmt = (
            select(model)
            .where(getattr(model, fk) == ref.entity_id)
            .order_by(desc(model.version))
            .limit(1)
        )
        row = self._session.execute(stmt).scalar_one_or_none()
        if row is None:
            return None
        return {
            "id": str(row.id),
            "version": row.version,
            "profile": row.profile or {},
            "top_factors": row.top_factors or [],
            "confidence": row.confidence,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }

    def append(
        self,
        *,
        ref: ProfileEntityRef,
        tenant_id: uuid.UUID,
        profile: Dict[str, Any],
        top_factors: List[Dict[str, Any]],
        source_event: Dict[str, Any],
        confidence: Optional[float] = None,
    ) -> Dict[str, Any]:
        model, fk = self._model_and_fk(ref.entity_type)
        latest = self.latest(ref)
        version = (latest["version"] + 1) if latest else 1
        row = model(
            **{fk: ref.entity_id},
            tenant_id=tenant_id,
            version=version,
            profile=profile,
            top_factors=top_factors,
            source_event=source_event,
            confidence=confidence,
        )
        self._session.add(row)
        self._session.flush()
        return {"id": str(row.id), "version": version}

    @staticmethod
    def _model_and_fk(entity_type: str) -> Tuple[Any, str]:
        # Imported lazily to avoid circular deps.
        from backend.app.models import (
            AgentProfile,
            BusinessProfile,
            ClientProfile,
            ManagerProfile,
        )
        mapping = {
            ENTITY_CLIENT: (ClientProfile, "contact_id"),
            ENTITY_AGENT: (AgentProfile, "agent_id"),
            ENTITY_MANAGER: (ManagerProfile, "manager_id"),
            ENTITY_BUSINESS: (BusinessProfile, "business_tenant_id"),
        }
        if entity_type not in mapping:
            raise ValueError(f"Unknown entity_type {entity_type!r}")
        return mapping[entity_type]


# ── Daily consolidation prompt ───────────────────────────────────────────


_DAILY_CONSOLIDATION_SYSTEM = (
    "You are the daily orchestrator for a conversation intelligence "
    "platform.  For each entity (client, agent, manager, or business) "
    "you are given (a) its prior profile payload and (b) the structured "
    "delta reports accumulated since the last run.  Produce the new "
    "profile payload.\n\n"
    "Hard rules:\n"
    "1. Ground every claim in evidence present in the deltas. Do not "
    "   invent trends, skills, or risks.\n"
    "2. Keep the narrative summary ≤ 3 sentences. Users will see it.\n"
    "3. Return between 3 and 5 top_factors — signed and labeled.\n"
    "4. Return up to 3 recommendations, ranked by expected impact.\n"
    "5. Emit a confidence in [0, 1] reflecting how much evidence you had.\n"
    "6. If the deltas contradict the prior profile, prefer the deltas.\n\n"
    "Return ONLY valid JSON with keys: summary, metrics, top_factors, "
    "recommendations, confidence, history_headline.  `metrics` is the "
    "entity's structured metric dict (schema per entity documented "
    "elsewhere); preserve unspecified fields from the prior profile so "
    "you don't regress coverage."
)


_ENTITY_CONTRACTS: Dict[str, str] = {
    ENTITY_CLIENT: (
        "This is a CLIENT profile. Metrics must include: sentiment_trend, "
        "rolling_churn_risk_pct, engagement_breadth, champion_health, "
        "outstanding_objections, commitment_language_density, "
        "buying_signal_count, preferred_communication_style, "
        "product_feedback_themes, competitor_pressure_index."
    ),
    ENTITY_AGENT: (
        "This is an AGENT profile.  Metrics must include: "
        "win_rate_by_stage, lsm_with_customers, reflection_to_question_ratio, "
        "objection_resolved_rate, talk_listen_ratio, patience_sec_p50, "
        "skill_scores (discovery, objection_handling, closing), "
        "coaching_completion_rate, weak_skills (list)."
    ),
    ENTITY_MANAGER: (
        "This is a MANAGER profile. Metrics must include: team_composition, "
        "at_risk_accounts, coaching_debt, escalation_queue, pipeline_coverage, "
        "cross_agent_patterns (list of observations)."
    ),
    ENTITY_BUSINESS: (
        "This is a BUSINESS (tenant) profile. Metrics must include: "
        "health_index, deal_quality_cohorts, topic_trend_report, "
        "competitive_pressure_index, pricing_sensitivity_index, "
        "content_gaps (list), team_skill_gaps (list), hiring_signal."
    ),
}


class Orchestrator:
    """End-to-end driver for the three cadences.

    Usage
    -----
    Real-time: ``orchestrator.record_delta(...)`` inside the pipeline.
    Daily: ``orchestrator.run_daily(session, tenant_id)`` from Celery Beat.
    Weekly: ``orchestrator.run_weekly(session, tenant_id)`` from Celery Beat.
    """

    def __init__(self, router: Optional[ModelRouter] = None) -> None:
        self._router = router or get_router()

    # ── Real-time ─────────────────────────────────────────────────────

    def record_delta(
        self,
        session: Session,
        *,
        tenant_id: uuid.UUID,
        interaction_id: uuid.UUID,
        scopes: Sequence[EntityScope],
        delta: Dict[str, Any],
    ) -> None:
        """Persist a delta report produced by :class:`DeltaReportWriter`."""
        from backend.app.models import DeltaReport
        row = DeltaReport(
            tenant_id=tenant_id,
            interaction_id=interaction_id,
            scopes=[{"type": s.entity_type, "id": s.entity_id} for s in scopes],
            delta=delta,
        )
        session.add(row)

    # ── Daily consolidation ───────────────────────────────────────────

    def run_daily(
        self,
        session: Session,
        tenant_id: uuid.UUID,
    ) -> Dict[str, int]:
        """Consolidate unconsumed deltas for one tenant into profile bumps.

        Returns counts keyed by entity_type for observability.
        """
        from backend.app.models import DeltaReport, Tenant

        tenant = session.get(Tenant, tenant_id)
        if tenant is None:
            return {}

        stmt = (
            select(DeltaReport)
            .where(
                and_(
                    DeltaReport.tenant_id == tenant_id,
                    DeltaReport.consumed_at.is_(None),
                )
            )
        )
        deltas = session.execute(stmt).scalars().all()
        if not deltas:
            return {}

        buckets: Dict[Tuple[str, str], List[Any]] = {}
        for d in deltas:
            for scope in d.scopes or []:
                key = (scope["type"], scope["id"])
                buckets.setdefault(key, []).append(d)

        store = ProfileStore(session)
        counts: Dict[str, int] = {}
        consumed_ids: List[uuid.UUID] = []
        for (entity_type, entity_id), entity_deltas in buckets.items():
            try:
                self._consolidate_one(
                    session=session,
                    store=store,
                    tenant=tenant,
                    entity_type=entity_type,
                    entity_id=uuid.UUID(entity_id),
                    deltas=entity_deltas,
                )
                counts[entity_type] = counts.get(entity_type, 0) + 1
                consumed_ids.extend(d.id for d in entity_deltas)
            except Exception:
                logger.exception(
                    "Daily consolidation failed for %s %s (tenant %s)",
                    entity_type, entity_id, tenant_id,
                )

        # Mark consumed in a single update.
        if consumed_ids:
            now = datetime.now(timezone.utc)
            session.execute(
                update(DeltaReport)
                .where(DeltaReport.id.in_(list(set(consumed_ids))))
                .values(consumed_at=now)
            )
        session.commit()
        return counts

    def _consolidate_one(
        self,
        *,
        session: Session,
        store: ProfileStore,
        tenant: Any,
        entity_type: str,
        entity_id: uuid.UUID,
        deltas: List[Any],
    ) -> None:
        """Consolidate one entity's deltas into a new profile version."""
        ref = ProfileEntityRef(entity_type=entity_type, entity_id=entity_id)
        prior = store.latest(ref) or {"profile": {}, "version": 0}

        prompt_body = {
            "entity_type": entity_type,
            "entity_id": str(entity_id),
            "prior_profile": prior["profile"],
            "deltas": [
                {
                    "interaction_id": str(d.interaction_id),
                    "created_at": d.created_at.isoformat() if d.created_at else None,
                    "delta": d.delta,
                }
                for d in deltas
            ],
        }

        contract = _ENTITY_CONTRACTS.get(entity_type, "")
        task_type_map = {
            ENTITY_CLIENT: TaskType.ORCH_CLIENT,
            ENTITY_AGENT: TaskType.ORCH_AGENT,
            ENTITY_MANAGER: TaskType.ORCH_MANAGER,
            ENTITY_BUSINESS: TaskType.ORCH_BUSINESS,
        }
        req = LLMRequest(
            task_type=task_type_map[entity_type],
            user_message=json.dumps(prompt_body),
            system_blocks=[
                CacheableBlock(text=_DAILY_CONSOLIDATION_SYSTEM),
                CacheableBlock(text=contract),
                tenant_context_block(tenant),
            ],
            max_tokens=3072,
            temperature=0.0,
            metadata={"custom_id": f"{entity_type}:{entity_id}"},
        )
        resp = self._router.invoke(req)
        try:
            payload = resp.parse_json()
        except Exception:
            logger.exception(
                "Orchestrator JSON parse failed for %s %s", entity_type, entity_id
            )
            return

        store.append(
            ref=ref,
            tenant_id=tenant.id,
            profile={
                "as_of": datetime.now(timezone.utc).isoformat(),
                "summary": payload.get("summary", ""),
                "metrics": payload.get("metrics", {}),
                "history": _append_history(
                    prior.get("profile", {}).get("history", []),
                    {
                        "version": prior.get("version", 0),
                        "as_of": prior.get("created_at"),
                        "headline": payload.get("history_headline", ""),
                    },
                ),
            },
            top_factors=payload.get("top_factors", []),
            source_event={
                "kind": "daily_consolidation",
                "delta_ids": [str(d.id) for d in deltas],
            },
            confidence=_clamp01(payload.get("confidence")),
        )

    # ── Weekly reflection ─────────────────────────────────────────────

    _WEEKLY_SYSTEM = (
        "You are the weekly self-improvement reviewer for the platform. "
        "You are given (a) each entity's last 7 daily profile versions, "
        "(b) the observed proxy outcomes (renewals, cancellations, "
        "action-item closures, reply sentiment).  Identify where the "
        "platform's predictions diverged from reality and propose "
        "specific calibration adjustments and new coaching priorities.  "
        "Return JSON with keys: calibration_adjustments (list of "
        "{scorer_name, suggested_delta, rationale}), new_coaching_focus "
        "(list of strings), drift_alerts (list of {feature, severity, "
        "note}), confidence (float)."
    )

    def run_weekly(
        self,
        session: Session,
        tenant_id: uuid.UUID,
        lookback_days: int = 7,
    ) -> Dict[str, Any]:
        from backend.app.models import (
            AgentProfile,
            BusinessProfile,
            ClientProfile,
            DeltaReport,
            ManagerProfile,
            Tenant,
        )

        tenant = session.get(Tenant, tenant_id)
        if tenant is None:
            return {}

        since = datetime.now(timezone.utc) - timedelta(days=lookback_days)

        def _recent(model, fk):
            stmt = (
                select(model)
                .where(model.tenant_id == tenant_id, model.created_at >= since)
                .order_by(desc(model.created_at))
                .limit(200)
            )
            return session.execute(stmt).scalars().all()

        snapshot = {
            "client_profiles": [self._abbrev_profile(p) for p in _recent(ClientProfile, "contact_id")],
            "agent_profiles": [self._abbrev_profile(p) for p in _recent(AgentProfile, "agent_id")],
            "manager_profiles": [self._abbrev_profile(p) for p in _recent(ManagerProfile, "manager_id")],
            "business_profiles": [self._abbrev_profile(p) for p in _recent(BusinessProfile, "business_tenant_id")],
            "delta_count": session.execute(
                select(DeltaReport).where(
                    DeltaReport.tenant_id == tenant_id,
                    DeltaReport.created_at >= since,
                )
            ).scalars().all().__len__(),
        }

        req = LLMRequest(
            task_type=TaskType.ORCH_WEEKLY,
            user_message=json.dumps(snapshot),
            system_blocks=[
                CacheableBlock(text=self._WEEKLY_SYSTEM),
                tenant_context_block(tenant),
            ],
            max_tokens=4096,
            temperature=0.0,
        )
        resp = self._router.invoke(req)
        try:
            payload = resp.parse_json()
        except Exception:
            logger.exception("Weekly reflection JSON parse failed for tenant %s", tenant_id)
            return {}
        return payload

    # ── Helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _abbrev_profile(row: Any) -> Dict[str, Any]:
        return {
            "entity_id": str(
                getattr(row, "contact_id", None)
                or getattr(row, "agent_id", None)
                or getattr(row, "manager_id", None)
                or getattr(row, "business_tenant_id", None),
            ),
            "version": row.version,
            "summary": (row.profile or {}).get("summary", ""),
            "top_factors": row.top_factors or [],
            "confidence": row.confidence,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }


def _append_history(history: List[Dict[str, Any]], entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not entry.get("version"):
        return history
    new = [entry] + list(history or [])
    return new[:10]  # cap to last 10 versions for bounded JSONB size


def _clamp01(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return None


# Module-level singleton for pipeline usage.
_default_orchestrator: Optional[Orchestrator] = None


def get_orchestrator() -> Orchestrator:
    global _default_orchestrator
    if _default_orchestrator is None:
        _default_orchestrator = Orchestrator()
    return _default_orchestrator
