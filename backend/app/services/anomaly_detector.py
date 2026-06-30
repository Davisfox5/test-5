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
from typing import Any, Dict, Iterable, List, Optional

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.models import (
    AlertChannelConfig,
    AlertDomainConfig,
    Interaction,
    ManagerAlert,
    SupportCase,
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
    # Which motion this anomaly belongs to. Drives which tab on the
    # Manager portal renders the resulting ManagerAlert and which
    # voice rules the (eventual) Haiku title-rewriter uses. Added
    # alongside migration ``dom_002``; existing detectors stamp
    # ``"sales"`` because their underlying signals (topics, sentiment,
    # churn) describe sales-floor activity.
    domain: str = "sales"


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

    # Sales detectors operate on sales-domain interactions; CS detectors
    # on customer_service; Support on it_support cases + interactions.
    # Each detector knows its own filter; the dispatch is just "run them
    # all and tolerate per-detector failures."
    all_detectors = (
        _detect_topic_spike,
        _detect_sentiment_drop,
        _detect_churn_surge,
        _detect_renewal_risk_spike,
        _detect_health_score_drop,
        _detect_csat_drop_support,
        _detect_escalation_surge,
        _detect_ttr_drift,
    )
    for detector in all_detectors:
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


# ── Shared windowed helpers (one query + Python day-bucketing) ──────────
#
# The detectors below compare a recent window against a per-day baseline
# median. The baseline used to be built by issuing one COUNT query per day
# (7 or 14 per detector, per tenant) — a textbook N+1 that Sentry flagged on
# ``manager_anomaly_scan_all_tenants``. Instead we fetch the whole baseline
# window in a single query and bucket the rows by day in Python.


def _bucket_by_day(
    timestamps: Iterable[datetime], baseline_start: datetime, num_days: int
) -> List[int]:
    """Count events per day bucket over ``num_days`` from ``baseline_start``.

    Bucket ``i`` covers ``[baseline_start + i days, baseline_start + (i+1)
    days)`` — identical windows to the old per-day loop, so the resulting
    daily counts (and their median) are unchanged.
    """
    one_day = timedelta(days=1)
    daily = [0] * num_days
    for ts in timestamps:
        idx = int((ts - baseline_start) // one_day)
        if 0 <= idx < num_days:
            daily[idx] += 1
    return daily


def _is_high_churn(insights: Any) -> bool:
    """True when an interaction's insights carry a high churn-risk signal."""
    if not isinstance(insights, dict):
        return False
    signal = insights.get("churn_risk_signal")
    return isinstance(signal, str) and signal.lower() == "high"


def _extract_topic_counts(insights: Any) -> Dict[str, int]:
    """Per-topic mention counts from one interaction's insights.

    Tolerates the two observed shapes in ``insights.topics``: a list of
    strings, and a list of ``{name, mentions}`` dicts.
    """
    counts: Dict[str, int] = {}
    if not isinstance(insights, dict):
        return counts
    topics = insights.get("topics")
    if not isinstance(topics, list):
        return counts
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

    # Baseline median is per-day: split the 7d span into 7 daily windows
    # and take the median count per topic. Median beats mean for noisy
    # daily volumes. Fetched in ONE query and bucketed by day in Python
    # (was 7 per-day queries — an N+1 on the 15-min scan).
    one_day = timedelta(days=1)
    daily_topic_counts: List[Dict[str, int]] = [dict() for _ in range(7)]
    baseline_stmt = select(Interaction.insights, Interaction.created_at).where(
        Interaction.tenant_id == tenant.id,
        Interaction.created_at >= baseline_start,
        Interaction.created_at < baseline_end,
        sa.or_(Interaction.domain == "sales", Interaction.domain.is_(None)),
    )
    for insights, created_at in session.execute(baseline_stmt).all():
        idx = int((created_at - baseline_start) // one_day)
        if not 0 <= idx < 7:
            continue
        bucket = daily_topic_counts[idx]
        for name, n in _extract_topic_counts(insights).items():
            bucket[name] = bucket.get(name, 0) + n

    daily_baselines: Dict[str, List[int]] = {}
    for day_counts in daily_topic_counts:
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
                domain="sales",
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
        # Restrict to sales-motion interactions. Pre-``dom_002`` rows
        # were backfilled to the tenant's default_domain — typically
        # "sales" — so this preserves behaviour for sales-only tenants
        # while making the detector correct for multi-motion tenants.
        sa.or_(Interaction.domain == "sales", Interaction.domain.is_(None)),
    )
    counts: Dict[str, int] = {}
    for (insights,) in session.execute(stmt).all():
        for name, n in _extract_topic_counts(insights).items():
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
            domain="sales",
        )
    ]


