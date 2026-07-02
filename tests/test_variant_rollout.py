"""Tests for variant winner selection (``evaluate_active_experiments``).

The decision rule is Welch's t-test + a minimum practical effect: noise-level
deltas must NOT crown a winner (the old ``delta > 0`` rule did), significant
improvements go to the human gate, significant regressions retire the
treatment, and a no-difference experiment keeps running until the sample cap.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


@compiles(UUID, "sqlite")
def _compile_uuid_sqlite(type_, compiler, **kw):
    return "CHAR(36)"


@pytest.fixture
def sync_session():
    from backend.app.db import Base
    import backend.app.models  # noqa: F401

    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _seed_experiment(session):
    from backend.app.models import Experiment, PromptVariant, Tenant

    tenant = Tenant(name="Acme", slug=f"acme-{uuid.uuid4().hex[:6]}")
    session.add(tenant)
    session.commit()
    control = PromptVariant(
        name="control", prompt_template="c", target_surface="analysis", status="active"
    )
    treatment = PromptVariant(
        name="treatment", prompt_template="t", target_surface="analysis", status="canary"
    )
    session.add_all([control, treatment])
    session.commit()
    exp = Experiment(
        name="exp-1",
        type="prompt_ab_test",
        status="running",
        control_variant_id=control.id,
        treatment_variant_id=treatment.id,
        start_date=datetime.now(timezone.utc) - timedelta(days=7),
    )
    session.add(exp)
    session.commit()
    return tenant, exp, control, treatment


def _seed_scores(session, tenant, variant_id, values):
    from backend.app.models import InsightQualityScore

    session.add_all(
        [
            InsightQualityScore(
                tenant_id=tenant.id,
                surface="analysis",
                evaluator_type="llm_judge",
                evaluator_id="haiku",
                dimension="composite",
                score=v,
                prompt_variant_id=variant_id,
            )
            for v in values
        ]
    )
    session.commit()


def _run(session):
    from backend.app.services.variant_rollout import evaluate_active_experiments

    return evaluate_active_experiments(session)


def test_clear_winner_goes_to_human_gate(sync_session):
    tenant, exp, control, treatment = _seed_experiment(sync_session)
    # 250 samples each; treatment clearly higher, both arms with variance.
    _seed_scores(sync_session, tenant, control.id, [0.60, 0.70] * 125)
    _seed_scores(sync_session, tenant, treatment.id, [0.75, 0.85] * 125)

    out = _run(sync_session)

    assert out["winners_declared"] == 1
    sync_session.refresh(exp)
    sync_session.refresh(treatment)
    assert exp.status == "ready_for_review"
    assert exp.result_summary["winner"] == "treatment"
    assert exp.result_summary["p_value"] < 0.05
    assert treatment.status == "ready_for_review"


def test_noise_level_delta_does_not_declare_winner(sync_session):
    tenant, exp, control, treatment = _seed_experiment(sync_session)
    # Same distribution, treatment nudged +0.002 — the old delta>0 rule
    # would have shipped this to review.
    _seed_scores(sync_session, tenant, control.id, [0.60, 0.80] * 125)
    _seed_scores(sync_session, tenant, treatment.id, [0.602, 0.802] * 125)

    out = _run(sync_session)

    assert out["winners_declared"] == 0
    sync_session.refresh(exp)
    sync_session.refresh(treatment)
    # Keeps collecting instead of concluding on noise.
    assert exp.status == "running"
    assert treatment.status == "canary"
    assert out["not_ready"] == 1


def test_significant_regression_retires_treatment(sync_session):
    tenant, exp, control, treatment = _seed_experiment(sync_session)
    _seed_scores(sync_session, tenant, control.id, [0.75, 0.85] * 125)
    _seed_scores(sync_session, tenant, treatment.id, [0.60, 0.70] * 125)

    out = _run(sync_session)

    assert out["inconclusive"] == 1
    sync_session.refresh(exp)
    sync_session.refresh(treatment)
    assert exp.status == "concluded"
    assert exp.result_summary["winner"] == "control"
    assert treatment.status == "retired"
    assert treatment.retired_at is not None


def test_no_difference_at_sample_cap_concludes(sync_session):
    from backend.app.services import variant_rollout

    tenant, exp, control, treatment = _seed_experiment(sync_session)
    _seed_scores(sync_session, tenant, control.id, [0.60, 0.80] * 125)
    _seed_scores(sync_session, tenant, treatment.id, [0.60, 0.80] * 125)

    # Lower the cap so 250 samples per arm counts as "enough to call it".
    original = variant_rollout.MAX_SAMPLES_PER_VARIANT
    variant_rollout.MAX_SAMPLES_PER_VARIANT = 250
    try:
        out = _run(sync_session)
    finally:
        variant_rollout.MAX_SAMPLES_PER_VARIANT = original

    assert out["inconclusive"] == 1
    sync_session.refresh(exp)
    sync_session.refresh(treatment)
    assert exp.status == "concluded"
    assert "No practical difference" in exp.conclusion
    assert treatment.status == "retired"


def test_insufficient_samples_waits(sync_session):
    tenant, exp, control, treatment = _seed_experiment(sync_session)
    _seed_scores(sync_session, tenant, control.id, [0.6] * 50)
    _seed_scores(sync_session, tenant, treatment.id, [0.9] * 50)

    out = _run(sync_session)

    assert out["not_ready"] == 1
    sync_session.refresh(exp)
    assert exp.status == "running"
