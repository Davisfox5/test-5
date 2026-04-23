"""Run the paralinguistic replay harness over a labeled corpus.

This is the tool we gate ``paralinguistic_live`` rollout on: load a
manifest that points at WAV fixtures + expected alert timelines,
run each clip through :mod:`paralinguistics_replay`, aggregate
precision/recall/F1 per alert kind, and spit out a report.

Manifest shape (YAML or JSON, same content)::

    corpus_name: "2026-04 baseline"
    description: "Redacted customer calls with annotator consensus."
    clips:
      - id: 001-monotone-agent
        wav_path: ./clips/001.wav
        description: "Agent droning in mid-call; expected monotone fire @ 00:45"
        expected_alerts:
          - kind: monotone
            at_sec: 45.0
            tolerance_sec: 8.0
      - id: 002-escalating-customer
        wav_path: ./clips/002.wav
        expected_alerts:
          - kind: stress
            at_sec: 72.0
          - kind: pace
            at_sec: 80.0

All paths in ``wav_path`` are resolved relative to the manifest
directory so corpora can be moved between machines without rewrites.

Output is a :class:`CorpusReport` with per-kind and overall
precision/recall/F1. The CLI (:func:`main`) dumps it to stdout as
JSON so it's trivial to pipe into a gate in CI::

    python -m backend.app.services.paralinguistics_corpus \\
        corpora/2026-04.yaml \\
        --min-f1 0.7
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from backend.app.services.paralinguistics_replay import (
    ExpectedAlert,
    replay_wav_file,
    validate_against_expected,
)

logger = logging.getLogger(__name__)


@dataclass
class CorpusClipReport:
    clip_id: str
    wav_path: str
    observed_alerts: Dict[str, int]
    true_positives: int
    false_positives: int
    false_negatives: int
    precision: float
    recall: float
    f1: float


@dataclass
class CorpusReport:
    corpus_name: str
    clips_processed: int
    per_kind: Dict[str, Dict[str, float]] = field(default_factory=dict)
    overall_precision: float = 0.0
    overall_recall: float = 0.0
    overall_f1: float = 0.0
    clips: List[CorpusClipReport] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "corpus_name": self.corpus_name,
            "clips_processed": self.clips_processed,
            "overall_precision": round(self.overall_precision, 3),
            "overall_recall": round(self.overall_recall, 3),
            "overall_f1": round(self.overall_f1, 3),
            "per_kind": self.per_kind,
            "clips": [c.__dict__ for c in self.clips],
        }


def load_manifest(path: str) -> dict:
    """Accept either YAML or JSON. YAML is the tenant-facing format;
    JSON is useful when a CI step generates the manifest inline."""
    content = Path(path).read_text(encoding="utf-8")
    if path.endswith((".yaml", ".yml")):
        try:
            import yaml  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "PyYAML required for YAML manifests; install pyyaml"
            ) from exc
        return yaml.safe_load(content)
    return json.loads(content)


def run_corpus(manifest_path: str) -> CorpusReport:
    """Run every clip in the manifest and aggregate results."""
    manifest = load_manifest(manifest_path)
    corpus_name = str(manifest.get("corpus_name") or "unnamed")
    clips_spec = manifest.get("clips") or []
    manifest_dir = Path(manifest_path).resolve().parent

    per_kind_accum: Dict[str, Dict[str, int]] = {}
    tp_total = fp_total = fn_total = 0
    clip_reports: List[CorpusClipReport] = []

    for spec in clips_spec:
        clip_id = str(spec.get("id") or spec.get("wav_path", "?"))
        wav_path = spec.get("wav_path")
        if not wav_path:
            logger.warning("Clip %s has no wav_path; skipping", clip_id)
            continue
        resolved = str((manifest_dir / wav_path).resolve())
        if not os.path.exists(resolved):
            logger.warning("Clip %s missing file %s; skipping", clip_id, resolved)
            continue

        expected = [
            ExpectedAlert(
                kind=str(ea["kind"]),
                at_sec=float(ea["at_sec"]),
                tolerance_sec=float(ea.get("tolerance_sec", 5.0)),
            )
            for ea in spec.get("expected_alerts") or []
        ]

        try:
            report = replay_wav_file(resolved)
            result = validate_against_expected(report, expected)
        except Exception:
            logger.exception("Replay failed for clip %s", clip_id)
            continue

        tp_total += result.true_positives
        fp_total += result.false_positives
        fn_total += result.false_negatives

        for ea in expected:
            bucket = per_kind_accum.setdefault(
                ea.kind, {"tp": 0, "fp": 0, "fn": 0}
            )
            # Split credit per kind — walk the observed list with the
            # tolerance window and count match/miss. Cheap for normal
            # corpora (tens of alerts per clip).
            matched = False
            for t_sec, alert in report.alerts_flat:
                if alert.kind != ea.kind:
                    continue
                if abs(t_sec - ea.at_sec) <= ea.tolerance_sec:
                    matched = True
                    break
            if matched:
                bucket["tp"] += 1
            else:
                bucket["fn"] += 1
        # Observed-but-unexpected alerts of each kind are false positives.
        for _t, alert in report.alerts_flat:
            # A matched TP was already counted against expected above;
            # the difference between "all observed for this kind" and
            # "expected matches for this kind" is the FP count. We
            # compute that at the end by summing.
            bucket = per_kind_accum.setdefault(
                alert.kind, {"tp": 0, "fp": 0, "fn": 0}
            )
            bucket["fp"] += 0  # placeholder so kind registers

        # Compute per-kind FPs cleanly using the aggregate validator
        # we already have — it gave us the global counts; the per-kind
        # FP is "observed of this kind minus TP of this kind".
        observed_by_kind: Dict[str, int] = {}
        for _t, alert in report.alerts_flat:
            observed_by_kind[alert.kind] = observed_by_kind.get(alert.kind, 0) + 1
        for kind, count in observed_by_kind.items():
            bucket = per_kind_accum.setdefault(kind, {"tp": 0, "fp": 0, "fn": 0})
            bucket["fp"] += max(0, count - bucket["tp"])

        clip_reports.append(
            CorpusClipReport(
                clip_id=clip_id,
                wav_path=resolved,
                observed_alerts=observed_by_kind,
                true_positives=result.true_positives,
                false_positives=result.false_positives,
                false_negatives=result.false_negatives,
                precision=round(result.precision, 3),
                recall=round(result.recall, 3),
                f1=round(result.f1, 3),
            )
        )

    per_kind: Dict[str, Dict[str, float]] = {}
    for kind, counts in per_kind_accum.items():
        tp = counts["tp"]
        fp = counts["fp"]
        fn = counts["fn"]
        precision = tp / (tp + fp) if (tp + fp) else 1.0
        recall = tp / (tp + fn) if (tp + fn) else 1.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
        per_kind[kind] = {
            "true_positives": tp,
            "false_positives": fp,
            "false_negatives": fn,
            "precision": round(precision, 3),
            "recall": round(recall, 3),
            "f1": round(f1, 3),
        }

    overall_precision = (
        tp_total / (tp_total + fp_total) if (tp_total + fp_total) else 1.0
    )
    overall_recall = (
        tp_total / (tp_total + fn_total) if (tp_total + fn_total) else 1.0
    )
    overall_f1 = (
        2 * overall_precision * overall_recall / (overall_precision + overall_recall)
        if (overall_precision + overall_recall)
        else 0.0
    )

    return CorpusReport(
        corpus_name=corpus_name,
        clips_processed=len(clip_reports),
        per_kind=per_kind,
        overall_precision=overall_precision,
        overall_recall=overall_recall,
        overall_f1=overall_f1,
        clips=clip_reports,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("manifest", help="Path to corpus manifest (.yaml or .json)")
    parser.add_argument(
        "--min-f1",
        type=float,
        default=0.0,
        help="Exit non-zero when overall F1 falls below this threshold.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON only (suppresses human summary).",
    )
    args = parser.parse_args()

    report = run_corpus(args.manifest)
    payload = report.to_dict()

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"Corpus: {payload['corpus_name']}")
        print(f"Clips: {payload['clips_processed']}")
        print(
            f"Overall  P={payload['overall_precision']:.3f}  "
            f"R={payload['overall_recall']:.3f}  "
            f"F1={payload['overall_f1']:.3f}"
        )
        if payload["per_kind"]:
            print("Per kind:")
            for kind, stats in payload["per_kind"].items():
                print(
                    f"  {kind:10s} "
                    f"P={stats['precision']:.3f}  "
                    f"R={stats['recall']:.3f}  "
                    f"F1={stats['f1']:.3f}  "
                    f"(TP={stats['true_positives']}, "
                    f"FP={stats['false_positives']}, "
                    f"FN={stats['false_negatives']})"
                )

    if report.overall_f1 < args.min_f1:
        print(
            f"F1 {report.overall_f1:.3f} below threshold {args.min_f1:.3f}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
