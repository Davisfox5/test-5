"""Shared audio utilities for telephony integrations.

This package is a Stream 0 deliverable — every telephony integration
stream (SIPREC, UC vendor APIs, Teams, AudioHook) imports the format
normalizer here so the downstream transcription pipeline always
receives PCM16 8 kHz or μ-law 8 kHz, regardless of which provider
delivered the audio. Frozen after Stream 0 lands; do not modify in
the per-stream PRs.
"""

from backend.app.services.audio.normalizer import (
    AudioFormat,
    detect_format,
    to_mulaw_8k,
    to_pcm16_8k,
)

__all__ = [
    "AudioFormat",
    "detect_format",
    "to_mulaw_8k",
    "to_pcm16_8k",
]
