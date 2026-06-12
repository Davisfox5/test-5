"""Tests for the per-utterance paralinguistic baseline + outlier module.

Pins the z-score logic and the notability threshold so a stealth
change to the cutoff is caught at PR time. The pure-Python layers
(``compute_baselines``, ``notable_utterances``) are exercised with
hand-built ``UtteranceFeatures`` lists so the tests don't need
parselmouth or an audio fixture.
"""

from __future__ import annotations

import math

import pytest

from backend.app.services.paralinguistic_baseline import (
    FEATURE_RENDER_ORDER,
    MIN_SPEAKER_UTTERANCES,
    SpeakerBaseline,
    UtteranceFeatures,
    Z_NOTABILITY,
    _mean_std,
    _z,
    compute_baselines,
    notable_utterances,
)


def _utt(
    idx: int,
    speaker: str,
    pitch: float | None = None,
    intensity: float | None = None,
    rate: float | None = None,
    pause: float | None = None,
    start: float | None = None,
) -> UtteranceFeatures:
    return UtteranceFeatures(
        segment_idx=idx,
        speaker_id=speaker,
        start=start if start is not None else float(idx),
        end=(start if start is not None else float(idx)) + 1.0,
        pitch_mean=pitch,
        intensity_mean=intensity,
        speaking_rate=rate,
        pause_before=pause,
    )


# ── pure-math layer ──────────────────────────────────────────────────


def test_z_notability_threshold_pinned():
    """Phase 2 plan locks z >= 1.5 as the notability threshold."""
    assert Z_NOTABILITY == 1.5


def test_min_speaker_utterances_pinned():
    """Speakers with too few utterances skip notable-tag emission."""
    assert MIN_SPEAKER_UTTERANCES == 4


def test_feature_render_order_canonical():
    assert FEATURE_RENDER_ORDER == (
        "pitch",
        "intensity",
        "speaking_rate",
        "pause_before",
    )


def test_mean_std_handles_empty_and_single():
    assert _mean_std([]) == (None, None)
    assert _mean_std([None]) == (None, None)
    mean, std = _mean_std([5.0])
    assert mean == 5.0 and std is None
    mean, std = _mean_std([1.0, 3.0, 5.0])
    assert mean == 3.0
    # Sample std of [1, 3, 5] is 2.0 (var = 4 with ddof=1).
    assert std == pytest.approx(2.0)


def test_mean_std_skips_none_and_nan():
    mean, std = _mean_std([1.0, None, float("nan"), 3.0])
    assert mean == 2.0
    assert std == pytest.approx(math.sqrt(2.0))


def test_z_returns_none_on_degenerate_inputs():
    assert _z(None, 5.0, 1.0) is None
    assert _z(5.0, None, 1.0) is None
    assert _z(5.0, 5.0, None) is None
    # Std of zero is the divide-by-zero floor; should also fail safely.
    assert _z(5.0, 5.0, 0.0) is None


def test_z_simple_value():
    assert _z(7.0, 5.0, 1.0) == 2.0
    assert _z(3.0, 5.0, 1.0) == -2.0


# ── compute_baselines ────────────────────────────────────────────────


def test_compute_baselines_groups_by_speaker():
    feats = [
        _utt(0, "rep", pitch=120.0, rate=3.0),
        _utt(1, "customer", pitch=180.0, rate=4.0),
        _utt(2, "rep", pitch=140.0, rate=3.4),
        _utt(3, "customer", pitch=200.0, rate=4.4),
    ]
    baselines = compute_baselines(feats)
    assert set(baselines.keys()) == {"rep", "customer"}
    assert baselines["rep"].n_utterances == 2
    assert baselines["rep"].pitch_mean == 130.0
    assert baselines["customer"].pitch_mean == 190.0
    assert baselines["customer"].speaking_rate_mean == pytest.approx(4.2)


def test_compute_baselines_handles_all_none_feature():
    feats = [
        _utt(0, "rep", intensity=None),
        _utt(1, "rep", intensity=None),
    ]
    bl = compute_baselines(feats)
    assert bl["rep"].intensity_mean is None
    assert bl["rep"].intensity_std is None


# ── notable_utterances ───────────────────────────────────────────────


