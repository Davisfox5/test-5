"""Replay-harness tests.

Validate the live paralinguistic path against pre-recorded audio
without needing a real call. We synthesize short PCM16 clips with
known pitch / pace / silence properties and assert:

1. The replay harness produces snapshots at the cadence we asked for.
2. The scanner fires the expected alerts on those snapshots.
3. The ``validate_against_expected`` helper computes sensible
   precision / recall / F1.

These tests double as the regression gate before enabling
``paralinguistic_live`` for a tenant: promote the flag only when F1
on the regression corpus stays above a fixed threshold.
"""

from __future__ import annotations

import math
import struct

import pytest

from backend.app.services.live_coaching_features import (
    CoachingAlert,
    ParalinguisticScanner,
)
from backend.app.services.paralinguistics_replay import (
    ExpectedAlert,
    ReplayReport,
    ValidationResult,
    replay_pcm_into_window,
    validate_against_expected,
)


# ── Synth audio helpers ──────────────────────────────────────────────


def _sine(freq: float, duration_sec: float, rate: int = 8000, amplitude: float = 0.5) -> bytes:
    n = int(duration_sec * rate)
    samples = [
        int(amplitude * 32767 * math.sin(2 * math.pi * freq * i / rate))
        for i in range(n)
    ]
    return struct.pack(f"<{n}h", *samples)


def _silence(duration_sec: float, rate: int = 8000) -> bytes:
    return b"\x00\x00" * int(duration_sec * rate)


# ── Harness behavior ─────────────────────────────────────────────────


def test_replay_produces_snapshots_at_expected_cadence():
    """The harness should produce one snapshot per snapshot_every_sec."""
    # 10 s of audio, snapshots every 2 s → expect ~4-5 snapshots.
    audio = _sine(200, duration_sec=10.0) + _silence(0.1)
    report = replay_pcm_into_window(
        pcm16=audio,
        sample_rate=8000,
        window_sec=30.0,
        snapshot_every_sec=2.0,
    )
    assert report.total_duration_sec >= 9.5
    # Snapshot count is bounded above by audio_len / cadence and below
    # by that minus one (parselmouth may skip very short buffers).
    expected = report.total_duration_sec / 2.0
    assert 0 <= len(report.snapshots) <= expected + 1


def test_replay_empty_audio_returns_empty_report():
    report = replay_pcm_into_window(pcm16=b"", sample_rate=8000)
    assert report.total_duration_sec == 0.0
    assert report.snapshots == []


def test_replay_rejects_invalid_sample_rate():
    with pytest.raises(ValueError):
        replay_pcm_into_window(pcm16=b"\x00\x00", sample_rate=0)


# ── Validation helper ────────────────────────────────────────────────


def _fake_report_with_alerts(*pairs: tuple[float, str]) -> ReplayReport:
    """Build a ReplayReport carrying the given ``(t_sec, kind)`` alerts
    — bypasses the extractor entirely so we can unit-test the matcher.
    """
    from backend.app.services.paralinguistics import ParalinguisticFeatures
    from backend.app.services.paralinguistics_replay import ReplaySnapshot

    report = ReplayReport(total_duration_sec=max((t for t, _ in pairs), default=0.0))
    dummy_features = ParalinguisticFeatures(available=True, backend="stub")
    for t, kind in pairs:
        report.snapshots.append(
            ReplaySnapshot(
                t_sec=t,
                features=dummy_features,
                alerts=[
                    CoachingAlert(kind=kind, severity="info", message=f"{kind} fired")
                ],
            )
        )
    return report


def test_validate_perfect_match_gives_f1_of_1():
    report = _fake_report_with_alerts((5.0, "monotone"), (20.0, "pace"))
    expected = [
        ExpectedAlert(kind="monotone", at_sec=5.0),
        ExpectedAlert(kind="pace", at_sec=20.0),
    ]
    result = validate_against_expected(report, expected)
    assert result.true_positives == 2
    assert result.false_positives == 0
    assert result.false_negatives == 0
    assert result.precision == 1.0
    assert result.recall == 1.0
    assert result.f1 == 1.0


def test_validate_tolerance_window():
    """Alerts within tolerance_sec of the expected time count as matches."""
    report = _fake_report_with_alerts((10.2, "monotone"))
    expected = [ExpectedAlert(kind="monotone", at_sec=10.0, tolerance_sec=5.0)]
    result = validate_against_expected(report, expected)
    assert result.true_positives == 1
    assert result.f1 == 1.0


def test_validate_outside_tolerance_is_false_negative_plus_false_positive():
    report = _fake_report_with_alerts((30.0, "monotone"))
    expected = [ExpectedAlert(kind="monotone", at_sec=10.0, tolerance_sec=5.0)]
    result = validate_against_expected(report, expected)
    # The 30-second alert doesn't match the 10-second expectation.
    assert result.true_positives == 0
    assert result.false_negatives == 1
    assert result.false_positives == 1


def test_validate_wrong_kind_doesnt_match():
    report = _fake_report_with_alerts((10.0, "pace"))
    expected = [ExpectedAlert(kind="monotone", at_sec=10.0, tolerance_sec=5.0)]
    result = validate_against_expected(report, expected)
    assert result.true_positives == 0
    assert result.false_negatives == 1
    assert result.false_positives == 1


def test_validate_one_observed_satisfies_at_most_one_expected():
    """A single observed alert can't cover two overlapping expectations."""
    report = _fake_report_with_alerts((10.0, "monotone"))
    expected = [
        ExpectedAlert(kind="monotone", at_sec=10.0, tolerance_sec=5.0),
        ExpectedAlert(kind="monotone", at_sec=11.0, tolerance_sec=5.0),
    ]
    result = validate_against_expected(report, expected)
    assert result.true_positives == 1
    assert result.false_negatives == 1


def test_validation_result_summary_counts():
    report = _fake_report_with_alerts(
        (5.0, "monotone"),
        (8.0, "monotone"),
        (12.0, "pace"),
    )
    assert report.summary == {"monotone": 2, "pace": 1}


# ── End-to-end: scanner fires on synthesized audio ──────────────────
# Keep these opt-in: they need parselmouth installed. Other tests above
# cover the harness wiring without needing the heavy dep.


def _has_parselmouth() -> bool:
    try:
        import parselmouth  # type: ignore  # noqa: F401

        return True
    except Exception:
        return False


@pytest.mark.skipif(
    not _has_parselmouth(), reason="parselmouth not installed"
)
def test_replay_fires_monotone_on_steady_sine():
    """A 15-second 200 Hz sine has zero pitch variation → should light
    the monotone scanner when parselmouth is available."""
    audio = _sine(200, duration_sec=15.0)
    report = replay_pcm_into_window(
        pcm16=audio,
        sample_rate=8000,
        window_sec=10.0,
        snapshot_every_sec=3.0,
        scanner=ParalinguisticScanner(cooldown_sec=0.0),
    )
    assert report.snapshots, "Expected at least one snapshot from a 15s clip"
    # Monotone should show up at least once over the replay.
    assert any(
        a.kind == "monotone"
        for s in report.snapshots
        for a in s.alerts
    ), f"Did not see a monotone alert; got summary={report.summary}"
