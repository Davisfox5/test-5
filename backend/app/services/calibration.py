"""Calibration — refit Platt scaling for every scorer against observed outcomes.

Runs weekly via Celery Beat.  For each scorer that has a configured
proxy outcome:

1. Collect ``(raw_score, observed_outcome)`` pairs from
   ``interaction_features`` where the outcome is present.
2. Fit Platt scaling (``A``, ``B``) on the pairs.
3. Compute Expected Calibration Error on a held-out 20% split.
4. Persist the result as a new :class:`ScorerVersion` row.  The next
   scoring call picks the most-recently-active version.

If an ECE above the 0.12 threshold is detected, the active flag is not
flipped; a warning is logged so the orchestrator's weekly reflection
surfaces it as a drift alert.

This module deliberately works with whatever data exists.  With fewer
than :data:`MIN_CALIBRATION_SAMPLES` observations, a scorer refit is
skipped and the existing (or default) version stays active.
"""

from __future__ import annotations

import logging
import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import and_, desc, select, update
from sqlalchemy.orm import Session

from backend.app.services.stats import (
    expected_calibration_error,
    platt_scale_apply,
    platt_scale_fit,
)

logger = logging.getLogger(__name__)


MIN_CALIBRATION_SAMPLES = 50
ECE_ALERT_THRESHOLD = 0.12
HELDOUT_FRACTION = 0.2


# ── Scorer → outcome mapping ─────────────────────────────────────────────


@dataclass
class ScorerCalibrationConfig:
    """How to extract (raw_score, outcome) pairs for one scorer."""

    scorer_name: str
    raw_score_path: Sequence[str]  # dotted JSON path inside llm_structured
    outcome_keys_positive: Sequence[str]
    outcome_keys_negative: Sequence[str]


DEFAULT_CALIBRATION_CONFIGS: List[ScorerCalibrationConfig] = [
    ScorerCalibrationConfig(
        scorer_name="sentiment",
        raw_score_path=("sentiment_score",),
        outcome_keys_positive=("customer_replied",),
        outcome_keys_negative=("customer_no_reply_72h", "customer_escalated"),
    ),
    ScorerCalibrationConfig(
        scorer_name="churn_risk",
        raw_score_path=("churn_risk",),
        outcome_keys_positive=("contact_churned_30d", "deal_lost"),
        outcome_keys_negative=("contact_active_30d", "tenant_renewed"),
    ),
    ScorerCalibrationConfig(
        scorer_name="upsell",
        raw_score_path=("upsell_score",),
        outcome_keys_positive=("tenant_upgraded",),
        outcome_keys_negative=("tenant_renewed",),  # renewal without upgrade
    ),
]


# ── Collector ────────────────────────────────────────────────────────────


def _read_path(obj: Dict[str, Any], path: Sequence[str]) -> Any:
    cursor: Any = obj
    for step in path:
        if not isinstance(cursor, dict):
            return None
        cursor = cursor.get(step)
        if cursor is None:
            return None
    return cursor


def _extract_outcome(
    outcomes: Dict[str, Any],
    positive_keys: Sequence[str],
    negative_keys: Sequence[str],
) -> Optional[int]:
    """Derive a binary outcome for calibration.

    Returns 1 for positive, 0 for negative, None when no relevant event
    is present (so the item is skipped rather than biasing the fit).
    """
    for key in positive_keys:
        if key in outcomes:
            return 1
    for key in negative_keys:
        if key in outcomes:
            return 0
    return None


def collect_pairs(
    session: Session,
    tenant_id: uuid.UUID,
    config: ScorerCalibrationConfig,
) -> List[Tuple[float, int]]:
    """Return ``(raw_score, outcome_0_or_1)`` pairs for one scorer."""
    from backend.app.models import InteractionFeatures

    rows = session.execute(
        select(InteractionFeatures).where(
            InteractionFeatures.tenant_id == tenant_id
        )
    ).scalars().all()
    pairs: List[Tuple[float, int]] = []
    for row in rows:
        raw = _read_path(row.llm_structured or {}, config.raw_score_path)
        if raw is None:
            continue
        try:
            raw_f = float(raw)
        except (TypeError, ValueError):
            continue
        outcome = _extract_outcome(
            row.proxy_outcomes or {},
            config.outcome_keys_positive,
            config.outcome_keys_negative,
        )
        if outcome is None:
            continue
        pairs.append((raw_f, outcome))
    return pairs


