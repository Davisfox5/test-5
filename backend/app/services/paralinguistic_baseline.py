"""Per-utterance paralinguistic features + speaker-relative outlier detection.

Phase 2 of the scoring roadmap: the existing
``paralinguistics.ParalinguisticExtractor`` produces *aggregate*
per-speaker stats. This module produces *per-utterance* values plus the
speaker's own baseline, so we can flag the moments that deviate
meaningfully from how that speaker normally sounds.

Splits cleanly into three layers so the pure-Python logic stays
testable without parselmouth:

1. ``extract_utterance_features(audio_path, segments)`` — parselmouth
   pass that produces one ``UtteranceFeatures`` per segment.
2. ``compute_baselines(features)`` — speaker → mean + std for each
   feature. Pure.
3. ``notable_utterances(features, baselines)`` — returns
   ``NotableTag`` rows for utterances crossing
   ``Z_NOTABILITY = 1.5`` on at least one feature. Pure.

The notability threshold is **the only limit** on how many tags get
emitted (decision Q6). On a flat call there will be zero; on an
expressive one there can be many. The prompt builder downstream knows
how to render a long list cleanly.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# Bumped only when the threshold or the categorisation changes — tests
# pin the exact value so a stealth tweak gets caught at PR time.
Z_NOTABILITY: float = 1.5

# A speaker needs at least this many utterances before z-scoring against
# their own baseline is meaningful. Below this we skip notable-tag
# emission for that speaker (and the prompt will still get the speaker
# in the structured per-speaker block).
MIN_SPEAKER_UTTERANCES: int = 4

# A feature's std must be strictly positive for z-scoring to be defined.
# ``_z()`` enforces this. Default float-eq-zero protection avoids
# divide-by-zero panics.
_STD_FLOOR: float = 1e-6


@dataclass
class UtteranceFeatures:
    """Per-segment acoustic + temporal features."""

    segment_idx: int
    speaker_id: str
    start: float
    end: float
    pitch_mean: Optional[float] = None  # Hz, None if extraction failed / no voice
    intensity_mean: Optional[float] = None  # dB
    speaking_rate: Optional[float] = None  # words per second
    pause_before: Optional[float] = None  # seconds since the previous segment


@dataclass
class SpeakerBaseline:
    """Aggregate per-speaker mean + std for each feature.

    Std is None when fewer than two utterances were available for that
    speaker (you can't define a deviation from a single point). The z-
    score functions short-circuit on None.
    """

    speaker_id: str
    pitch_mean: Optional[float] = None
    pitch_std: Optional[float] = None
    intensity_mean: Optional[float] = None
    intensity_std: Optional[float] = None
    speaking_rate_mean: Optional[float] = None
    speaking_rate_std: Optional[float] = None
    pause_before_mean: Optional[float] = None
    pause_before_std: Optional[float] = None
    n_utterances: int = 0


@dataclass
class NotableTag:
    """One outlier utterance flagged for inline tagging in the prompt."""

    segment_idx: int
    speaker_id: str
    start: float
    # ``features`` is a list of (feature_name, signed_z) pairs in the
    # order the prompt builder should render them. Sign matters — the
    # LLM cares about ↑ vs ↓ (e.g. "pause-before high" reads very
    # differently from "pause-before low").
    features: List[Tuple[str, float]] = field(default_factory=list)


# ── Layer 1: parselmouth pass ────────────────────────────────────────


def extract_utterance_features(
    audio_path: Optional[str],
    segments: Sequence[Any],
) -> List[UtteranceFeatures]:
    """Compute per-segment pitch/intensity/rate/pause.

    ``segments`` is the same shape as ``backend.app.services.metrics.Segment``
    (start, end, text, speaker_id), but accepts any object that exposes
    those four attributes. Speaking rate and pause-before are computed
    from segment timing + text alone — no audio dependency. Pitch and
    intensity require parselmouth; when it's not available those fields
    stay ``None``.

    The function never raises on extraction failure — every error path
    falls through to ``None`` for the affected feature so a single
    parselmouth slice failing doesn't poison the whole batch.
    """
    if not segments:
        return []
    parselmouth, snd = _open_audio(audio_path)
    out: List[UtteranceFeatures] = []
    sorted_segs = sorted(segments, key=lambda s: float(getattr(s, "start", 0.0)))
    prev_end: Optional[float] = None
    for idx, seg in enumerate(sorted_segs):
        start = float(getattr(seg, "start", 0.0))
        end = float(getattr(seg, "end", 0.0))
        speaker_id = getattr(seg, "speaker_id", None) or "unknown"
        text = getattr(seg, "text", "") or ""
        duration = max(0.0, end - start)
        word_count = len([w for w in text.split() if w.strip()])
        speaking_rate = word_count / duration if duration > 0 else None
        pause_before = (
            max(0.0, start - prev_end) if prev_end is not None else None
        )
        prev_end = end
        feat = UtteranceFeatures(
            segment_idx=idx,
            speaker_id=str(speaker_id),
            start=start,
            end=end,
            speaking_rate=speaking_rate,
            pause_before=pause_before,
        )
        if parselmouth is not None and snd is not None and duration > 0:
            feat.pitch_mean, feat.intensity_mean = _slice_pitch_intensity(
                parselmouth, snd, start, end
            )
        out.append(feat)
    return out


def _open_audio(audio_path: Optional[str]) -> Tuple[Optional[Any], Optional[Any]]:
    """Best-effort audio open. Returns ``(parselmouth_module, Sound)``
    or ``(None, None)`` when parselmouth isn't installed or the file
    can't be read. Caller threads ``None`` through so the rest of the
    pipeline still produces speaking-rate / pause-before features.
    """
    if not audio_path:
        return None, None
    try:
        import parselmouth  # type: ignore
    except ImportError:
        logger.debug("parselmouth not installed; pitch/intensity skipped")
        return None, None
    try:
        snd = parselmouth.Sound(audio_path)
        return parselmouth, snd
    except Exception:
        logger.exception("parselmouth Sound() failed for %s", audio_path)
        return None, None


def _slice_pitch_intensity(
    parselmouth: Any, snd: Any, start: float, end: float
) -> Tuple[Optional[float], Optional[float]]:
    """Compute per-slice pitch + intensity means.

    Praat returns 0 for unvoiced frames; we filter those out before
    averaging so the mean reflects the speaker's voiced pitch, not a
    slope toward zero from silence.
    """
    pitch_mean: Optional[float] = None
    intensity_mean: Optional[float] = None
    try:
        sliced = snd.extract_part(
            from_time=start, to_time=end, preserve_times=True
        )
    except Exception:
        return (None, None)
    try:
        pitch_arr = sliced.to_pitch().selected_array["frequency"]
        voiced = [float(v) for v in pitch_arr if v and v > 0]
        if voiced:
            pitch_mean = sum(voiced) / len(voiced)
    except Exception:
        pitch_mean = None
    try:
        intensity_arr = sliced.to_intensity().values.T.flatten()
        nonzero = [float(v) for v in intensity_arr if v and v > 0]
        if nonzero:
            intensity_mean = sum(nonzero) / len(nonzero)
    except Exception:
        intensity_mean = None
    return (pitch_mean, intensity_mean)


# ── Layer 2: per-speaker baselines (pure) ────────────────────────────


def compute_baselines(
    features: Sequence[UtteranceFeatures],
) -> Dict[str, SpeakerBaseline]:
    """Aggregate ``features`` into per-speaker mean + std for each feature."""
    by_speaker: Dict[str, List[UtteranceFeatures]] = {}
    for f in features:
        by_speaker.setdefault(f.speaker_id, []).append(f)

    out: Dict[str, SpeakerBaseline] = {}
    for speaker, items in by_speaker.items():
        baseline = SpeakerBaseline(
            speaker_id=speaker, n_utterances=len(items)
        )
        baseline.pitch_mean, baseline.pitch_std = _mean_std(
            [i.pitch_mean for i in items]
        )
        baseline.intensity_mean, baseline.intensity_std = _mean_std(
            [i.intensity_mean for i in items]
        )
        baseline.speaking_rate_mean, baseline.speaking_rate_std = _mean_std(
            [i.speaking_rate for i in items]
        )
        baseline.pause_before_mean, baseline.pause_before_std = _mean_std(
            [i.pause_before for i in items]
        )
        out[speaker] = baseline
    return out


def _mean_std(
    values: Sequence[Optional[float]],
) -> Tuple[Optional[float], Optional[float]]:
    """Mean + sample std over the non-None values.

    Sample std (ddof=1) is the right choice for a speaker baseline:
    we're estimating from a sample of utterances, not enumerating a
    finite population. Returns ``(None, None)`` when fewer than two
    values are present (mean from one point is fine, but std isn't).
    """
    clean = [v for v in values if v is not None and not _isnan(v)]
    if not clean:
        return None, None
    mean = sum(clean) / len(clean)
    if len(clean) < 2:
        return mean, None
    var = sum((v - mean) ** 2 for v in clean) / (len(clean) - 1)
    return mean, math.sqrt(var)


def _isnan(v: float) -> bool:
    try:
        return math.isnan(v)
    except (TypeError, ValueError):
        return False


# ── Layer 3: notable utterance detection (pure) ──────────────────────


# Feature names the notable-tag list emits, in render order. Owned
# here so prompt-builder + tests stay coupled to the same spelling.
FEATURE_RENDER_ORDER: Tuple[str, ...] = (
    "pitch",
    "intensity",
    "speaking_rate",
    "pause_before",
)


def _z(value: Optional[float], mean: Optional[float], std: Optional[float]) -> Optional[float]:
    """Two-sided z. Returns None when value, mean, or std is missing
    or std is below the float floor (degenerate baseline)."""
    if value is None or mean is None or std is None:
        return None
    if std < _STD_FLOOR:
        return None
    return (value - mean) / std


def notable_utterances(
    features: Sequence[UtteranceFeatures],
    baselines: Dict[str, SpeakerBaseline],
    threshold: float = Z_NOTABILITY,
) -> List[NotableTag]:
    """Pick out utterances whose z-score on any tracked feature crosses
    the threshold against the same speaker's own baseline.

    No hard cap on the output list — z >= 1.5 is the only limit. On a
    flat call this returns []; on a noisy call it can return many.
    Tests pin both extremes.
    """
    tags: List[NotableTag] = []
    for f in features:
        baseline = baselines.get(f.speaker_id)
        if baseline is None or baseline.n_utterances < MIN_SPEAKER_UTTERANCES:
            continue
        zs: List[Tuple[str, float]] = []
        pairs = [
            ("pitch", f.pitch_mean, baseline.pitch_mean, baseline.pitch_std),
            ("intensity", f.intensity_mean, baseline.intensity_mean, baseline.intensity_std),
            (
                "speaking_rate",
                f.speaking_rate,
                baseline.speaking_rate_mean,
                baseline.speaking_rate_std,
            ),
            (
                "pause_before",
                f.pause_before,
                baseline.pause_before_mean,
                baseline.pause_before_std,
            ),
        ]
        for name, value, mean, std in pairs:
            z = _z(value, mean, std)
            if z is None:
                continue
            if abs(z) >= threshold:
                zs.append((name, round(z, 2)))
        if zs:
            # Render order: keep canonical so prompt output is stable
            # across calls.
            ordered = sorted(zs, key=lambda kv: FEATURE_RENDER_ORDER.index(kv[0]))
            tags.append(
                NotableTag(
                    segment_idx=f.segment_idx,
                    speaker_id=f.speaker_id,
                    start=f.start,
                    features=ordered,
                )
            )
    return tags


# ── Convenience facade ────────────────────────────────────────────────


def analyze(
    audio_path: Optional[str], segments: Sequence[Any]
) -> Tuple[
    List[UtteranceFeatures],
    Dict[str, SpeakerBaseline],
    List[NotableTag],
]:
    """One-shot: features → baselines → notables."""
    features = extract_utterance_features(audio_path, segments)
    baselines = compute_baselines(features)
    tags = notable_utterances(features, baselines)
    return features, baselines, tags