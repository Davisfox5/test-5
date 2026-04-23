"""Tests for the paralinguistic corpus harness."""

from __future__ import annotations

import json
import math
import struct
import wave
from pathlib import Path

import pytest

from backend.app.services.paralinguistics_corpus import (
    CorpusReport,
    load_manifest,
    run_corpus,
)


def _write_wav(path: Path, freq: float, duration_sec: float = 2.0) -> None:
    rate = 16000
    n = int(rate * duration_sec)
    samples = [
        int(0.25 * 32767 * math.sin(2 * math.pi * freq * i / rate))
        for i in range(n)
    ]
    pcm = struct.pack(f"<{n}h", *samples)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)


def test_load_manifest_supports_json_and_yaml(tmp_path: Path):
    json_path = tmp_path / "m.json"
    json_path.write_text(json.dumps({"corpus_name": "x", "clips": []}))
    assert load_manifest(str(json_path))["corpus_name"] == "x"


def test_run_corpus_empty_manifest_returns_perfect_scores(tmp_path: Path):
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({"corpus_name": "empty", "clips": []}))
    report = run_corpus(str(manifest))
    assert isinstance(report, CorpusReport)
    assert report.clips_processed == 0
    # Vacuous-truth semantics: with no expectations and no observations,
    # P/R/F1 == 1.0 — matches how the validator treats an empty list.
    assert report.overall_precision == 1.0
    assert report.overall_recall == 1.0


def test_run_corpus_skips_missing_wav_files(tmp_path: Path):
    manifest = tmp_path / "m.json"
    manifest.write_text(
        json.dumps(
            {
                "corpus_name": "skips",
                "clips": [
                    {
                        "id": "missing",
                        "wav_path": "./does-not-exist.wav",
                        "expected_alerts": [],
                    }
                ],
            }
        )
    )
    report = run_corpus(str(manifest))
    assert report.clips_processed == 0


def test_run_corpus_counts_zero_expected_zero_observed(tmp_path: Path):
    """A clip with no expectations and no observed alerts still counts
    toward the processed total and produces 0 TP/FP/FN."""
    wav_path = tmp_path / "silence.wav"
    _write_wav(wav_path, freq=100, duration_sec=2.0)

    manifest = tmp_path / "m.json"
    manifest.write_text(
        json.dumps(
            {
                "corpus_name": "silence",
                "clips": [
                    {
                        "id": "silence",
                        "wav_path": "./silence.wav",
                        "expected_alerts": [],
                    }
                ],
            }
        )
    )
    report = run_corpus(str(manifest))
    assert report.clips_processed == 1
    assert report.clips[0].true_positives == 0
    assert report.clips[0].false_negatives == 0


def test_report_to_dict_roundtrip_includes_per_kind(tmp_path: Path):
    """Structural check of the CorpusReport shape."""
    report = CorpusReport(
        corpus_name="x",
        clips_processed=1,
        per_kind={"monotone": {"precision": 1.0, "recall": 1.0, "f1": 1.0}},
        overall_precision=1.0,
        overall_recall=1.0,
        overall_f1=1.0,
    )
    payload = report.to_dict()
    assert payload["corpus_name"] == "x"
    assert payload["per_kind"]["monotone"]["f1"] == 1.0
    assert payload["overall_f1"] == 1.0