# ── Fitter ───────────────────────────────────────────────────────────────


@dataclass
class CalibrationFitResult:
    scorer_name: str
    A: float
    B: float
    ece: float
    n: int
    activated: bool
    reason: Optional[str] = None


def fit_one_scorer(
    session: Session,
    tenant_id: uuid.UUID,
    config: ScorerCalibrationConfig,
    seed: int = 42,
) -> CalibrationFitResult:
    pairs = collect_pairs(session, tenant_id, config)
    n = len(pairs)
    if n < MIN_CALIBRATION_SAMPLES:
        return CalibrationFitResult(
            scorer_name=config.scorer_name,
            A=0.0,
            B=0.0,
            ece=0.0,
            n=n,
            activated=False,
            reason=f"insufficient_samples:{n}<{MIN_CALIBRATION_SAMPLES}",
        )
    rng = random.Random(seed)
    shuffled = pairs[:]
    rng.shuffle(shuffled)
    heldout = max(5, int(len(shuffled) * HELDOUT_FRACTION))
    train = shuffled[heldout:]
    eval_ = shuffled[:heldout]
    A, B = platt_scale_fit([r for r, _ in train], [y for _, y in train])
    probs = [platt_scale_apply(r, A, B) for r, _ in eval_]
    ece = expected_calibration_error(probs, [y for _, y in eval_])
    activated = ece <= ECE_ALERT_THRESHOLD
    result = CalibrationFitResult(
        scorer_name=config.scorer_name,
        A=A,
        B=B,
        ece=ece,
        n=n,
        activated=activated,
        reason=None if activated else f"ece_above_threshold:{ece}",
    )
    _persist_scorer_version(session, tenant_id, result)
    return result


def _persist_scorer_version(
    session: Session,
    tenant_id: uuid.UUID,
    fit: CalibrationFitResult,
) -> None:
    """Insert a new ScorerVersion row for the fit.  Active=True only when
    ECE is within threshold; prior active rows for the same (tenant,
    scorer_name) are deactivated first to keep a single active version.
    """
    from backend.app.models import ScorerVersion

    version_label = datetime.now(timezone.utc).strftime("platt-%Y%m%d%H%M%S")
    if fit.activated:
        session.execute(
            update(ScorerVersion)
            .where(
                and_(
                    ScorerVersion.tenant_id == tenant_id,
                    ScorerVersion.scorer_name == fit.scorer_name,
                    ScorerVersion.is_active.is_(True),
                )
            )
            .values(is_active=False)
        )
    row = ScorerVersion(
        tenant_id=tenant_id,
        scorer_name=fit.scorer_name,
        version=version_label,
        parameters={"A": fit.A, "B": fit.B, "n": fit.n},
        calibration={"A": fit.A, "B": fit.B, "ece": fit.ece},
        ece=fit.ece,
        is_active=fit.activated,
    )
    session.add(row)
    session.commit()


def fit_all_scorers(
    session: Session,
    tenant_id: uuid.UUID,
) -> List[CalibrationFitResult]:
    results: List[CalibrationFitResult] = []
    for config in DEFAULT_CALIBRATION_CONFIGS:
        try:
            results.append(fit_one_scorer(session, tenant_id, config))
        except Exception:
            logger.exception("Calibration fit failed for %s", config.scorer_name)
    return results


# ── Active version retrieval ─────────────────────────────────────────────


def active_calibration(
    session: Session,
    tenant_id: uuid.UUID,
    scorer_name: str,
) -> Optional[Dict[str, float]]:
    """Return ``{A, B, ece}`` for the currently active version, if any.

    Falls back to the global default (``tenant_id IS NULL``) when no
    tenant-specific version is active.
    """
    from backend.app.models import ScorerVersion

    for tid in (tenant_id, None):
        stmt = (
            select(ScorerVersion)
            .where(
                ScorerVersion.scorer_name == scorer_name,
                ScorerVersion.is_active.is_(True),
                ScorerVersion.tenant_id.is_(tid) if tid is None else ScorerVersion.tenant_id == tid,
            )
            .order_by(desc(ScorerVersion.created_at))
            .limit(1)
        )
        row = session.execute(stmt).scalar_one_or_none()
        if row is not None:
            return row.calibration
    return None
