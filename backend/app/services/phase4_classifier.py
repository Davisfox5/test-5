"""Phase 4 binary classifiers for churn / upsell prediction.

Sibling to ``churn_model.py`` (which handles longitudinal time-to-churn
via Cox proportional hazards). This module ships per-call binary
classifiers — *given this single call's extracted features, what's the
probability that the customer churns / converts to expansion within the
label horizon?*

Design
------

* **Pure Python** — no numpy / sklearn dependency, mirroring the
  precedent set by :mod:`churn_model` and :mod:`irt`. Logistic
  regression via vanilla batch gradient descent with L2 regularisation.
  Good enough for O(10^4) training rows; swap for sklearn behind the
  same interface when we outgrow it.

* **Cold-start fallback.** Until a tenant accumulates
  :data:`MIN_TRAIN_EVENTS` labeled outcomes, :func:`predict` returns
  ``status="insufficient_data"`` and the caller falls through to the
  Phase 3 rubric (``evidence_scoring.compute_rubric``). The rubric IS
  the cold-start path until ML earns its keep.

* **Dual-logging continues.** Even when the classifier is active, the
  pipeline still writes the LLM bucket → rubric → classifier prediction
  side-by-side on ``Interaction.insights`` so we can compare them on
  the same call and catch model drift before it reaches users.

* **Persistence via** :class:`ScorerVersion` — same table the Cox
  module uses. ``scorer_name`` is ``"churn_lr_phase4"`` or
  ``"upsell_lr_phase4"``; the JSONB ``parameters`` holds weights +
  intercept + standardisation stats + Platt-calibration constants.

* **Calibration via Platt scaling** (one-parameter sigmoid fit on a
  held-out split). Cheaper than isotonic for the low-data regime we
  start in; isotonic post-fit is a drop-in upgrade once n_events
  crosses a few thousand.
"""

from __future__ import annotations

import logging
import math
import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# Below this we don't even attempt to fit. Above MIN but below RELIABLE
# we serve predictions tagged ``status="learning"`` so the UI can show
# a "still calibrating" caveat.
MIN_TRAIN_EVENTS = 50
RELIABLE_TRAIN_EVENTS = 1000

# Held-out fraction used for the Platt-calibration fit. With small
# datasets we skip the held-out split entirely (calibration_alpha = 1.0,
# beta = 0.0 → identity).
CALIBRATION_HELDOUT_FRACTION = 0.2
MIN_HELDOUT_FOR_CALIBRATION = 30

# Default label horizon for the binary "churned-by-X-days" target.
DEFAULT_LABEL_HORIZON_DAYS = 90
SUPPORTED_LABEL_HORIZONS = (30, 90, 180, 365)

# Outcome bundles per target. Both lists are tried in order; the first
# matching event for the customer within the label horizon yields the
# positive label.
CHURN_OUTCOME_TYPES: Tuple[str, ...] = ("churned",)
UPSELL_OUTCOME_TYPES: Tuple[str, ...] = ("upsold",)


Target = Literal["churn", "upsell"]


# Feature columns we extract from ``InteractionFeatures``. Pinned by
# ``test_phase4_features.py`` so a stealth rename in another module
# can't silently zero a column at training time. Any feature that is
# ``None`` for a row excludes that row from training (we never impute).
FEATURE_NAMES: Tuple[str, ...] = (
    # LLM-bucket-mapped numerics (Phase 1):
    "sentiment_score",
    "churn_risk",
    "upsell_score",
    # Evidence counts (Phase 3):
    "objection_count",
    "unresolved_objection_count",
    "commitment_count",
    "discovery_questions",
    "competitor_mention_count",
    # Deterministic rubric (Phase 3):
    "rubric_discovery_quality",
    "rubric_commitment_strength",
    "rubric_objection_resolution_rate",
    "rubric_win_likelihood",
    # Rapport (Phase 5 / Phase 2):
    "rapport_lsm_overall",
    "rapport_vocal_accommodation_overall",
)


