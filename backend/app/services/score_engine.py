"""Score engine — composite scores with top-K factor decomposition.

Every user-facing aggregate (sentiment, churn risk, health score, agent
performance, deal momentum) passes through this module.  Each scorer
returns a :class:`ScoreResult` containing:

- ``value`` — the headline number (0–100 or 0–1 depending on the score)
- ``confidence`` — 0–1, derived from sample size, calibration ECE, and
  ensemble disagreement.
- ``top_factors`` — 3–5 most influential features, signed and labeled,
  ranked by ``|β_j · z_j|``.
- ``recommendations`` — 1–3 prioritized next actions tied to the top
  factors.
- ``scorer_version`` — identifier so downstream systems can detect when
  a score's meaning has changed.

The philosophy of this module: all model weights, calibration
parameters, and raw feature vectors stay server-side.  The presentation
layer only sees the outer ``ScoreResult``.  Tenants can raise the
``top_factors`` cap via the ``expert_mode`` setting but never see β
coefficients.

Implementation is deliberately simple (additive linear composite with
explicit weights + Platt calibration) so it can ship today while the
ML-heavier scorers (Cox survival, GBM with SHAP) come online in later
phases without changing the contract.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from backend.app.services.stats import (
    platt_scale_apply,
    wilson_interval,
)


# ── Public contract ──────────────────────────────────────────────────────


@dataclass
class Factor:
    """One feature's contribution to a score, ready to render as a chip."""

    label: str  # human-readable name, e.g. "Patience"
    direction: str  # "+" or "-"
    magnitude_pct: float  # 0–100, share of total absolute factor weight
    why: str  # one-sentence explanation
    feature_id: str  # internal feature key (not shown to end users)


@dataclass
class Recommendation:
    """A prioritized next action tied to one or more factors."""

    action: str
    priority: str  # "high" | "medium" | "low"
    expected_impact: str  # short phrase, e.g. "+0.5 sentiment"
    basis_feature_ids: List[str] = field(default_factory=list)


@dataclass
class ScoreResult:
    value: float
    confidence: float
    top_factors: List[Factor]
    recommendations: List[Recommendation]
    scorer_version: str
    calibrated: bool = False

    def to_public(self, expert_mode: bool = False) -> Dict[str, Any]:
        """Serialize for API responses — never leaks internal weights."""
        cap = 10 if expert_mode else 3
        return {
            "value": round(self.value, 2),
            "confidence": round(self.confidence, 3),
            "top_factors": [
                {
                    "label": f.label,
                    "direction": f.direction,
                    "magnitude_pct": round(f.magnitude_pct, 1),
                    "why": f.why,
                }
                for f in self.top_factors[:cap]
            ],
            "recommendations": [
                {
                    "action": r.action,
                    "priority": r.priority,
                    "expected_impact": r.expected_impact,
                }
                for r in self.recommendations[:3]
            ],
            "scorer_version": self.scorer_version,
        }


# ── Feature registry — human labels + why-strings ────────────────────────


