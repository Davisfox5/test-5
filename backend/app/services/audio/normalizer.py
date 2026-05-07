"""Audio format normalization for the telephony ingestion pipeline.

Every telephony integration ultimately funnels audio into the same
transcription path (Deepgram live or Whisper batch). Different vendors
deliver audio in different shapes:

- SIPREC sources (Cisco CUBE / Avaya SBCE / Metaswitch Perimeta) —
  typically PCMU (μ-law 8 kHz) or PCMA (A-law 8 kHz) RTP payloads, or
  L16 PCM 16 kHz on newer deployments.
- UC vendor APIs (RingCentral / Webex Calling / Zoom Phone) — REST
  recording downloads, usually MP3 or WAV at the platform's native
  sample rate (16 kHz or 48 kHz).
- Microsoft Teams compliance bot — would deliver L16 PCM 16 kHz from
  Graph media streams, but the .NET media bot is out of scope this
  round.
- Genesys AudioHook — negotiated PCMU 8 kHz or L16 16 kHz over a
  WebSocket binary frame.

The transcription pipeline expects either:
- μ-law 8 kHz (Deepgram streaming format) — see ``to_mulaw_8k``, or
- PCM16 8 kHz (Whisper batch / pyannote diarization) — see ``to_pcm16_8k``.

This module is a Stream 0 deliverable — locked once it lands. Each
telephony stream imports it as-is. If a stream needs a format that's
not supported here, it must coordinate via the plan doc rather than
forking the normalizer.
"""

from __future__ import annotations

import audioop  # stdlib; deprecated in 3.13 but available in 3.9 (this codebase)
import io
from enum import Enum
from typing import Optional


class AudioFormat(str, Enum):
    """Supported source formats for telephony audio ingestion.

    Naming convention: ``{codec}_{sample_rate}``. ``mulaw`` and
    ``alaw`` are the G.711 ITU codecs (8-bit, 8 kHz canonical, but
    occasionally 16 kHz on modern SBCs). ``pcm16`` is signed 16-bit
    little-endian linear PCM.
    """

    MULAW_8K = "mulaw_8k"
    ALAW_8K = "alaw_8k"
    PCM16_8K = "pcm16_8k"
    PCM16_16K = "pcm16_16k"
    PCM16_24K = "pcm16_24k"
    PCM16_48K = "pcm16_48k"
    OPUS_16K = "opus_16k"
    OPUS_48K = "opus_48k"
    MP3 = "mp3"
    WAV = "wav"
    FLAC = "flac"


# ── Format detection ────────────────────────────────────────────────────


def detect_format(data: bytes, hint: Optional[str] = None) -> AudioFormat:
    """Identify the format of an audio payload.

    Magic-byte detection for container formats (MP3/WAV/FLAC/Opus
    Ogg). Raw codec payloads (μ-law, A-law, PCM) carry no magic bytes,
    so they require a ``hint`` from the caller — typically the
    SDP-negotiated format string from SIPREC, the AudioHook
    open-message format field, or the vendor-API recording metadata.

    Hint values are case-insensitive and match the ``AudioFormat``
    enum names (e.g. ``"pcm16_16k"``, ``"mulaw_8k"``).

    Raises ``ValueError`` when neither magic bytes nor the hint
    resolve to a known format.
    """

    if not data:
        raise ValueError("Cannot detect format of empty audio payload")

    # Container-format magic bytes (each is unambiguous within the
    # set we care about).
    if len(data) >= 4 and data[:4] == b"RIFF":
        # WAV is RIFF-WAVE; we don't validate the WAVE marker
        # because pydub's loader will fail loudly if it isn't.
        return AudioFormat.WAV
    if len(data) >= 4 and data[:4] == b"fLaC":
        return AudioFormat.FLAC
    if len(data) >= 3 and data[:3] == b"ID3":
        return AudioFormat.MP3
    # MPEG audio frames start with 0xFFFx (sync word). Fragile but
    # works for headerless MP3 deliveries from some UC vendors.
    if len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return AudioFormat.MP3
    if len(data) >= 4 and data[:4] == b"OggS":
        # Ogg container — we assume Opus inside; vendors that send
        # Vorbis or FLAC-in-Ogg are not in scope today.
        return AudioFormat.OPUS_48K

    if hint is not None:
        try:
            return AudioFormat(hint.lower())
        except ValueError as exc:
            raise ValueError(
                f"Audio payload has no recognizable magic bytes and "
                f"hint {hint!r} is not a known AudioFormat value"
            ) from exc

    raise ValueError(
        "Cannot detect audio format from magic bytes and no hint provided. "
        "Caller must pass a hint for raw-codec payloads (μ-law, A-law, PCM)."
    )