# ── Training-data assembly ───────────────────────────────────────────


@dataclass
class LRDatum:
    """One labeled training row."""

    x: List[float]  # standardised feature vector in FEATURE_NAMES order
    y: int  # 1 = positive event within horizon, 0 = censored


def feature_vector(insights: Dict[str, Any]) -> List[Optional[float]]:
    """Pull the FEATURE_NAMES vector out of the analysis ``insights`` dict.

    Returns one Optional[float] per FEATURE_NAMES entry. Callers decide
    what to do with the Nones (training excludes the row; inference
    falls back to the rubric).
    """
    coaching = insights.get("coaching") or {}
    evidence = insights.get("evidence") or {}
    rubric = insights.get("rubric") or {}
    rapport = insights.get("rapport") or {}
    vocal = rapport.get("vocal_accommodation") or {}

    def _num(v: Any) -> Optional[float]:
        if v is None:
            return None
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        if math.isnan(f) or math.isinf(f):
            return None
        return f

    _ = coaching  # kept for future extension; the LLM emits coaching
    # blocks but the classifier doesn't consume them yet.
    return [
        _num(insights.get("sentiment_score")),
        _num(insights.get("churn_risk")),
        _num(insights.get("upsell_score")),
        _num(evidence.get("objection_count")),
        _num(evidence.get("unresolved_objection_count")),
        _num(evidence.get("commitment_count")),
        _num(evidence.get("discovery_questions")),
        _num(evidence.get("competitor_mention_count")),
        _num(rubric.get("discovery_quality")),
        _num(rubric.get("commitment_strength")),
        _num(rubric.get("objection_resolution_rate")),
        _num(rubric.get("win_likelihood")),
        _num(rapport.get("lsm_overall")),
        _num(vocal.get("overall")),
    ]


def build_training_set(
    session: Session,
    tenant_id: uuid.UUID,
    target: Target,
    label_horizon_days: int = DEFAULT_LABEL_HORIZON_DAYS,
) -> List[Tuple[List[Optional[float]], int]]:
    """Pair each interaction's feature vector with a binary outcome label.

    Returns a list of ``(raw_feature_vector, label)`` — *raw* because
    standardisation has to be fit on the training split, not the full
    set, and the caller does that in :func:`fit_lr`.

    Excludes rows with no ``customer_id`` (no way to attribute an
    outcome) and rows where ``created_at`` is missing.
    """
    if label_horizon_days not in SUPPORTED_LABEL_HORIZONS:
        raise ValueError(
            f"Unsupported label horizon: {label_horizon_days!r} "
            f"(allowed: {SUPPORTED_LABEL_HORIZONS})"
        )
    from backend.app.models import (
        CustomerOutcomeEvent,
        Interaction,
        InteractionFeatures,
    )

    rows = session.execute(
        select(Interaction, InteractionFeatures)
        .join(
            InteractionFeatures,
            InteractionFeatures.interaction_id == Interaction.id,
        )
        .where(Interaction.tenant_id == tenant_id)
        .where(Interaction.customer_id.is_not(None))
    ).all()
    if not rows:
        return []

    customer_ids = {ix.customer_id for ix, _ in rows if ix.customer_id}
    outcome_types = (
        CHURN_OUTCOME_TYPES if target == "churn" else UPSELL_OUTCOME_TYPES
    )
    outcome_rows = (
        session.execute(
            select(CustomerOutcomeEvent)
            .where(CustomerOutcomeEvent.tenant_id == tenant_id)
            .where(CustomerOutcomeEvent.customer_id.in_(customer_ids))
            .where(CustomerOutcomeEvent.event_type.in_(outcome_types))
        )
        .scalars()
        .all()
    )
    by_customer: Dict[uuid.UUID, List[CustomerOutcomeEvent]] = {}
    for ev in outcome_rows:
        if ev.customer_id is None:
            continue
        by_customer.setdefault(ev.customer_id, []).append(ev)

    horizon = timedelta(days=label_horizon_days)
    out: List[Tuple[List[Optional[float]], int]] = []
    for ix, features in rows:
        if ix.created_at is None:
            continue
        # Pull features from the persisted insights so the training-
        # path features match exactly what runs at inference. We read
        # ``llm_structured`` (which mirrors ``Interaction.insights`` for
        # Phase 0 telemetry); fall back to ``deterministic`` for the
        # rubric block when ``llm_structured`` is empty.
        merged = dict(features.llm_structured or {})
        det = features.deterministic or {}
        for k in ("rubric", "rapport", "evidence", "coaching"):
            if k not in merged and k in det:
                merged[k] = det[k]
        x = feature_vector(merged)
        if any(v is None for v in x):
            continue
        # Positive label: ANY matching outcome event within the horizon.
        events = by_customer.get(ix.customer_id, [])
        positive = any(
            ev.detected_at is not None
            and ix.created_at <= ev.detected_at <= ix.created_at + horizon
            for ev in events
        )
        out.append(([float(v) for v in x], 1 if positive else 0))
    return out


