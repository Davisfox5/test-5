"""Pure-Python audio codec helpers.

Narrow surface — just the bits we need for live Media Streams ingest:

* :func:`ulaw_to_pcm16` — decode G.711 μ-law bytes to 16-bit signed PCM.
* :func:`resample_pcm16` — linear-interpolation resample PCM16 mono
  between arbitrary sample rates.
* :func:`pcm16_to_mono` — collapse interleaved stereo PCM16 to mono
  by averaging channels.

Why we own this: the stdlib ``audioop`` module is removed in Python
3.13. Rather than pin the community ``audioop-lts`` package we ship a
small pure-Python replacement so the runtime stays fork-free and the
3.13 migration is only a matter of bumping the Python version.

Correctness: the μ-law table is the standard G.711 ITU table. The
resampler is the same linear-interp approach audioop used. For our
use case (voice band, 8 kHz live telephony, down-stream analysis at
8-16 kHz) linear interpolation is indistinguishable from fancier
resamplers.
"""

from __future__ import annotations

import struct
from typing import List

# ── μ-law decode ────────────────────────────────────────────────────


def _build_ulaw_table() -> List[int]:
    """Build the ITU-T G.711 μ-law → 16-bit linear lookup table.

    Reference: Rec. G.711 §Table 2a. 255 ≈ silence, 0 ≈ full-scale
    negative. The inverse (encoding) isn't needed on our side — we
    only *decode* provider audio for analysis.
    """
    BIAS = 0x84  # 132
    CLIP = 32635
    table: List[int] = []
    for byte in range(256):
        val = ~byte & 0xFF
        sign = val & 0x80
        exponent = (val >> 4) & 0x07
        mantissa = val & 0x0F
        sample = ((mantissa << 3) + BIAS) << exponent
        sample -= BIAS
        if sign:
            sample = -sample
        # Clamp to int16 range (defensive — table should never exceed it).
        if sample > CLIP:
            sample = CLIP
        elif sample < -CLIP:
            sample = -CLIP
        table.append(sample)
    return table


_ULAW_TABLE = _build_ulaw_table()


def ulaw_to_pcm16(data: bytes) -> bytes:
    """Convert G.711 μ-law bytes to little-endian 16-bit signed PCM.

    One μ-law byte = one PCM16 sample; output is always 2×
    ``len(data)`` bytes. No state is carried across calls, so callers
    can hand us frame-sized chunks and concatenate the outputs.
    """
    if not data:
        return b""
    # struct.pack of a big list is ~3× faster than list-comprehension +
    # "".join(bytes(…)) for the frame sizes we handle (160–1600 bytes).
    samples = [_ULAW_TABLE[b] for b in data]
    return struct.pack(f"<{len(samples)}h", *samples)


# ── PCM16 helpers ───────────────────────────────────────────────────


def pcm16_to_mono(data: bytes, channels: int) -> bytes:
    """Average interleaved PCM16 channels down to mono.

    ``channels=1`` returns the input unchanged. ``channels=2`` is the
    common stereo case; higher counts are supported but uncommon in
    telephony feeds.
    """
    if channels <= 1:
        return data
    n_samples = len(data) // (2 * channels)
    unpacked = struct.unpack(f"<{n_samples * channels}h", data)
    mono: List[int] = []
    for i in range(n_samples):
        start = i * channels
        chunk = unpacked[start:start + channels]
        mono.append(sum(chunk) // channels)
    return struct.pack(f"<{n_samples}h", *mono)


def resample_pcm16(data: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Linear-interpolate PCM16 mono from ``src_rate`` to ``dst_rate``.

    Matches audioop.ratecv's semantics for our use case (mono, int16,
    no state carry-over). For telephony-band voice the difference vs
    a polyphase FIR resampler is inaudible.
    """
    if src_rate <= 0 or dst_rate <= 0:
        raise ValueError("sample rates must be > 0")
    if src_rate == dst_rate or not data:
        return data
    n_in = len(data) // 2
    if n_in == 0:
        return b""
    unpacked = struct.unpack(f"<{n_in}h", data)
    n_out = int(n_in * dst_rate / src_rate)
    if n_out <= 0:
        return b""
    step = (n_in - 1) / max(1, n_out - 1) if n_out > 1 else 0.0
    out: List[int] = []
    for i in range(n_out):
        pos = i * step
        lo = int(pos)
        hi = min(n_in - 1, lo + 1)
        frac = pos - lo
        sample = int(unpacked[lo] + (unpacked[hi] - unpacked[lo]) * frac)
        # Clamp defensively against FP rounding pushing us past int16.
        if sample > 32767:
            sample = 32767
        elif sample < -32768:
            sample = -32768
        out.append(sample)
    return struct.pack(f"<{len(out)}h", *out)


__all__ = ["ulaw_to_pcm16", "pcm16_to_mono", "resample_pcm16"]
