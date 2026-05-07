"""Tests for ``backend.app.services.audio.normalizer``.

Stream 0 deliverable. Every telephony integration stream depends on
this module producing μ-law 8 kHz or PCM16 8 kHz no matter what the
source vendor delivers. These tests pin the contract so a regression
in normalizer.py breaks every downstream stream, not just one.
"""

from __future__ import annotations

import audioop
import struct

import pytest

from backend.app.services.audio.normalizer import (
    AudioFormat,
    detect_format,
    to_mulaw_8k,
    to_pcm16_8k,
)


# ── Fixture builders (no external files) ────────────────────────────────


def _pcm16_sine(duration_sec: float, sample_rate: int, freq_hz: float = 440.0) -> bytes:
    """Generate a PCM16 mono sine-wave buffer at the requested rate.

    Pure-stdlib so the test doesn't depend on numpy or scipy.
    """

    import math

    n_samples = int(duration_sec * sample_rate)
    samples = bytearray()
    for i in range(n_samples):
        # Stay safely under int16 max to avoid clipping artifacts in
        # the ratecv stage.
        v = int(20000 * math.sin(2 * math.pi * freq_hz * i / sample_rate))
        samples += struct.pack("<h", v)
    return bytes(samples)


# ── detect_format ───────────────────────────────────────────────────────


def test_detect_wav_by_magic():
    payload = b"RIFF" + b"\x00" * 36 + b"data" + b"\x00" * 8
    assert detect_format(payload) == AudioFormat.WAV


def test_detect_flac_by_magic():
    assert detect_format(b"fLaC" + b"\x00" * 8) == AudioFormat.FLAC


def test_detect_mp3_by_id3_tag():
    assert detect_format(b"ID3\x03\x00\x00\x00" + b"\x00" * 8) == AudioFormat.MP3


def test_detect_mp3_by_sync_word():
    # 0xFFFB is a common MPEG-1 layer III frame header start.
    assert detect_format(b"\xff\xfb\x90\x00" + b"\x00" * 8) == AudioFormat.MP3


def test_detect_ogg_opus():
    assert detect_format(b"OggS" + b"\x00" * 8) == AudioFormat.OPUS_48K


def test_detect_with_hint_falls_back_when_no_magic():
    # Raw PCM has no magic bytes; the caller must hint.
    raw = _pcm16_sine(0.01, 16000)
    assert detect_format(raw, hint="pcm16_16k") == AudioFormat.PCM16_16K


def test_detect_rejects_empty():
    with pytest.raises(ValueError, match="empty"):
        detect_format(b"")


def test_detect_rejects_unhintable_unknown():
    # Random bytes, no hint → must error rather than guess.
    with pytest.raises(ValueError, match="hint"):
        detect_format(b"\x01\x02\x03\x04\x05")


def test_detect_rejects_bad_hint():
    with pytest.raises(ValueError, match="not a known"):
        detect_format(b"\x01\x02\x03\x04", hint="nonsense_99k")


# ── to_pcm16_8k ─────────────────────────────────────────────────────────


def test_pcm16_8k_passthrough():
    src = _pcm16_sine(0.05, 8000)
    out = to_pcm16_8k(src, AudioFormat.PCM16_8K)
    assert out == src


def test_pcm16_16k_downsamples_to_8k():
    src = _pcm16_sine(0.5, 16000)
    out = to_pcm16_8k(src, AudioFormat.PCM16_16K)
    # 16k → 8k halves the sample count; ratecv may round, so allow ±1.
    assert abs(len(out) - len(src) // 2) <= 4


def test_pcm16_48k_downsamples_to_8k():
    src = _pcm16_sine(0.1, 48000)
    out = to_pcm16_8k(src, AudioFormat.PCM16_48K)
    assert abs(len(out) - len(src) // 6) <= 8


def test_mulaw_8k_decodes_to_pcm16_8k():
    pcm_src = _pcm16_sine(0.05, 8000)
    mulaw = audioop.lin2ulaw(pcm_src, 2)
    out = to_pcm16_8k(mulaw, AudioFormat.MULAW_8K)
    # Round-trip through μ-law is lossy but length must double
    # (μ-law is 8-bit; PCM16 is 16-bit).
    assert len(out) == len(mulaw) * 2


def test_alaw_8k_decodes_to_pcm16_8k():
    pcm_src = _pcm16_sine(0.05, 8000)
    alaw = audioop.lin2alaw(pcm_src, 2)
    out = to_pcm16_8k(alaw, AudioFormat.ALAW_8K)
    assert len(out) == len(alaw) * 2


def test_opus_raises_not_implemented():
    # Opus support is deliberately deferred from Stream 0; streams
    # that need it must coordinate via the plan doc.
    with pytest.raises(NotImplementedError, match="Opus"):
        to_pcm16_8k(b"\x00" * 100, AudioFormat.OPUS_48K)


# ── to_mulaw_8k ─────────────────────────────────────────────────────────


def test_mulaw_8k_from_pcm16_8k():
    src = _pcm16_sine(0.05, 8000)
    mu = to_mulaw_8k(src, AudioFormat.PCM16_8K)
    # 16-bit → 8-bit: half the bytes.
    assert len(mu) == len(src) // 2


def test_mulaw_8k_passthrough_via_pcm():
    pcm_src = _pcm16_sine(0.05, 8000)
    mu_src = audioop.lin2ulaw(pcm_src, 2)
    # μ-law-in → μ-law-out; lossy round-trip via PCM16 in the middle
    # but length must match.
    mu_out = to_mulaw_8k(mu_src, AudioFormat.MULAW_8K)
    assert len(mu_out) == len(mu_src)


def test_mulaw_8k_from_pcm16_16k_downsamples_then_encodes():
    src = _pcm16_sine(0.5, 16000)
    mu = to_mulaw_8k(src, AudioFormat.PCM16_16K)
    # 16k PCM16 → 8k μ-law: input bytes / 4 (half rate, half width).
    assert abs(len(mu) - len(src) // 4) <= 4


# ── TelephonyProvider Literal sanity ────────────────────────────────────


def test_telephony_provider_literal_is_exhaustive():
    """If a stream silently adds a provider string elsewhere without
    extending the Literal, this test won't catch it (Literal is a
    type-checker thing, not a runtime constraint). The point of this
    test is to ensure the contract is *imported* and stays alive — if
    someone deletes the Literal, this fails."""

    from backend.app.services.telephony import TelephonyProvider  # noqa: F401

    # We don't introspect Literal contents at runtime in 3.9 cleanly
    # without typing.get_args; do a lightweight check instead.
    import typing

    args = typing.get_args(TelephonyProvider)
    expected = {
        "twilio",
        "signalwire",
        "telnyx",
        "siprec_cisco_cube",
        "siprec_avaya_sbce",
        "siprec_metaswitch",
        "ringcentral",
        "webex_calling",
        "zoom_phone",
        "teams_compliance",
        "genesys_audiohook",
    }
    assert set(args) == expected, (
        "TelephonyProvider Literal drifted from the plan-doc namespace. "
        "If you intentionally added a provider, update the test to match."
    )
