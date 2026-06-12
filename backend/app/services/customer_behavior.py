"""Customer behavior radar + change-readiness index — Phase 5.

Pure computation. Reads ``customer_signals`` emitted by the analysis LLM
(see ANALYSIS_SYSTEM_PROMPT in ai_analysis.py) plus optional paralinguistic
features, returns a 6-axis radar plus a derived 0-100 readiness score.

Rule-based at launch. Once Phase 0 outcome telemetry accrues, axis weights
can be recalibrated per-tenant against actual conversion / retention without
changing the public surface — same fields, increasing predictive value.

The 6 axes:

- ``commitment`` — frequency of forward-language ("we'll go ahead", "yes")
- ``openness`` — change talk vs sustain talk balance
- ``engagement`` — vocal energy + question rate from the customer side
- ``trust`` — context-sharing and agreement with reframes
- ``decision_urgency`` — timeline language ("by Q2") vs distance language
- ``friction`` — unresolved objection density (inverted on the radar; lower is better)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


# ── Public contract ──────────────────────────────────────────────────────


@dataclass
class BehaviorRadar:
    """6-axis radar of customer behavior. Each axis is bounded [0.0, 1.0]."""

    commitment: float = 0.0
    openness: float = 0.0
    engagement: float = 0.0
    trust: float = 0.0
    decision_urgency: float = 0.0
    friction: float = 0.0  # higher = more unresolved friction

    def as_dict(self) -> Dict[str, float]:
        return asdict(self)


@dataclass
class ChangeReadiness:
    """Single 0-100 readiness score derived from the radar axes."""

    score: int  # 0 - 100
    confidence: str  # 'low' | 'medium' | 'high'
    contributing: Dict[str, float] = field(default_factory=dict)
    # ``contributing`` is the per-axis weighted contribution to the score
    # (signed; friction enters negative). Useful for surfacing the top 2-3
    # reasons in the UI without re-running the math.

    def as_dict(self) -> Dict[str, Any]:
        return {
            "score": self.score,
            "confidence": self.confidence,
            "contributing": self.contributing,
        }


# Default weights for the readiness index.
# Sums to 1.0 across positive axes; friction enters as a penalty.
DEFAULT_WEIGHTS: Dict[str, float] = {
    "commitment": 0.25,
    "openness": 0.25,
    "engagement": 0.10,
    "trust": 0.15,
    "decision_urgency": 0.15,
    "friction": 0.10,  # subtracted, not added
}


# ── Helpers ──────────────────────────────────────────────────────────────


def _saturating(count: int, scale: float = 2.0) -> float:
    """Map a non-negative count to [0.0, 1.0] with diminishing returns.

    Uses a Hill function. ``scale`` controls saturation: count=scale → 0.5,
    count=2*scale → 0.67, count=5*scale → 0.83. Tunable per-axis if certain
    signals are scarcer than others.
    """
    if count <= 0:
        return 0.0
    return count / (count + scale)


def _safe_len(value: Any) -> int:
    if not isinstance(value, list):
        return 0
    return len(value)


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


# ── Radar ────────────────────────────────────────────────────────────────


def compute_behavior_radar(
    customer_signals: Optional[Dict[str, Any]],
    paralinguistics: Optional[Dict[str, Any]] = None,
    transcript_segments: Optional[List[Dict[str, Any]]] = None,
) -> BehaviorRadar:
    """Compute the 6-axis behavior radar.

    Parameters
    ----------
    customer_signals:
        The ``customer_signals`` block from the analysis LLM output.
        Expected shape (all lists; missing keys treated as empty):
        ``{"commitment_language": [...], "change_talk": [...],
        "sustain_talk": [...], "trust_signals": [...],
        "urgency_language": [...], "objections": [{"quote": str, "resolved": bool}, ...]}``
    paralinguistics:
        Optional ``ParalinguisticFeatures.as_dict()`` output. The
        ``per_speaker`` map is used for the engagement axis when present.
    transcript_segments:
        Optional list of transcript segments; used as a fallback for
        the engagement axis (customer question count) when paralinguistics
        is unavailable.

    Returns
    -------
    A ``BehaviorRadar`` with each axis in [0, 1]. Empty inputs return
    a zero radar — no signal is treated as no behavior, not "neutral".
    """
    cs = customer_signals or {}

    commitment = _saturating(_safe_len(cs.get("commitment_language")), scale=2.0)

    change_n = _safe_len(cs.get("change_talk"))
    sustain_n = _safe_len(cs.get("sustain_talk"))
    # Net openness: change-talk drives up, sustain-talk drags down.
    openness = _clamp(
        _saturating(change_n, 2.0) - 0.5 * _saturating(sustain_n, 2.0)
    )

    trust = _saturating(_safe_len(cs.get("trust_signals")), scale=2.0)

    urgency = _saturating(_safe_len(cs.get("urgency_language")), scale=2.0)

    objections = cs.get("objections") or []
    unresolved = [o for o in objections if isinstance(o, dict) and not o.get("resolved", False)]
    friction = _saturating(len(unresolved), scale=2.0)

    engagement = _engagement_axis(cs, paralinguistics, transcript_segments)

    return BehaviorRadar(
        commitment=round(commitment, 3),
        openness=round(openness, 3),
        engagement=round(engagement, 3),
        trust=round(trust, 3),
        decision_urgency=round(urgency, 3),
        friction=round(friction, 3),
    )


def _engagement_axis(
    customer_signals: Dict[str, Any],
    paralinguistics: Optional[Dict[str, Any]],
    transcript_segments: Optional[List[Dict[str, Any]]],
) -> float:
    """Engagement blends vocal energy with question rate.

    Preferred: paralinguistic intensity / pitch variability from the
    customer speaker. Fallback: customer question rate from transcript
    segments. Final fallback: total quote density across all customer
    signal lists.
    """
    # Try paralinguistics first.
    if paralinguistics and paralinguistics.get("available"):
        per_speaker = paralinguistics.get("per_speaker") or {}
        # Pick the speaker that looks most like the customer. Without
        # role labels here, we average across speakers — the radar is
        # for the customer side and most customer interactions have a
        # single dominant non-rep speaker.
        intensities = [
            s.get("intensity_db_p50")
            for s in per_speaker.values()
            if isinstance(s, dict) and s.get("intensity_db_p50") is not None
        ]
        pitch_stds = [
            s.get("pitch_std_semitones")
            for s in per_speaker.values()
            if isinstance(s, dict) and s.get("pitch_std_semitones") is not None
        ]
        if intensities and pitch_stds:
            # Normalize: intensity 50-75 dB → 0..1, pitch_std 1-5 semitones → 0..1.
            mean_intensity = sum(intensities) / len(intensities)
            mean_pitch_std = sum(pitch_stds) / len(pitch_stds)
            intensity_norm = _clamp((mean_intensity - 50.0) / 25.0)
            expressiveness_norm = _clamp((mean_pitch_std - 1.0) / 4.0)
            return _clamp(0.5 * intensity_norm + 0.5 * expressiveness_norm)

    # Transcript fallback: customer question rate.
    if transcript_segments:
        customer_questions = sum(
            1
            for seg in transcript_segments
            if isinstance(seg, dict)
            and (seg.get("speaker") or "").lower() not in {"agent", "rep"}
            and "?" in (seg.get("text") or "")
        )
        if customer_questions > 0:
            return _saturating(customer_questions, scale=3.0)

    # Last resort: total signal density across the customer_signals block.
    total = sum(
        _safe_len(customer_signals.get(k))
        for k in ("commitment_language", "change_talk", "sustain_talk",
                  "trust_signals", "urgency_language")
    )
    total += _safe_len(customer_signals.get("objections"))
    return _saturating(total, scale=8.0)


# ── Change-Readiness Index ───────────────────────────────────────────────


def compute_change_readiness(
    radar: BehaviorRadar,
    weights: Optional[Dict[str, float]] = None,
    signal_density: Optional[int] = None,
) -> ChangeReadiness:
    """Derive a single 0-100 readiness score from the radar axes.

    Parameters
    ----------
    radar:
        Output of ``compute_behavior_radar``.
    weights:
        Optional override of ``DEFAULT_WEIGHTS``. Keys must match radar
        axis names; ``friction`` enters as a penalty regardless of sign.
        Per-tenant calibration (Phase 4) lands here without changing the
        public API.
    signal_density:
        Optional total number of customer-side signal quotes across all
        axes; drives the ``confidence`` band. When omitted, confidence
        defaults to ``"medium"``.
    """
    w = weights or DEFAULT_WEIGHTS

    contributions: Dict[str, float] = {
        "commitment": w.get("commitment", 0.0) * radar.commitment,
        "openness": w.get("openness", 0.0) * radar.openness,
        "engagement": w.get("engagement", 0.0) * radar.engagement,
        "trust": w.get("trust", 0.0) * radar.trust,
        "decision_urgency": w.get("decision_urgency", 0.0) * radar.decision_urgency,
        "friction": -w.get("friction", 0.0) * radar.friction,
    }
    raw = sum(contributions.values())
    score = int(round(100 * _clamp(raw)))

    if signal_density is None:
        confidence = "medium"
    elif signal_density < 5:
        confidence = "low"
    elif signal_density < 15:
        confidence = "medium"
    else:
        confidence = "high"

    return ChangeReadiness(
        score=score,
        confidence=confidence,
        contributing={k: round(v, 4) for k, v in contributions.items()},
    )


def signal_density_from(customer_signals: Optional[Dict[str, Any]]) -> int:
    """Total count of customer-side signal quotes across the block.

    Used by the readiness service to set the confidence band.
    """
    cs = customer_signals or {}
    return (
        _safe_len(cs.get("commitment_language"))
        + _safe_len(cs.get("change_talk"))
        + _safe_len(cs.get("sustain_talk"))
        + _safe_len(cs.get("trust_signals"))
        + _safe_len(cs.get("urgency_language"))
        + _safe_len(cs.get("objections"))
    )
