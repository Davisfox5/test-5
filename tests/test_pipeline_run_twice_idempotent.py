"""Whole-pipeline idempotency: running ``_run_pipeline`` twice must pay
each LLM step exactly once and produce zero duplicate rows.

This is the duplicate-delivery scenario from docs/complexity/01 (3a):
``task_acks_late=True`` + a 1h visibility timeout means a fully-run
pipeline can be redelivered and re-executed end to end. With the step
ledger + idempotent re-derivation in place, the second pass must:

* reuse the persisted analysis (no second Sonnet call),
* reuse the persisted scorecards (no second Haiku call, no duplicate
  ``InteractionScore`` rows),
* replace — not duplicate — machine-written ``ActionItem`` /
  ``InteractionSnippet`` rows, while manually-created action items and
  library-curated snippets survive.

External services are faked at the ``_get_*_service`` seams; the DB is
real (SQLite) so the delete-then-insert and ledger commits are exercised
for real.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import tests.db_fixtures  # noqa: F401  (sqlite JSONB/UUID compilers)

from backend.app.db import Base
from backend.app.models import (
    ActionItem,
    Interaction,
    InteractionScore,
    InteractionSnippet,
    InteractionStepRun,
    ScorecardTemplate,
    Tenant,
)


SEGMENTS = [
    {"start": 0.0, "end": 5.0, "text": "REP: Hi, thanks for joining.", "speaker_id": "rep"},
    {"start": 5.0, "end": 12.0, "text": "CUSTOMER: We want to renew.", "speaker_id": "cust"},
]

FAKE_INSIGHTS: Dict[str, Any] = {
    "summary": "Customer wants to renew; rep will send the updated quote today.",
    "sentiment_score": 0.6,
    "action_items": [
        {"title": "Send updated quote", "description": "Email quote", "priority": "high"},
        {"title": "Book follow-up call", "description": "Next week", "priority": "medium"},
    ],
}


class _Calls:
    def __init__(self) -> None:
        self.analyze = 0
        self.score_many = 0
        self.triage = 0


@pytest.fixture()
def env(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path}/pipeline.db"
    engine = create_engine(url, poolclass=NullPool, connect_args={"timeout": 30})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    session = factory()
    tenant = Tenant(
        id=uuid.uuid4(),
        name="T",
        slug=f"t-{uuid.uuid4().hex[:8]}",
        pii_redaction_enabled=False,
    )
    session.add(tenant)
    session.flush()
    template = ScorecardTemplate(
        tenant_id=tenant.id, name="QA", criteria=[{"name": "Greeting", "weight": 1}]
    )
    session.add(template)
    ix = Interaction(
        id=uuid.uuid4(), tenant_id=tenant.id, channel="voice", transcript=SEGMENTS
    )
    session.add(ix)
    session.commit()
    tid, iid, template_id = tenant.id, ix.id, template.id
    session.close()

    calls = _Calls()

    import backend.app.tasks as tasks

    class FakeMetrics:
        def compute(self, segs):
            return {"talk_ratio": 0.5}

    class FakeCompressor:
        def compress(self, segs):
            return segs

    class FakeTriage:
        async def score_complexity(self, text, metadata):
            calls.triage += 1
            return {"complexity_score": 0.4, "recommended_tier": "sonnet"}

    class FakeAnalysis:
        async def analyze(self, *a, **kw):
            calls.analyze += 1
            return dict(FAKE_INSIGHTS)

    class FakeScorecards:
        async def score_many(self, transcript, templates, insights):
            calls.score_many += 1
            return [
                {
                    "template_id": t["id"],
                    "total_score": 88,
                    "criterion_scores": [{"name": "Greeting", "score": 88}],
                }
                for t in templates
            ]

    class FakeSnippets:
        def identify_notable_segments(self, insights, agent_id, tenant_id):
            return [
                {
                    "start_time": 0.0,
                    "end_time": 5.0,
                    "snippet_type": "objection",
                    "quality": "good",
                    "title": "Renewal ask",
                }
            ]

    class FakeSearch:
        async def index_interaction(self, *a, **kw):
            return None

    monkeypatch.setattr(tasks, "_get_metrics_service", lambda: FakeMetrics())
    monkeypatch.setattr(tasks, "_get_compressor", lambda: FakeCompressor())
    monkeypatch.setattr(tasks, "_get_triage_service", lambda: FakeTriage())
    monkeypatch.setattr(tasks, "_get_analysis_service", lambda: FakeAnalysis())
    monkeypatch.setattr(tasks, "_get_scorecard_service", lambda: FakeScorecards())
    monkeypatch.setattr(tasks, "_get_snippet_service", lambda: FakeSnippets())
    monkeypatch.setattr(tasks, "_get_search_service", lambda: FakeSearch())
    monkeypatch.setattr(tasks, "_enqueue_delta_report", lambda **kw: None)
    monkeypatch.setattr(
        tasks.evaluate_analysis, "apply_async", lambda *a, **kw: None
    )

    # Personalization / RAG blocks: DB+Qdrant backed — stub to constants.
    import backend.app.services.personalization_service as perso

    monkeypatch.setattr(perso, "build_analysis_context_block", lambda s, t: "")
    monkeypatch.setattr(perso, "build_rag_context_block", lambda *a, **kw: "")
    monkeypatch.setattr(perso, "get_parameter_overrides", lambda *a, **kw: {})

    # Entity resolution: stub the whole module via sys.modules — the real
    # one imports rapidfuzz (absent in the test env) and the pipeline
    # imports it function-locally, so a stub module is the clean seam.
    import sys
    import types

    async def fake_resolve(**kw):
        return SimpleNamespace(
            customer_action="none", customer_score=0.0, customer_id=None,
            suggestions=None,
        )

    er_stub = types.ModuleType("backend.app.services.entity_resolution")
    er_stub.resolve_interaction_entities = fake_resolve
    monkeypatch.setitem(
        sys.modules, "backend.app.services.entity_resolution", er_stub
    )

    import backend.app.services.warnings_commitments as wc

    async def fake_detect(**kw):
        return SimpleNamespace(
            warnings_upserted=0, warnings_re_raised=0,
            commitments_created=0, commitments_marked_done=0,
        )

    monkeypatch.setattr(wc, "detect_and_persist", fake_detect)

    # Plan synthesis opens the module-level async engine (Postgres URL in
    # tests) — make the session factory raise so the block takes its
    # non-fatal failure path deterministically.
    import backend.app.db as appdb

    def raising_async_session(*a, **kw):
        raise RuntimeError("no async DB in tests")

    monkeypatch.setattr(appdb, "async_session", raising_async_session)

    try:
        yield SimpleNamespace(
            factory=factory, tenant_id=tid, interaction_id=iid,
            template_id=template_id, calls=calls, tasks=tasks,
        )
    finally:
        engine.dispose()


def _run_once(env):
    session = env.factory()
    try:
        tenant = session.get(Tenant, env.tenant_id)
        ix = session.get(Interaction, env.interaction_id)
        env.tasks._run_pipeline(session, str(env.interaction_id), SEGMENTS, tenant, ix)
    finally:
        session.close()


def _counts(env):
    session = env.factory()
    try:
        return {
            "action_items": session.query(ActionItem).count(),
            "scores": session.query(InteractionScore).count(),
            "snippets": session.query(InteractionSnippet).count(),
        }
    finally:
        session.close()


def test_pipeline_twice_pays_once_and_never_duplicates(env):
    _run_once(env)

    assert env.calls.analyze == 1
    assert env.calls.score_many == 1
    first = _counts(env)
    assert first == {"action_items": 2, "scores": 1, "snippets": 1}

    session = env.factory()
    ix = session.get(Interaction, env.interaction_id)
    assert ix.status == "analyzed"
    assert (ix.insights or {}).get("summary", "").startswith("Customer wants")
    ledger = {
        r.step_key: r.status
        for r in session.query(InteractionStepRun).filter_by(
            interaction_id=env.interaction_id
        )
    }
    assert ledger.get("analysis") == "succeeded"
    assert ledger.get("scorecards") == "succeeded"
    assert ledger.get("plan_synthesis") == "failed"  # async DB stubbed out
    session.close()

    # A human curates between deliveries: manual action item + library
    # snippet. Both must survive the redelivered run.
    session = env.factory()
    session.add(
        ActionItem(
            interaction_id=env.interaction_id, tenant_id=env.tenant_id,
            title="Manual: send swag", status="open", manually_created=True,
        )
    )
    snip = session.query(InteractionSnippet).first()
    snip.in_library = True
    session.commit()
    session.close()

    # ── Redelivery: the whole pipeline runs again ────────────────────
    _run_once(env)

    assert env.calls.analyze == 1, "second delivery re-paid the Sonnet analysis"
    assert env.calls.score_many == 1, "second delivery re-paid the scorecard call"

    second = _counts(env)
    # 2 machine + 1 manual action items; scores replaced not duplicated;
    # the library snippet survives and its recomputed twin is skipped.
    assert second == {"action_items": 3, "scores": 1, "snippets": 1}

    session = env.factory()
    titles = {a.title for a in session.query(ActionItem).all()}
    assert "Manual: send swag" in titles
    assert session.query(InteractionSnippet).filter_by(in_library=True).count() == 1
    session.close()


def test_pipeline_retry_after_late_failure_reuses_analysis(env):
    """Failure after the analysis commit (the old guard's blind spot):
    the retry must reuse the paid analysis even though the failure
    handler stamped ``insights['error']``."""
    _run_once(env)
    assert env.calls.analyze == 1

    # Simulate the task failure-handler stamp (tasks.py failure path).
    session = env.factory()
    ix = session.get(Interaction, env.interaction_id)
    stamped = dict(ix.insights or {})
    stamped.update({"error": "TypeError: boom", "step": "voice_pipeline", "retry_count": 1})
    ix.insights = stamped
    ix.status = "failed"
    session.commit()
    session.close()

    _run_once(env)
    assert env.calls.analyze == 1, "retry re-paid despite persisted analysis"

    session = env.factory()
    ix = session.get(Interaction, env.interaction_id)
    assert ix.status == "analyzed"
    assert "error" not in (ix.insights or {})
    session.close()
