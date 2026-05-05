"""Tests for the customer behavior radar + change-readiness service."""

from __future__ import annotations

import pytest

from backend.app.services.customer_behavior import (
    BehaviorRadar,
    ChangeReadiness,
    DEFAULT_WEIGHTS,
    _saturating,
    compute_behavior_radar,
    compute_change_readiness,
    signal_density_from,
)


# ── _saturating ──────────────────────────────────────────────────────────


def test_saturating_zero_returns_zero():
    assert _saturating(0) == 0.0


def test_saturating_negative_returns_zero():
    assert _saturating(-3) == 0.0


def test_saturating_at_scale_returns_half():
    assert _saturating(2, scale=2.0) == pytest.approx(0.5)


def test_saturating_diminishing_returns():
    assert _saturating(2) < _saturating(4) < _saturating(8) < _saturating(16) < 1.0


def test_saturating_never_reaches_one():
    # Even at huge counts, output stays below 1.
    assert _saturating(10_000, scale=2.0) < 1.0


# ── compute_behavior_radar ───────────────────────────────────────────────


def test_radar_empty_inputs_returns_zeros():
    radar = compute_behavior_radar(None)
    assert radar.commitment == 0.0
    assert radar.openness == 0.0
    assert radar.engagement == 0.0
    assert radar.trust == 0.0
    assert radar.decision_urgency == 0.0
    assert radar.friction == 0.0


def test_radar_empty_dict_returns_zeros():
    radar = compute_behavior_radar({})
    assert radar.as_dict() == BehaviorRadar().as_dict()


def test_radar_commitment_axis_scales_with_signal_count():
    a = compute_behavior_radar({"commitment_language": ["yes"]})
    b = compute_behavior_radar({"commitment_language": ["yes", "let's go"]})
    c = compute_behavior_radar(
        {"commitment_language": ["yes", "let's go", "sign me up", "we're in", "perfect"]}
    )
    assert 0 < a.commitment < b.commitment < c.commitment < 1.0


def test_radar_openness_net_change_minus_sustain():
    only_change = compute_behavior_radar({"change_talk": ["I want to fix this"] * 4})
    only_sustain = compute_behavior_radar({"sustain_talk": ["we're fine as-is"] * 4})
    mixed = compute_behavior_radar(
        {
            "change_talk": ["I want to fix this"] * 4,
            "sustain_talk": ["we're fine as-is"] * 4,
        }
    )
    assert only_change.openness > 0.0
    assert only_sustain.openness == 0.0  # sustain alone drags openness toward zero, clamped
    assert mixed.openness < only_change.openness  # sustain drags down


def test_radar_friction_only_unresolved_objections():
    radar = compute_behavior_radar(
        {
            "objections": [
                {"quote": "your pricing is steep", "resolved": False},
                {"quote": "we tried a competitor", "resolved": True},
                {"quote": "what about onboarding speed", "resolved": True},
            ]
        }
    )
    # Two resolved + one unresolved → friction reflects only the one unresolved.
    expected = _saturating(1, scale=2.0)
    assert radar.friction == pytest.approx(expected, rel=1e-3)


def test_radar_engagement_uses_paralinguistics_when_available():
    paraling = {
        "available": True,
        "per_speaker": {
            "customer": {
                "intensity_db_p50": 65.0,    # mid-range loud → ~0.6 normalized
                "pitch_std_semitones": 3.0,  # expressive → 0.5 normalized
            }
        },
    }
    radar = compute_behavior_radar({}, paralinguistics=paraling)
    assert radar.engagement > 0.4
    assert radar.engagement < 1.0


def test_radar_engagement_falls_back_to_transcript_questions():
    segments = [
        {"speaker": "customer", "text": "What's the timeline?"},
        {"speaker": "customer", "text": "And the price?"},
        {"speaker": "agent", "text": "Glad you asked..."},
    ]
    radar = compute_behavior_radar({}, transcript_segments=segments)
    # Two customer questions → non-zero engagement, no paralinguistics.
    assert 0 < radar.engagement < 1.0


def test_radar_engagement_zero_when_no_signals_at_all():
    radar = compute_behavior_radar(None, paralinguistics=None, transcript_segments=None)
    assert radar.engagement == 0.0


# ── compute_change_readiness ─────────────────────────────────────────────


def test_readiness_zero_radar_returns_zero():
    readiness = compute_change_readiness(BehaviorRadar())
    assert readiness.score == 0


def test_readiness_high_friction_drags_score_down():
    # All positive axes maxed out, friction zero.
    high = BehaviorRadar(
        commitment=1.0, openness=1.0, engagement=1.0,
        trust=1.0, decision_urgency=1.0, friction=0.0,
    )
    # Same positives, but friction maxed.
    high_friction = BehaviorRadar(
        commitment=1.0, openness=1.0, engagement=1.0,
        trust=1.0, decision_urgency=1.0, friction=1.0,
    )
    assert compute_change_readiness(high).score > compute_change_readiness(high_friction).score


def test_readiness_score_bounded_0_100():
    # Even with extreme weights, score must stay in [0, 100].
    extreme_high = BehaviorRadar(
        commitment=1.0, openness=1.0, engagement=1.0,
        trust=1.0, decision_urgency=1.0, friction=0.0,
    )
    score = compute_change_readiness(extreme_high).score
    assert 0 <= score <= 100


def test_readiness_confidence_bands():
    radar = BehaviorRadar(commitment=0.5)
    assert compute_change_readiness(radar, signal_density=2).confidence == "low"
    assert compute_change_readiness(radar, signal_density=10).confidence == "medium"
    assert compute_change_readiness(radar, signal_density=20).confidence == "high"


def test_readiness_confidence_default_when_density_missing():
    readiness = compute_change_readiness(BehaviorRadar(commitment=0.5))
    assert readiness.confidence == "medium"


def test_readiness_contributing_factors_present():
    radar = BehaviorRadar(commitment=0.5, openness=0.5, friction=0.2)
    readiness = compute_change_readiness(radar)
    assert "commitment" in readiness.contributing
    assert "friction" in readiness.contributing
    # Friction enters as negative contribution.
    assert readiness.contributing["friction"] < 0


def test_readiness_custom_weights_respected():
    radar = BehaviorRadar(commitment=1.0)
    # Default weight on commitment is 0.25 → score ~25.
    default_score = compute_change_readiness(radar).score
    # Bump commitment weight to 1.0; everything else zero.
    custom_weights = {**DEFAULT_WEIGHTS, "commitment": 1.0}
    bumped_score = compute_change_readiness(radar, weights=custom_weights).score
    assert bumped_score > default_score


# ── signal_density_from ──────────────────────────────────────────────────


def test_signal_density_from_empty():
    assert signal_density_from(None) == 0
    assert signal_density_from({}) == 0


def test_signal_density_sums_all_lists():
    cs = {
        "commitment_language": ["a", "b"],
        "change_talk": ["c"],
        "sustain_talk": [],
        "trust_signals": ["d", "e", "f"],
        "urgency_language": ["g"],
        "objections": [{"quote": "h", "resolved": True}],
    }
    assert signal_density_from(cs) == 8


def test_signal_density_ignores_non_list_values():
    cs = {"commitment_language": "not-a-list", "change_talk": ["a"]}
    assert signal_density_from(cs) == 1
