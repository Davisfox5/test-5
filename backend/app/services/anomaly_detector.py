"""Anomaly detector for the manager dashboard.

Three sliding-window detectors that diff a recent window against a
baseline drawn from ``interactions.insights`` JSONB. No new feature
pipelines, no LLM calls inside the detector itself; each detected
anomaly INSERTs one ``ManagerAlert`` row whose title is later replaced
by a Haiku-rendered plain-English sentence by the alert fanout layer.

Detectors run on a sync SQLAlchemy ``Session`` because Celery beat is
the primary caller. Each detector is idempotent: a partial unique index
on ``(tenant_id, fingerprint) WHERE resolved_at IS NULL`` prevents the
same active spike from re-firing, so repeated runs are safe.

Cadence: every 15 minutes via ``anomaly_scan_all_tenants`` in tasks.py.
"""

from __future__ import annotations

import hashlib
import logging
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.models import (
    AlertChannelConfig,
    Interaction,
    ManagerAlert,
    Tenant,
)
from backend.app.services.plain_english import sanitize_manager_text

logger = logging.getLogger(__name__)


# ── Defaults (overridden per tenant via ``alert_channel_config``) ────────

DEFAULT_TOPIC_SPIKE_PCT_CHANGE = 500  # +500% week-over-week
DEFAULT_TOPIC_SPIKE_MIN_VOLUME = 5
DEFAULT_SENTIMENT_DROP = 1.5  # absolute drop in 0-10 score
DEFAULT_CHURN_SURGE_MULTIPLIER = 2.0
DEFAULT_METHODOLOGY_DROP = 0.15


@dataclass(frozen=True)
class DetectedAnomaly:
    kind: str
    severity: str
    title: str
    body: str
    evidence: Dict[str, Any]
    fingerprint: str


# ── Public entry points ──────────────────────────────────────────────────


def scan_tenant(session: Session, tenant: Tenant) -> List[ManagerAlert]:
    """Run all detectors for one tenant. Insert and return new alerts.

    Each detector runs independently; one failing detector doesn't block
    the others. Returns the list of newly inserted ``ManagerAlert`` rows
    (existing fingerprints are silently skipped via the partial unique
    index).
    """
    config = _load_config(session, tenant.id)
    now = datetime.now(timezone.utc)
    found: List[DetectedAnomaly] = []

    for detector in (_detect_topic_spike, _detect_sentiment_drop, _detect_churn_surge):
        try:
            anomalies = detector(session, tenant, config, now)
            found.extend(anomalies)
        except Exception:
            logger.exception(
                "Anomaly detector %s failed for tenant %s",
                detector.__name__,
                tenant.id,
            )

    inserted: List[ManagerAlert] = []
    for a in found:
        row = _insert_alert(session, tenant.id, a)
        if row is not None:
            inserted.append(row)
    if inserted:
        session.commit()
    return inserted


def scan_all_tenants(session: Session) -> Dict[str, Any]:
    """Beat-task entrypoint. Iterates every tenant, collects per-tenant
    inserted-alert counts, never lets one failure abort the batch."""
    tenants = session.execute(select(Tenant)).scalars().all()
    by_tenant: Dict[str, int] = {}
    total = 0
    for tenant in tenants:
        try:
            inserted = scan_tenant(session, tenant)
            by_tenant[str(tenant.id)] = len(inserted)
            total += len(inserted)
        except Exception:
            logger.exception(
                "scan_tenant failed for tenant %s (non-fatal)", tenant.id
            )
            by_tenant[str(tenant.id)] = -1
    return {"tenants_scanned": len(tenants), "alerts_inserted": total, "by_tenant": by_tenant}


def resolve_stale(session: Session, *, dry_run: bool = False) -> int:
    """Mark ``resolved_at`` on alerts whose underlying spike has subsided.

    Called every 6h via beat. Conservative: only resolves alerts where
    the originating fingerprint condition is no longer true in the most
    recent 24h window. Releases the partial-unique slot so a recurring
    spike can re-fire.
    """
    now = datetime.now(timezone.utc)
    stmt = select(ManagerAlert).where(
        ManagerAlert.resolved_at.is_(None),
        ManagerAlert.opened_at < now - timedelta(hours=12),
    )
    rows = session.execute(stmt).scalars().all()
    resolved = 0
    for alert in rows:
        if _still_active(session, alert):
            continue
        alert.resolved_at = now
        resolved += 1
    if resolved and not dry_run:
        session.commit()
    return resolved