def _sentiment_scores(
    session: Session, tenant_id, start: datetime, end: datetime
) -> List[float]:
    stmt = select(Interaction.insights).where(
        Interaction.tenant_id == tenant_id,
        Interaction.created_at >= start,
        Interaction.created_at < end,
        # Restrict to sales-motion interactions. Pre-``dom_002`` rows
        # were backfilled to the tenant's default_domain — typically
        # "sales" — so this preserves behaviour for sales-only tenants
        # while making the detector correct for multi-motion tenants.
        sa.or_(Interaction.domain == "sales", Interaction.domain.is_(None)),
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
    # One query over the 14-day baseline, bucketed by day in Python
    # (was 14 per-day COUNT queries — an N+1 on the 15-min scan).
    baseline_rows = session.execute(
        select(Interaction.insights, Interaction.created_at).where(
            Interaction.tenant_id == tenant.id,
            Interaction.created_at >= baseline_start,
            Interaction.created_at < recent_start,
            sa.or_(Interaction.domain == "sales", Interaction.domain.is_(None)),
        )
    ).all()
    daily_counts = _bucket_by_day(
        (ca for insights, ca in baseline_rows if _is_high_churn(insights)),
        baseline_start,
        14,
    )
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
            domain="sales",
        )
    ]


def _high_churn_count(
    session: Session, tenant_id, start: datetime, end: datetime
) -> int:
    stmt = select(Interaction.insights).where(
        Interaction.tenant_id == tenant_id,
        Interaction.created_at >= start,
        Interaction.created_at < end,
        # Restrict to sales-motion interactions. Pre-``dom_002`` rows
        # were backfilled to the tenant's default_domain — typically
        # "sales" — so this preserves behaviour for sales-only tenants
        # while making the detector correct for multi-motion tenants.
        sa.or_(Interaction.domain == "sales", Interaction.domain.is_(None)),
    )
    count = 0
    for (insights,) in session.execute(stmt).all():
        if not isinstance(insights, dict):
            continue
        signal = insights.get("churn_risk_signal")
        if isinstance(signal, str) and signal.lower() == "high":
            count += 1
    return count


# ── CS detector: renewal-risk spike ─────────────────────────────────────
#
# Mirrors the sales ``churn_surge`` detector but reads CS-domain
# interactions only. The signal is the same shape (``churn_risk_signal``
# in insights), but the framing is renewal/account-health, not
# pipeline-loss.


def _cs_high_risk_count(
    session: Session, tenant_id, start: datetime, end: datetime
) -> int:
    stmt = select(Interaction.insights).where(
        Interaction.tenant_id == tenant_id,
        Interaction.created_at >= start,
        Interaction.created_at < end,
        Interaction.domain == "customer_service",
    )
    count = 0
    for (insights,) in session.execute(stmt).all():
        if not isinstance(insights, dict):
            continue
        signal = insights.get("churn_risk_signal")
        if isinstance(signal, str) and signal.lower() == "high":
            count += 1
    return count


def _detect_renewal_risk_spike(
    session: Session,
    tenant: Tenant,
    config: AlertChannelConfig,
    now: datetime,
) -> List[DetectedAnomaly]:
    domain_cfg = _load_domain_config(session, tenant.id, "customer_service")
    multiplier = float(
        _threshold(
            domain_cfg,
            config,
            "churn_surge_multiplier",
            DEFAULT_CHURN_SURGE_MULTIPLIER,
        )
    )

    recent_start = now - timedelta(hours=24)
    current = _cs_high_risk_count(session, tenant.id, recent_start, now)
    if current < 3:
        return []

    baseline_start = now - timedelta(days=15)
    # One query over the 14-day baseline, bucketed by day in Python
    # (was 14 per-day COUNT queries — an N+1 on the 15-min scan).
    baseline_rows = session.execute(
        select(Interaction.insights, Interaction.created_at).where(
            Interaction.tenant_id == tenant.id,
            Interaction.created_at >= baseline_start,
            Interaction.created_at < recent_start,
            Interaction.domain == "customer_service",
        )
    ).all()
    daily_counts = _bucket_by_day(
        (ca for insights, ca in baseline_rows if _is_high_churn(insights)),
        baseline_start,
        14,
    )
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
        f"{current} customers flagged as renewal risks in 24 hours, "
        f"vs typical {int(median)} per day."
    )
    return [
        DetectedAnomaly(
            kind="renewal_risk_spike",
            severity=severity,
            title=sanitize_manager_text(title_raw, max_words=25),
            body=(
                f"CS calls in the last 24 hours surfaced {current} accounts "
                f"with high churn signal; the 14-day daily median is "
                f"{median:.1f}."
            ),
            evidence=evidence,
            fingerprint=_fingerprint("renewal_risk_spike", "tenant"),
            domain="customer_service",
        )
    ]