def _calm_baseline(speaker: str = "rep") -> tuple[
    list[UtteranceFeatures], dict[str, SpeakerBaseline]
]:
    """8 utterances all at the same nominal value — std ~= 0 on every
    feature, so any later perturbation z-scores high.

    Using a flat baseline keeps the test math simple: a single value
    deviating from the all-120 baseline z-scores at +∞ on the natural
    formula, but our ``_z`` helper short-circuits when std < ``_STD_FLOOR``
    so flat baselines emit no tags. Tests that need outliers add their
    perturbation BEFORE computing the baseline.
    """
    feats = [
        _utt(i, speaker, pitch=120.0, intensity=70.0, rate=3.0, pause=0.5)
        for i in range(8)
    ]
    return feats, compute_baselines(feats)


def test_notable_utterances_empty_when_baseline_flat():
    feats, baselines = _calm_baseline()
    # Flat baseline → std = 0 on every feature → ``_z`` returns None
    # for every comparison → empty tag list.
    tags = notable_utterances(feats, baselines)
    assert tags == []


def test_notable_utterances_flags_pitch_outlier():
    """Mix one true outlier with seven calm utterances; the recomputed
    baseline should still flag the outlier (its absolute deviation
    dominates the small inflated std)."""
    feats = [
        _utt(i, "rep", pitch=120.0, intensity=70.0, rate=3.0, pause=0.5)
        for i in range(7)
    ]
    feats.append(
        _utt(8, "rep", pitch=180.0, intensity=70.0, rate=3.0, pause=0.5),
    )
    baselines = compute_baselines(feats)
    tags = notable_utterances(feats, baselines)
    # The outlier dominates; expect at least one tag and it should
    # name pitch as the crossed feature.
    assert any(t.segment_idx == 8 for t in tags)
    target = next(t for t in tags if t.segment_idx == 8)
    assert any(name == "pitch" for name, _ in target.features)


def test_notable_utterances_skips_speakers_with_too_few_utterances():
    """A speaker who only appears 2 times shouldn't get tags even if
    one utterance is wildly out of band (no real baseline yet)."""
    feats = [
        _utt(0, "customer", pitch=80.0),
        _utt(1, "customer", pitch=300.0),  # huge jump but n=2
    ] + [_utt(i + 2, "rep", pitch=120.0) for i in range(8)]
    baselines = compute_baselines(feats)
    tags = notable_utterances(feats, baselines)
    customer_tags = [t for t in tags if t.speaker_id == "customer"]
    assert customer_tags == []


def test_notable_utterances_renders_canonical_feature_order():
    """Outlier on pitch + rate + pause (not intensity) — render order
    must follow FEATURE_RENDER_ORDER, not the order they crossed."""
    feats = [
        _utt(
            i, "rep",
            pitch=120.0 + (i % 3) * 0.5,
            intensity=70.0,
            rate=3.0 + (i % 3) * 0.1,
            pause=0.4 + (i % 3) * 0.05,
        )
        for i in range(7)
    ]
    feats.append(
        _utt(8, "rep", pitch=180.0, intensity=70.0, rate=6.0, pause=2.5),
    )
    bl = compute_baselines(feats)
    tags = notable_utterances(feats, bl)
    target = next(t for t in tags if t.segment_idx == 8)
    names = [n for n, _ in target.features]
    # Whatever subset crossed must appear in FEATURE_RENDER_ORDER order.
    assert names == [n for n in FEATURE_RENDER_ORDER if n in names]


def test_notable_utterances_no_hard_cap():
    """Q6 (user override): no hard cap on tag count. If many
    utterances cross the threshold against an externally-provided
    baseline, all of them come back. We supply the baseline directly
    here so the test isolates the cap behaviour from the
    self-flattening that happens when outliers join the baseline."""
    # Externally-built baseline: pitch_mean=120, pitch_std=10. Eight
    # baseline utterances feed the n_utterances gate; the actual mean
    # + std are what we hand in via ``baselines``.
    baseline = SpeakerBaseline(
        speaker_id="rep",
        pitch_mean=120.0,
        pitch_std=10.0,
        intensity_mean=70.0,
        intensity_std=0.5,
        speaking_rate_mean=3.0,
        speaking_rate_std=0.1,
        pause_before_mean=0.5,
        pause_before_std=0.05,
        n_utterances=10,
    )
    # 30 utterances, every one at pitch=180 (z = +6.0 against the
    # supplied baseline).
    feats = [_utt(i, "rep", pitch=180.0) for i in range(30)]
    tags = notable_utterances(feats, {"rep": baseline})
    assert len(tags) == 30
    for t in tags:
        names = [n for n, _ in t.features]
        assert "pitch" in names
