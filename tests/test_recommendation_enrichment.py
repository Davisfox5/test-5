"""Tests for the per-recommendation account-brief enrichment pass.

Covers the pure helpers (``_validate_brief``, ``_system_prompt``,
``longform_voice_rules_for``), the deterministic context assembler
(``assemble_account_context``), the composer (``compose_brief``) and
its sanitization, the Celery entry point (``enrich_by_id``), and the
queueing gate (``queue_enrichment_for``).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


@compiles(UUID, "sqlite")
def _compile_uuid_sqlite(type_, compiler, **kw):
    return "CHAR(36)"


# Mix of sync (pure-function) and async (DB-backed) tests in this file,
# so each async test is marked individually rather than via a module-wide
# ``pytestmark``.


# ── async engine/session fixture (mirrors test_manager_routes.py) ──────


@pytest_asyncio.fixture
async def engine_factory():
    from backend.app.db import Base
    import backend.app.models  # noqa: F401

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield engine, factory
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def seeded(engine_factory):
    _, factory = engine_factory
    from backend.app.models import (
        Commitment,
        Customer,
        CustomerCommitment,
        CustomerConcern,
        Interaction,
        SupportCase,
        Tenant,
    )

    async with factory() as session:
        tenant = Tenant(name="Acme", slug=f"acme-{uuid.uuid4().hex[:6]}")
        session.add(tenant)
        await session.flush()

        customer = Customer(
            tenant_id=tenant.id,
            name="Northwind",
            # UTC date to match the service clock — local date.today() is a
            # day behind UTC in US evenings, shifting days_to_renewal by one.
            renewal_date=datetime.now(timezone.utc).date() + timedelta(days=30),
            health_score=62.0,
            onboarding_status="stalled",
        )
        session.add(customer)
        await session.flush()

        now = datetime.now(timezone.utc)

        ix1 = Interaction(
            tenant_id=tenant.id,
            customer_id=customer.id,
            channel="voice",
            domain="customer_service",
            insights={
                "summary": "Champion raised concern about report latency.",
                "sentiment_overall": "negative",
                "churn_risk_signal": "considering competitor",
                "key_moments": ["asked about export API"],
            },
        )
        ix2 = Interaction(
            tenant_id=tenant.id,
            customer_id=customer.id,
            channel="email",
            domain="customer_service",
            insights={
                "summary": "Follow-up email confirming next steps.",
                "sentiment_overall": "neutral",
            },
        )
        session.add_all([ix1, ix2])
        await session.flush()
        ix1.created_at = now - timedelta(days=10)
        ix2.created_at = now - timedelta(days=3)

        concern = CustomerConcern(
            tenant_id=tenant.id,
            customer_id=customer.id,
            topic="report_latency",
            description="Reports take minutes to load.",
            status="active",
            severity="high",
        )
        session.add(concern)

        promise = CustomerCommitment(
            tenant_id=tenant.id,
            customer_id=customer.id,
            description="Will share Q3 budget by end of month.",
            quote="We'll have Q3 numbers to you by month end.",
            status="open",
        )
        session.add(promise)

        our_commitment = Commitment(
            tenant_id=tenant.id,
            customer_id=customer.id,
            interaction_id=ix1.id,
            text="Send updated performance benchmarks.",
            evidence_excerpt="I'll send the benchmarks over.",
            status="pending",
            actor_side="rep",
        )
        session.add(our_commitment)

        case = SupportCase(
            tenant_id=tenant.id,
            customer_id=customer.id,
            subject="Dashboard slow to load",
            status="open",
        )
        session.add(case)
        await session.flush()
        case.opened_at = now - timedelta(days=2)

        await session.commit()
        await session.refresh(tenant)
        await session.refresh(customer)
        return {"tenant": tenant, "customer": customer}


# ── _validate_brief ──────────────────────────────────────────────────────


def test_validate_brief_accepts_mixed_sections_and_preserves_order():
    from backend.app.services.recommendation_enrichment import _validate_brief

    raw = {
        "headline": "Call Northwind before Friday's renewal review.",
        "sections": [
            {"kind": "situation", "title": "Where things stand", "body": "Text."},
            {
                "kind": "talking_points",
                "title": "Bring up",
                "items": ["Latency fix shipped", "Q3 budget"],
            },
        ],
    }
    out = _validate_brief(raw)
    assert out is not None
    assert out["headline"] == raw["headline"]
    assert [s["kind"] for s in out["sections"]] == ["situation", "talking_points"]
    assert out["sections"][0]["body"] == "Text."
    assert out["sections"][1]["items"] == ["Latency fix shipped", "Q3 budget"]


def test_validate_brief_drops_unknown_kind_but_keeps_valid_ones():
    from backend.app.services.recommendation_enrichment import _validate_brief

    raw = {
        "headline": "Do the thing.",
        "sections": [
            {"kind": "not_a_real_kind", "title": "Bogus", "body": "x"},
            {"kind": "play", "title": "The move", "body": "Do this."},
        ],
    }
    out = _validate_brief(raw)
    assert out is not None
    assert [s["kind"] for s in out["sections"]] == ["play"]


def test_validate_brief_drops_section_with_neither_body_nor_items():
    from backend.app.services.recommendation_enrichment import _validate_brief

    raw = {
        "headline": "Do the thing.",
        "sections": [
            {"kind": "watch_out", "title": "Landmine"},
            {"kind": "play", "title": "The move", "body": "Do this."},
        ],
    }
    out = _validate_brief(raw)
    assert out is not None
    assert [s["kind"] for s in out["sections"]] == ["play"]


def test_validate_brief_missing_or_empty_headline_returns_none():
    from backend.app.services.recommendation_enrichment import _validate_brief

    valid_sections = [{"kind": "play", "title": "The move", "body": "Do this."}]
    assert _validate_brief({"sections": valid_sections}) is None
    assert _validate_brief({"headline": "", "sections": valid_sections}) is None
    assert _validate_brief({"headline": "   ", "sections": valid_sections}) is None
    assert _validate_brief("not a dict") is None


def test_validate_brief_all_sections_invalid_returns_none():
    from backend.app.services.recommendation_enrichment import _validate_brief

    raw = {
        "headline": "Do the thing.",
        "sections": [
            {"kind": "not_real", "body": "x"},
            {"kind": "play"},
        ],
    }
    assert _validate_brief(raw) is None


# ── _system_prompt ─────────────────────────────────────────────────────


def test_system_prompt_lists_every_section_kind_and_json_contract():
    from backend.app.services.recommendation_enrichment import (
        SECTION_KINDS,
        _system_prompt,
    )

    prompt = _system_prompt("customer_service")
    for kind in SECTION_KINDS:
        assert kind in prompt
    assert "One short sentence per item" not in prompt
    assert '"headline"' in prompt
    assert '"sections"' in prompt
    assert '"kind"' in prompt


# ── longform_voice_rules_for ─────────────────────────────────────────────


def test_longform_voice_rules_replaces_cap_and_framing_keeps_bans():
    from backend.app.services.plain_english import (
        longform_voice_rules_for,
        manager_voice_rules_for,
    )

    short_rules = manager_voice_rules_for("customer_service")
    long_rules = longform_voice_rules_for("customer_service")

    assert "One short sentence per item" in short_rules
    assert "One short sentence per item" not in long_rules
    assert "clipboard notes" in short_rules
    assert "clipboard notes" not in long_rules
    assert "working brief for a colleague" in long_rules
    # Em-dash ban and banned phrases survive the swap.
    assert "em-dashes" in long_rules
    assert "You did a great job" in long_rules
    assert "In conclusion" in long_rules


# ── assemble_account_context ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_assemble_account_context_pulls_every_signal(engine_factory, seeded):
    _, factory = engine_factory
    from backend.app.services.recommendation_enrichment import (
        assemble_account_context,
    )

    async with factory() as session:
        customer = await session.get(
            type(seeded["customer"]), seeded["customer"].id
        )
        ctx = await assemble_account_context(
            session, seeded["tenant"].id, customer, kb_query=None
        )

    assert ctx["customer_name"] == "Northwind"
    assert ctx["renewal"]["days_to_renewal"] == 30
    assert ctx["renewal"]["health_score"] == 62.0
    assert ctx["renewal"]["onboarding_status"] == "stalled"

    assert len(ctx["recent_interactions"]) == 2
    summaries = [i.get("summary") for i in ctx["recent_interactions"]]
    assert "Champion raised concern about report latency." in summaries
    sentiments = [i.get("sentiment") for i in ctx["recent_interactions"]]
    assert "negative" in sentiments

    assert len(ctx["tracked_concerns"]) == 1
    assert ctx["tracked_concerns"][0]["topic"] == "report_latency"

    assert len(ctx["customer_promises"]) == 1
    assert "Q3 budget" in ctx["customer_promises"][0]["description"]

    assert len(ctx["our_open_commitments"]) == 1
    assert ctx["our_open_commitments"][0]["text"] == (
        "Send updated performance benchmarks."
    )

    assert len(ctx["support_cases"]) == 1
    assert ctx["support_cases"][0]["subject"] == "Dashboard slow to load"

    # kb_query=None: no KB lookup attempted, no kb_matches key.
    assert "kb_matches" not in ctx


# ── compose_brief / enrich_by_id ─────────────────────────────────────────


class _StubResponse:
    def __init__(self, data):
        self._data = data

    def parse_json(self):
        return self._data


class _StubRouter:
    def __init__(self, result=None, exc=None):
        self._result = result
        self._exc = exc

    async def ainvoke(self, req):
        if self._exc is not None:
            raise self._exc
        return _StubResponse(self._result)


def _valid_brief_payload():
    return {
        "headline": "Call Northwind before the renewal review.",
        "sections": [
            {
                "kind": "play",
                "title": "The move",
                "body": "Walk through the latency fix and confirm the Q3 budget number.",
            },
            {
                "kind": "watch_out",
                "title": "Landmine",
                "items": ["Don't reopen the pricing discussion."],
            },
        ],
    }


async def _make_open_recommendation(factory, tenant_id, customer_id, **overrides):
    from backend.app.models import ManagerRecommendation

    defaults = dict(
        tenant_id=tenant_id,
        domain="customer_service",
        category="prevent_no_touch_churn",
        title="Reach out to Northwind before renewal",
        rationale="Renewal in 30 days; no CS touch in 45 days.",
        evidence={"days_to_renewal": 30},
        target={"customer_id": str(customer_id)} if customer_id else {},
        score=80.0,
        status="open",
        expires_at=datetime.now(timezone.utc) + timedelta(days=14),
    )
    defaults.update(overrides)
    async with factory() as session:
        rec = ManagerRecommendation(**defaults)
        session.add(rec)
        await session.commit()
        await session.refresh(rec)
        return rec.id


@pytest.mark.asyncio
async def test_enrich_by_id_happy_path(engine_factory, seeded, monkeypatch):
    _, factory = engine_factory

    monkeypatch.setattr("backend.app.db.async_session", factory)
    monkeypatch.setattr(
        "backend.app.services.recommendation_enrichment.get_router",
        lambda: _StubRouter(result=_valid_brief_payload()),
    )

    rec_id = await _make_open_recommendation(
        factory, seeded["tenant"].id, seeded["customer"].id
    )

    from backend.app.services.recommendation_enrichment import enrich_by_id

    result = await enrich_by_id(str(rec_id))
    assert result["status"] == "enriched"
    assert set(result["sections"]) == {"play", "watch_out"}

    from backend.app.models import ManagerRecommendation

    async with factory() as session:
        rec = await session.get(ManagerRecommendation, rec_id)
        assert rec.brief is not None
        assert [s["kind"] for s in rec.brief["sections"]] == ["play", "watch_out"]
        assert rec.enriched_at is not None
        assert rec.rationale == "Renewal in 30 days; no CS touch in 45 days."


@pytest.mark.asyncio
async def test_enrich_by_id_compose_failure(engine_factory, seeded, monkeypatch):
    _, factory = engine_factory

    monkeypatch.setattr("backend.app.db.async_session", factory)
    monkeypatch.setattr(
        "backend.app.services.recommendation_enrichment.get_router",
        lambda: _StubRouter(exc=RuntimeError("provider blip")),
    )

    rec_id = await _make_open_recommendation(
        factory, seeded["tenant"].id, seeded["customer"].id
    )

    from backend.app.services.recommendation_enrichment import enrich_by_id

    result = await enrich_by_id(str(rec_id))
    assert result["status"] == "compose_failed"

    from backend.app.models import ManagerRecommendation

    async with factory() as session:
        rec = await session.get(ManagerRecommendation, rec_id)
        assert rec.brief is None


@pytest.mark.asyncio
async def test_enrich_by_id_skips_non_open_recommendation(
    engine_factory, seeded, monkeypatch
):
    _, factory = engine_factory
    monkeypatch.setattr("backend.app.db.async_session", factory)

    rec_id = await _make_open_recommendation(
        factory, seeded["tenant"].id, seeded["customer"].id, status="applied"
    )

    from backend.app.services.recommendation_enrichment import enrich_by_id

    result = await enrich_by_id(str(rec_id))
    assert result["status"] == "skipped_not_open"


@pytest.mark.asyncio
async def test_enrich_by_id_skips_no_customer_target(
    engine_factory, seeded, monkeypatch
):
    _, factory = engine_factory
    monkeypatch.setattr("backend.app.db.async_session", factory)

    rec_id = await _make_open_recommendation(
        factory, seeded["tenant"].id, None, target={}
    )

    from backend.app.services.recommendation_enrichment import enrich_by_id

    result = await enrich_by_id(str(rec_id))
    assert result["status"] == "no_customer_target"


# ── queue_enrichment_for ─────────────────────────────────────────────────


def _fake_recommendation(customer_id=None):
    from backend.app.models import ManagerRecommendation

    rec = ManagerRecommendation(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        domain="customer_service",
        category="prevent_no_touch_churn",
        title="t",
        rationale="r",
        evidence={},
        target={"customer_id": str(customer_id)} if customer_id else {},
        score=50.0,
        expires_at=datetime.now(timezone.utc) + timedelta(days=14),
    )
    return rec


def test_queue_enrichment_for_returns_zero_when_disabled(monkeypatch):
    from backend.app.services import recommendation_enrichment as mod

    class _FakeSettings:
        RECOMMENDATION_ENRICHMENT_ENABLED = False

    monkeypatch.setattr(
        "backend.app.config.get_settings", lambda: _FakeSettings()
    )
    rows = [_fake_recommendation(customer_id=uuid.uuid4())]
    assert mod.queue_enrichment_for(rows) == 0


def test_queue_enrichment_for_queues_customer_targeted_rows_only(monkeypatch):
    # Import first (with real settings) so the later ``get_settings``
    # monkeypatch below doesn't blow up module-level Celery construction
    # on a re-import.
    import backend.app.tasks  # noqa: F401
    from backend.app.services import recommendation_enrichment as mod

    class _FakeSettings:
        RECOMMENDATION_ENRICHMENT_ENABLED = True

    monkeypatch.setattr(
        "backend.app.config.get_settings", lambda: _FakeSettings()
    )

    calls = []

    class _FakeTask:
        def delay(self, rec_id):
            calls.append(rec_id)

    monkeypatch.setattr(
        "backend.app.tasks.enrich_manager_recommendation", _FakeTask()
    )

    targeted = _fake_recommendation(customer_id=uuid.uuid4())
    untargeted = _fake_recommendation(customer_id=None)
    queued = mod.queue_enrichment_for([targeted, untargeted])

    assert queued == 1
    assert calls == [str(targeted.id)]


# ── Sanitization (compose_brief) ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_compose_brief_scrubs_em_dash_but_does_not_truncate_long_body(
    engine_factory, seeded, monkeypatch
):
    _, factory = engine_factory

    long_body = (
        "The account raised report latency concerns during the call and "
        "the champion mentioned they are evaluating a competitor tool "
        "because dashboards take minutes to load during peak hours which "
        "is affecting their weekly business review — this is the single "
        "biggest blocker to renewal and needs a concrete fix commitment "
        "before Friday's call with the champion and their VP of ops team."
    )
    assert len(long_body.split()) > 40
    assert "—" in long_body

    payload = {
        "headline": "Call Northwind before the renewal review.",
        "sections": [
            {"kind": "situation", "title": "Where things stand", "body": long_body},
        ],
    }

    monkeypatch.setattr(
        "backend.app.services.recommendation_enrichment.get_router",
        lambda: _StubRouter(result=payload),
    )

    from backend.app.models import ManagerRecommendation
    from backend.app.services.recommendation_enrichment import compose_brief

    rec = ManagerRecommendation(
        tenant_id=seeded["tenant"].id,
        domain="customer_service",
        category="prevent_no_touch_churn",
        title="Reach out to Northwind before renewal",
        rationale="r",
        evidence={},
        target={"customer_id": str(seeded["customer"].id)},
        score=80.0,
        expires_at=datetime.now(timezone.utc) + timedelta(days=14),
    )
    brief = await compose_brief(rec, {"customer_name": "Northwind"})

    assert brief is not None
    body = brief["sections"][0]["body"]
    assert "—" not in body
    assert len(body.split()) > 40