# Registry of features we might surface.  Keyed by (feature_id, sign).
# A negative-sign row is shown when the factor *hurts* the score.
_FEATURE_REGISTRY: Dict[str, Dict[str, str]] = {
    "sentiment_trajectory_slope+": {
        "label": "Mood improving",
        "why": "The customer's tone rose steadily during the call.",
    },
    "sentiment_trajectory_slope-": {
        "label": "Mood declining",
        "why": "The customer's tone slipped as the call progressed.",
    },
    "sentiment_end_valence+": {
        "label": "Strong close",
        "why": "The conversation ended on a positive note.",
    },
    "sentiment_end_valence-": {
        "label": "Weak close",
        "why": "The conversation ended on a negative note.",
    },
    "linguistic_style_match+": {
        "label": "Good rapport",
        "why": "Language patterns between agent and customer aligned well.",
    },
    "linguistic_style_match-": {
        "label": "Weak rapport",
        "why": "Agent and customer spoke past each other stylistically.",
    },
    "patience_sec+": {
        "label": "Patient listening",
        "why": "The agent left thoughtful pauses after the customer spoke.",
    },
    "patience_sec-": {
        "label": "Rushed responses",
        "why": "The agent jumped in quickly after the customer finished.",
    },
    "interactivity_per_min+": {
        "label": "Active dialogue",
        "why": "Conversation moved back and forth smoothly.",
    },
    "interactivity_per_min-": {
        "label": "Monologuing",
        "why": "Long stretches with only one speaker talking.",
    },
    "commitment_language_count+": {
        "label": "Commitment signals",
        "why": "Customer used language indicating intent to move forward.",
    },
    "sustain_talk_count-": {
        "label": "Resistance signals",
        "why": "Customer used language defending the status quo.",
    },
    "churn_risk_language+": {
        "label": "Cancel-intent phrases",
        "why": "Words associated with leaving appeared in the transcript.",
    },
    "stakeholder_count+": {
        "label": "Broad engagement",
        "why": "Multiple stakeholders participated in the conversation.",
    },
    "stakeholder_count-": {
        "label": "Single-threaded",
        "why": "Only one contact is engaged on this account.",
    },
    "action_item_completion_rate+": {
        "label": "Follow-through",
        "why": "Action items from prior calls have closed on time.",
    },
    "action_item_completion_rate-": {
        "label": "Follow-through gap",
        "why": "Action items from prior calls are overdue.",
    },
    "scorecard_score+": {
        "label": "Strong QA",
        "why": "Scorecard grading is above team average.",
    },
    "scorecard_score-": {
        "label": "QA gaps",
        "why": "Scorecard grading trails team average.",
    },
    "reflection_ratio+": {
        "label": "Reflective listening",
        "why": "Agent summarized the customer's points back to them.",
    },
    "question_rate_per_min+": {
        "label": "Good discovery",
        "why": "Agent asked a healthy volume of questions.",
    },
    "question_rate_per_min-": {
        "label": "Thin discovery",
        "why": "Agent asked fewer questions than the team benchmark.",
    },
    "filler_rate_per_min-": {
        "label": "Verbal disfluency",
        "why": "Filler words were frequent enough to be noticeable.",
    },
    "interruption_count_total-": {
        "label": "Interrupting",
        "why": "The agent cut off the customer mid-thought.",
    },
    "next_step_specific+": {
        "label": "Concrete next step",
        "why": "Call ended with a specific date, attendee, and agenda.",
    },
    "next_step_specific-": {
        "label": "No firm next step",
        "why": "No concrete next step was set before the call closed.",
    },
    "competitor_pressure-": {
        "label": "Competitor pressure",
        "why": "Competitors were mentioned and not fully addressed.",
    },
    "response_latency_p90-": {
        "label": "Slow follow-ups",
        "why": "Agent responses to customer messages are slower than peers.",
    },
}


# ── Composite scorer ─────────────────────────────────────────────────────


@dataclass
class WeightedFeature:
    """Declarative entry in a composite scorer's weight table."""

    feature_id: str
    weight: float
    baseline_mean: float
    baseline_std: float
    direction: int = 1  # +1 = higher is better; -1 = higher is worse
    recommendation: Optional[str] = None  # shown when factor is negative


