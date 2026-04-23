"""Tests for the paralinguistic extractor.

Since the test environment rarely has praat-parselmouth installed, the
tests focus on the graceful-degradation contract and the pure helpers.
"""

import pytest

from backend.app.services.paralinguistics import (
    ParalinguisticExtractor,
    ParalinguisticFeatures,
    SpeakerAudioSegment,
    _mean_safe,
    _pct,
    _pitch_std_semitones,
)


def test_extractor_returns_unavailable_when_no_segments():
    result = ParalinguisticExtractor().extract([])
    assert result.available is False
    assert result.note == "no_segments"


def test_extractor_degrades_without_parselmouth_or_audio():
    result = ParalinguisticExtractor().extract(
        [SpeakerAudioSegment(speaker_id="agent", start=0.0, end=1.0)]
    )
    # Either backend missing → unavailable; or backend present but no
    # audio path → unavailable.  Either way `available` must be False.
    assert result.available is False


def test_pct_returns_linear_interpolated_percentile():
    assert _pct([1.0, 2.0, 3.0, 4.0], 0.5) == pytest.approx(2.5)


def test_pct_handles_empty_input():
    assert _pct([], 0.5) is None


def test_mean_safe_ignores_none_values():
    assert _mean_safe([1.0, None, 3.0]) == pytest.approx(2.0)


def test_mean_safe_returns_none_on_empty():
    assert _mean_safe([]) is None
    assert _mean_safe([None, None]) is None


def test_pitch_std_semitones_none_for_insufficient_data():
    assert _pitch_std_semitones([200.0]) is None


def test_pitch_std_semitones_positive_for_varied_pitch():
    # Values spanning roughly one octave (200, 250, 300, 400) produce
    # a non-trivial semitone spread.
    out = _pitch_std_semitones([200.0, 250.0, 300.0, 400.0])
    assert out is not None and out > 0


def test_features_dataclass_default_shape():
    feat = ParalinguisticFeatures(available=False)
    assert feat.backend == "none"
    assert feat.per_speaker == {}