# ── Topic-spike detector ────────────────────────────────────────────────


def _detect_topic_spike(
    session: Session,
    tenant: Tenant,
    config: AlertChannelConfig,
    now: datetime,
) -> List[DetectedAnomaly]:
    """Topic mentions in the last 48h vs the prior 7d median.

    Pulls ``insights->'topics'`` per interaction, aggregates by topic
    name in Python (JSONB array unnesting is fiddly in SQLAlchemy core
    and the volumes here are small). Topic names are lower-cased and
    stripped to avoid double-counting near-duplicates.
    """
    threshold = (
        config.topic_spike_pct_change_threshold
        if config and config.topic_spike_pct_change_threshold is not None
        else DEFAULT_TOPIC_SPIKE_PCT_CHANGE
    )
    min_volume = (
        config.topic_spike_min_volume
        if config and config.topic_spike_min_volume is not None
        else DEFAULT_TOPIC_SPIKE_MIN_VOLUME
    )

    window = now - timedelta(hours=48)
    baseline_start = now - timedelta(days=9)
    baseline_end = window

    recent_counts = _topic_counts(session, tenant.id, window, now)
    baseline_counts = _topic_counts(session, tenant.id, baseline_start, baseline_end)

    # Baseline median is per-day: split the 7d span into 7 daily windows
    # and take the median count per topic. Median beats mean for noisy
    # daily volumes.
    daily_baselines: Dict[str, List[int]] = {}
    for day_start in (baseline_start + timedelta(days=i) for i in range(7)):
        day_counts = _topic_counts(
            session, tenant.id, day_start, day_start + timedelta(days=1)
        )
        for topic, count in day_counts.items():
            daily_baselines.setdefault(topic, []).append(count)
        for topic in recent_counts:
            if topic not in day_counts:
                daily_baselines.setdefault(topic, []).append(0)

    found: List[DetectedAnomaly] = []
    for topic, current in recent_counts.items():
        if current < min_volume:
            continue
        history = daily_baselines.get(topic) or [0]
        prior_median = statistics.median(history) if history else 0.0
        # 48h current vs 1-day median * 2 — scale to comparable window.
        prior_expected = max(prior_median * 2.0, 1.0)
        pct_change = ((current - prior_expected) / prior_expected) * 100.0
        if pct_change < threshold:
            continue
        severity = _severity_from_pct_change(pct_change)
        evidence = {
            "topic": topic,
            "current_count": current,
            "prior_median_per_day": prior_median,
            "pct_change": round(pct_change, 1),
            "window_hours": 48,
            "min_volume": min_volume,
        }
        title_raw = (
            f"{topic.capitalize()} mentions jumped {int(pct_change)}% in 48 hours "
            f"({current} calls)."
        )
        found.append(
            DetectedAnomaly(
                kind="topic_spike",
                severity=severity,
                title=sanitize_manager_text(title_raw, max_words=25),
                body=(
                    f"Topic baseline was about {prior_median:.1f} mentions per day; "
                    f"the last 48 hours saw {current}."
                ),
                evidence=evidence,
                fingerprint=_fingerprint("topic_spike", topic),
            )
        )
    return found


def _topic_counts(
    session: Session, tenant_id, start: datetime, end: datetime
) -> Dict[str, int]:
    """Sum mention-style topic counts per topic name in a window.

    Tolerates the two observed shapes in ``insights.topics``:
    a list of strings, and a list of ``{name, mentions}`` dicts.
    """
    stmt = select(Interaction.insights).where(
        Interaction.tenant_id == tenant_id,
        Interaction.created_at >= start,
        Interaction.created_at < end,
    )
    counts: Dict[str, int] = {}
    for (insights,) in session.execute(stmt).all():
        if not isinstance(insights, dict):
            continue
        topics = insights.get("topics")
        if not isinstance(topics, list):
            continue
        for entry in topics:
            if isinstance(entry, str):
                name, n = entry.strip().lower(), 1
            elif isinstance(entry, dict):
                raw = entry.get("name") or entry.get("topic")
                if not isinstance(raw, str):
                    continue
                name = raw.strip().lower()
                n = int(entry.get("mentions") or entry.get("count") or 1)
            else:
                continue
            if not name:
                continue
            counts[name] = counts.get(name, 0) + n
    return counts