class CompositeScorer:
    """Additive linear composite with standardized features.

    The score is the weighted sum of ``z_j = (x_j − μ_j) / σ_j`` times
    the declared weight.  Results are clamped to ``[0, 100]`` and the
    top-K factors surface by ``|β_j · z_j|`` ranking.  Platt calibration
    is applied when ``(A, B)`` is present.
    """

    def __init__(
        self,
        name: str,
        version: str,
        features: Sequence[WeightedFeature],
        intercept: float = 50.0,
        calibration: Optional[Dict[str, float]] = None,
    ) -> None:
        self.name = name
        self.version = version
        self.features = list(features)
        self.intercept = intercept
        self.calibration = calibration or {}

    def score(self, values: Dict[str, Optional[float]], top_k: int = 3) -> ScoreResult:
        contributions: List[Dict[str, Any]] = []
        total = self.intercept
        n_present = 0
        for wf in self.features:
            raw = values.get(wf.feature_id)
            if raw is None:
                continue
            n_present += 1
            std = wf.baseline_std if wf.baseline_std > 1e-9 else 1.0
            z = (float(raw) - wf.baseline_mean) / std
            contribution = wf.weight * z * wf.direction
            total += contribution
            contributions.append({
                "feature_id": wf.feature_id,
                "z": z,
                "contribution": contribution,
                "direction": wf.direction,
                "recommendation": wf.recommendation,
            })

        raw_score = max(0.0, min(100.0, total))
        calibrated = False
        if "A" in self.calibration and "B" in self.calibration:
            prob = platt_scale_apply(
                total, self.calibration["A"], self.calibration["B"]
            )
            raw_score = round(prob * 100, 2)
            calibrated = True

        # Rank factors by absolute contribution, keep top-K sized to
        # scale the magnitude_pct for the surviving factors.
        contributions.sort(key=lambda c: abs(c["contribution"]), reverse=True)
        total_abs = sum(abs(c["contribution"]) for c in contributions) or 1.0
        top_factors: List[Factor] = []
        for c in contributions[: max(top_k, 3)]:
            sign = "+" if c["contribution"] >= 0 else "-"
            registry_key = f"{c['feature_id']}{sign}"
            meta = _FEATURE_REGISTRY.get(registry_key)
            if meta is None:
                # Fallback label so unknown features still surface.
                meta = {"label": c["feature_id"].replace("_", " ").title(), "why": ""}
            top_factors.append(Factor(
                label=meta["label"],
                direction=sign,
                magnitude_pct=100 * abs(c["contribution"]) / total_abs,
                why=meta["why"],
                feature_id=c["feature_id"],
            ))

        recommendations = self._build_recommendations(contributions)

        # Confidence blends coverage (how many inputs we had vs. the
        # registered weights) with the calibration-ECE if known.
        coverage = n_present / max(len(self.features), 1)
        ece = float(self.calibration.get("ece", 0.0))
        confidence = max(0.0, min(1.0, coverage * (1.0 - ece)))

        return ScoreResult(
            value=raw_score,
            confidence=round(confidence, 3),
            top_factors=top_factors,
            recommendations=recommendations,
            scorer_version=f"{self.name}:{self.version}",
            calibrated=calibrated,
        )

    @staticmethod
    def _build_recommendations(
        contributions: List[Dict[str, Any]],
    ) -> List[Recommendation]:
        """Pick at most 3 recommendations from the worst-contributing features.

        We only recommend against factors that *hurt* the score and that
        carry a configured recommendation string.
        """
        negatives = [c for c in contributions if c["contribution"] < 0 and c["recommendation"]]
        negatives.sort(key=lambda c: c["contribution"])  # most negative first

        out: List[Recommendation] = []
        for i, c in enumerate(negatives[:3]):
            priority = "high" if i == 0 else "medium" if i == 1 else "low"
            out.append(Recommendation(
                action=c["recommendation"],
                priority=priority,
                expected_impact=f"+{abs(c['contribution']):.1f} points",
                basis_feature_ids=[c["feature_id"]],
            ))
        return out


# ── Default scorer configurations ────────────────────────────────────────


def default_sentiment_scorer() -> CompositeScorer:
    """Sentiment scorer: combines LLM numeric + trajectory + close features."""
    return CompositeScorer(
        name="sentiment",
        version="v1",
        intercept=50.0,
        features=[
            WeightedFeature("sentiment_score_llm", 4.0, 5.0, 2.0),
            WeightedFeature("sentiment_trajectory_slope", 3.0, 0.0, 0.3),
            WeightedFeature("sentiment_end_valence", 3.5, 5.0, 2.0),
            WeightedFeature("linguistic_style_match", 2.0, 0.75, 0.15),
            WeightedFeature(
                "interruption_count_total", 1.5, 2.0, 2.0, direction=-1,
                recommendation="Leave more space before replying after the customer speaks.",
            ),
            WeightedFeature("laughter_events", 1.0, 1.0, 1.0),
            # Acoustic stress markers on the agent side drag sentiment
            # lower: a tight, strained voice reads as impatience even when
            # the words are polite.
            WeightedFeature(
                "agent_voice_stress", 1.5, 0.0, 1.0, direction=-1,
                recommendation="Agent voice tension detected — slow breathing, longer pauses.",
            ),
            WeightedFeature(
                "agent_monotone", 1.0, 0.0, 1.0, direction=-1,
                recommendation="Flat delivery dulls rapport — vary tone and emphasis.",
            ),
        ],
    )