# ── CS detector: account-health drop ────────────────────────────────────
#
# Average sentiment across CS interactions over 24h vs a 14d baseline.
# Conceptually similar to ``sentiment_drop`` but scoped to CS so a
# noisy sales week doesn't trigger a CS alert (or vice versa).


def _cs_sentiment_scores(
    session: Session, tenant_id, start: datetime, end: datetime
) -> List[float]:
    stmt = select(Interaction.insights).where(
        Interaction.tenant_id == tenant_id,
        Interaction.created_at >= start,
        Interaction.created_at < end,
        Interaction.domain == "customer_service",
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


def _detect_health_score_drop(
    session: Session,
    tenant: Tenant,
    config: AlertChannelConfig,
    now: datetime,
) -> List[DetectedAnomaly]:
    domain_cfg = _load_domain_config(session, tenant.id, "customer_service")
    drop = float(
        _threshold(
            domain_cfg,
            config,
            "sentiment_drop_threshold",
            DEFAULT_SENTIMENT_DROP,
        )
    )

    recent_start = now - timedelta(hours=24)
    baseline_start = now - timedelta(days=15)
    baseline_end = recent_start

    recent_scores = _cs_sentiment_scores(session, tenant.id, recent_start, now)
    baseline_scores = _cs_sentiment_scores(session, tenant.id, baseline_start, baseline_end)

    if len(recent_scores) < 5 or not baseline_scores:
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
        f"Account health dropped {delta:.1f} points across CS in 24 hours "
        f"({recent_avg:.1f} vs {baseline_avg:.1f} baseline)."
    )
    return [
        DetectedAnomaly(
            kind="health_score_drop",
            severity=severity,
            title=sanitize_manager_text(title_raw, max_words=25),
            body=(
                f"{len(recent_scores)} CS calls in 24h averaged "
                f"{recent_avg:.1f}; 14-day CS baseline was {baseline_avg:.1f}."
            ),
            evidence=evidence,
            fingerprint=_fingerprint("health_score_drop", "tenant"),
            domain="customer_service",
        )
    ]


# ── Support detector: CSAT drop ─────────────────────────────────────────


def _support_csat_scores(
    session: Session, tenant_id, start: datetime, end: datetime
) -> List[float]:
    """CSAT scores on cases resolved in the window. Pulls from SupportCase
    rows that have ``csat_score`` populated and a ``resolved_at`` in
    range."""
    stmt = select(SupportCase.csat_score).where(
        SupportCase.tenant_id == tenant_id,
        SupportCase.resolved_at.isnot(None),
        SupportCase.resolved_at >= start,
        SupportCase.resolved_at < end,
        SupportCase.csat_score.isnot(None),
    )
    out: List[float] = []
    for (score,) in session.execute(stmt).all():
        if score is None:
            continue
        try:
            out.append(float(score))
        except (TypeError, ValueError):
            continue
    return out


def _detect_csat_drop_support(
    session: Session,
    tenant: Tenant,
    config: AlertChannelConfig,
    now: datetime,
) -> List[DetectedAnomaly]:
    """24h CSAT average vs 14d baseline. CSAT is 1-5; a 0.5-point drop is
    significant. Re-uses the sentiment_drop_threshold knob, halved to
    match CSAT's narrower range."""
    domain_cfg = _load_domain_config(session, tenant.id, "it_support")
    raw_drop = float(
        _threshold(
            domain_cfg,
            config,
            "sentiment_drop_threshold",
            DEFAULT_SENTIMENT_DROP,
        )
    )
    drop = max(raw_drop / 3.0, 0.5)  # 1-5 scale, so divide

    recent_start = now - timedelta(hours=24)
    baseline_start = now - timedelta(days=15)
    baseline_end = recent_start

    recent = _support_csat_scores(session, tenant.id, recent_start, now)
    baseline = _support_csat_scores(session, tenant.id, baseline_start, baseline_end)
    if len(recent) < 5 or not baseline:
        return []
    recent_avg = statistics.mean(recent)
    baseline_avg = statistics.mean(baseline)
    delta = baseline_avg - recent_avg
    if delta < drop:
        return []
    severity = "high" if delta >= drop + 0.5 else "medium"
    evidence = {
        "current_avg": round(recent_avg, 2),
        "baseline_avg": round(baseline_avg, 2),
        "delta": round(delta, 2),
        "current_n": len(recent),
        "baseline_n": len(baseline),
        "window_hours": 24,
    }
    title_raw = (
        f"CSAT dropped {delta:.1f} points in support over 24 hours "
        f"({recent_avg:.1f} vs {baseline_avg:.1f} baseline)."
    )
    return [
        DetectedAnomaly(
            kind="csat_drop_support",
            severity=severity,
            title=sanitize_manager_text(title_raw, max_words=25),
            body=(
                f"{len(recent)} cases closed in 24h averaged "
                f"{recent_avg:.1f}; 14-day baseline was {baseline_avg:.1f}."
            ),
            evidence=evidence,
            fingerprint=_fingerprint("csat_drop_support", "tenant"),
            domain="it_support",
        )
    ]


