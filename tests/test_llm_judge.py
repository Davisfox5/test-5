"""Tests for the LLM-as-judge rubric versioning and blind-first ordering.

Two concerns:

1. **Rubric versioning** — every ``InsightQualityScore`` row the module
   writes must be traceable to the ``RUBRIC_VERSION`` it was scored under
   (``InsightQualityScore`` has no JSON details column, so the version
   rides along in ``reasoning``).
2. **Blind-first ordering** — the classifier/reply judges must show the
   judge the raw evidence *before* the model's own verdict, so the judge
   forms an independent view instead of anchoring on the model's answer.

Uses an in-memory sync SQLite engine (same trick as
test_manager_anomaly_detector.py) and monkeypatches ``_call_judge`` so no
real Anthropic call happens.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from backend.app.services import llm_judge


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


@compiles(UUID, "sqlite")
def _compile_uuid_sqlite(type_, compiler, **kw):
    return "CHAR(36)"


@pytest.fixture
def sync_session():
    from backend.app.db import Base
    import backend.app.models  # noqa: F401 — registers mapped classes

    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.fixture
def seeded_tenant(sync_session):
    from backend.app.models import Tenant

    tenant = Tenant(name="Acme", slug=f"acme-{uuid.uuid4().hex[:6]}")
    sync_session.add(tenant)
    sync_session.commit()
    sync_session.refresh(tenant)
    return tenant


_DUMMY_CLASSIFIER_SCORES = {
    "scores": {
        "is_external_correctness": {"score": 0.9, "reasoning": "Looks right."},
        "category_correctness": {"score": 0.8, "reasoning": "Sales bucket fits."},
        "confidence_calibration": {"score": 0.7, "reasoning": "Reasonable."},
    }
}

_DUMMY_REPLY_SCORES = {
    "scores": {
        "coherence": {"score": 0.9, "reasoning": "Answers the question."},
        "factuality": {"score": 0.8, "reasoning": "Matches KB."},
        "tone_match": {"score": 0.9, "reasoning": "On brand."},
        "kb_groundedness": {"score": 0.8, "reasoning": "Cited correctly."},
        "review_flag_calibration": {"score": 0.9, "reasoning": "Flag set correctly."},
    }
}


def test_rubric_version_persisted_on_every_row(sync_session, seeded_tenant, monkeypatch):
    from backend.app.models import Interaction

    interaction = Interaction(
        tenant_id=seeded_tenant.id,
        channel="email",
        direction="inbound",
        from_address="customer@example.com",
        to_addresses=["support@acme.test"],
        subject="Question",
        raw_text="Hi, is this a support request?",
        is_internal=False,
        classification="support",
        classification_confidence=0.8,
    )
    sync_session.add(interaction)
    sync_session.commit()
    sync_session.refresh(interaction)

    monkeypatch.setattr(llm_judge, "_call_judge", lambda rubric, content: _DUMMY_CLASSIFIER_SCORES)
    monkeypatch.setattr(llm_judge, "_flag_if_needed", lambda *a, **k: None)

    result = llm_judge.evaluate_classification(sync_session, str(interaction.id))
    assert result["status"] == "ok"
    assert result["scores_written"] == 3

    from backend.app.models import InsightQualityScore

    rows = (
        sync_session.query(InsightQualityScore)
        .filter(InsightQualityScore.interaction_id == interaction.id)
        .all()
    )
    assert len(rows) == 3
    tag = f"[rubric_v{llm_judge.RUBRIC_VERSION}]"
    for row in rows:
        assert row.reasoning.startswith(tag)


def test_classifier_judge_shows_evidence_before_model_verdict(
    sync_session, seeded_tenant, monkeypatch
):
    from backend.app.models import Interaction

    interaction = Interaction(
        tenant_id=seeded_tenant.id,
        channel="email",
        direction="inbound",
        from_address="customer@example.com",
        to_addresses=["support@acme.test"],
        subject="Question",
        raw_text="Hi, is this a support request?",
        is_internal=False,
        classification="support",
        classification_confidence=0.8,
    )
    sync_session.add(interaction)
    sync_session.commit()
    sync_session.refresh(interaction)

    captured = {}

    def _fake_call_judge(rubric, content):
        captured["content"] = content
        return _DUMMY_CLASSIFIER_SCORES

    monkeypatch.setattr(llm_judge, "_call_judge", _fake_call_judge)
    monkeypatch.setattr(llm_judge, "_flag_if_needed", lambda *a, **k: None)

    llm_judge.evaluate_classification(sync_session, str(interaction.id))

    content = captured["content"]
    evidence_idx = content.index("Body preview")
    verdict_header_idx = content.index("Model output to grade")
    verdict_idx = content.index("classification: support")
    assert evidence_idx < verdict_header_idx < verdict_idx
    assert "read only after forming your own view" in content


def test_reply_judge_shows_evidence_before_model_output(
    sync_session, seeded_tenant, monkeypatch
):
    from backend.app.models import Interaction

    inbound = Interaction(
        tenant_id=seeded_tenant.id,
        channel="email",
        direction="inbound",
        conversation_id=None,
        raw_text="Can you tell me your pricing?",
    )
    sync_session.add(inbound)
    sync_session.commit()
    sync_session.refresh(inbound)

    reply = Interaction(
        tenant_id=seeded_tenant.id,
        channel="email",
        direction="outbound",
        conversation_id=inbound.conversation_id,
        subject="Re: pricing",
        raw_text="Thanks for reaching out! " + ("Our pricing starts at $99/mo. " * 3),
    )
    sync_session.add(reply)
    sync_session.commit()
    sync_session.refresh(reply)

    captured = {}

    def _fake_call_judge(rubric, content):
        captured["content"] = content
        return _DUMMY_REPLY_SCORES

    monkeypatch.setattr(llm_judge, "_call_judge", _fake_call_judge)
    monkeypatch.setattr(llm_judge, "_flag_if_needed", lambda *a, **k: None)
    monkeypatch.setattr(llm_judge, "_edit_distance_dimension", lambda *a, **k: None)

    llm_judge.evaluate_reply(sync_session, str(reply.id))

    content = captured["content"]
    evidence_idx = content.index("Tenant tone")
    verdict_header_idx = content.index("Model output to grade")
    reply_body_idx = content.index("Thanks for reaching out!")
    assert evidence_idx < verdict_header_idx < reply_body_idx
    assert "read only after forming your own view" in content


def test_classifier_judge_attributes_per_surface_variant(
    sync_session, seeded_tenant, monkeypatch
):
    """When ``insights["prompt_variants"]["email_classifier"]`` is present,
    the judge should attribute the score to that variant rather than the
    single ``interaction.prompt_variant_id`` column (which belongs to the
    ``analysis`` surface)."""
    from backend.app.models import Interaction, InsightQualityScore

    classifier_variant_id = uuid.uuid4()
    analysis_variant_id = uuid.uuid4()

    interaction = Interaction(
        tenant_id=seeded_tenant.id,
        channel="email",
        direction="inbound",
        from_address="customer@example.com",
        to_addresses=["support@acme.test"],
        subject="Question",
        raw_text="Hi, is this a support request?",
        is_internal=False,
        classification="support",
        classification_confidence=0.8,
        # The analysis surface's single slot — must NOT be what
        # email_classifier scores get attributed to when a per-surface id
        # is available.
        prompt_variant_id=analysis_variant_id,
        insights={"prompt_variants": {"email_classifier": str(classifier_variant_id)}},
    )
    sync_session.add(interaction)
    sync_session.commit()
    sync_session.refresh(interaction)

    monkeypatch.setattr(llm_judge, "_call_judge", lambda rubric, content: _DUMMY_CLASSIFIER_SCORES)
    monkeypatch.setattr(llm_judge, "_flag_if_needed", lambda *a, **k: None)

    result = llm_judge.evaluate_classification(sync_session, str(interaction.id))
    assert result["status"] == "ok"

    rows = (
        sync_session.query(InsightQualityScore)
        .filter(InsightQualityScore.interaction_id == interaction.id)
        .all()
    )
    assert rows
    for row in rows:
        assert row.prompt_variant_id == classifier_variant_id


def test_classifier_judge_falls_back_to_column_without_per_surface_variant(
    sync_session, seeded_tenant, monkeypatch
):
    """No ``insights["prompt_variants"]`` entry ⇒ fall back to
    ``interaction.prompt_variant_id`` (pre-attribution behavior)."""
    from backend.app.models import Interaction, InsightQualityScore

    analysis_variant_id = uuid.uuid4()

    interaction = Interaction(
        tenant_id=seeded_tenant.id,
        channel="email",
        direction="inbound",
        from_address="customer@example.com",
        to_addresses=["support@acme.test"],
        subject="Question",
        raw_text="Hi, is this a support request?",
        is_internal=False,
        classification="support",
        classification_confidence=0.8,
        prompt_variant_id=analysis_variant_id,
    )
    sync_session.add(interaction)
    sync_session.commit()
    sync_session.refresh(interaction)

    monkeypatch.setattr(llm_judge, "_call_judge", lambda rubric, content: _DUMMY_CLASSIFIER_SCORES)
    monkeypatch.setattr(llm_judge, "_flag_if_needed", lambda *a, **k: None)

    llm_judge.evaluate_classification(sync_session, str(interaction.id))

    rows = (
        sync_session.query(InsightQualityScore)
        .filter(InsightQualityScore.interaction_id == interaction.id)
        .all()
    )
    assert rows
    for row in rows:
        assert row.prompt_variant_id == analysis_variant_id