def default_churn_scorer() -> CompositeScorer:
    """Churn-risk scorer: higher value = higher risk (scored 0–100)."""
    return CompositeScorer(
        name="churn_risk",
        version="v1",
        intercept=30.0,
        features=[
            WeightedFeature("churn_risk_llm", 4.0, 0.4, 0.25),
            WeightedFeature("sustain_talk_count", 2.5, 1.0, 1.5),
            WeightedFeature("sentiment_trajectory_slope", 3.0, 0.0, 0.3, direction=-1),
            WeightedFeature(
                "stakeholder_count", 2.0, 3.0, 1.5, direction=-1,
                recommendation="Bring additional stakeholders into the next conversation.",
            ),
            WeightedFeature(
                "action_item_completion_rate", 2.5, 0.7, 0.2, direction=-1,
                recommendation="Close overdue action items before the next call.",
            ),
            WeightedFeature("churn_risk_language", 3.0, 0.0, 1.0),
            WeightedFeature("competitor_pressure", 2.0, 0.0, 1.0),
            # Customer-side acoustic escalation: sustained loudness
            # relative to the tenant baseline is a reliable cue that
            # the call is going hot, independent of the transcript.
            WeightedFeature("customer_hot_voice", 2.0, 0.0, 1.0),
            # Arousal axis (pitch variance + intensity + rate +
            # jitter/shimmer). ``neutral`` baseline ≈ 0.35; above 0.5
            # flags an escalating customer even when the transcript
            # stays polite.
            WeightedFeature("customer_arousal", 1.5, 0.35, 0.2),
        ],
    )


def default_health_scorer() -> CompositeScorer:
    """Account health scorer: composite of sentiment, engagement, and follow-through."""
    return CompositeScorer(
        name="health_score",
        version="v1",
        intercept=50.0,
        features=[
            WeightedFeature("sentiment_delta_vs_baseline", 3.0, 0.0, 1.0),
            WeightedFeature("action_item_completion_rate", 3.0, 0.7, 0.2),
            WeightedFeature("stakeholder_count", 2.0, 3.0, 1.5),
            WeightedFeature("scorecard_score", 2.0, 75.0, 15.0),
            WeightedFeature("response_latency_p90", 1.5, 24.0, 12.0, direction=-1,
                            recommendation="Tighten response time on customer follow-ups."),
            WeightedFeature("competitor_pressure", 1.5, 0.0, 1.0, direction=-1),
            WeightedFeature(
                "agent_voice_stress", 1.0, 0.0, 1.0, direction=-1,
                recommendation="Voice strain spotted — coach on calm-voice techniques.",
            ),
        ],
    )


def default_agent_scorer() -> CompositeScorer:
    """Agent performance scorer — complexity-agnostic behavior composite."""
    return CompositeScorer(
        name="agent_performance",
        version="v1",
        intercept=50.0,
        features=[
            WeightedFeature("linguistic_style_match", 2.0, 0.75, 0.15),
            WeightedFeature("patience_sec", 2.0, 0.7, 0.3),
            WeightedFeature("reflection_ratio", 2.5, 1.0, 0.5),
            WeightedFeature("question_rate_per_min", 2.0, 2.5, 1.0),
            WeightedFeature("next_step_specific", 2.5, 0.6, 0.5),
            WeightedFeature("scorecard_score", 3.0, 75.0, 15.0),
            WeightedFeature("filler_rate_per_min", 1.0, 4.0, 2.0, direction=-1,
                            recommendation="Reduce filler words in customer-facing moments."),
            WeightedFeature("interruption_count_total", 1.5, 2.0, 2.0, direction=-1,
                            recommendation="Practice pausing before responding."),
        ],
    )


# ── Input-building helpers — flatten the feature store into scorer dicts ─


def flatten_features_for_sentiment(features: Dict[str, Any]) -> Dict[str, Optional[float]]:
    det = features.get("deterministic", {}) or {}
    llm = features.get("llm_structured", {}) or {}
    traj = llm.get("sentiment_trajectory") or []
    slope = _trajectory_slope([t.get("score") for t in traj])
    end_val = traj[-1]["score"] if traj else None
    para = _paralinguistic_signals(det, tenant_baselines=None)
    return {
        "sentiment_score_llm": llm.get("sentiment_score") or llm.get("sentiment_score_llm"),
        "sentiment_trajectory_slope": slope,
        "sentiment_end_valence": end_val,
        "linguistic_style_match": det.get("linguistic_style_match"),
        "interruption_count_total": det.get("interruption_count_total"),
        "laughter_events": det.get("laughter_events"),
        "agent_voice_stress": para["agent_voice_stress"],
        "agent_monotone": para["agent_monotone"],
    }