# ── Support detector: escalation surge ──────────────────────────────────


def _support_escalation_count(
    session: Session, tenant_id, start: datetime, end: datetime
) -> int:
    stmt = select(SupportCase.id).where(
        SupportCase.tenant_id == tenant_id,
        SupportCase.escalated_at.isnot(None),
        SupportCase.escalated_at >= start,
        SupportCase.escalated_at < end,
    )
    return len(session.execute(stmt).all())


def _detect_escalation_surge(
    session: Session,
    tenant: Tenant,
    config: AlertChannelConfig,
    now: datetime,
) -> List[DetectedAnomaly]:
    domain_cfg = _load_domain_config(session, tenant.id, "it_support")
    multiplier = float(
        _threshold(
            domain_cfg,
            config,
            "churn_surge_multiplier",
            DEFAULT_CHURN_SURGE_MULTIPLIER,
        )
    )
    recent_start = now - timedelta(hours=24)
    current = _support_escalation_count(session, tenant.id, recent_start, now)
    if current < 3:
        return []
    baseline_start = now - timedelta(days=15)
    # One query over the 14-day baseline, bucketed by day in Python
    # (was 14 per-day COUNT queries — an N+1 on the 15-min scan).
    escalated_at_rows = session.execute(
        select(SupportCase.escalated_at).where(
            SupportCase.tenant_id == tenant.id,
            SupportCase.escalated_at.isnot(None),
            SupportCase.escalated_at >= baseline_start,
            SupportCase.escalated_at < recent_start,
        )
    ).all()
    daily_counts = _bucket_by_day(
        (ts for (ts,) in escalated_at_rows), baseline_start, 14
    )
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
        f"{current} support cases escalated in 24 hours, "
        f"vs typical {int(median)} per day."
    )
    return [
        DetectedAnomaly(
            kind="escalation_surge",
            severity=severity,
            title=sanitize_manager_text(title_raw, max_words=25),
            body=(
                f"Baseline median is {median:.1f} escalations per day across "
                f"the last 14 days; the current 24h count crossed {threshold:.0f}."
            ),
            evidence=evidence,
            fingerprint=_fingerprint("escalation_surge", "tenant"),
            domain="it_support",
        )
    ]


# ── Support detector: time-to-resolve drift ─────────────────────────────


def _support_ttr_hours(
    session: Session, tenant_id, start: datetime, end: datetime
) -> List[float]:
    """Time-to-resolve in hours for cases resolved in the window. Only
    includes cases with both an opened_at and a resolved_at."""
    stmt = select(SupportCase.opened_at, SupportCase.resolved_at).where(
        SupportCase.tenant_id == tenant_id,
        SupportCase.resolved_at.isnot(None),
        SupportCase.resolved_at >= start,
        SupportCase.resolved_at < end,
    )
    out: List[float] = []
    for opened, resolved in session.execute(stmt).all():
        if opened is None or resolved is None:
            continue
        try:
            delta = (resolved - opened).total_seconds() / 3600.0
            if delta > 0:
                out.append(delta)
        except (TypeError, ValueError):
            continue
    return out


