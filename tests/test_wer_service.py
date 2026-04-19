"""Unit tests for the WER aggregation pipeline math."""

from __future__ import annotations

from backend.app.services import wer_service


def test_levenshtein_basic() -> None:
    assert wer_service._levenshtein("", "") == 0
    assert wer_service._levenshtein("hello", "hello") == 0
    assert wer_service._levenshtein("hello", "hallo") == 1
    assert wer_service._levenshtein("kitten", "sitting") == 3


def test_wer_for_pair_identical_is_zero() -> None:
    assert wer_service._wer_for_pair("hello world", "hello world") == 0.0


def test_wer_for_pair_complete_change_is_one() -> None:
    assert wer_service._wer_for_pair("aaaaa", "bbbbb") == 1.0


def test_wer_for_pair_handles_empty() -> None:
    assert wer_service._wer_for_pair("", "abcd") == 1.0
    assert wer_service._wer_for_pair("abcd", "") == 1.0
