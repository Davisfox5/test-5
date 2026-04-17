"""Call metrics — computes conversation analytics from diarized segments.

All metrics are derived purely from text and timestamps.  Zero LLM cost.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from backend.app.services.transcription import Segment

# Filler-word patterns.  Multi-word fillers are checked first so they aren't
# double-counted by single-word entries.
_MULTI_WORD_FILLERS: List[str] = ["you know", "i mean"]
_SINGLE_WORD_FILLERS: List[str] = [
    "um", "uh", "like", "so", "basically", "actually", "right",
]

# Pre-compiled regex for sentence-ending question marks.
_QUESTION_RE = re.compile(r"\?\s*$")

# Regex to split text into rough "sentences" (split on .!? followed by space
# or end-of-string).
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


class CallMetricsService:
    """Computes conversation-level metrics from diarized transcript segments."""

    def compute(
        self,
        segments: List[Segment],
        agent_speaker_ids: Optional[List[str]] = None,
    ) -> dict:
        """Return a dict of call metrics.

        Args:
            segments: Ordered transcript segments with speaker labels.
            agent_speaker_ids: Speaker IDs that belong to the agent.  If
                ``None`` the heuristic assumes the *first* speaker is the
                agent.

        Returns:
            Dictionary with all computed metrics.
        """
        if not segments:
            return self._empty_metrics()

        agent_ids = set(agent_speaker_ids) if agent_speaker_ids else None

        # If no explicit agent IDs, treat the first speaker as the agent.
        if agent_ids is None:
            first_speaker = next(
                (s.speaker_id for s in segments if s.speaker_id is not None),
                None,
            )
            agent_ids = {first_speaker} if first_speaker is not None else set()

        # ── Aggregate per-speaker stats ──────────────────────────────────
        speaker_talk_time: Dict[str, float] = defaultdict(float)
        speaker_word_count: Dict[str, int] = defaultdict(int)
        total_call_start = segments[0].start
        total_call_end = segments[-1].end
        total_duration = max(total_call_end - total_call_start, 0.001)

        # Track agent monologues.
        current_mono_speaker: Optional[str] = None
        current_mono_start: float = 0.0
        current_mono_end: float = 0.0
        longest_agent_monologue: float = 0.0

        # Filler tracking (agent only).
        filler_counts: Dict[str, int] = {f: 0 for f in _SINGLE_WORD_FILLERS}
        for mf in _MULTI_WORD_FILLERS:
            filler_counts[mf] = 0

        question_count: int = 0

        # Silence / gap tracking.
        covered_intervals: List[Tuple[float, float]] = []

        # Interruption tracking.
        interruptions: int = 0

        for idx, seg in enumerate(segments):
            duration = max(seg.end - seg.start, 0.0)
            sid = seg.speaker_id or "__unknown__"
            speaker_talk_time[sid] += duration
            words = seg.text.split()
            speaker_word_count[sid] += len(words)
            covered_intervals.append((seg.start, seg.end))

            is_agent = sid in agent_ids

            # ── Monologue tracking (agent only) ──────────────────────
            if is_agent:
                if current_mono_speaker == sid:
                    # Extend the current monologue.
                    current_mono_end = seg.end
                else:
                    # Flush previous monologue if it was the agent's.
                    if current_mono_speaker is not None and current_mono_speaker in agent_ids:
                        mono_len = current_mono_end - current_mono_start
                        longest_agent_monologue = max(longest_agent_monologue, mono_len)
                    current_mono_speaker = sid
                    current_mono_start = seg.start
                    current_mono_end = seg.end
            else:
                # Non-agent segment — flush any ongoing agent monologue.
                if current_mono_speaker is not None and current_mono_speaker in agent_ids:
                    mono_len = current_mono_end - current_mono_start
                    longest_agent_monologue = max(longest_agent_monologue, mono_len)
                current_mono_speaker = sid
                current_mono_start = seg.start
                current_mono_end = seg.end

            # ── Agent-specific text metrics ──────────────────────────
            if is_agent:
                text_lower = seg.text.lower()

                # Multi-word fillers.
                for mf in _MULTI_WORD_FILLERS:
                    filler_counts[mf] += text_lower.count(mf)

                # Single-word fillers — match only whole words.
                for fw in _SINGLE_WORD_FILLERS:
                    pattern = r"\b" + re.escape(fw) + r"\b"
                    filler_counts[fw] += len(re.findall(pattern, text_lower))

                # Questions.
                sentences = _SENTENCE_SPLIT_RE.split(seg.text)
                for sentence in sentences:
                    if _QUESTION_RE.search(sentence):
                        question_count += 1

            # ── Interruptions ────────────────────────────────────────
            if idx > 0:
                prev = segments[idx - 1]
                if (
                    prev.speaker_id != seg.speaker_id
                    and seg.start < prev.end
                ):
                    interruptions += 1

        # Flush final monologue.
        if current_mono_speaker is not None and current_mono_speaker in agent_ids:
            mono_len = current_mono_end - current_mono_start
            longest_agent_monologue = max(longest_agent_monologue, mono_len)

        # ── Compute talk / listen / silence percentages ──────────────
        agent_time = sum(
            t for sid, t in speaker_talk_time.items() if sid in agent_ids
        )
        customer_time = sum(
            t for sid, t in speaker_talk_time.items() if sid not in agent_ids
        )
        talk_pct = round(agent_time / total_duration * 100, 2)
        listen_pct = round(customer_time / total_duration * 100, 2)

        # Silence: merge overlapping intervals, then compute gaps.
        merged = self._merge_intervals(covered_intervals)
        spoken_time = sum(end - start for start, end in merged)
        silence_time = max(total_duration - spoken_time, 0.0)
        silence_pct = round(silence_time / total_duration * 100, 2)

        # ── Speech rate per speaker ──────────────────────────────────
        speech_rate_wpm: Dict[str, float] = {}
        for sid in speaker_talk_time:
            minutes = speaker_talk_time[sid] / 60.0
            if minutes > 0:
                speech_rate_wpm[sid] = round(speaker_word_count[sid] / minutes, 1)
            else:
                speech_rate_wpm[sid] = 0.0

        filler_word_count = sum(filler_counts.values())

        return {
            "talk_pct": talk_pct,
            "listen_pct": listen_pct,
            "longest_monologue_sec": round(longest_agent_monologue, 2),
            "question_count": question_count,
            "filler_word_count": filler_word_count,
            "filler_words": filler_counts,
            "speech_rate_wpm": speech_rate_wpm,
            "interruptions": interruptions,
            "silence_pct": silence_pct,
        }

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _merge_intervals(
        intervals: List[Tuple[float, float]],
    ) -> List[Tuple[float, float]]:
        """Merge overlapping (start, end) intervals."""
        if not intervals:
            return []
        sorted_iv = sorted(intervals, key=lambda x: x[0])
        merged: List[Tuple[float, float]] = [sorted_iv[0]]
        for start, end in sorted_iv[1:]:
            prev_start, prev_end = merged[-1]
            if start <= prev_end:
                merged[-1] = (prev_start, max(prev_end, end))
            else:
                merged.append((start, end))
        return merged

    @staticmethod
    def _empty_metrics() -> dict:
        """Return zeroed-out metrics when there are no segments."""
        return {
            "talk_pct": 0.0,
            "listen_pct": 0.0,
            "longest_monologue_sec": 0.0,
            "question_count": 0,
            "filler_word_count": 0,
            "filler_words": {f: 0 for f in _SINGLE_WORD_FILLERS + _MULTI_WORD_FILLERS},
            "speech_rate_wpm": {},
            "interruptions": 0,
            "silence_pct": 0.0,
        }
