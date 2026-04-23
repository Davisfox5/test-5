"""Soak test for LiveParalinguisticWindow + ParalinguisticScanner.

Drives N concurrent virtual calls through the replay harness at
realistic frame cadence and reports:

* wall-clock duration
* total snapshots emitted
* per-call latency (p50/p90/p99) for the ``maybe_snapshot`` call
* rough CPU load (mean process CPU %) during the run
* memory high-water mark

Not a test — it's an operational harness. Run it against a known
audio fixture on a box that matches your worker sizing and use the
output to pick the ``paralinguistic_live`` concurrency cap per box.

Usage::

    python scripts/loadtest_live_paralinguistic.py \\
        --clip corpora/clips/example.wav \\
        --concurrency 25 \\
        --duration-sec 60

``--clip`` must be a 16-bit PCM WAV. Any length works; the script
loops it so calls can run longer than the source.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import sys
import time
import tempfile
import wave
from pathlib import Path
from typing import List

# Ensure the project root is on sys.path when the script is invoked
# directly (``python scripts/loadtest_…``) rather than via ``python -m``.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.app.services.audio_codecs import pcm16_to_mono, resample_pcm16
from backend.app.services.live_coaching_features import ParalinguisticScanner
from backend.app.services.paralinguistics_live import LiveParalinguisticWindow
from backend.app.services.paralinguistics_replay import _pcm16_to_ulaw


async def _simulate_call(
    mulaw_frames: List[bytes],
    *,
    duration_sec: float,
    recompute_every_sec: float,
    scanner: ParalinguisticScanner,
    snapshot_latencies: List[float],
    alert_total: list[int],
) -> None:
    """One virtual call: feed frames at ~50 Hz (Media Streams cadence)
    until ``duration_sec`` elapses, record snapshot latencies and alerts.
    """
    window = LiveParalinguisticWindow(
        window_sec=30.0,
        recompute_every_sec=recompute_every_sec,
    )
    started = time.monotonic()
    frame_idx = 0
    while (time.monotonic() - started) < duration_sec:
        window.feed(mulaw_frames[frame_idx % len(mulaw_frames)])
        frame_idx += 1
        if frame_idx % 150 == 0:  # every ~3 s
            t0 = time.monotonic()
            snap = window.maybe_snapshot()
            snapshot_latencies.append((time.monotonic() - t0) * 1000)
            if snap is not None:
                alerts = scanner.push(snap)
                alert_total[0] += len(alerts)
        # Sleep until the next 20 ms frame so we don't spin.
        await asyncio.sleep(0.02)


def _load_fixture(path: str) -> List[bytes]:
    with wave.open(path, "rb") as wav:
        if wav.getsampwidth() != 2:
            raise SystemExit("Fixture must be 16-bit PCM WAV")
        rate = wav.getframerate()
        channels = wav.getnchannels()
        frames = wav.readframes(wav.getnframes())
    frames = pcm16_to_mono(frames, channels)
    if rate != 8000:
        frames = resample_pcm16(frames, rate, 8000)
    mulaw = _pcm16_to_ulaw(frames)
    # Chop to Media-Streams-sized frames (160 bytes each at 8 kHz μ-law).
    frame_size = 160
    chunks: List[bytes] = []
    for offset in range(0, len(mulaw) - frame_size + 1, frame_size):
        chunks.append(mulaw[offset:offset + frame_size])
    if not chunks:
        raise SystemExit("Fixture produced zero frames after conversion")
    return chunks


def _process_cpu_percent() -> float:
    """Cheap cross-platform CPU measurement. Falls back to 0 on macOS
    without psutil; soak runs on Linux anyway."""
    try:
        import psutil  # type: ignore

        return float(psutil.Process().cpu_percent(interval=0.5))
    except Exception:
        return 0.0


def _rss_mb() -> float:
    try:
        import resource

        # Linux reports kb, macOS reports bytes. We're on Linux in prod.
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    except Exception:
        return 0.0


async def _run(args: argparse.Namespace) -> dict:
    mulaw_frames = _load_fixture(args.clip)
    scanner = ParalinguisticScanner(cooldown_sec=30.0)

    snapshot_latencies: List[float] = []
    alerts_fired = [0]
    started = time.monotonic()

    tasks = [
        asyncio.create_task(
            _simulate_call(
                mulaw_frames,
                duration_sec=args.duration_sec,
                recompute_every_sec=args.recompute_every_sec,
                scanner=scanner,
                snapshot_latencies=snapshot_latencies,
                alert_total=alerts_fired,
            )
        )
        for _ in range(args.concurrency)
    ]

    cpu_samples: List[float] = []

    async def _sample_cpu() -> None:
        while not all(t.done() for t in tasks):
            cpu_samples.append(_process_cpu_percent())
            await asyncio.sleep(1.0)

    sampler = asyncio.create_task(_sample_cpu())
    await asyncio.gather(*tasks)
    sampler.cancel()
    try:
        await sampler
    except asyncio.CancelledError:
        pass

    elapsed = time.monotonic() - started
    latencies = snapshot_latencies or [0.0]
    return {
        "concurrency": args.concurrency,
        "duration_sec_requested": args.duration_sec,
        "duration_sec_observed": round(elapsed, 2),
        "snapshots_total": len(snapshot_latencies),
        "snapshots_per_call": round(len(snapshot_latencies) / max(1, args.concurrency), 2),
        "alerts_fired": alerts_fired[0],
        "snapshot_ms_p50": round(statistics.median(latencies), 2),
        "snapshot_ms_p90": round(_percentile(latencies, 90), 2),
        "snapshot_ms_p99": round(_percentile(latencies, 99), 2),
        "snapshot_ms_max": round(max(latencies), 2),
        "cpu_mean_pct": round(statistics.mean(cpu_samples), 1) if cpu_samples else 0.0,
        "rss_mb_max": round(_rss_mb(), 1),
    }


def _percentile(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    sorted_v = sorted(values)
    k = (len(sorted_v) - 1) * (p / 100.0)
    lo = int(math.floor(k))
    hi = int(math.ceil(k))
    if lo == hi:
        return sorted_v[lo]
    return sorted_v[lo] + (sorted_v[hi] - sorted_v[lo]) * (k - lo)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--clip",
        required=True,
        help="Path to a 16-bit PCM WAV (any rate / channel count).",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=10,
        help="Concurrent virtual calls (default 10).",
    )
    parser.add_argument(
        "--duration-sec",
        type=float,
        default=30.0,
        help="How long each virtual call runs (default 30 s).",
    )
    parser.add_argument(
        "--recompute-every-sec",
        type=float,
        default=3.0,
        help="Window recompute cadence (default 3 s — matches production).",
    )
    args = parser.parse_args()

    result = asyncio.run(_run(args))
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
