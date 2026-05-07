"""Tests for the paralinguistic prompt-block formatter.

Pins the rendered structured-block string and the inline-tag map so a
stealth wording change is caught at PR time.
"""

from __future__ import annotations

from backend.app.services.paralinguistic_baseline import NotableTag
from backend.app.services.paralinguistic_prompt import (
    PROMPT_HEADER,
    PROMPT_INSTRUCTION,
    SPEAKER_FIELDS,
    ParalinguisticPromptBlock,
    build_prompt_block,
)


def test_speaker_fields_match_extractor_keys():
    """Pinning the keys we read off ``ParalinguisticFeatures`` so a
    rename in ``paralinguistics.py`` _measure_slices() can't silently
    blank the prompt block."""
    assert {f["key"] for f in SPEAKER_FIELDS} == {
        "pitch_hz_p50",
        "pitch_std_semitones",
        "speaking_rate_syll_per_sec",
        "pause_rate_per_min",
        "intensity_db_p50",
    }


def test_build_prompt_block_silent_fallback_when_unavailable():
    """Decision Q3: when the extractor returned available=False the
    prompt block must be empty so the orchestration layer can skip
    injection entirely."""
    block = build_prompt_block({"available": False, "per_speaker": {}}, [])
    assert block.is_empty()
    assert block.structured_text == ""
    assert block.inline_tags == {}


def test_build_prompt_block_silent_fallback_on_none_input():
    block = build_prompt_block(None, [])
    assert block.is_empty()


def test_build_prompt_block_renders_per_speaker_lines():
    para = {
        "available": True,
        "per_speaker": {
            "rep": {
                "pitch_hz_p50": 142.0,
                "pitch_std_semitones": 3.7,
                "speaking_rate_syll_per_sec": 3.4,
                "pause_rate_per_min": 4.2,
                "intensity_db_p50": 64.3,
            },
            "customer": {
                "pitch_hz_p50": 195.0,
                "pitch_std_semitones": 7.1,
                "speaking_rate_syll_per_sec": 4.2,
                "pause_rate_per_min": 1.5,
                "intensity_db_p50": 68.0,
            },
        },
    }
    block = build_prompt_block(para, [])
    text = block.structured_text
    assert PROMPT_HEADER in text
    assert PROMPT_INSTRUCTION in text
    # Speakers ordered alphabetically — "customer" before "rep".
    cust_idx = text.index("- customer:")
    rep_idx = text.index("- rep:")
    assert cust_idx < rep_idx
    # Spot-check the rendered field shape.
    assert "pitch median 142 Hz" in text
    assert "pitch range 3.7 semitones" in text
    assert "speaking rate 3.40 syll/s" in text
    assert "pause rate 4.2 /min" in text
    assert "intensity 64 dB" in text


def test_build_prompt_block_skips_speakers_with_no_data():
    """A speaker line with all-None values shouldn't appear at all —
    no empty bullets in the prompt."""
    para = {
        "available": True,
        "per_speaker": {
            "rep": {"pitch_hz_p50": 130.0},
            "ghost": {
                "pitch_hz_p50": None,
                "pitch_std_semitones": None,
                "speaking_rate_syll_per_sec": None,
                "pause_rate_per_min": None,
                "intensity_db_p50": None,
            },
        },
    }
    block = build_prompt_block(para, [])
    assert "- ghost:" not in block.structured_text
    assert "- rep:" in block.structured_text


def test_build_prompt_block_inline_tags_directional():
    """Inline tag string carries direction (↑/↓) and magnitude in σ."""
    para = {
        "available": True,
        "per_speaker": {"rep": {"pitch_hz_p50": 130.0}},
    }
    notable = [
        NotableTag(
            segment_idx=12,
            speaker_id="rep",
            start=42.5,
            features=[("pitch", 1.8), ("pause_before", -1.6)],
        ),
    ]
    block = build_prompt_block(para, notable)
    assert block.inline_tags == {12: "[pitch ↑1.8σ · pause-before ↓1.6σ]"}


def test_build_prompt_block_arousal_appended_when_present():
    para = {
        "available": True,
        "per_speaker": {"rep": {"pitch_hz_p50": 130.0}},
        "arousal": {"rep": 0.62, "customer": 0.41},
    }
    block = build_prompt_block(para, [])
    text = block.structured_text
    assert "Arousal:" in text
    # Speakers sorted alphabetically inside the arousal line too.
    assert text.index("customer arousal 0.41") < text.index("rep arousal 0.62")


def test_paralinguistic_prompt_block_is_empty_helper():
    assert ParalinguisticPromptBlock().is_empty()
    assert not ParalinguisticPromptBlock(structured_text="x").is_empty()
    assert not ParalinguisticPromptBlock(inline_tags={1: "[x]"}).is_empty()