# ── Pure-Python logistic regression with Platt calibration ───────────


@dataclass
class LRModel:
    """Trained logistic regression + Platt-calibration wrapper.

    Inference: ``standardise(x) → linear(x) → sigmoid → platt → P(y=1)``
    where each step uses constants frozen at fit time. Serialisation is
    ``as_dict()`` / ``from_dict()`` so the entire model fits in a
    JSONB column without pickle / version-coupling risk.
    """

    weights: List[float]
    intercept: float
    feature_names: List[str]
    feature_means: List[float]
    feature_stds: List[float]
    n_train: int
    n_events: int
    log_loss: float
    platt_alpha: float = 1.0
    platt_beta: float = 0.0
    fitted_at: str = ""
    target: str = "churn"
    label_horizon_days: int = DEFAULT_LABEL_HORIZON_DAYS

    def as_dict(self) -> Dict[str, Any]:
        return {
            "weights": list(self.weights),
            "intercept": self.intercept,
            "feature_names": list(self.feature_names),
            "feature_means": list(self.feature_means),
            "feature_stds": list(self.feature_stds),
            "n_train": self.n_train,
            "n_events": self.n_events,
            "log_loss": self.log_loss,
            "platt_alpha": self.platt_alpha,
            "platt_beta": self.platt_beta,
            "fitted_at": self.fitted_at,
            "target": self.target,
            "label_horizon_days": self.label_horizon_days,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "LRModel":
        return cls(
            weights=list(d.get("weights", [])),
            intercept=float(d.get("intercept", 0.0)),
            feature_names=list(d.get("feature_names", [])),
            feature_means=list(d.get("feature_means", [])),
            feature_stds=list(d.get("feature_stds", [])),
            n_train=int(d.get("n_train", 0)),
            n_events=int(d.get("n_events", 0)),
            log_loss=float(d.get("log_loss", 0.0)),
            platt_alpha=float(d.get("platt_alpha", 1.0)),
            platt_beta=float(d.get("platt_beta", 0.0)),
            fitted_at=str(d.get("fitted_at", "")),
            target=str(d.get("target", "churn")),
            label_horizon_days=int(
                d.get("label_horizon_days", DEFAULT_LABEL_HORIZON_DAYS)
            ),
        )

    def _standardise(self, x: Sequence[float]) -> List[float]:
        out = []
        for i, v in enumerate(x):
            mean = self.feature_means[i] if i < len(self.feature_means) else 0.0
            std = self.feature_stds[i] if i < len(self.feature_stds) else 1.0
            out.append((float(v) - mean) / std if std > 0 else 0.0)
        return out

    def linear(self, x: Sequence[float]) -> float:
        z = self.intercept
        std_x = self._standardise(x)
        for w, xi in zip(self.weights, std_x):
            z += w * xi
        return z

    def predict_proba(self, x: Sequence[float]) -> float:
        z = self.linear(x)
        # Platt calibration on the linear score: P = sigmoid(α z + β)
        p = _sigmoid(self.platt_alpha * z + self.platt_beta)
        return max(0.0, min(1.0, p))


def _sigmoid(z: float) -> float:
    if z >= 0:
        e = math.exp(-z)
        return 1.0 / (1.0 + e)
    e = math.exp(z)
    return e / (1.0 + e)


def _standardise_batch(
    X: Sequence[Sequence[float]],
) -> Tuple[List[List[float]], List[float], List[float]]:
    """Return (standardised_X, means, stds). Std clamped to ≥ 1e-6 so
    a zero-variance column doesn't divide-by-zero."""
    if not X:
        return [], [], []
    n_features = len(X[0])
    means = [sum(row[i] for row in X) / len(X) for i in range(n_features)]
    stds: List[float] = []
    for i in range(n_features):
        var = sum((row[i] - means[i]) ** 2 for row in X) / len(X)
        std = math.sqrt(var) if var > 0 else 1.0
        stds.append(max(std, 1e-6))
    standardised = [
        [(row[i] - means[i]) / stds[i] for i in range(n_features)] for row in X
    ]
    return standardised, means, stds


def _train_lr(
    X: Sequence[Sequence[float]],
    y: Sequence[int],
    *,
    l2: float = 1.0,
    lr: float = 0.1,
    epochs: int = 200,
) -> Tuple[List[float], float, float]:
    """Batch gradient descent on the negative log-likelihood + L2.

    Returns ``(weights, intercept, final_log_loss)`` with weights in
    feature order. Inputs are assumed standardised. Convergence is not
    guaranteed for pathological datasets — a learning-rate schedule
    halves the step when the log-loss rises.
    """
    n_features = len(X[0]) if X else 0
    weights = [0.0] * n_features
    intercept = 0.0
    n = len(X)
    if n == 0:
        return weights, intercept, 0.0
    prev_loss = float("inf")
    step = lr
    for _ in range(epochs):
        # Forward pass
        grad_w = [0.0] * n_features
        grad_b = 0.0
        loss = 0.0
        for xi, yi in zip(X, y):
            z = intercept + sum(w * xi[j] for j, w in enumerate(weights))
            p = _sigmoid(z)
            err = p - yi
            grad_b += err
            for j in range(n_features):
                grad_w[j] += err * xi[j]
            # Numerically stable log loss.
            if yi == 1:
                loss += -math.log(max(p, 1e-12))
            else:
                loss += -math.log(max(1.0 - p, 1e-12))
        # Average + L2 penalty
        loss /= n
        l2_penalty = 0.5 * l2 * sum(w * w for w in weights) / n
        loss += l2_penalty
        if loss > prev_loss * 1.01:
            step *= 0.5
            if step < 1e-6:
                break
        prev_loss = loss
        # Update
        intercept -= step * (grad_b / n)
        for j in range(n_features):
            grad = grad_w[j] / n + l2 * weights[j] / n
            weights[j] -= step * grad
    return weights, intercept, prev_loss


def _platt_fit(
    raw_scores: Sequence[float], y: Sequence[int]
) -> Tuple[float, float]:
    """One-parameter sigmoid Platt fit: ``P = sigmoid(α z + β)``.

    Closed-form maximum-likelihood for tiny held-out splits is fragile;
    we run a 30-iteration Newton step instead. Returns ``(α, β)``.
    """
    n = len(raw_scores)
    if n < MIN_HELDOUT_FOR_CALIBRATION:
        return 1.0, 0.0
    # Target smoothing — Platt's recommended trick for small samples.
    n_pos = sum(y)
    n_neg = n - n_pos
    if n_pos == 0 or n_neg == 0:
        return 1.0, 0.0
    t_pos = (n_pos + 1.0) / (n_pos + 2.0)
    t_neg = 1.0 / (n_neg + 2.0)
    targets = [t_pos if yi == 1 else t_neg for yi in y]
    alpha = 1.0
    beta = 0.0
    # Hard cap on the step magnitude so the Newton step doesn't blow up
    # on poorly-conditioned data (well-separated scores → very large
    # gradients). Empirically, |d_alpha|+|d_beta| ≤ 1 keeps the iterate
    # in the basin of attraction without slowing convergence on the
    # well-conditioned case.
    MAX_STEP = 1.0
    for _ in range(60):
        gA = gB = 0.0
        hAA = hAB = hBB = 0.0
        for z, t in zip(raw_scores, targets):
            p = _sigmoid(alpha * z + beta)
            d = t - p
            gA += -d * z
            gB += -d
            w = p * (1.0 - p)
            hAA += w * z * z
            hAB += w * z
            hBB += w
        det = hAA * hBB - hAB * hAB
        if abs(det) < 1e-12:
            break
        d_alpha = (hBB * gA - hAB * gB) / det
        d_beta = (-hAB * gA + hAA * gB) / det
        # Trust-region: if the proposed step exceeds MAX_STEP in the
        # L1 sense, scale it down. Much simpler than a line-search and
        # plenty for our 2-parameter problem.
        magnitude = abs(d_alpha) + abs(d_beta)
        if magnitude > MAX_STEP:
            scale = MAX_STEP / magnitude
            d_alpha *= scale
            d_beta *= scale
        alpha -= d_alpha
        beta -= d_beta
        if abs(d_alpha) + abs(d_beta) < 1e-6:
            break
    return alpha, beta


def fit_lr(
    raw_data: Sequence[Tuple[Sequence[float], int]],
    *,
    target: Target = "churn",
    label_horizon_days: int = DEFAULT_LABEL_HORIZON_DAYS,
    feature_names: Sequence[str] = FEATURE_NAMES,
    l2: float = 1.0,
    seed: int = 17,
) -> LRModel:
    """Fit an LR + Platt-calibrated wrapper end to end.

    Splits ``raw_data`` 80/20 (deterministically by ``seed``) for
    Platt calibration. Below :data:`MIN_HELDOUT_FOR_CALIBRATION` rows
    in the held-out split, calibration is skipped (α=1, β=0 → identity).
    """
    if not raw_data:
        raise ValueError("Cannot fit LR with empty training set")
    rng = random.Random(seed)
    indices = list(range(len(raw_data)))
    rng.shuffle(indices)
    n_holdout = max(0, int(len(indices) * CALIBRATION_HELDOUT_FRACTION))
    holdout_idx = set(indices[:n_holdout])
    X_train = [raw_data[i][0] for i in range(len(raw_data)) if i not in holdout_idx]
    y_train = [raw_data[i][1] for i in range(len(raw_data)) if i not in holdout_idx]
    X_holdout = [raw_data[i][0] for i in range(len(raw_data)) if i in holdout_idx]
    y_holdout = [raw_data[i][1] for i in range(len(raw_data)) if i in holdout_idx]

    X_std, means, stds = _standardise_batch(X_train)
    weights, intercept, log_loss = _train_lr(X_std, y_train, l2=l2)

    # Calibration on the held-out split (or on training if holdout is
    # too small — degrades to identity in that case).
    if len(X_holdout) >= MIN_HELDOUT_FOR_CALIBRATION:
        # Standardise the holdout with the *train* means/stds.
        X_holdout_std = [
            [(row[i] - means[i]) / stds[i] for i in range(len(row))]
            for row in X_holdout
        ]
        raw_scores = [
            intercept + sum(w * xi[j] for j, w in enumerate(weights))
            for xi in X_holdout_std
        ]
        alpha, beta = _platt_fit(raw_scores, y_holdout)
    else:
        alpha, beta = 1.0, 0.0

    n_events = sum(y for _, y in raw_data)
    return LRModel(
        weights=weights,
        intercept=intercept,
        feature_names=list(feature_names),
        feature_means=means,
        feature_stds=stds,
        n_train=len(raw_data),
        n_events=n_events,
        log_loss=log_loss,
        platt_alpha=alpha,
        platt_beta=beta,
        fitted_at=datetime.now(timezone.utc).isoformat(),
        target=target,
        label_horizon_days=label_horizon_days,
    )


# ── Train/persist/load orchestration ─────────────────────────────────


@dataclass
class TrainResult:
    status: Literal["ok", "learning", "insufficient_data"]
    tenant_id: uuid.UUID
    target: Target
    n_total: int
    n_events: int
    model_version: Optional[str] = None
    log_loss: Optional[float] = None
    metrics: Dict[str, Any] = field(default_factory=dict)


def train_for_tenant(
    session: Session,
    tenant_id: uuid.UUID,
    target: Target,
    label_horizon_days: int = DEFAULT_LABEL_HORIZON_DAYS,
) -> TrainResult:
    raw_data = build_training_set(
        session, tenant_id, target, label_horizon_days
    )
    n_events = sum(y for _, y in raw_data)
    if n_events < MIN_TRAIN_EVENTS:
        return TrainResult(
            status="insufficient_data",
            tenant_id=tenant_id,
            target=target,
            n_total=len(raw_data),
            n_events=n_events,
        )
    model = fit_lr(
        raw_data, target=target, label_horizon_days=label_horizon_days
    )
    learning_mode = n_events < RELIABLE_TRAIN_EVENTS
    version = (
        f"{_scorer_name(target)}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    )
    metrics = _compute_inline_metrics(model, raw_data)
    _persist_model(
        session, tenant_id, version, model, target, learning_mode, metrics
    )
    return TrainResult(
        status="learning" if learning_mode else "ok",
        tenant_id=tenant_id,
        target=target,
        n_total=len(raw_data),
        n_events=n_events,
        model_version=version,
        log_loss=model.log_loss,
        metrics=metrics,
    )


def _scorer_name(target: Target) -> str:
    return f"{target}_lr_phase4"


def _compute_inline_metrics(
    model: LRModel, raw_data: Sequence[Tuple[Sequence[float], int]]
) -> Dict[str, Any]:
    """Compute Brier + ECE on the training set itself.

    These are training-set metrics — optimistic — so calibration UI
    should call out the difference between fit metrics and the held-
    out metrics. Cheap to compute alongside the fit.
    """
    from backend.app.services.phase4_calibration import (
        brier_score,
        expected_calibration_error,
    )

    preds = [model.predict_proba(x) for x, _ in raw_data]
    y = [yi for _, yi in raw_data]
    return {
        "brier_score": round(brier_score(preds, y), 4),
        "ece": round(expected_calibration_error(preds, y), 4),
        "positive_rate": round(sum(y) / len(y), 4) if y else 0.0,
    }


def _persist_model(
    session: Session,
    tenant_id: uuid.UUID,
    version: str,
    model: LRModel,
    target: Target,
    learning_mode: bool,
    metrics: Dict[str, Any],
) -> None:
    from backend.app.models import ScorerVersion
    from sqlalchemy import and_, update

    name = _scorer_name(target)
    session.execute(
        update(ScorerVersion)
        .where(
            and_(
                ScorerVersion.tenant_id == tenant_id,
                ScorerVersion.scorer_name == name,
                ScorerVersion.is_active.is_(True),
            )
        )
        .values(is_active=False)
    )
    row = ScorerVersion(
        tenant_id=tenant_id,
        scorer_name=name,
        version=version,
        parameters={**model.as_dict(), "learning_mode": learning_mode},
        calibration={
            "fit_type": "logistic_regression",
            "calibration_type": "platt",
            "learning_mode": learning_mode,
            "label_horizon_days": model.label_horizon_days,
            "metrics_inline": metrics,
        },
        is_active=True,
    )
    session.add(row)
    session.commit()


def load_active_model(
    session: Session, tenant_id: uuid.UUID, target: Target
) -> Optional[LRModel]:
    from backend.app.models import ScorerVersion
    from sqlalchemy import desc

    name = _scorer_name(target)
    stmt = (
        select(ScorerVersion)
        .where(
            ScorerVersion.tenant_id == tenant_id,
            ScorerVersion.scorer_name == name,
            ScorerVersion.is_active.is_(True),
        )
        .order_by(desc(ScorerVersion.created_at))
        .limit(1)
    )
    row = session.execute(stmt).scalar_one_or_none()
    if row is None:
        return None
    try:
        return LRModel.from_dict(row.parameters or {})
    except (TypeError, ValueError):
        logger.exception(
            "Failed to load Phase 4 %s model for tenant %s", target, tenant_id
        )
        return None


# ── Inference ────────────────────────────────────────────────────────


@dataclass
class ClassifierPrediction:
    status: Literal["ok", "learning", "insufficient_data"]
    target: Target
    probability: Optional[float]
    feature_names: List[str]
    feature_values: List[Optional[float]]
    model_version: Optional[str] = None
    label_horizon_days: int = DEFAULT_LABEL_HORIZON_DAYS
    caveat: Optional[str] = None


def predict(
    session: Session,
    tenant_id: uuid.UUID,
    target: Target,
    insights: Dict[str, Any],
) -> ClassifierPrediction:
    """Apply the active model to ``insights``; cold-start fallback.

    ``status="insufficient_data"`` means no model is active for this
    tenant + target — the caller falls through to the rubric. ``status``
    is "learning" or "ok" depending on whether the model has crossed
    :data:`RELIABLE_TRAIN_EVENTS`.
    """
    raw = feature_vector(insights)
    model = load_active_model(session, tenant_id, target)
    if model is None:
        return ClassifierPrediction(
            status="insufficient_data",
            target=target,
            probability=None,
            feature_names=list(FEATURE_NAMES),
            feature_values=list(raw),
            caveat="no_active_model",
        )
    if any(v is None for v in raw):
        return ClassifierPrediction(
            status="insufficient_data",
            target=target,
            probability=None,
            feature_names=list(model.feature_names),
            feature_values=list(raw),
            caveat="missing_features",
        )
    proba = model.predict_proba([float(v) for v in raw])
    learning_mode = (
        load_learning_mode_flag(session, tenant_id, target)
        if model.n_events < RELIABLE_TRAIN_EVENTS
        else False
    )
    return ClassifierPrediction(
        status="learning" if learning_mode else "ok",
        target=target,
        probability=round(proba, 4),
        feature_names=list(model.feature_names),
        feature_values=list(raw),
        model_version=model.fitted_at,
        label_horizon_days=model.label_horizon_days,
        caveat="below_reliable_threshold" if learning_mode else None,
    )


def load_learning_mode_flag(
    session: Session, tenant_id: uuid.UUID, target: Target
) -> bool:
    """Read the persisted ``learning_mode`` flag from the active row.

    Cheaper than re-running the n_events check on every inference. The
    flag is set at training time and never moves until the next
    training cycle.
    """
    from backend.app.models import ScorerVersion
    from sqlalchemy import desc

    name = _scorer_name(target)
    stmt = (
        select(ScorerVersion)
        .where(
            ScorerVersion.tenant_id == tenant_id,
            ScorerVersion.scorer_name == name,
            ScorerVersion.is_active.is_(True),
        )
        .order_by(desc(ScorerVersion.created_at))
        .limit(1)
    )
    row = session.execute(stmt).scalar_one_or_none()
    if row is None:
        return False
    return bool((row.parameters or {}).get("learning_mode", False))