def _detect_ttr_drift(
    session: Session,
    tenant: Tenant,
    config: AlertChannelConfig,
    now: datetime,
) -> List[DetectedAnomaly]:
    """24h average TTR vs 14d baseline; alert when current rises >= 50%
    above baseline (with a 1-hour absolute floor so a quiet day with one
    long case doesn't fire)."""
    recent_start = now - timedelta(hours=24)
    baseline_start = now - timedelta(days=15)
    baseline_end = recent_start

    recent = _support_ttr_hours(session, tenant.id, recent_start, now)
    baseline = _support_ttr_hours(session, tenant.id, baseline_start, baseline_end)
    if len(recent) < 5 or not baseline:
        return []
    recent_avg = statistics.mean(recent)
    baseline_avg = statistics.mean(baseline)
    if recent_avg < baseline_avg * 1.5:
        return []
    if (recent_avg - baseline_avg) < 1.0:
        # Sub-1h drift on a fast baseline isn't worth paging on.
        return []
    severity = "high" if recent_avg >= baseline_avg * 2.0 else "medium"
    evidence = {
        "current_avg_hours": round(recent_avg, 2),
        "baseline_avg_hours": round(baseline_avg, 2),
        "delta_hours": round(recent_avg - baseline_avg, 2),
        "current_n": len(recent),
        "baseline_n": len(baseline),
        "window_hours": 24,
    }
    title_raw = (
        f"Time to resolve rose to {recent_avg:.1f}h in 24 hours, "
        f"vs {baseline_avg:.1f}h baseline."
    )
    return [
        DetectedAnomaly(
            kind="ttr_drift",
            severity=severity,
            title=sanitize_manager_text(title_raw, max_words=25),
            body=(
                f"{len(recent)} cases resolved in 24h averaged "
                f"{recent_avg:.1f} hours; 14-day baseline was {baseline_avg:.1f}h."
            ),
            evidence=evidence,
            fingerprint=_fingerprint("ttr_drift", "tenant"),
            domain="it_support",
        )
    ]


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
    if alert.kind == "renewal_risk_spike":
        current = _cs_high_risk_count(session, tenant.id, now - timedelta(hours=24), now)
        baseline = (alert.evidence or {}).get("baseline_daily_median") or 0
        return current >= max(float(baseline) * 1.2, 3.0)
    if alert.kind == "health_score_drop":
        scores = _cs_sentiment_scores(session, tenant.id, now - timedelta(hours=24), now)
        if len(scores) < 5:
            return True
        baseline_avg = (alert.evidence or {}).get("baseline_avg")
        delta = (alert.evidence or {}).get("delta") or DEFAULT_SENTIMENT_DROP
        if not isinstance(baseline_avg, (int, float)):
            return True
        return (float(baseline_avg) - statistics.mean(scores)) >= float(delta) * 0.5
    if alert.kind == "csat_drop_support":
        scores = _support_csat_scores(session, tenant.id, now - timedelta(hours=24), now)
        if len(scores) < 5:
            return True
        baseline_avg = (alert.evidence or {}).get("baseline_avg")
        delta = (alert.evidence or {}).get("delta") or 0.5
        if not isinstance(baseline_avg, (int, float)):
            return True
        return (float(baseline_avg) - statistics.mean(scores)) >= float(delta) * 0.5
    if alert.kind == "escalation_surge":
        current = _support_escalation_count(session, tenant.id, now - timedelta(hours=24), now)
        baseline = (alert.evidence or {}).get("baseline_daily_median") or 0
        return current >= max(float(baseline) * 1.2, 3.0)
    if alert.kind == "ttr_drift":
        recent = _support_ttr_hours(session, tenant.id, now - timedelta(hours=24), now)
        if len(recent) < 5:
            return True
        baseline_avg = (alert.evidence or {}).get("baseline_avg_hours") or 0
        if not isinstance(baseline_avg, (int, float)) or baseline_avg <= 0:
            return True
        return statistics.mean(recent) >= float(baseline_avg) * 1.25
    return True


# ── Helpers ─────────────────────────────────────────────────────────────


def _load_config(session: Session, tenant_id) -> AlertChannelConfig:
    cfg = session.get(AlertChannelConfig, tenant_id)
    if cfg is None:
        cfg = AlertChannelConfig(tenant_id=tenant_id)
        session.add(cfg)
        session.flush()
    return cfg


def _load_domain_config(
    session: Session, tenant_id, domain: str
) -> Optional[AlertDomainConfig]:
    """Per-(tenant, domain) override row. None when no override exists."""
    return session.get(AlertDomainConfig, (tenant_id, domain))


def _threshold(
    domain_cfg: Optional[AlertDomainConfig],
    tenant_cfg: AlertChannelConfig,
    field: str,
    default,
):
    """Read a knob: domain override > tenant default > code default.

    A NULL on the domain row means "use the tenant default for this
    knob" — common case where a tenant has tuned CS specifically but
    inherits the rest. Lets a CS-specific sentiment_drop threshold
    coexist with a tenant-wide topic_spike threshold without duplicating
    every value.
    """
    if domain_cfg is not None:
        v = getattr(domain_cfg, field, None)
        if v is not None:
            return v
    v = getattr(tenant_cfg, field, None)
    if v is not None:
        return v
    return default


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
        domain=anomaly.domain,
    )
    session.add(row)
    try:
        session.flush()
        return row
    except IntegrityError:
        session.rollback()
        return None
