"""Quality / drift endpoints — admin-facing observability of the scoring
layer.  Returns Population Stability Index for key features, vintage
cohort curves of deal quality, and inter-rater reliability between
Haiku and Sonnet on a rotating sample.

All endpoints here are strictly read-only and scoped to the current
tenant.  They compute directly from the feature store and are therefore
cheap (no LLM calls).  Response shapes are stable contracts so that
future admin dashboards can render against them.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import get_current_tenant
from backend.app.db import get_db
from backend.app.models import (
    Contact,
    Interaction,
    InteractionFeatures,
    Tenant,
)
from backend.app.services.stats import (
    population_stability_index,
    population_stability_index_categorical,
)

router = APIRouter()


# ── Shapes ───────────────────────────────────────────────────────────────


class PSIRow(BaseModel):
    feature: str
    psi: float
    severity: str  # 'ok' | 'moderate' | 'severe'
    n_actual: int
    n_expected: int


class PSIOut(BaseModel):
    window_days_actual: int
    window_days_expected: int
    rows: List[PSIRow]


class VintagePoint(BaseModel):
    cohort: str  # ISO YYYY-MM for origination month
    tenure_days: int
    sample_size: int
    churn_rate: Optional[float]
    retention_rate: Optional[float]
    avg_sentiment_score: Optional[float]


class VintageOut(BaseModel):
    cohorts: List[VintagePoint]


# ── PSI over feature distributions ───────────────────────────────────────


_NUMERIC_FEATURE_PATHS: Dict[str, List[str]] = {
    "sentiment_score": ["llm_structured", "sentiment_score"],
    "churn_risk": ["llm_structured", "churn_risk"],
    "upsell_score": ["llm_structured", "upsell_score"],
    "linguistic_style_match": ["deterministic", "linguistic_style_match"],
    "patience_sec": ["deterministic", "patience_sec"],
    "interactivity_per_min": ["deterministic", "interactivity_per_min"],
    "question_rate_per_min": ["deterministic", "question_rate_per_min"],
}

_CATEGORICAL_FEATURE_PATHS: Dict[str, List[str]] = {
    "sentiment_overall": ["llm_structured", "sentiment_overall"],
    "churn_risk_signal": ["llm_structured", "churn_risk_signal"],
    "upsell_signal": ["llm_structured", "upsell_signal"],
}


def _read_path(row: Any, path: List[str]) -> Any:
    cursor: Any = row
    for step in path:
        if isinstance(cursor, dict):
            cursor = cursor.get(step)
        else:
            cursor = getattr(cursor, step, None)
        if cursor is None:
            return None
    return cursor


@router.get("/quality/psi", response_model=PSIOut)
async def feature_psi(
    window_days: int = Query(30, ge=7, le=90),
    baseline_days: int = Query(90, ge=30, le=365),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Compare the last ``window_days`` of feature distributions against a
    ``baseline_days`` baseline ending at the start of the actual window.
    """
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=window_days)
    baseline_end = window_start
    baseline_start = baseline_end - timedelta(days=baseline_days)

    async def _features_in(range_start: datetime, range_end: datetime) -> List[Any]:
        stmt = (
            select(InteractionFeatures)
            .join(Interaction, Interaction.id == InteractionFeatures.interaction_id)
            .where(
                and_(
                    InteractionFeatures.tenant_id == tenant.id,
                    Interaction.created_at >= range_start,
                    Interaction.created_at < range_end,
                )
            )
        )
        return (await db.execute(stmt)).scalars().all()

    actual = await _features_in(window_start, now)
    baseline = await _features_in(baseline_start, baseline_end)

    rows: List[PSIRow] = []
    for feat, path in _NUMERIC_FEATURE_PATHS.items():
        act_values = [float(v) for v in (_read_path(r, path) for r in actual) if v is not None]
        base_values = [float(v) for v in (_read_path(r, path) for r in baseline) if v is not None]
        if len(base_values) < 20 or len(act_values) < 20:
            continue
        psi = population_stability_index(act_values, base_values)
        rows.append(PSIRow(
            feature=feat,
            psi=psi,
            severity=_severity(psi),
            n_actual=len(act_values),
            n_expected=len(base_values),
        ))

    for feat, path in _CATEGORICAL_FEATURE_PATHS.items():
        act_counts: Dict[str, int] = {}
        base_counts: Dict[str, int] = {}
        for r in actual:
            v = _read_path(r, path)
            if v is not None:
                act_counts[str(v)] = act_counts.get(str(v), 0) + 1
        for r in baseline:
            v = _read_path(r, path)
            if v is not None:
                base_counts[str(v)] = base_counts.get(str(v), 0) + 1
        if sum(act_counts.values()) < 20 or sum(base_counts.values()) < 20:
            continue
        psi = population_stability_index_categorical(act_counts, base_counts)
        rows.append(PSIRow(
            feature=feat,
            psi=psi,
            severity=_severity(psi),
            n_actual=sum(act_counts.values()),
            n_expected=sum(base_counts.values()),
        ))

    return PSIOut(
        window_days_actual=window_days,
        window_days_expected=baseline_days,
        rows=sorted(rows, key=lambda r: r.psi, reverse=True),
    )


