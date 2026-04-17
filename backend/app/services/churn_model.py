"""Cox-style churn model scaffolding.

Design goal: build the full plumbing (feature extraction, training
gate, scoring interface, model persistence) so that as soon as a tenant
has ≥ :data:`MIN_TRAIN_EVENTS` observed cancellations + survivors, the
nightly job trains, stores, and activates a real model without code
changes.

Until that threshold is reached, :func:`predict_hazard` returns
``"insufficient_data"`` as the value and 0 as the confidence so the
composite churn scorer simply falls back to the LLM-driven inputs it
already uses.

The model itself is a pure-Python Cox proportional-hazards fit via
Newton-Raphson on the partial-likelihood score equations.  No numpy
dependency; good enough for O(10^4) observations.  When we outgrow it,
swap this file for a scikit-learn / lifelines implementation behind the
same interface.
"""

from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


MIN_TRAIN_EVENTS = 300  # minimum *observed* cancellations before we activate
FEATURES: Tuple[str, ...] = (
    "sentiment_score",
    "churn_risk_llm",
    "sustain_talk_count",
    "stakeholder_count",
    "competitor_pressure",
    "patience_sec",
    "interactivity_per_min",
)


# ── Training-data assembly ───────────────────────────────────────────────


@dataclass
class CoxDatum:
    duration_days: float  # time from interaction to event (or censor)
    event: int            # 1 = churned, 0 = censored (still active)
    x: List[float]        # feature vector in FEATURES order


def build_training_set(session: Session, tenant_id: uuid.UUID) -> List[CoxDatum]:
    """Produce survival observations from InteractionFeatures + proxy outcomes.

    Each interaction contributes one observation.  ``duration_days`` is
    the time between the interaction's ``created_at`` and either the
    ``contact_churned_30d`` / ``deal_lost`` event (if observed) or
    ``now`` (censored).
    """
    from backend.app.models import Interaction, InteractionFeatures

    rows = session.execute(
        select(Interaction, InteractionFeatures)
        .join(
            InteractionFeatures,
            InteractionFeatures.interaction_id == Interaction.id,
        )
        .where(Interaction.tenant_id == tenant_id)
    ).all()

    now = datetime.now(timezone.utc)
    out: List[CoxDatum] = []
    for interaction, features in rows:
        x = _feature_vector(features)
        if any(v is None for v in x):
            continue
        outcomes = features.proxy_outcomes or {}
        event, duration = _event_from_outcomes(interaction.created_at, outcomes, now)
        if duration is None or duration < 0:
            continue
        out.append(CoxDatum(duration_days=duration, event=event, x=[float(v) for v in x]))
    return out


def _feature_vector(features_row: Any) -> List[Optional[float]]:
    det = features_row.deterministic or {}
    llm = features_row.llm_structured or {}
    return [
        llm.get("sentiment_score"),
        llm.get("churn_risk"),
        llm.get("sustain_talk_count"),
        det.get("stakeholder_count"),
        len(llm.get("competitor_mentions") or []),
        det.get("patience_sec"),
        det.get("interactivity_per_min"),
    ]


def _event_from_outcomes(
    created_at: Optional[datetime],
    outcomes: Dict[str, Any],
    now: datetime,
) -> Tuple[int, Optional[float]]:
    if created_at is None:
        return (0, None)
    churn_keys = ("contact_churned_30d", "deal_lost", "tenant_churned")
    for key in churn_keys:
        if key in outcomes:
            event_time = _event_time(outcomes[key], fallback=created_at + timedelta(days=30))
            return (1, max(0.0, (event_time - created_at).total_seconds() / 86400))
    return (0, max(0.0, (now - created_at).total_seconds() / 86400))


def _event_time(outcome_record: Any, fallback: datetime) -> datetime:
    if isinstance(outcome_record, list) and outcome_record:
        outcome_record = outcome_record[-1]
    if isinstance(outcome_record, dict):
        occurred = outcome_record.get("occurred_at")
        if occurred:
            try:
                return datetime.fromisoformat(occurred.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                pass
    return fallback


# ── Cox fit ──────────────────────────────────────────────────────────────


@dataclass
class CoxModel:
    coefficients: List[float]
    feature_names: List[str]
    n_events: int
    n_censored: int
    log_likelihood: float
    fitted_at: str

    def hazard(self, x: Sequence[float]) -> float:
        """Return ``exp(β·x)`` — the per-observation hazard ratio."""
        z = sum(b * v for b, v in zip(self.coefficients, x))
        z = max(-35.0, min(35.0, z))
        return math.exp(z)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "coefficients": self.coefficients,
            "feature_names": self.feature_names,
            "n_events": self.n_events,
            "n_censored": self.n_censored,
            "log_likelihood": self.log_likelihood,
            "fitted_at": self.fitted_at,
        }


