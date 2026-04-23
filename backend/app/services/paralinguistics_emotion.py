"""Emotion + arousal classification on top of paralinguistic features.

Two public helpers:

1. :func:`compute_arousal` — deterministic. Takes a per-speaker feature
   dict (already produced by :class:`ParalinguisticExtractor`) and
   returns a scalar arousal score in ``[0.0, 1.0]`` plus a coarse
   label (``calm`` / ``neutral`` / ``elevated`` / ``agitated``).
   Zero new dependencies. High arousal = fast speech + varied pitch
   + loud + jittery/shimmery voice; low arousal = monotone + slow +
   quiet + clean voice. Scores are normalized so the axis is stable
   across tenants regardless of baseline loudness.

2. :func:`classify_emotion` — optional. If ``speechbrain`` (with its
   ``emotion-recognition-wav2vec2-IEMOCAP`` checkpoint) is installed,
   runs a per-speaker-segment pass and returns an emotion label
   (``happy`` / ``sad`` / ``angry`` / ``neutral``) with a confidence
   score. Without the dependency the function short-circuits — we
   never download 1 GB of weights on a whim.

Both helpers slot into the existing ``ParalinguisticFeatures`` shape:
we inject ``arousal`` and (when available) ``emotion`` subkeys into
each ``per_speaker`` dict and the ``overall`` dict. Scorers that want
either signal read them through :mod:`score_engine` exactly like any
other feature.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence

logger = logging.getLogger(__name__)


# ── Deterministic arousal ────────────────────────────────────────────


@dataclass
class ArousalResult:
    score: float  # 0.0 calm .. 1.0 agitated
    label: str   # calm | neutral | elevated | agitated

    def as_dict(self) -> Dict[str, Any]:
        return {"score": round(self.score, 3), "label": self.label}


# Reference points for each axis — chosen from the same call-center
# corpus that seeded the scanner thresholds. These are coarse on
# purpose: we want the score to move for obvious emotional swings
# (loud/fast/jittery speech) without being dominated by one noisy
# feature. Scale each axis to 0..1 independently, then average.
_PITCH_STD_CALM = 1.0    # semitones — near-monotone
_PITCH_STD_AGITATED = 6.0

_INTENSITY_CALM_DB = 50.0
_INTENSITY_AGITATED_DB = 80.0

_RATE_CALM = 1.5
_RATE_AGITATED = 5.0

_JITTER_CALM = 0.005
_JITTER_AGITATED = 0.04

_SHIMMER_CALM = 0.03
_SHIMMER_AGITATED = 0.15


def compute_arousal(features: Dict[str, Any]) -> Optional[ArousalResult]:
    """Return an arousal score for one speaker's feature dict, or None
    if too few inputs are present to form a reasonable estimate.

    ``features`` is the per-speaker payload from
    :class:`ParalinguisticExtractor` — e.g. ``{"pitch_std_semitones":
    3.1, "intensity_db_p50": 68, ...}``. Missing keys are ignored;
    axes we can't score simply don't contribute to the average.
    """
    pieces: list[float] = []

    pitch_std = features.get("pitch_std_semitones")
    if pitch_std is not None:
        pieces.append(_scale(pitch_std, _PITCH_STD_CALM, _PITCH_STD_AGITATED))

    intensity = features.get("intensity_db_p50")
    if intensity is not None:
        pieces.append(_scale(intensity, _INTENSITY_CALM_DB, _INTENSITY_AGITATED_DB))

    rate = features.get("speaking_rate_syll_per_sec")
    if rate is not None:
        pieces.append(_scale(rate, _RATE_CALM, _RATE_AGITATED))

    jitter = features.get("jitter_local")
    if jitter is not None:
        pieces.append(_scale(jitter, _JITTER_CALM, _JITTER_AGITATED))

    shimmer = features.get("shimmer_local")
    if shimmer is not None:
        pieces.append(_scale(shimmer, _SHIMMER_CALM, _SHIMMER_AGITATED))

    # Need at least two axes to be meaningful — one axis alone is too
    # noisy (e.g. a quiet line could look "calm" even if the speaker
    # is angry but distant from the mic).
    if len(pieces) < 2:
        return None

    score = sum(pieces) / len(pieces)
    score = max(0.0, min(1.0, score))
    return ArousalResult(score=score, label=_arousal_label(score))


def _scale(value: float, low: float, high: float) -> float:
    """Linearly map ``value`` from ``[low, high]`` to ``[0, 1]``,
    clamping at each end. Works symmetrically when low < high or
    low > high (for features that decrease with arousal — not used
    here but handy to keep the call sites uniform)."""
    if high == low:
        return 0.5
    raw = (value - low) / (high - low)
    return max(0.0, min(1.0, raw))


def _arousal_label(score: float) -> str:
    if score < 0.25:
        return "calm"
    if score < 0.5:
        return "neutral"
    if score < 0.75:
        return "elevated"
    return "agitated"


def annotate_arousal(para_block: Dict[str, Any]) -> Dict[str, Any]:
    """Attach ``arousal`` to every speaker entry + the overall entry
    inside a paralinguistic ``as_dict()`` block.

    Returns the block for chaining. No-op when the block is missing
    or ``available=False``.
    """
    if not para_block or not para_block.get("available"):
        return para_block
    per_speaker = para_block.get("per_speaker") or {}
    for sid, feats in list(per_speaker.items()):
        if not isinstance(feats, dict):
            continue
        result = compute_arousal(feats)
        if result is not None:
            feats["arousal"] = result.as_dict()
    overall = para_block.get("overall") or {}
    if isinstance(overall, dict):
        result = compute_arousal(overall)
        if result is not None:
            overall["arousal"] = result.as_dict()
    return para_block


# ── Optional SpeechBrain emotion ─────────────────────────────────────


@dataclass
class EmotionResult:
    label: str
    confidence: float

    def as_dict(self) -> Dict[str, Any]:
        return {"label": self.label, "confidence": round(self.confidence, 3)}


# Process-level cache for the SpeechBrain classifier — model load is
# ~1 GB and takes several seconds. Celery workers reuse it across
# tasks. ``None`` = tried and failed (library missing or model fetch
# blocked); the helper short-circuits on subsequent calls.
_emotion_classifier: Any = None
_emotion_classifier_loaded = False


def _get_emotion_classifier() -> Any:
    global _emotion_classifier, _emotion_classifier_loaded
    if _emotion_classifier_loaded:
        return _emotion_classifier
    _emotion_classifier_loaded = True
    try:
        from speechbrain.inference.interfaces import foreign_class  # type: ignore
    except Exception:
        logger.info(
            "speechbrain not installed; emotion classification disabled. "
            "Install with: pip install speechbrain"
        )
        return None
    try:
        _emotion_classifier = foreign_class(
            source="speechbrain/emotion-recognition-wav2vec2-IEMOCAP",
            pymodule_file="custom_interface.py",
            classname="CustomEncoderWav2vec2Classifier",
        )
    except Exception:
        logger.exception("Failed to load SpeechBrain emotion classifier")
        _emotion_classifier = None
    return _emotion_classifier


def classify_emotion(audio_path: str) -> Optional[EmotionResult]:
    """Classify the dominant emotion on the given audio file.

    Returns ``None`` when SpeechBrain isn't installed, the model
    couldn't be loaded, or classification fails. Labels come from the
    IEMOCAP corpus: ``neu`` (neutral), ``hap`` (happy), ``sad``,
    ``ang`` (angry) — we pass them through verbatim so callers can
    decide how to render.
    """
    classifier = _get_emotion_classifier()
    if classifier is None:
        return None
    try:
        out = classifier.classify_file(audio_path)
        # SpeechBrain returns (out_prob, score, index, text_lab).
        # ``score`` is the softmax probability of the top class;
        # ``text_lab`` is a list containing the label string.
        score = float(out[1])
        label = str(out[3][0]) if out[3] else "neu"
        return EmotionResult(label=label, confidence=score)
    except Exception:
        logger.exception("Emotion classification failed for %s", audio_path)
        return None


def annotate_emotion(
    para_block: Dict[str, Any],
    segment_paths: Sequence[tuple[str, str]],
) -> Dict[str, Any]:
    """Run emotion classification on per-speaker audio segments and
    attach the results to ``para_block``.

    ``segment_paths`` is ``[(speaker_id, audio_path), ...]`` — callers
    produce per-speaker audio by either concatenating diarized slices
    or using a single whole-call file when diarization isn't available.
    Each speaker gets one emotion label + confidence; duplicate entries
    for the same speaker update in place (last wins). No-op when
    SpeechBrain isn't installed.
    """
    if not para_block or not para_block.get("available") or not segment_paths:
        return para_block
    classifier = _get_emotion_classifier()
    if classifier is None:
        return para_block
    per_speaker = para_block.get("per_speaker") or {}
    for speaker_id, audio_path in segment_paths:
        result = classify_emotion(audio_path)
        if result is None:
            continue
        entry = per_speaker.setdefault(speaker_id, {})
        entry["emotion"] = result.as_dict()
    return para_block


__all__ = [
    "ArousalResult",
    "EmotionResult",
    "compute_arousal",
    "annotate_arousal",
    "classify_emotion",
    "annotate_emotion",
]
