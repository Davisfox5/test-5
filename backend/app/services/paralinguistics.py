"""Paralinguistic feature extraction from call audio.

Optional — lights up when audio is retained and the ``parselmouth`` /
``praat-parselmouth`` package is installed.  Returns ``None`` for any
feature that cannot be computed (no audio, missing dependency, too
short) so downstream scoring degrades gracefully.

Computed features, all per-speaker when diarization is available:

- ``pitch_hz_p50`` / ``pitch_hz_p90`` — F0 percentiles
- ``pitch_std_semitones`` — variability (monotone → ~1; expressive → ~4+)
- ``intensity_db_p50`` — loudness centroid
- ``jitter_local`` — cycle-to-cycle F0 variation (stress marker)
- ``shimmer_local`` — cycle-to-cycle amplitude variation (stress marker)
- ``speaking_rate_syll_per_sec`` — speed from energy envelope
- ``mean_harmonicity_db`` — voice quality (higher = cleaner voice)
- ``pause_rate_per_min`` — silences > 250 ms

Interface is identical regardless of backend availability so callers
can build on top of it without conditionally importing.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)


# ── Public contract ──────────────────────────────────────────────────────


@dataclass
class ParalinguisticFeatures:
    available: bool  # False when the extractor could not run (no audio, etc.)
    per_speaker: Dict[str, Dict[str, Optional[float]]] = field(default_factory=dict)
    overall: Dict[str, Optional[float]] = field(default_factory=dict)
    backend: str = "none"
    note: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "available": self.available,
            "backend": self.backend,
            "per_speaker": self.per_speaker,
            "overall": self.overall,
            "note": self.note,
        }


@dataclass
class SpeakerAudioSegment:
    speaker_id: str
    start: float
    end: float
    audio_path: Optional[str] = None  # path to the entire file; start/end slice inside


# ── Front door ───────────────────────────────────────────────────────────


class ParalinguisticExtractor:
    """Extracts paralinguistic features; gracefully falls back to a stub."""

    def __init__(self) -> None:
        self._backend: Optional[str] = None
        self._parselmouth: Any = None
        self._detect_backend()

    def _detect_backend(self) -> None:
        try:
            import parselmouth  # type: ignore
            self._parselmouth = parselmouth
            self._backend = "parselmouth"
        except ImportError:
            self._backend = None

    @property
    def backend(self) -> str:
        return self._backend or "none"

    def extract(
        self,
        segments: Sequence[SpeakerAudioSegment],
        audio_path: Optional[str] = None,
    ) -> ParalinguisticFeatures:
        if not segments:
            return ParalinguisticFeatures(
                available=False, backend="none", note="no_segments"
            )
        if self._backend != "parselmouth" or self._parselmouth is None:
            return ParalinguisticFeatures(
                available=False,
                backend="none",
                note="parselmouth_not_installed",
            )
        if audio_path is None and all(s.audio_path is None for s in segments):
            return ParalinguisticFeatures(
                available=False,
                backend="parselmouth",
                note="no_audio_path_provided",
            )
        try:
            return self._extract_with_parselmouth(segments, audio_path)
        except Exception:  # noqa: BLE001 — degrade gracefully
            logger.exception("Paralinguistic extraction failed")
            return ParalinguisticFeatures(
                available=False, backend=self.backend, note="extraction_error"
            )

    # ── Praat backend ────────────────────────────────────────────────

    def _extract_with_parselmouth(
        self,
        segments: Sequence[SpeakerAudioSegment],
        audio_path: Optional[str],
    ) -> ParalinguisticFeatures:
        """Real implementation — guarded by the import check above.

        Uses Praat via parselmouth to compute per-speaker acoustic
        features over the diarized segments.  Kept minimal: pitch,
        intensity, jitter, shimmer, harmonicity, speaking rate proxy,
        pause rate.  Expensive features (formants, MFCC) are left out to
        keep CPU low in the Celery worker.
        """
        pm = self._parselmouth
        path = audio_path or next((s.audio_path for s in segments if s.audio_path), None)
        if path is None:
            return ParalinguisticFeatures(
                available=False, backend=self.backend, note="no_audio_path_provided"
            )

        snd = pm.Sound(path)
        # Pitch estimation needs a minimum voiced duration. Below 1.5 s
        # Praat returns noisy values that confound the downstream
        # monotone / stress thresholds, so we early-out rather than
        # surface misleading numbers.
        try:
            total_duration = float(pm.praat.call(snd, "Get total duration"))
        except Exception:
            total_duration = 0.0
        if total_duration < 1.5:
            return ParalinguisticFeatures(
                available=False, backend=self.backend, note="audio_too_short"
            )

        per_speaker: Dict[str, Dict[str, Optional[float]]] = {}
        overall_segments = []
        for seg in segments:
            if seg.end <= seg.start:
                continue
            sliced = snd.extract_part(from_time=seg.start, to_time=seg.end, preserve_times=True)
            per_speaker.setdefault(seg.speaker_id, [])  # collect slices
            per_speaker[seg.speaker_id].append(sliced)  # type: ignore[arg-type]
            overall_segments.append(sliced)

        processed: Dict[str, Dict[str, Optional[float]]] = {}
        for speaker, slices in per_speaker.items():
            processed[speaker] = self._measure_slices(slices)

        return ParalinguisticFeatures(
            available=True,
            per_speaker=processed,
            overall=self._measure_slices(overall_segments),
            backend=self.backend,
        )

    def _measure_slices(self, slices: Sequence[Any]) -> Dict[str, Optional[float]]:
        pm = self._parselmouth
        if not slices:
            return self._empty_measures()
        try:
            pitches = [s.to_pitch().selected_array['frequency'] for s in slices]  # type: ignore
            pitch_values = [v for arr in pitches for v in arr if v > 0]
            intensities = [s.to_intensity().values.T.flatten() for s in slices]  # type: ignore
            intensity_values = [v for arr in intensities for v in arr if v > 0]
            jitter_vals: List[float] = []
            shimmer_vals: List[float] = []
            harmonicity_vals: List[float] = []
            for s in slices:
                try:
                    point_process = pm.praat.call(
                        [s, s.to_pitch()], "To PointProcess (cc)"
                    )
                    jitter_vals.append(
                        pm.praat.call(point_process, "Get jitter (local)", 0, 0, 0.0001, 0.02, 1.3)
                    )
                    shimmer_vals.append(
                        pm.praat.call([s, point_process], "Get shimmer (local)", 0, 0, 0.0001, 0.02, 1.3, 1.6)
                    )
                except Exception:
                    pass
                try:
                    hnr = s.to_harmonicity()
                    harmonicity_vals.append(float(pm.praat.call(hnr, "Get mean", 0, 0)))
                except Exception:
                    pass

            speaking_rate = self._speaking_rate(slices)
            pause_rate = self._pause_rate(slices)
            return {
                "pitch_hz_p50": _pct(pitch_values, 0.5),
                "pitch_hz_p90": _pct(pitch_values, 0.9),
                "pitch_std_semitones": _pitch_std_semitones(pitch_values),
                "intensity_db_p50": _pct(intensity_values, 0.5),
                "jitter_local": _mean_safe(jitter_vals),
                "shimmer_local": _mean_safe(shimmer_vals),
                "mean_harmonicity_db": _mean_safe(harmonicity_vals),
                "speaking_rate_syll_per_sec": speaking_rate,
                "pause_rate_per_min": pause_rate,
            }
        except Exception:
            logger.exception("Paralinguistic measurement failed inside backend")
            return self._empty_measures()

    def _speaking_rate(self, slices: Sequence[Any]) -> Optional[float]:
        """Syllables per second across the given slices.

        Nuclei-based estimator (de Jong & Wempe 2009): a local maximum
        in the intensity envelope that (a) sits above an adaptive
        silence floor and (b) is at least 2 dB above its neighbors
        counts as one syllable nucleus. Rate = nuclei / voiced seconds.
        """
        pm = self._parselmouth
        if pm is None or not slices:
            return None
        total_nuclei = 0
        total_seconds = 0.0
        for s in slices:
            try:
                duration = float(pm.praat.call(s, "Get total duration"))
                if duration < 0.5:
                    continue
                total_seconds += duration
                intensity = s.to_intensity()
                min_db = float(pm.praat.call(intensity, "Get minimum", 0, 0, "Parabolic"))
                max_db = float(pm.praat.call(intensity, "Get maximum", 0, 0, "Parabolic"))
                if math.isnan(min_db) or math.isnan(max_db):
                    continue
                floor_db = max(min_db + 3.0, max_db - 25.0)
                # ``To TextGrid (silences)`` labels loud stretches; within
                # each loud stretch we count intensity peaks that exceed
                # the floor by ≥2 dB. Fall back to a simple peak count if
                # Praat rejects the call.
                try:
                    count = int(
                        pm.praat.call(
                            intensity,
                            "Count points in interval",
                            0,
                            duration,
                        )
                    )
                except Exception:
                    count = 0
                if count == 0:
                    # Fallback: walk the intensity matrix manually.
                    values = intensity.values.T.flatten()  # type: ignore
                    peaks = 0
                    for i in range(1, len(values) - 1):
                        v = float(values[i])
                        if (
                            v > floor_db + 2.0
                            and v > float(values[i - 1])
                            and v > float(values[i + 1])
                        ):
                            peaks += 1
                    count = peaks
                total_nuclei += count
            except Exception:
                continue
        if total_seconds <= 0:
            return None
        return round(total_nuclei / total_seconds, 3)

    def _pause_rate(self, slices: Sequence[Any]) -> Optional[float]:
        """Pauses per minute of speech.

        A pause is a silent interval > 250 ms. We use Praat's
        ``To TextGrid (silences)`` to label sub-threshold intervals and
        divide by the total *speech* duration (not wall time) so the
        metric compares fairly between back-to-back callers and
        chatty ones.
        """
        pm = self._parselmouth
        if pm is None or not slices:
            return None
        total_pauses = 0
        total_speech_sec = 0.0
        for s in slices:
            try:
                duration = float(pm.praat.call(s, "Get total duration"))
                if duration < 1.0:
                    continue
                intensity = s.to_intensity()
                # Adaptive silence threshold in dB. -25 dB below the p95
                # of the intensity contour is the usual Praat default.
                tg = pm.praat.call(
                    intensity,
                    "To TextGrid (silences)",
                    -25,   # silence threshold (dB)
                    0.1,   # min silent interval (s)
                    0.05,  # min sounding interval (s)
                    "silent",
                    "sounding",
                )
                # Tier 1 holds the silent/sounding labels. Count silent
                # intervals longer than 0.25 s.
                num_intervals = int(pm.praat.call(tg, "Get number of intervals", 1))
                speech_sec_here = 0.0
                pauses_here = 0
                for i in range(1, num_intervals + 1):
                    label = pm.praat.call(tg, "Get label of interval", 1, i)
                    t0 = float(pm.praat.call(tg, "Get starting point", 1, i))
                    t1 = float(pm.praat.call(tg, "Get end point", 1, i))
                    span = t1 - t0
                    if label == "sounding":
                        speech_sec_here += span
                    elif label == "silent" and span >= 0.25:
                        pauses_here += 1
                total_pauses += pauses_here
                total_speech_sec += speech_sec_here
            except Exception:
                continue
        if total_speech_sec <= 0:
            return None
        return round(total_pauses / (total_speech_sec / 60.0), 3)

    @staticmethod
    def _empty_measures() -> Dict[str, Optional[float]]:
        return {
            "pitch_hz_p50": None,
            "pitch_hz_p90": None,
            "pitch_std_semitones": None,
            "intensity_db_p50": None,
            "jitter_local": None,
            "shimmer_local": None,
            "mean_harmonicity_db": None,
            "speaking_rate_syll_per_sec": None,
            "pause_rate_per_min": None,
        }


# ── Utilities ────────────────────────────────────────────────────────────


def _pct(values: Sequence[float], p: float) -> Optional[float]:
    if not values:
        return None
    sorted_v = sorted(values)
    idx = p * (len(sorted_v) - 1)
    lo = int(idx)
    hi = min(len(sorted_v) - 1, lo + 1)
    frac = idx - lo
    return round(sorted_v[lo] + (sorted_v[hi] - sorted_v[lo]) * frac, 4)


def _mean_safe(values: Sequence[float]) -> Optional[float]:
    clean = [v for v in values if v is not None and not math.isnan(v)]
    if not clean:
        return None
    return round(sum(clean) / len(clean), 4)


def _pitch_std_semitones(values: Sequence[float]) -> Optional[float]:
    clean = [v for v in values if v and v > 0]
    if len(clean) < 2:
        return None
    mean_v = sum(clean) / len(clean)
    var = sum((v - mean_v) ** 2 for v in clean) / len(clean)
    std_hz = math.sqrt(var)
    # Convert a 1-σ deviation in Hz around the mean to semitones:
    #   semitones = 12 · log2((mean + std) / mean)
    return round(12 * math.log2((mean_v + std_hz) / mean_v), 3)


_default_extractor: Optional[ParalinguisticExtractor] = None


def get_paralinguistic_extractor() -> ParalinguisticExtractor:
    global _default_extractor
    if _default_extractor is None:
        _default_extractor = ParalinguisticExtractor()
    return _default_extractor