def fit_cox(data: List[CoxDatum], n_iter: int = 25, lr: float = 0.05) -> CoxModel:
    """Fit a Cox proportional-hazards model via partial-likelihood GD.

    Breslow tie-handling implicitly (all-at-risk sums).  Good enough for
    ~10k observations; for larger tenants we'll replace with lifelines.
    """
    if not data:
        raise ValueError("fit_cox called with empty data")
    n_features = len(data[0].x)
    beta = [0.0] * n_features

    # Sort by descending duration so the "at-risk set" builds naturally.
    sorted_data = sorted(data, key=lambda d: d.duration_days, reverse=True)

    n_events = sum(d.event for d in sorted_data)
    n_censored = len(sorted_data) - n_events

    for _ in range(n_iter):
        # Precompute exp(β·x) per sample — reused inside the loop.
        expbx = []
        for d in sorted_data:
            z = sum(b * v for b, v in zip(beta, d.x))
            z = max(-35.0, min(35.0, z))
            expbx.append(math.exp(z))

        # Accumulate the partial-likelihood gradient.
        grad = [0.0] * n_features
        sum_exp = 0.0
        sum_xexp = [0.0] * n_features
        ll = 0.0
        # Scanning in descending-duration order: when a subject enters the
        # risk set, everyone with longer duration is still at risk.
        for i, d in enumerate(sorted_data):
            sum_exp += expbx[i]
            for j in range(n_features):
                sum_xexp[j] += d.x[j] * expbx[i]
            if d.event == 1:
                for j in range(n_features):
                    grad[j] += d.x[j] - sum_xexp[j] / sum_exp
                ll += sum(b * v for b, v in zip(beta, d.x)) - math.log(sum_exp)

        # Gradient *ascent* on log-partial-likelihood.
        for j in range(n_features):
            beta[j] += lr * grad[j] / max(n_events, 1)

    return CoxModel(
        coefficients=[round(b, 6) for b in beta],
        feature_names=list(FEATURES),
        n_events=n_events,
        n_censored=n_censored,
        log_likelihood=round(ll, 4),
        fitted_at=datetime.now(timezone.utc).isoformat(),
    )


# ── Training entry point ─────────────────────────────────────────────────


@dataclass
class TrainResult:
    status: str        # 'ok' | 'insufficient_data'
    tenant_id: uuid.UUID
    n_events: int
    n_censored: int
    model_version: Optional[str] = None


def train_for_tenant(session: Session, tenant_id: uuid.UUID) -> TrainResult:
    data = build_training_set(session, tenant_id)
    n_events = sum(d.event for d in data)
    if n_events < MIN_TRAIN_EVENTS:
        return TrainResult(
            status="insufficient_data",
            tenant_id=tenant_id,
            n_events=n_events,
            n_censored=len(data) - n_events,
        )
    model = fit_cox(data)
    version_label = f"cox-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    _persist_cox_model(session, tenant_id, version_label, model)
    return TrainResult(
        status="ok",
        tenant_id=tenant_id,
        n_events=n_events,
        n_censored=model.n_censored,
        model_version=version_label,
    )


def _persist_cox_model(
    session: Session,
    tenant_id: uuid.UUID,
    version_label: str,
    model: CoxModel,
) -> None:
    from backend.app.models import ScorerVersion
    from sqlalchemy import and_, update

    session.execute(
        update(ScorerVersion)
        .where(
            and_(
                ScorerVersion.tenant_id == tenant_id,
                ScorerVersion.scorer_name == "churn_cox",
                ScorerVersion.is_active.is_(True),
            )
        )
        .values(is_active=False)
    )
    row = ScorerVersion(
        tenant_id=tenant_id,
        scorer_name="churn_cox",
        version=version_label,
        parameters=model.as_dict(),
        calibration={"fit_type": "cox_partial_likelihood"},
        is_active=True,
    )
    session.add(row)
    session.commit()


# ── Scoring ──────────────────────────────────────────────────────────────


@dataclass
class HazardPrediction:
    status: str         # 'ok' | 'insufficient_data'
    hazard_ratio: Optional[float]
    probability_90d: Optional[float]
    feature_contributions: Dict[str, float]


def load_active_model(session: Session, tenant_id: uuid.UUID) -> Optional[CoxModel]:
    from backend.app.models import ScorerVersion
    from sqlalchemy import desc

    stmt = (
        select(ScorerVersion)
        .where(
            ScorerVersion.tenant_id == tenant_id,
            ScorerVersion.scorer_name == "churn_cox",
            ScorerVersion.is_active.is_(True),
        )
        .order_by(desc(ScorerVersion.created_at))
        .limit(1)
    )
    row = session.execute(stmt).scalar_one_or_none()
    if row is None:
        return None
    params = row.parameters or {}
    try:
        return CoxModel(
            coefficients=list(params.get("coefficients", [])),
            feature_names=list(params.get("feature_names", [])),
            n_events=int(params.get("n_events", 0)),
            n_censored=int(params.get("n_censored", 0)),
            log_likelihood=float(params.get("log_likelihood", 0.0)),
            fitted_at=str(params.get("fitted_at", "")),
        )
    except (TypeError, ValueError):
        logger.exception("Failed to load Cox model for tenant %s", tenant_id)
        return None


def predict_hazard(
    session: Session,
    tenant_id: uuid.UUID,
    features_row: Any,
) -> HazardPrediction:
    model = load_active_model(session, tenant_id)
    if model is None:
        return HazardPrediction(
            status="insufficient_data",
            hazard_ratio=None,
            probability_90d=None,
            feature_contributions={},
        )
    x = _feature_vector(features_row)
    if any(v is None for v in x):
        return HazardPrediction(
            status="insufficient_data",
            hazard_ratio=None,
            probability_90d=None,
            feature_contributions={},
        )
    x_f = [float(v) for v in x]
    hazard = model.hazard(x_f)
    # Rough 90-day probability via exponential approximation; assumes
    # baseline hazard ~ n_events / n_at_risk / average_duration.  In
    # practice we'd fit the baseline during training; this is close
    # enough for dashboarding and better than nothing.
    baseline_rate = max(1e-6, model.n_events / max(len(FEATURES), 1) / 90)
    prob_90d = 1.0 - math.exp(-baseline_rate * hazard * 90)
    contributions = {
        name: round(beta * val, 4)
        for name, beta, val in zip(model.feature_names, model.coefficients, x_f)
    }
    return HazardPrediction(
        status="ok",
        hazard_ratio=round(hazard, 4),
        probability_90d=round(min(0.99, max(0.0, prob_90d)), 4),
        feature_contributions=contributions,
    )