# ── Sentiment-drop detector ─────────────────────────────────────────────


def _detect_sentiment_drop(
    session: Session,
    tenant: Tenant,
    config: AlertChannelConfig,
    now: datetime,
) -> List[DetectedAnomaly]:
    """Rolling 24h sentiment average vs the prior 14d baseline."""
    drop = (
        float(config.sentiment_drop_threshold)
        if config and config.sentiment_drop_threshold is not None
        else DEFAULT_SENTIMENT_DROP
    )

    recent_start = now - timedelta(hours=24)
    baseline_start = now - timedelta(days=15)
    baseline_end = recent_start

    recent_scores = _sentiment_scores(session, tenant.id, recent_start, now)
    baseline_scores = _sentiment_scores(session, tenant.id, baseline_start, baseline_end)

    if len(recent_scores) < 10 or not baseline_scores:
        return []

    recent_avg = statistics.mean(recent_scores)
    baseline_avg = statistics.mean(baseline_scores)
    delta = baseline_avg - recent_avg
    if delta < drop:
        return []

    severity = "high" if delta >= drop + 1.0 else "medium"
    evidence = {
        "current_avg": round(recent_avg, 2),
        "baseline_avg": round(baseline_avg, 2),
        "delta": round(delta, 2),
        "current_n": len(recent_scores),
        "baseline_n": len(baseline_scores),
        "window_hours": 24,
    }
    title_raw = (
        f"Sentiment dropped {delta:.1f} points over the last 24 hours "
        f"({recent_avg:.1f} vs {baseline_avg:.1f} baseline)."
    )
    return [
        DetectedAnomaly(
            kind="sentiment_drop",
            severity=severity,
            title=sanitize_manager_text(title_raw, max_words=25),
            body=(
                f"{len(recent_scores)} calls in 24h averaged "
                f"{recent_avg:.1f}; 14-day baseline was {baseline_avg:.1f}."
            ),
            evidence=evidence,
            fingerprint=_fingerprint("sentiment_drop", "tenant"),
        )
    ]


def _sentiment_scores(
    session: Session, tenant_id, start: datetime, end: datetime
) -> List[float]:
    stmt = select(Interaction.insights).where(
        Interaction.tenant_id == tenant_id,
        Interaction.created_at >= start,
        Interaction.created_at < end,
    )
    out: List[float] = []
    for (insights,) in session.execute(stmt).all():
        if not isinstance(insights, dict):
            continue
        raw = insights.get("sentiment_score")
        try:
            if raw is not None:
                out.append(float(raw))
        except (TypeError, ValueError):
            continue
    return out


# ── Churn-surge detector ────────────────────────────────────────────────


def _detect_churn_surge(
    session: Session,
    tenant: Tenant,
    config: AlertChannelConfig,
    now: datetime,
) -> List[DetectedAnomaly]:
    """24h count of high-churn-risk calls vs the 14d daily median."""
    multiplier = (
        float(config.churn_surge_multiplier)
        if config and config.churn_surge_multiplier is not None
        else DEFAULT_CHURN_SURGE_MULTIPLIER
    )

    recent_start = now - timedelta(hours=24)
    current = _high_churn_count(session, tenant.id, recent_start, now)
    if current < 3:
        return []

    baseline_start = now - timedelta(days=15)
    daily_counts: List[int] = []
    for i in range(14):
        s = baseline_start + timedelta(days=i)
        daily_counts.append(_high_churn_count(session, tenant.id, s, s + timedelta(days=1)))
    median = statistics.median(daily_counts) if daily_counts else 0.0
    threshold = max(median * multiplier, 3.0)
    if current < threshold:
        return []

    severity = "high" if current >= max(median * (multiplier + 1.0), 5.0) else "medium"
    evidence = {
        "current_count": current,
        "baseline_daily_median": median,
        "multiplier": multiplier,
        "window_hours": 24,
    }
    title_raw = (
        f"{current} high-churn-risk calls in 24 hours, vs typical "
        f"{int(median)} per day."
    )
    return [
        DetectedAnomaly(
            kind="churn_surge",
            severity=severity,
            title=sanitize_manager_text(title_raw, max_words=25),
            body=(
                f"Baseline median is {median:.1f} per day across the last 14 days. "
                f"Current 24h count crossed {threshold:.0f}."
            ),
            evidence=evidence,
            fingerprint=_fingerprint("churn_surge", "tenant"),
        )
    ]


