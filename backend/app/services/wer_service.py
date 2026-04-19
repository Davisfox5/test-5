"""Word-Error-Rate aggregation pipeline.

Runs weekly (Sun 02:00 UTC).  For each tenant:

1. Pull ``transcript_corrections`` from the prior 7 days.
2. Compute character-level edit distance per correction.
3. Aggregate by ``(tenant, asr_engine, channel)`` into ``wer_metrics``.
4. Fan out a ``vocabulary.candidate_pending`` webhook when WER > 8% as a
   prompt for the tenant admin to review the candidate keyterms.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, Tuple

from sqlalchemy.orm import Session

from backend.app.models import (
    Interaction,
    Tenant,
    TranscriptCorrection,
    WerMetric,
)

logger = logging.getLogger(__name__)

WER_VOCAB_REVIEW_THRESHOLD = 0.08  # 8%
WER_ASR_UPGRADE_THRESHOLD = 0.12   # 12%


def _levenshtein(a: str, b: str) -> int:
    """Simple O(len(a)*len(b)) edit distance.

    Pure Python so the service has no extra deps; corrections are short
    enough (< 200 chars typical) that this is a non-issue.
    """
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        curr = [i] + [0] * len(b)
        for j, cb in enumerate(b, start=1):
            ins = curr[j - 1] + 1
            dele = prev[j] + 1
            sub = prev[j - 1] + (0 if ca == cb else 1)
            curr[j] = min(ins, dele, sub)
        prev = curr
    return prev[-1]


def _wer_for_pair(original: str, corrected: str) -> float:
    """Word-error rate, normalised to a [0, 1] float (so we can store as Float)."""
    distance = _levenshtein(original, corrected)
    word_count = max(len((original or "").split()), 1)
    return min(1.0, distance / max(len(original), len(corrected), 1))


def compute_weekly(session: Session) -> Dict[str, Any]:
    """Aggregate the prior 7 days of corrections per (tenant, engine, channel)."""
    period_end = date.today()
    period_start = period_end - timedelta(days=7)
    cutoff = datetime.combine(period_start, datetime.min.time())

    # Join corrections to interactions so we can group by engine + channel.
    rows = (
        session.query(
            TranscriptCorrection.tenant_id,
            Interaction.engine,
            Interaction.channel,
            TranscriptCorrection.original_text,
            TranscriptCorrection.corrected_text,
        )
        .join(Interaction, Interaction.id == TranscriptCorrection.interaction_id)
        .filter(TranscriptCorrection.created_at >= cutoff)
        .all()
    )

    buckets: Dict[Tuple[Any, str, str], Tuple[int, float]] = {}
    for tenant_id, engine, channel, original, corrected in rows:
        key = (tenant_id, engine or "unknown", channel or "unknown")
        wer = _wer_for_pair(original or "", corrected or "")
        n, total = buckets.get(key, (0, 0.0))
        buckets[key] = (n + 1, total + wer)

    written = 0
    breached = 0
    for (tenant_id, engine, channel), (n, total) in buckets.items():
        wer_avg = total / n
        session.add(
            WerMetric(
                tenant_id=tenant_id,
                asr_engine=engine,
                channel=channel,
                sample_size=n,
                word_error_rate=round(wer_avg, 4),
                period_start=period_start,
                period_end=period_end,
            )
        )
        written += 1
        if wer_avg > WER_VOCAB_REVIEW_THRESHOLD:
            breached += 1
            _alert_high_wer(session, tenant_id, engine, channel, wer_avg)
    session.commit()
    return {
        "tenants_processed": len({k[0] for k in buckets.keys()}),
        "buckets_written": written,
        "high_wer_alerts": breached,
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
    }


def _alert_high_wer(
    session: Session, tenant_id: Any, engine: str, channel: str, wer: float
) -> None:
    severity = "asr_upgrade" if wer > WER_ASR_UPGRADE_THRESHOLD else "vocab_review"
    try:
        from backend.app.services.webhook_dispatcher import dispatch_sync

        dispatch_sync(
            session,
            tenant_id,
            "vocabulary.candidate_pending",
            {
                "event": "vocabulary.candidate_pending",
                "tenant_id": str(tenant_id),
                "asr_engine": engine,
                "channel": channel,
                "word_error_rate": round(wer, 4),
                "severity": severity,
            },
        )
    except Exception:
        logger.exception("WER alert webhook dispatch failed (non-fatal)")
