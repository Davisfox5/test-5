"""MediaStreamPump — bounded queue, drop-oldest, non-blocking snapshots.

Challenge #2b/#2d: the receive loop must never block on our compute,
overload must degrade visibly (drop counter) instead of silently, and a
Praat snapshot that blows its deadline is skipped while the cadence
backs off. All synthetic — no live audio exists pre-launch.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, List, Optional

from backend.app.services.telephony.media_stream_pump import MediaStreamPump


def _frame(i: int) -> bytes:
    return bytes([i % 256]) * 160  # one 20 ms μ-law frame


class _FakeJob:
    def __init__(self, duration: float = 0.0, features: Any = "FEATURES"):
        self.duration = duration
        self.features = features

    def run(self):
        if self.duration:
            time.sleep(self.duration)
        return self.features


class _FakeWindow:
    """Minimal stand-in for LiveParalinguisticWindow."""

    def __init__(self, jobs: Optional[List[Any]] = None):
        self.fed: List[bytes] = []
        self.jobs = list(jobs or [])
        self.begin_calls = 0
        self.overruns = 0
        self.oks = 0

    def feed(self, audio: bytes) -> None:
        self.fed.append(audio)

    def maybe_begin_snapshot(self):
        self.begin_calls += 1
        if self.jobs:
            return self.jobs.pop(0)
        return None

    def note_overrun(self) -> None:
        self.overruns += 1

    def note_ok(self) -> None:
        self.oks += 1


class _Features:
    available = True


# ── Ordering + drop-oldest ─────────────────────────────────────────────


def test_pump_forwards_frames_in_order():
    async def scenario():
        sent: List[bytes] = []

        async def send(a: bytes) -> None:
            sent.append(a)

        pump = MediaStreamPump(send_audio=send)
        task = asyncio.get_event_loop().create_task(pump.run())
        for i in range(10):
            assert pump.offer(_frame(i)) is True
        await pump.aclose()
        await asyncio.wait_for(task, timeout=2)
        assert sent == [_frame(i) for i in range(10)]

    asyncio.run(scenario())


def test_offer_never_blocks_and_drops_oldest_when_full():
    async def scenario():
        release = asyncio.Event()

        async def slow_send(a: bytes) -> None:
            await release.wait()

        pump = MediaStreamPump(send_audio=slow_send, queue_max_frames=5)
        task = asyncio.get_event_loop().create_task(pump.run())
        await asyncio.sleep(0)  # consumer parks on the first frame

        started = time.monotonic()
        for i in range(50):
            pump.offer(_frame(i))
        elapsed = time.monotonic() - started

        assert elapsed < 0.5, "offer() must never block"
        assert pump.frames_dropped > 0
        assert pump.frames_offered == 50
        # Newest frames survive; the queue never exceeds its bound.
        assert pump._queue.qsize() <= 5

        release.set()
        await pump.aclose()
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(scenario())


# ── Snapshots ──────────────────────────────────────────────────────────


def test_fast_snapshot_is_published_and_resets_cadence():
    async def scenario():
        published: List[Any] = []

        async def send(a: bytes) -> None:
            pass

        async def publish(f: Any) -> None:
            published.append(f)

        window = _FakeWindow(jobs=[_FakeJob(0.0, _Features())])
        pump = MediaStreamPump(send_audio=send, window=window, publish=publish)
        task = asyncio.get_event_loop().create_task(pump.run())
        pump.offer(_frame(0))
        await asyncio.sleep(0.2)
        await pump.aclose()
        await asyncio.wait_for(task, timeout=2)

        assert len(published) == 1
        assert window.oks == 1
        assert window.overruns == 0

    asyncio.run(scenario())


def test_overrunning_snapshot_is_skipped_and_backs_off():
    async def scenario():
        published: List[Any] = []

        async def send(a: bytes) -> None:
            pass

        async def publish(f: Any) -> None:
            published.append(f)

        window = _FakeWindow(jobs=[_FakeJob(0.6, _Features())])
        pump = MediaStreamPump(
            send_audio=send,
            window=window,
            publish=publish,
            snapshot_deadline_sec=0.1,
        )
        task = asyncio.get_event_loop().create_task(pump.run())
        pump.offer(_frame(0))
        await asyncio.sleep(0.3)
        await pump.aclose()
        await asyncio.wait_for(task, timeout=2)

        assert published == []
        assert window.overruns == 1
        assert window.oks == 0

    asyncio.run(scenario())


def test_ingest_continues_while_snapshot_runs():
    """The whole point of the pump: a slow Praat job must not stop
    frames from reaching the transcriber."""

    async def scenario():
        sent: List[bytes] = []

        async def send(a: bytes) -> None:
            sent.append(a)

        window = _FakeWindow(jobs=[_FakeJob(0.4, _Features())])
        pump = MediaStreamPump(
            send_audio=send,
            window=window,
            snapshot_deadline_sec=5.0,
        )
        task = asyncio.get_event_loop().create_task(pump.run())

        pump.offer(_frame(0))  # triggers the slow snapshot
        await asyncio.sleep(0.05)
        for i in range(1, 30):
            pump.offer(_frame(i))
            await asyncio.sleep(0.005)
        # Well before the 0.4 s job finishes, all frames must be sent.
        assert len(sent) == 30

        await pump.aclose()
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(scenario())


def test_only_one_snapshot_in_flight():
    async def scenario():
        async def send(a: bytes) -> None:
            pass

        # Two jobs available, but the first is slow — the second must
        # not start while the first is in flight.
        window = _FakeWindow(jobs=[_FakeJob(0.3, _Features()), _FakeJob(0.0, _Features())])
        pump = MediaStreamPump(send_audio=send, window=window, snapshot_deadline_sec=5.0)
        task = asyncio.get_event_loop().create_task(pump.run())

        for i in range(10):
            pump.offer(_frame(i))
            await asyncio.sleep(0.01)

        # Only the first job was popped; begin was gated meanwhile.
        assert len(window.jobs) == 1

        await pump.aclose()
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(scenario())


def test_transcriber_errors_do_not_stop_the_pump():
    async def scenario():
        calls: List[bytes] = []

        async def flaky_send(a: bytes) -> None:
            calls.append(a)
            if len(calls) == 1:
                raise RuntimeError("deepgram hiccup")

        pump = MediaStreamPump(send_audio=flaky_send)
        task = asyncio.get_event_loop().create_task(pump.run())
        pump.offer(_frame(0))
        pump.offer(_frame(1))
        await pump.aclose()
        await asyncio.wait_for(task, timeout=2)
        assert len(calls) == 2

    asyncio.run(scenario())


def test_aclose_lands_sentinel_even_when_queue_full():
    async def scenario():
        release = asyncio.Event()

        async def slow_send(a: bytes) -> None:
            await release.wait()

        pump = MediaStreamPump(send_audio=slow_send, queue_max_frames=2)
        task = asyncio.get_event_loop().create_task(pump.run())
        await asyncio.sleep(0)
        for i in range(10):
            pump.offer(_frame(i))
        await pump.aclose()
        release.set()
        # Must terminate — the sentinel got through despite the full queue.
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(scenario())
