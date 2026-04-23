"""End-to-end SpeechBrain emotion classification tests.

Skipped unless ``speechbrain`` is installed *and* the model is
available locally (either already downloaded or reachable from
Huggingface). Production workers should pre-warm the model via
``paralinguistics_emotion.prefetch_emotion_classifier`` during boot.

Two gates:

1. Library presence — ``speechbrain`` import succeeds.
2. Model reachability — ``prefetch_emotion_classifier`` returns True
   with a short budget so CI doesn't hang on a 1 GB download.
"""

from __future__ import annotations

import math
import os
import struct
import tempfile
import threading
import time
import wave

import pytest


def _speechbrain_available() -> bool:
    try:
        import speechbrain  # type: ignore  # noqa: F401
        return True
    except Exception:
        return False


def _model_available(timeout_sec: float = 120.0) -> bool:
    """Return True if the emotion model loads within the budget.

    Runs the prefetch in a background thread so a stuck HF download
    doesn't hang CI indefinitely. The module-level cache means a later
    successful load still works — we only give up *this attempt*.
    """
    if not _speechbrain_available():
        return False
    # Honor an explicit env override for offline CI runs.
    if os.environ.get("LINDA_EMOTION_TESTS_DISABLED"):
        return False

    from backend.app.services.paralinguistics_emotion import (
        prefetch_emotion_classifier,
        reset_emotion_classifier_cache,
    )

    reset_emotion_classifier_cache()

    done = threading.Event()
    result = {"ok": False}

    def _loader() -> None:
        try:
            result["ok"] = bool(prefetch_emotion_classifier())
        finally:
            done.set()

    t = threading.Thread(target=_loader, daemon=True)
    t.start()
    done.wait(timeout=timeout_sec)
    return result["ok"]


# Evaluate gates once at collection time so skipif has something to
# consult. A slow model fetch therefore costs at most ``timeout_sec``
# during test collection, not per test.
_HAS_EMOTION_MODEL = _model_available()


def _synthesize_wav(
    path: str, freq: float, duration: float = 3.0, rate: int = 16000
) -> None:
    n = int(rate * duration)
    samples = [
        int(0.3 * 32767 * math.sin(2 * math.pi * freq * i / rate))
        for i in range(n)
    ]
    pcm = struct.pack(f"<{n}h", *samples)
    with wave.open(path, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)


@pytest.mark.skipif(
    not _HAS_EMOTION_MODEL,
    reason="SpeechBrain emotion model not available (library missing or download timeout)",
)
def test_classify_emotion_returns_plausible_label_and_confidence():
    """Round-trip: synthesize a WAV, run classify_emotion, expect one
    of the IEMOCAP labels + a real-valued confidence."""
    from backend.app.services.paralinguistics_emotion import classify_emotion

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        path = tmp.name
    try:
        _synthesize_wav(path, freq=220.0)
        t0 = time.time()
        result = classify_emotion(path)
        elapsed = time.time() - t0

        assert result is not None, "classify_emotion returned None despite model being available"
        assert result.label in {"neu", "hap", "sad", "ang"}
        assert 0.0 <= result.confidence <= 1.0
        # Inference on a 3 s clip should take well under a minute even
        # on CPU. Anything longer is a regression.
        assert elapsed < 60.0, f"inference too slow: {elapsed:.1f}s"
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass


@pytest.mark.skipif(
    not _HAS_EMOTION_MODEL,
    reason="SpeechBrain emotion model not available",
)
def test_annotate_emotion_populates_per_speaker_entry():
    """``annotate_emotion`` should set the ``emotion`` subkey on each
    speaker entry when the classifier is available."""
    from backend.app.services.paralinguistics_emotion import annotate_emotion

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        path = tmp.name
    try:
        _synthesize_wav(path, freq=220.0)
        block = {
            "available": True,
            "backend": "parselmouth",
            "per_speaker": {"agent": {"pitch_std_semitones": 2.0}},
            "overall": {},
        }
        out = annotate_emotion(block, [("agent", path)])
        assert "emotion" in out["per_speaker"]["agent"]
        emo = out["per_speaker"]["agent"]["emotion"]
        assert emo["label"] in {"neu", "hap", "sad", "ang"}
        assert 0.0 <= emo["confidence"] <= 1.0
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass
