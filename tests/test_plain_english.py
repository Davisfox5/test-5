"""Unit tests for the manager-voice sanitizer."""

from __future__ import annotations

import pytest

from backend.app.services.plain_english import (
    sanitize_manager_payload,
    sanitize_manager_text,
)


def test_strips_em_dash_replaces_with_period():
    out = sanitize_manager_text("Refund volume jumped 6x — service issue likely.", max_words=25)
    assert "—" not in out
    assert "." in out


def test_strips_en_dash():
    out = sanitize_manager_text("Q3 close rate – up 12% week-over-week.")
    assert "–" not in out


def test_caps_word_count():
    long = "one two three four five six seven eight nine ten eleven twelve"
    out = sanitize_manager_text(long, max_words=5)
    assert len(out.split()) == 5


def test_strips_banned_phrase_in_conclusion():
    out = sanitize_manager_text("In conclusion, sentiment dropped 1.4 points.", max_words=25)
    assert "in conclusion" not in out.lower()
    assert "sentiment dropped" in out.lower()


def test_strips_banned_phrase_just_to_be_clear():
    out = sanitize_manager_text("Just to be clear, churn is up 30% this week.", max_words=25)
    assert "just to be clear" not in out.lower()
    assert "churn is up" in out.lower()


def test_empty_string_round_trip():
    assert sanitize_manager_text("") == ""


def test_preserves_quote_keys_in_payload():
    payload = {
        "title": "Refund mentions jumped — by 6x",
        "evidence_quote": "We need a refund — immediately.",
        "rationale": "It's worth noting that volume tripled.",
    }
    sanitize_manager_payload(
        payload, max_words_per_field={"title": 25, "rationale": 25}
    )
    # The quote field retains its em-dash verbatim.
    assert "—" in payload["evidence_quote"]
    # Title is scrubbed.
    assert "—" not in payload["title"]
    # Banned phrase is scrubbed from rationale.
    assert "it's worth noting" not in payload["rationale"].lower()


def test_recursive_scrub_into_lists_of_dicts():
    payload = {
        "recommendations": [
            {"title": "Coach the team — discovery is weak."},
            {"title": "Run a campaign – pricing pushback."},
        ]
    }
    sanitize_manager_payload(payload, max_words_per_field={"title": 25})
    for rec in payload["recommendations"]:
        assert "—" not in rec["title"]
        assert "–" not in rec["title"]


def test_word_cap_respects_sentence_punctuation():
    out = sanitize_manager_text(
        "one two three four five six seven", max_words=4
    )
    assert out.endswith(".")
    assert len(out.split()) == 4


@pytest.mark.parametrize(
    "phrase",
    [
        "going forward, consider",
        "make sure to",
        "remember to",
        "i want to make sure",
    ],
)
def test_banned_phrases_removed(phrase):
    sentence = f"Sentiment is down 2 points. {phrase} review yesterday's calls."
    out = sanitize_manager_text(sentence)
    assert phrase.lower() not in out.lower()
