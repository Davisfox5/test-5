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

        Routes to one of two implementations based on whether segments
        carry real timestamps:

        * Audio-based (default) — used when Deepgram-style start/end
          times are present. Talk/listen %, monologue length, silence,
          and WPM are all derived from clock time.
        * Text-based — used when timestamps are missing (every text
          ingest, including transcripts pushed from Gong/Chorus/Otter
          or pasted directly). The SAME metric keys are returned, but
          computed from word counts and turn counts instead of seconds.
          ``silence_pct`` and per-speaker WPM degrade gracefully to 0
          since there's no time basis.

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

        # Detect timestamp presence — text-channel ingests set start ==
        # end == 0 (or distribute fake even-spacing across a duration we
        # don't actually know). If no segment has a non-zero end, we're
        # in text-mode and clock-based math would produce zeros.
        has_real_timestamps = any(
            (seg.end or 0.0) - (seg.start or 0.0) > 0.05 for seg in segments
        )

        agent_ids = set(agent_speaker_ids) if agent_speaker_ids else None

        # If no explicit agent IDs, treat the first speaker as the agent.
        if agent_ids is None:
            first_speaker = next(
                (s.speaker_id for s in segments if s.speaker_id is not None),
                None,
            )
            agent_ids = {first_speaker} if first_speaker is not None else set()

        if not has_real_timestamps:
            return self._compute_text_based(segments, agent_ids)

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
            # Explicit None check — ``or`` collapses integer 0 (the
            # first speaker in some emitters) to the fallback string.
            sid = (
                seg.speaker_id if seg.speaker_id is not None
                else "__unknown__"
            )
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

    def _compute_text_based(
        self,
        segments: List[Segment],
        agent_ids: set,
    ) -> dict:
        """Text-channel metrics: word-count math instead of clock time.

        Reuses every metric key the audio path produces so the schema
        stays stable for the SPA / aggregates. Time-based values
        (silence_pct, WPM, longest_monologue_sec) degrade gracefully:

        * ``talk_pct`` / ``listen_pct`` — rep word count / total words.
          A direct proxy for who's dominating the conversation.
        * ``longest_monologue_sec`` — the rep's longest single utterance
          re-expressed as a synthetic-seconds value at 150 wpm so the
          dashboard's "longest monologue" panel reads consistently.
        * ``silence_pct`` — 0.0 (text has no silence by definition).
          Surfaced rather than hidden so aggregates don't break.
        * ``speech_rate_wpm`` — empty dict; we can't infer WPM without
          time. Frontend should treat empty as "n/a".
        * ``filler_words`` / ``question_count`` / ``interruptions`` —
          computed identically; they're text-only features.
        """
        speaker_word_count: Dict[str, int] = defaultdict(int)
        longest_agent_utterance_words: int = 0
        filler_counts: Dict[str, int] = {f: 0 for f in _SINGLE_WORD_FILLERS}
        for mf in _MULTI_WORD_FILLERS:
            filler_counts[mf] = 0
        question_count: int = 0

        for seg in segments:
            # Explicit None check — see audio path comment above.
            sid = (
                seg.speaker_id if seg.speaker_id is not None
                else "__unknown__"
            )
            words = seg.text.split()
            speaker_word_count[sid] += len(words)

            is_agent = sid in agent_ids
            if is_agent:
                longest_agent_utterance_words = max(
                    longest_agent_utterance_words, len(words)
                )
                text_lower = seg.text.lower()
                for mf in _MULTI_WORD_FILLERS:
                    filler_counts[mf] += text_lower.count(mf)
                for fw in _SINGLE_WORD_FILLERS:
                    pattern = r"\b" + re.escape(fw) + r"\b"
                    filler_counts[fw] += len(re.findall(pattern, text_lower))
                sentences = _SENTENCE_SPLIT_RE.split(seg.text)
                for sentence in sentences:
                    if _QUESTION_RE.search(sentence):
                        question_count += 1

        total_words = sum(speaker_word_count.values()) or 1
        agent_words = sum(
            n for sid, n in speaker_word_count.items() if sid in agent_ids
        )
        customer_words = total_words - agent_words

        talk_pct = round(agent_words / total_words * 100, 2)
        listen_pct = round(customer_words / total_words * 100, 2)
        # Synthesize a seconds figure for the longest monologue so the
        # dashboard panel reads consistently with audio calls. 150 wpm
        # is roughly the lower bound of conversational pace.
        longest_monologue_sec = round(longest_agent_utterance_words / 150 * 60, 1)

        return {
            "talk_pct": talk_pct,
            "listen_pct": listen_pct,
            "longest_monologue_sec": longest_monologue_sec,
            "question_count": question_count,
            "filler_word_count": sum(filler_counts.values()),
            "filler_words": filler_counts,
            # Empty dict signals "no time basis available" to the frontend.
            "speech_rate_wpm": {},
            "interruptions": 0,
            "silence_pct": 0.0,
            # Surface the underlying word counts for transparency on the
            # SPA; the existing audio path doesn't emit these so they
            # only appear on text channels.
            "agent_words": agent_words,
            "customer_words": customer_words,
            "source": "text",
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
