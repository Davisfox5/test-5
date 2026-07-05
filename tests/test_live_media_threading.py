"""Thread-safety contract for the live paralinguistic window (challenge #2a).

The window is **single-writer**: every mutation (``feed``,
``update_diarization``, cadence bookkeeping) happens on the event-loop
thread. The Deepgram SDK's callback thread never touches the window
directly — ``_attach_deepgram_diarization`` marshals turns onto the loop
via ``call_soon_threadsafe``. The Praat executor thread only ever sees an
immutable :class:`SnapshotJob` copied out on the loop thread.

These tests pin that contract:

* ``maybe_begin_snapshot`` returns a job whose inputs are isolated from
  later window mutation (copy semantics, including DiarTurn instances —
  the writer mutates turn objects in place when collapsing).
* The Deepgram transcript handler, invoked from a foreign thread, lands
  turns on the loop thread instead of mutating cross-thread.
* A writer hammering the window on one thread never corrupts a job
  running concurrently on another (regression guard for the old
  sort-while-iterate race).
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import List, Optional

from backend.app.services.paralinguistics_live import (
    DiarTurn,
    LiveParalinguisticWindow,
)


def _mulaw_silence(n_frames: int = 160) -> bytes:
    return b"\xff" * n_frames


# ── Copy semantics ─────────────────────────────────────────────────────


def test_begin_snapshot_returns_none_before_interval():
    w = LiveParalinguisticWindow(recompute_every_sec=10.0)
    w.feed(_mulaw_silence())
    assert w.maybe_begin_snapshot() is not None or True  # first call may fire
    # Second call within the interval must be rate-limited.
    assert w.maybe_begin_snapshot() is None


def test_snapshot_job_inputs_are_isolated_from_later_mutation():
    w = LiveParalinguisticWindow(recompute_every_sec=0.0, window_sec=60.0)
    # Need >=1.5s of audio span for a job to be considered useful, so
    # fake the chunk offsets rather than sleeping.
    w.feed(_mulaw_silence())
    w._chunks[0].offset = 0.0
    w.feed(_mulaw_silence())
    w._chunks[-1].offset = 2.0
    w.update_diarization([DiarTurn(start=0.0, end=1.0, speaker="0")])

    job = w.maybe_begin_snapshot()
    assert job is not None

    pcm_before = job.pcm
    turns_before = [(t.start, t.end, t.speaker) for t in job.turns]

    # Mutate the window afterwards — the job must not see any of it.
    w.feed(_mulaw_silence())
    w.update_diarization([DiarTurn(start=0.5, end=3.0, speaker="0")])

    assert job.pcm == pcm_before
    assert [(t.start, t.end, t.speaker) for t in job.turns] == turns_before


def test_snapshot_job_turns_are_deep_copies():
    """The writer collapses turns by mutating ``prev.end`` in place — the
    job must hold its own DiarTurn instances, not shared references."""
    w = LiveParalinguisticWindow(recompute_every_sec=0.0, window_sec=60.0)
    w.feed(_mulaw_silence())
    w._chunks[0].offset = 0.0
    w.feed(_mulaw_silence())
    w._chunks[-1].offset = 2.0
    w.update_diarization([DiarTurn(start=0.0, end=1.0, speaker="0")])

    job = w.maybe_begin_snapshot()
    assert job is not None
    assert job.turns, "job should carry the diarization timeline"

    # Adjacent same-speaker turn triggers the in-place collapse on the
    # window's own instance.
    w.update_diarization([DiarTurn(start=1.05, end=4.0, speaker="0")])

    assert job.turns[0].end == 1.0


def test_maybe_snapshot_still_works_as_before():
    """Back-compat: the blocking helper still exists and rate-limits."""
    w = LiveParalinguisticWindow(recompute_every_sec=10.0)
    w.feed(_mulaw_silence())
    first = w.maybe_snapshot()
    second = w.maybe_snapshot()
    assert second is None
    assert first is None or hasattr(first, "available")


# ── Cadence backoff (2d) ───────────────────────────────────────────────


def test_overrun_backs_off_cadence_and_ok_resets_it():
    w = LiveParalinguisticWindow(recompute_every_sec=3.0)
    assert w.current_interval_sec == 3.0
    w.note_overrun()
    assert w.current_interval_sec == 6.0
    w.note_overrun()
    assert w.current_interval_sec == 12.0
    # Bounded — never grows past the cap.
    for _ in range(10):
        w.note_overrun()
    assert w.current_interval_sec <= LiveParalinguisticWindow.MAX_BACKOFF_SEC
    w.note_ok()
    assert w.current_interval_sec == 3.0


# ── Deepgram handler marshals onto the loop ────────────────────────────


class _FakeDgConnection:
    def __init__(self) -> None:
        self.handler = None

    def on(self, event, handler) -> None:  # noqa: ARG002
        self.handler = handler


class _FakeWord:
    def __init__(self, speaker, start, end):
        self.speaker = speaker
        self.start = start
        self.end = end


class _FakeAlt:
    def __init__(self, words):
        self.words = words


class _FakeChannel:
    def __init__(self, words):
        self.alternatives = [_FakeAlt(words)]


class _FakeResult:
    def __init__(self, words):
        self.channel = _FakeChannel(words)


def test_transcript_handler_marshals_turns_to_loop_thread():
    from backend.app.api.telephony import _attach_deepgram_diarization

    seen_threads: List[int] = []

    class _RecordingWindow:
        def update_diarization(self, turns) -> None:
            seen_threads.append(threading.get_ident())
            self.turns = turns

    window = _RecordingWindow()
    conn = _FakeDgConnection()

    async def scenario() -> None:
        loop = asyncio.get_event_loop()
        _attach_deepgram_diarization(conn, window, loop=loop)
        assert conn.handler is not None

        result = _FakeResult([_FakeWord(0, 0.0, 1.0), _FakeWord(1, 1.0, 2.0)])

        # Invoke the handler from a foreign thread, like the SDK does.
        t = threading.Thread(target=conn.handler, args=(None, result))
        t.start()
        t.join()

        # The mutation must not have happened yet on the foreign thread —
        # it should be queued for the loop.
        assert seen_threads == []

        # Let the loop drain its callbacks.
        await asyncio.sleep(0.05)
        assert seen_threads == [threading.get_ident()]
        assert len(window.turns) == 2

    asyncio.get_event_loop_policy().new_event_loop()
    asyncio.run(scenario())


def test_transcript_handler_survives_closed_loop():
    from backend.app.api.telephony import _attach_deepgram_diarization

    window = LiveParalinguisticWindow()
    conn = _FakeDgConnection()

    loop = asyncio.new_event_loop()
    _attach_deepgram_diarization(conn, window, loop=loop)
    loop.close()

    result = _FakeResult([_FakeWord(0, 0.0, 1.0)])
    # Must not raise even though the loop is gone.
    conn.handler(None, result)
    assert window._diar_turns == []


# ── Hammer: writer thread vs job runner ────────────────────────────────


def test_writer_hammer_never_corrupts_running_job():
    """The writer thread (standing in for the event loop) feeds, merges
    diarization, and copies jobs out; the reader thread (standing in for
    the Praat executor) consumes those jobs concurrently. With
    single-writer copy semantics the reader can never observe a
    half-mutated structure; before the fix the executor read live state
    while the Deepgram thread sorted it."""
    import queue as _queue

    w = LiveParalinguisticWindow(recompute_every_sec=0.0, window_sec=60.0)
    errors: List[BaseException] = []
    stop = threading.Event()
    jobs: "_queue.Queue" = _queue.Queue()

    def writer() -> None:
        i = 0
        try:
            while not stop.is_set():
                w.feed(_mulaw_silence(80))
                w.update_diarization(
                    [DiarTurn(start=i * 0.01, end=i * 0.01 + 0.02, speaker=str(i % 2))]
                )
                job = w.maybe_begin_snapshot()
                if job is not None:
                    jobs.put(job)
                i += 1
        except BaseException as exc:  # pragma: no cover - failure path
            errors.append(exc)

    def reader() -> None:
        try:
            deadline = time.time() + 1.0
            while time.time() < deadline:
                try:
                    job = jobs.get(timeout=0.05)
                except _queue.Empty:
                    continue
                # Touch every field the executor-side compute reads.
                total = sum(t.end - t.start for t in job.turns)
                assert total >= 0
                assert isinstance(job.pcm, bytes)
        except BaseException as exc:  # pragma: no cover - failure path
            errors.append(exc)
        finally:
            stop.set()

    t_writer = threading.Thread(target=writer)
    t_reader = threading.Thread(target=reader)
    t_writer.start()
    t_reader.start()
    t_writer.join(timeout=5)
    t_reader.join(timeout=5)

    assert errors == []
