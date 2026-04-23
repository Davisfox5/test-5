"""Tests for the live paralinguistic window + scanner.

Focus on the parts we can cover without parselmouth installed:

* ``LiveParalinguisticWindow`` trims its buffer by ``window_sec``.
* ``LiveParalinguisticWindow.maybe_snapshot`` respects ``recompute_every_sec``.
* ``ParalinguisticScanner`` fires the right alerts for monotone / pace /
  stress / silence, and respects its cooldown.
* Scorer flatten helpers surface the three paralinguistic signals with
  the right sign.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

from backend.app.services.live_coaching_features import (
    CoachingAlert,
    ParalinguisticScanner,
)
from backend.app.services.paralinguistics import ParalinguisticFeatures
from backend.app.services.paralinguistics_live import LiveParalinguisticWindow
from backend.app.services.score_engine import (
    flatten_features_for_churn,
    flatten_features_for_sentiment,
)


# ── Window buffering ───────────────────────────────────────────────────


def test_window_trims_old_audio_by_window_sec():
    w = LiveParalinguisticWindow(window_sec=0.1, recompute_every_sec=10.0)
    w.feed(b"\xff" * 160)
    time.sleep(0.15)
    w.feed(b"\xff" * 160)
    # Only the most recent chunk should remain in the buffer.
    assert len(w._chunks) == 1


def test_window_skips_snapshot_when_recompute_interval_not_elapsed():
    w = LiveParalinguisticWindow(window_sec=60.0, recompute_every_sec=10.0)
    w.feed(b"\xff" * 160)
    # First call resets the internal last-snapshot timer by taking one;
    # in practice the result is None because the buffer is too short.
    first = w.maybe_snapshot()
    second = w.maybe_snapshot()
    # Second call must be rate-limited regardless of what the first did.
    assert second is None
    # First call is fine either way (None on short buffer, or a features
    # object if parselmouth happens to be installed).
    assert first is None or hasattr(first, "available")


# ── Scanner ────────────────────────────────────────────────────────────


def _features(**agent):
    return ParalinguisticFeatures(
        available=True,
        backend="parselmouth",
        per_speaker={"agent": dict(agent)},
        overall=dict(agent),
    )


def test_scanner_emits_monotone_for_low_pitch_std():
    scanner = ParalinguisticScanner()
    alerts = scanner.push(_features(pitch_std_semitones=1.2))
    kinds = [a.kind for a in alerts]
    assert "monotone" in kinds


def test_scanner_emits_pace_for_fast_rate():
    scanner = ParalinguisticScanner()
    alerts = scanner.push(_features(speaking_rate_syll_per_sec=5.5))
    assert any(a.kind == "pace" for a in alerts)


def test_scanner_emits_stress_for_high_jitter():
    scanner = ParalinguisticScanner()
    alerts = scanner.push(_features(jitter_local=0.05, shimmer_local=0.05))
    assert any(a.kind == "stress" for a in alerts)


def test_scanner_emits_silence_for_high_pause_rate():
    scanner = ParalinguisticScanner()
    alerts = scanner.push(_features(pause_rate_per_min=12.0))
    assert any(a.kind == "silence" for a in alerts)


def test_scanner_respects_cooldown_per_kind():
    scanner = ParalinguisticScanner(cooldown_sec=60.0)
    feats = _features(pitch_std_semitones=1.0)
    first = scanner.push(feats)
    second = scanner.push(feats)
    assert any(a.kind == "monotone" for a in first)
    assert all(a.kind != "monotone" for a in second)


def test_scanner_no_alerts_when_not_available():
    scanner = ParalinguisticScanner()
    not_available = ParalinguisticFeatures(
        available=False, backend="none", per_speaker={}, overall={}
    )
    assert scanner.push(not_available) == []


# ── Scorer flatten helpers ─────────────────────────────────────────────


def _paralinguistic_det(**agent) -> dict:
    return {
        "deterministic": {
            "paralinguistic": {
                "available": True,
                "backend": "parselmouth",
                "per_speaker": {"agent": dict(agent)},
                "overall": dict(agent),
            }
        },
        "llm_structured": {},
    }


def test_sentiment_flatten_surfaces_agent_voice_stress():
    feats = _paralinguistic_det(jitter_local=0.05, shimmer_local=0.05)
    flat = flatten_features_for_sentiment(feats)
    assert flat["agent_voice_stress"] == 1.0
    assert flat["agent_monotone"] == 0.0


def test_sentiment_flatten_surfaces_monotone_for_flat_pitch():
    feats = _paralinguistic_det(pitch_std_semitones=1.0)
    flat = flatten_features_for_sentiment(feats)
    assert flat["agent_monotone"] == 1.0


def test_churn_flatten_computes_customer_hot_voice_from_baseline():
    feats = {
        "deterministic": {
            "paralinguistic": {
                "available": True,
                "backend": "parselmouth",
                "per_speaker": {
                    "agent": {"pitch_std_semitones": 3.0},
                    "customer": {"intensity_db_p50": 82.0},
                },
                "overall": {},
            }
        },
        "llm_structured": {},
    }
    flat = flatten_features_for_churn(
        feats, tenant_baselines={"customer_intensity_db_p90": 70.0}
    )
    # 82 - 70 = 12 dB over the tenant's loudness baseline.
    assert flat["customer_hot_voice"] == 12.0


def test_churn_flatten_zero_hot_voice_without_baseline():
    feats = {
        "deterministic": {
            "paralinguistic": {
                "available": True,
                "backend": "parselmouth",
                "per_speaker": {
                    "agent": {},
                    "customer": {"intensity_db_p50": 82.0},
                },
                "overall": {},
            }
        },
        "llm_structured": {},
    }
    flat = flatten_features_for_churn(feats, tenant_baselines=None)
    assert flat["customer_hot_voice"] == 0.0


def test_flatten_safe_when_paralinguistic_unavailable():
    feats = {
        "deterministic": {
            "paralinguistic": {"available": False, "backend": "none"}
        },
        "llm_structured": {},
    }
    flat = flatten_features_for_sentiment(feats)
    assert flat["agent_voice_stress"] == 0.0
    assert flat["agent_monotone"] == 0.0