# ── Internal: load any source format into PCM16 mono at a known rate ────


def _to_pcm16_mono(data: bytes, src_format: AudioFormat) -> tuple[bytes, int]:
    """Convert any supported source format into PCM16 mono.

    Returns ``(pcm16_bytes, sample_rate)``. Stereo inputs are
    downmixed by averaging channels. The caller resamples / re-encodes
    from PCM16 mono to whatever the pipeline needs.

    MP3/WAV/FLAC are decoded via pydub (which shells out to ffmpeg).
    Opus is currently unsupported — when a stream actually needs it,
    add ``opuslib`` or ``pyav`` to requirements and extend this
    function inside Stream 0 (NOT in the per-stream PR).
    """

    if src_format == AudioFormat.MULAW_8K:
        return audioop.ulaw2lin(data, 2), 8000
    if src_format == AudioFormat.ALAW_8K:
        return audioop.alaw2lin(data, 2), 8000
    if src_format == AudioFormat.PCM16_8K:
        return data, 8000
    if src_format == AudioFormat.PCM16_16K:
        return data, 16000
    if src_format == AudioFormat.PCM16_24K:
        return data, 24000
    if src_format == AudioFormat.PCM16_48K:
        return data, 48000

    if src_format in (AudioFormat.MP3, AudioFormat.WAV, AudioFormat.FLAC):
        # Lazy import — pydub pulls in audioop too and shells out to
        # ffmpeg, so we don't pay the cost on raw-codec paths.
        try:
            from pydub import AudioSegment  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "pydub is required to decode container formats "
                "(MP3/WAV/FLAC). Install it via requirements.txt."
            ) from exc

        ext_map = {
            AudioFormat.MP3: "mp3",
            AudioFormat.WAV: "wav",
            AudioFormat.FLAC: "flac",
        }
        seg = AudioSegment.from_file(io.BytesIO(data), format=ext_map[src_format])
        # Force PCM16 mono.
        seg = seg.set_sample_width(2).set_channels(1)
        return seg.raw_data, seg.frame_rate

    if src_format in (AudioFormat.OPUS_16K, AudioFormat.OPUS_48K):
        raise NotImplementedError(
            "Opus decoding is not enabled in Stream 0. If a telephony "
            "stream needs Opus, add opuslib or pyav to requirements "
            "and extend services.audio.normalizer (coordinate via "
            "the plan doc)."
        )

    raise ValueError(f"Unsupported source format: {src_format}")


# ── Public conversion API ───────────────────────────────────────────────


def to_pcm16_8k(data: bytes, src_format: AudioFormat) -> bytes:
    """Normalize ``data`` to PCM16 mono at 8 kHz.

    Used by batch-transcription paths (Whisper, pyannote
    diarization). The 8 kHz target matches the dominant telephony
    sample rate; downstream models accept 8 kHz and avoid the
    upsample cost.
    """

    pcm, rate = _to_pcm16_mono(data, src_format)
    if rate == 8000:
        return pcm
    # ``ratecv`` returns ``(converted_bytes, state)``. We don't need
    # the state because we're converting a single buffer in one shot.
    converted, _ = audioop.ratecv(pcm, 2, 1, rate, 8000, None)
    return converted


def to_mulaw_8k(data: bytes, src_format: AudioFormat) -> bytes:
    """Normalize ``data`` to μ-law (G.711) mono at 8 kHz.

    Used by live-transcription paths that feed Deepgram's streaming
    API in μ-law (the format Twilio / SignalWire / Telnyx already
    deliver, so SIPREC and AudioHook converge on it for symmetry).
    """

    pcm_8k = to_pcm16_8k(data, src_format)
    return audioop.lin2ulaw(pcm_8k, 2)
