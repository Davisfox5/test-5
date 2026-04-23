"""Replay harness for live paralinguistic validation.

Lets us exercise ``LiveParalinguisticWindow`` and
``ParalinguisticScanner`` against pre-recorded or synthesized audio
files, *without* needing a real call. Two use cases:

1. **CI / regression** — ship labeled fixtures (short synthesized
   clips with known pitch/pace properties, or redacted customer
   recordings) and assert the scanner fires on the expected frames.
2. **Pre-launch validation** — replay a batch of historical calls
   through the live path and compare the resulting alert timeline
   against ground-truth coach annotations. Output is a
   :class:`ReplayReport` with per-alert precision/recall, so we can
   say with confidence that the live mode is ready.

The harness is deliberately deterministic: no wall-clock waits, no
asyncio. We simulate real-time by advancing a synthetic clock with
``monkeypatch`` of ``time.time`` from the caller, or by using
:func:`replay_wav_fast` which disables the recompute rate-limit and
forces a snapshot every ``snapshot_every_sec`` of simulated audio.
"""

from __future__ import annotations

import contextlib
import logging
import struct
import time
import wave
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, List, Optional

from backend.app.services.audio_codecs import pcm16_to_mono, resample_pcm16
from backend.app.services.live_coaching_features import (
    CoachingAlert,
    ParalinguisticScanner,
)
from backend.app.services.paralinguistics import ParalinguisticFeatures
from backend.app.services.paralinguistics_live import LiveParalinguisticWindow

logger = logging.getLogger(__name__)


# Media-Streams-sized frames are 20 ms at 8 kHz μ-law = 160 bytes.
_FRAME_BYTES = 160
_FRAME_SEC = 0.020


@dataclass
class ReplaySnapshot:
    t_sec: float  # seconds into the replay
    features: ParalinguisticFeatures
    alerts: List[CoachingAlert] = field(default_factory=list)


@dataclass
class ReplayReport:
    """Result of one replay run.

    ``snapshots`` is every snapshot the window produced during replay
    plus the alerts the scanner fired for it. ``summary`` counts alerts
    by kind — the most useful field when comparing runs.
    """

    total_duration_sec: float
    snapshots: List[ReplaySnapshot] = field(default_factory=list)

    @property
    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for s in self.snapshots:
            for a in s.alerts:
                counts[a.kind] = counts.get(a.kind, 0) + 1
        return counts

    @property
    def alerts_flat(self) -> List[tuple[float, CoachingAlert]]:
        """Flat timeline of ``(t_sec, alert)`` pairs in replay order."""
        return [(s.t_sec, a) for s in self.snapshots for a in s.alerts]


# ── Core replay ─────────────────────────────────────────────────────────


def replay_pcm_into_window(
    *,
    pcm16: bytes,
    sample_rate: int,
    window_sec: float = 30.0,
    snapshot_every_sec: float = 3.0,
    scanner: Optional[ParalinguisticScanner] = None,
    clock: Optional[Callable[[], float]] = None,
) -> ReplayReport:
    """Feed PCM16 audio into the live window frame-by-frame and collect
    snapshots + scanner alerts.

    ``clock`` defaults to a deterministic advancing counter so tests
    don't depend on wall time. Override with ``time.time`` (and call
    :func:`replay_pcm_realtime` instead) if you actually want a
    real-time replay for stress-testing the IO path.
    """
    if sample_rate <= 0:
        raise ValueError("sample_rate must be > 0")
    if not pcm16:
        return ReplayReport(total_duration_sec=0.0)

    # Convert PCM16 → μ-law 8 kHz mono so the window sees the same bytes
    # a live Media Streams connection would hand it.
    target_rate = 8000
    if sample_rate != target_rate:
        pcm16 = resample_pcm16(pcm16, sample_rate, target_rate)
    mulaw = _pcm16_to_ulaw(pcm16)

    # Deterministic synthetic clock that advances by one frame per feed.
    # Each call to feed() uses its own timestamp, so the window sees
    # chunks as if they arrived every 20 ms regardless of actual wall
    # time — ideal for CI and ground-truth comparisons.
    _now = [clock() if clock else 0.0]
    advance_per_frame = _FRAME_SEC

    def _clock() -> float:
        return _now[0]

    # Install the synthetic clock on both the window and scanner via
    # monkeypatching the module-level time.time reference.
    report = ReplayReport(total_duration_sec=0.0)
    scanner = scanner or ParalinguisticScanner()

    window = LiveParalinguisticWindow(
        sample_rate=target_rate,
        window_sec=window_sec,
        recompute_every_sec=snapshot_every_sec,
    )

    with _patched_time(_clock):
        # Warm the monotonic reference inside the window.
        window._call_start = _now[0]

        next_snapshot_at = snapshot_every_sec
        for frame_start in range(0, len(mulaw), _FRAME_BYTES):
            chunk = mulaw[frame_start:frame_start + _FRAME_BYTES]
            if not chunk:
                break
            window.feed(chunk)
            _now[0] += advance_per_frame

            elapsed = _now[0] - window._call_start
            if elapsed >= next_snapshot_at:
                snap = window.maybe_snapshot()
                if snap is not None:
                    alerts = scanner.push(snap)
                    report.snapshots.append(
                        ReplaySnapshot(t_sec=elapsed, features=snap, alerts=alerts)
                    )
                next_snapshot_at = elapsed + snapshot_every_sec

        report.total_duration_sec = _now[0] - window._call_start
    return report