def _high_churn_count(
    session: Session, tenant_id, start: datetime, end: datetime
) -> int:
    stmt = select(Interaction.insights).where(
        Interaction.tenant_id == tenant_id,
        Interaction.created_at >= start,
        Interaction.created_at < end,
    )
    count = 0
    for (insights,) in session.execute(stmt).all():
        if not isinstance(insights, dict):
            continue
        signal = insights.get("churn_risk_signal")
        if isinstance(signal, str) and signal.lower() == "high":
            count += 1
    return count


# ── Resolution check (used by resolve_stale) ────────────────────────────


def _still_active(session: Session, alert: ManagerAlert) -> bool:
    """Best-effort check that the alert's condition is still true.

    Conservative: when we can't prove the condition has subsided, we
    leave the alert open. That keeps a flaky data source from clearing
    real alerts; the manager can always dismiss explicitly.
    """
    now = datetime.now(timezone.utc)
    tenant = session.get(Tenant, alert.tenant_id)
    if tenant is None:
        return False
    if alert.kind == "topic_spike":
        topic = (alert.evidence or {}).get("topic")
        if not isinstance(topic, str):
            return True
        recent = _topic_counts(session, tenant.id, now - timedelta(hours=48), now)
        min_vol = (alert.evidence or {}).get("min_volume") or DEFAULT_TOPIC_SPIKE_MIN_VOLUME
        return recent.get(topic, 0) >= int(min_vol)
    if alert.kind == "sentiment_drop":
        scores = _sentiment_scores(session, tenant.id, now - timedelta(hours=24), now)
        if len(scores) < 10:
            return True
        baseline_avg = (alert.evidence or {}).get("baseline_avg")
        delta = (alert.evidence or {}).get("delta") or DEFAULT_SENTIMENT_DROP
        if not isinstance(baseline_avg, (int, float)):
            return True
        return (float(baseline_avg) - statistics.mean(scores)) >= float(delta) * 0.5
    if alert.kind == "churn_surge":
        current = _high_churn_count(session, tenant.id, now - timedelta(hours=24), now)
        baseline = (alert.evidence or {}).get("baseline_daily_median") or 0
        return current >= max(float(baseline) * 1.2, 3.0)
    return True


# ── Helpers ─────────────────────────────────────────────────────────────


def _load_config(session: Session, tenant_id) -> AlertChannelConfig:
    cfg = session.get(AlertChannelConfig, tenant_id)
    if cfg is None:
        cfg = AlertChannelConfig(tenant_id=tenant_id)
        session.add(cfg)
        session.flush()
    return cfg


def _fingerprint(kind: str, subject: str) -> str:
    return hashlib.sha256(f"{kind}::{subject.lower()}".encode("utf-8")).hexdigest()[:32]


def _severity_from_pct_change(pct: float) -> str:
    if pct >= 1000:
        return "high"
    if pct >= 500:
        return "medium"
    return "low"


def _insert_alert(
    session: Session, tenant_id, anomaly: DetectedAnomaly
) -> Optional[ManagerAlert]:
    """Insert one alert, deduping against any active fingerprint.

    The Postgres partial unique index is the durable correctness layer;
    the Python pre-check here makes the dedupe work consistently even
    when running against SQLite in unit tests (which doesn't honor the
    partial-index ``WHERE`` clause the same way).
    """
    existing = session.execute(
        select(ManagerAlert.id).where(
            ManagerAlert.tenant_id == tenant_id,
            ManagerAlert.fingerprint == anomaly.fingerprint,
            ManagerAlert.resolved_at.is_(None),
        )
    ).first()
    if existing is not None:
        return None
    row = ManagerAlert(
        tenant_id=tenant_id,
        kind=anomaly.kind,
        severity=anomaly.severity,
        title=anomaly.title,
        body=anomaly.body,
        evidence=anomaly.evidence,
        fingerprint=anomaly.fingerprint,
    )
    session.add(row)
    try:
        session.flush()
        return row
    except IntegrityError:
        session.rollback()
        return None
