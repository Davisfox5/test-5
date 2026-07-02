"""Tests for vocabulary candidate discovery.

Covers the 30-day lookback window on the corrections + interactions
sources in ``_discover_for_tenant`` — the ``# 1. Corrections within last
30 days.`` comment previously had no date filter behind it.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
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


@pytest.fixture
def seeded(sync_session):
    from backend.app.models import Interaction, Tenant

    tenant = Tenant(name="Acme", slug=f"acme-{uuid.uuid4().hex[:6]}")
    sync_session.add(tenant)
    sync_session.commit()
    ix = Interaction(tenant_id=tenant.id, channel="voice", domain="sales")
    sync_session.add(ix)
    sync_session.commit()
    sync_session.refresh(tenant)
    sync_session.refresh(ix)
    return tenant, ix


def _add_correction(sync_session, tenant, ix, *, original, corrected, when):
    from backend.app.models import TranscriptCorrection

    row = TranscriptCorrection(
        tenant_id=tenant.id,
        interaction_id=ix.id,
        segment_index=0,
        original_text=original,
        corrected_text=corrected,
    )
    sync_session.add(row)
    sync_session.flush()
    row.created_at = when
    sync_session.commit()
    return row


def test_discover_ignores_corrections_older_than_lookback_window(
    sync_session, seeded
):
    from backend.app.models import VocabularyCandidate
    from backend.app.services.vocabulary_service import (
        CORRECTION_LOOKBACK_DAYS,
        _discover_for_tenant,
    )

    tenant, ix = seeded
    now = datetime.now(timezone.utc)
    # Within the window — should surface a candidate.
    _add_correction(
        sync_session,
        tenant,
        ix,
        original="the call with acme",
        corrected="the call with Acmecorp",
        when=now - timedelta(days=CORRECTION_LOOKBACK_DAYS - 1),
    )
    # Outside the window — should be excluded entirely.
    _add_correction(
        sync_session,
        tenant,
        ix,
        original="talked to stale",
        corrected="talked to Stalecorp",
        when=now - timedelta(days=CORRECTION_LOOKBACK_DAYS + 1),
    )

    _discover_for_tenant(sync_session, tenant)
    sync_session.commit()

    terms = {
        row.term
        for row in (
            sync_session.execute(
                select(VocabularyCandidate).where(
                    VocabularyCandidate.source == "corrections"
                )
            )
        ).scalars()
    }
    assert "Acmecorp" in terms
    assert "Stalecorp" not in terms