def replay_wav_file(
    path: str,
    *,
    window_sec: float = 30.0,
    snapshot_every_sec: float = 3.0,
    scanner: Optional[ParalinguisticScanner] = None,
) -> ReplayReport:
    """Convenience: open a WAV file and replay it."""
    with wave.open(path, "rb") as wav:
        n_channels = wav.getnchannels()
        sampwidth = wav.getsampwidth()
        rate = wav.getframerate()
        frames = wav.readframes(wav.getnframes())

    if sampwidth != 2:
        raise ValueError(f"replay_wav_file expects 16-bit PCM, got {sampwidth * 8} bit")
    frames = pcm16_to_mono(frames, n_channels)

    return replay_pcm_into_window(
        pcm16=frames,
        sample_rate=rate,
        window_sec=window_sec,
        snapshot_every_sec=snapshot_every_sec,
        scanner=scanner,
    )


def _pcm16_to_ulaw(pcm16: bytes) -> bytes:
    """Encode 16-bit PCM to G.711 μ-law — only used by the replay
    harness to produce bytes that match what Media Streams delivers.

    We don't need an encoder in the live ingest path (we only decode
    what providers send) so this lives here rather than in the public
    audio_codecs module.
    """
    if not pcm16:
        return b""
    n = len(pcm16) // 2
    samples = struct.unpack(f"<{n}h", pcm16)
    BIAS = 0x84
    CLIP = 32635
    out = bytearray()
    for sample in samples:
        sign = 0x80 if sample < 0 else 0x00
        if sample < 0:
            sample = -sample
        if sample > CLIP:
            sample = CLIP
        sample += BIAS
        # Compute exponent as position of the highest set bit above the
        # shifted mantissa — same table Python's removed audioop used.
        exponent = 7
        mask = 0x4000
        while exponent > 0 and (sample & mask) == 0:
            mask >>= 1
            exponent -= 1
        mantissa = (sample >> ((exponent + 3))) & 0x0F
        out.append(~(sign | (exponent << 4) | mantissa) & 0xFF)
    return bytes(out)


# ── Validation (ground-truth comparison) ────────────────────────────────


@dataclass
class ExpectedAlert:
    kind: str
    at_sec: float
    tolerance_sec: float = 5.0


@dataclass
class ValidationResult:
    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0

    @property
    def precision(self) -> float:
        denom = self.true_positives + self.false_positives
        return self.true_positives / denom if denom else 1.0

    @property
    def recall(self) -> float:
        denom = self.true_positives + self.false_negatives
        return self.true_positives / denom if denom else 1.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0


def validate_against_expected(
    report: ReplayReport,
    expected: Iterable[ExpectedAlert],
) -> ValidationResult:
    """Compare the alerts the scanner fired against ground truth.

    Each expected alert is satisfied by *at most one* observed alert of
    the same kind within its tolerance window (first-come, first-served).
    Observed alerts that don't match any expected entry become false
    positives; unmatched expected entries become false negatives.

    The result exposes precision / recall / F1 — good enough to gate a
    promotion: "we will only turn on live coaching for tenants after
    F1 ≥ 0.7 on the regression corpus" is the kind of threshold this
    supports.
    """
    observed = report.alerts_flat
    consumed: set[int] = set()
    result = ValidationResult()

    for exp in expected:
        matched = False
        for idx, (t, alert) in enumerate(observed):
            if idx in consumed:
                continue
            if alert.kind != exp.kind:
                continue
            if abs(t - exp.at_sec) <= exp.tolerance_sec:
                consumed.add(idx)
                matched = True
                result.true_positives += 1
                break
        if not matched:
            result.false_negatives += 1

    result.false_positives = sum(
        1 for idx, _ in enumerate(observed) if idx not in consumed
    )
    return result


# ── Helpers ─────────────────────────────────────────────────────────────


@contextlib.contextmanager
def _patched_time(clock_fn: Callable[[], float]):
    """Swap ``time.time`` inside the paralinguistics modules so the
    window and scanner see the synthetic clock.

    We only patch the specific module references we care about — keeps
    test isolation tight and avoids fighting with pytest's own timing.
    Both modules do ``import time`` at the top, so we swap
    ``module.time.time`` via a proxy that carries everything else
    through untouched.
    """
    from types import SimpleNamespace

    from backend.app.services import live_coaching_features
    from backend.app.services import paralinguistics_live

    # Build a shim that looks like the time module for our two callers:
    # ``.time()`` returns the synthetic clock; everything else falls
    # through to the real module so nothing else in these files breaks.
    real = time
    shim = SimpleNamespace(
        time=clock_fn,
        monotonic=real.monotonic,
        sleep=real.sleep,
        perf_counter=real.perf_counter,
    )

    originals = {}
    for mod in (live_coaching_features, paralinguistics_live):
        originals[mod] = mod.time
        mod.time = shim
    try:
        yield
    finally:
        for mod, orig in originals.items():
            mod.time = orig


__all__ = [
    "ReplayReport",
    "ReplaySnapshot",
    "ExpectedAlert",
    "ValidationResult",
    "replay_pcm_into_window",
    "replay_wav_file",
    "validate_against_expected",
]