def flatten_features_for_churn(
    features: Dict[str, Any],
    contact_rollup: Optional[Dict[str, Any]] = None,
    tenant_baselines: Optional[Dict[str, Any]] = None,
) -> Dict[str, Optional[float]]:
    det = features.get("deterministic", {}) or {}
    llm = features.get("llm_structured", {}) or {}
    traj = llm.get("sentiment_trajectory") or []
    slope = _trajectory_slope([t.get("score") for t in traj])
    churn_language = len(llm.get("churn_risk_factors") or [])
    rollup = contact_rollup or {}
    para = _paralinguistic_signals(det, tenant_baselines=tenant_baselines)
    return {
        "churn_risk_llm": llm.get("churn_risk"),
        "sustain_talk_count": llm.get("sustain_talk_count"),
        "sentiment_trajectory_slope": slope,
        "stakeholder_count": det.get("stakeholder_count"),
        "action_item_completion_rate": rollup.get("action_item_completion_rate"),
        "churn_risk_language": churn_language,
        "competitor_pressure": len(llm.get("competitor_mentions") or []),
        "customer_hot_voice": para["customer_hot_voice"],
        "customer_arousal": para["customer_arousal"],
    }


def _paralinguistic_signals(
    deterministic: Dict[str, Any],
    tenant_baselines: Optional[Dict[str, Any]],
) -> Dict[str, float]:
    """Collapse the paralinguistic block into the 0/1-ish signals the
    scorers consume.

    Three outputs: ``agent_voice_stress`` (0/1 if jitter or shimmer
    clear stress thresholds), ``agent_monotone`` (0/1 if pitch σ is
    below 2.0 semitones), ``customer_hot_voice`` (proportion of how
    far the customer's median intensity sits above the tenant p90 —
    0 when no baseline is available).
    """
    block = (deterministic or {}).get("paralinguistic") or {}
    if not block or not block.get("available"):
        return {
            "agent_voice_stress": 0.0,
            "agent_monotone": 0.0,
            "customer_hot_voice": 0.0,
        }
    per_speaker = block.get("per_speaker") or {}
    # Pick the agent/customer rows by convention: "agent" / "customer"
    # when live-ingest set them, else positional (first = agent is the
    # typical diarization convention when we seed from a live call).
    agent = per_speaker.get("agent") or next(iter(per_speaker.values()), {}) or {}
    customer = per_speaker.get("customer")
    if customer is None and len(per_speaker) > 1:
        # Second speaker in insertion order.
        speaker_ids = list(per_speaker.keys())
        customer = per_speaker.get(speaker_ids[1], {})
    customer = customer or {}

    jitter = agent.get("jitter_local") or 0.0
    shimmer = agent.get("shimmer_local") or 0.0
    stress = 1.0 if (jitter > 0.02 or shimmer > 0.1) else 0.0

    pitch_std = agent.get("pitch_std_semitones")
    monotone = 1.0 if (pitch_std is not None and pitch_std < 2.0) else 0.0

    hot = 0.0
    cust_db = customer.get("intensity_db_p50")
    baseline = (tenant_baselines or {}).get("customer_intensity_db_p90")
    if cust_db is not None and baseline:
        try:
            hot = max(0.0, float(cust_db) - float(baseline))
        except (TypeError, ValueError):
            hot = 0.0

    customer_arousal = 0.0
    arousal = customer.get("arousal")
    if isinstance(arousal, dict) and arousal.get("score") is not None:
        try:
            customer_arousal = float(arousal["score"])
        except (TypeError, ValueError):
            customer_arousal = 0.0

    return {
        "agent_voice_stress": stress,
        "agent_monotone": monotone,
        "customer_hot_voice": hot,
        "customer_arousal": customer_arousal,
    }


def _trajectory_slope(values: List[Optional[float]]) -> Optional[float]:
    """OLS slope of (index, value) pairs.  Returns None for <2 points."""
    clean = [(i, float(v)) for i, v in enumerate(values) if v is not None]
    if len(clean) < 2:
        return None
    mean_x = sum(x for x, _ in clean) / len(clean)
    mean_y = sum(y for _, y in clean) / len(clean)
    num = sum((x - mean_x) * (y - mean_y) for x, y in clean)
    den = sum((x - mean_x) ** 2 for x, _ in clean)
    return round(num / den, 4) if den > 0 else 0.0


# ── Back-compat helpers for the analytics endpoints ──────────────────────


def confidence_from_wilson(successes: int, trials: int) -> float:
    """Cheap confidence estimate from sample size alone."""
    lo, hi = wilson_interval(successes, trials)
    return round(max(0.0, 1.0 - (hi - lo)), 3)