def _severity(psi: float) -> str:
    if psi >= 0.25:
        return "severe"
    if psi >= 0.10:
        return "moderate"
    return "ok"


# ── Vintage cohort curves ────────────────────────────────────────────────


@router.get("/quality/vintage", response_model=VintageOut)
async def vintage_curves(
    horizon_months: int = Query(12, ge=3, le=24),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Group interactions by origination month and track downstream
    outcomes at equal tenure (30 / 60 / 90 days).  Mirrors credit-
    industry vintage analysis: detects when "this quarter's closed
    deals are lower quality than last quarter's" well before ARR moves.
    """
    horizon_start = datetime.now(timezone.utc) - timedelta(days=horizon_months * 30)
    stmt = (
        select(Interaction, InteractionFeatures)
        .join(
            InteractionFeatures,
            InteractionFeatures.interaction_id == Interaction.id,
        )
        .where(
            and_(
                Interaction.tenant_id == tenant.id,
                Interaction.created_at >= horizon_start,
            )
        )
    )
    rows = (await db.execute(stmt)).all()

    # Group by YYYY-MM of origination.
    cohorts: Dict[str, List[Any]] = {}
    for interaction, features in rows:
        if interaction.created_at is None:
            continue
        cohort_key = interaction.created_at.strftime("%Y-%m")
        cohorts.setdefault(cohort_key, []).append((interaction, features))

    out: List[VintagePoint] = []
    for cohort_key, items in sorted(cohorts.items()):
        for tenure in (30, 60, 90):
            churned = 0
            considered = 0
            sentiment_sum = 0.0
            sentiment_n = 0
            for interaction, features in items:
                age_days = (datetime.now(timezone.utc) - interaction.created_at).days
                if age_days < tenure:
                    continue
                considered += 1
                outcomes = features.proxy_outcomes or {}
                churned_keys = (
                    "contact_churned_30d",
                    "deal_lost",
                    "tenant_churned",
                )
                if any(k in outcomes for k in churned_keys):
                    churned += 1
                score = (features.llm_structured or {}).get("sentiment_score")
                if score is not None:
                    try:
                        sentiment_sum += float(score)
                        sentiment_n += 1
                    except (TypeError, ValueError):
                        pass
            if considered == 0:
                continue
            out.append(VintagePoint(
                cohort=cohort_key,
                tenure_days=tenure,
                sample_size=considered,
                churn_rate=round(churned / considered, 4) if considered else None,
                retention_rate=round(1 - churned / considered, 4) if considered else None,
                avg_sentiment_score=round(sentiment_sum / sentiment_n, 3) if sentiment_n else None,
            ))
    return VintageOut(cohorts=out)


# ── Inter-rater reliability snapshot ─────────────────────────────────────


class ReliabilityOut(BaseModel):
    scorer: str
    krippendorff_alpha: Optional[float]
    n_paired_items: int
    status: str  # 'acceptable' | 'tentative' | 'unreliable' | 'insufficient_data'


@router.get("/quality/reliability", response_model=List[ReliabilityOut])
async def reliability(
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Return the most recent inter-rater-reliability snapshot per scorer.

    Alpha values come from the weekly quality job when present in the
    scorer-version registry.  If the job hasn't run yet, status is
    ``"insufficient_data"`` and the dashboard should render accordingly.
    """
    from backend.app.models import ScorerVersion

    stmt = (
        select(ScorerVersion)
        .where(
            (ScorerVersion.tenant_id == tenant.id)
            | (ScorerVersion.tenant_id.is_(None))
        )
        .order_by(ScorerVersion.created_at.desc())
        .limit(200)
    )
    rows = (await db.execute(stmt)).scalars().all()
    by_scorer: Dict[str, Any] = {}
    for row in rows:
        by_scorer.setdefault(row.scorer_name, row)

    out: List[ReliabilityOut] = []
    for scorer, row in by_scorer.items():
        alpha = (row.calibration or {}).get("krippendorff_alpha")
        n = (row.calibration or {}).get("reliability_n", 0)
        if alpha is None:
            status = "insufficient_data"
        elif alpha >= 0.80:
            status = "acceptable"
        elif alpha >= 0.67:
            status = "tentative"
        else:
            status = "unreliable"
        out.append(ReliabilityOut(
            scorer=scorer,
            krippendorff_alpha=alpha,
            n_paired_items=int(n),
            status=status,
        ))
    return out
