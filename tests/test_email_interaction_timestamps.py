"""Email Interactions must carry the message's real Date, not ingest time.

Regression guard for the "whole thread shows one identical timestamp"
bug: the backfill ingests a 50-message thread in one transaction, so
every row's server-default created_at was the same transaction clock.
``ingest_email`` now stamps ``created_at`` from
``NormalizedEmail.received_at`` (the parsed Date header); the server
default only applies when the header was missing/unparseable.

Runs against a real (sync, in-memory SQLite) session because the thing
under test is what actually lands in the row.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Importing the async fixtures registers the JSONB/UUID → SQLite type
# shims (@compiles is global), which the sync engine here needs too.
import tests.db_fixtures  # noqa: F401
from backend.app.models import Base, Interaction, Tenant
from backend.app.services.email_ingest.ingest import NormalizedEmail, ingest_email


class _AlwaysExternalClassifier:
    async def classify(self, *_a, **_k):
        return SimpleNamespace(
            is_external=True, classification="sales", confidence=0.99, reason="test"
        )


@pytest.fixture
def sync_session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def tenant(sync_session):
    t = Tenant(name="T", slug="t")
    sync_session.add(t)
    sync_session.flush()
    return t


def _email(message_id: str, received_at):
    return NormalizedEmail(
        provider="gmail",
        provider_message_id=f"pmid-{message_id}",
        message_id=f"<{message_id}@example.com>",
        in_reply_to=None,
        subject="Hello",
        from_address="prospect@acme.com",
        to_addresses=["rep@vendor.com"],
        body_text="hi",
        received_at=received_at,
        direction="inbound",
    )


def test_created_at_is_the_message_date(sync_session, tenant):
    sent = datetime(2026, 7, 6, 13, 19, 19, tzinfo=timezone.utc)
    iid = asyncio.run(
        ingest_email(sync_session, tenant, _email("m1", sent), _AlwaysExternalClassifier())
    )
    row = sync_session.query(Interaction).filter(Interaction.id == iid).one()
    assert row.created_at is not None
    # SQLite returns naive datetimes; compare in UTC either way.
    got = row.created_at if row.created_at.tzinfo else row.created_at.replace(tzinfo=timezone.utc)
    assert got == sent


def test_missing_date_falls_back_to_server_default(sync_session, tenant):
    iid = asyncio.run(
        ingest_email(sync_session, tenant, _email("m2", None), _AlwaysExternalClassifier())
    )
    sync_session.commit()
    row = sync_session.query(Interaction).filter(Interaction.id == iid).one()
    assert row.created_at is not None


def test_backfilled_thread_keeps_distinct_times(sync_session, tenant):
    times = [
        datetime(2026, 7, 6, 13, 19, tzinfo=timezone.utc),
        datetime(2026, 7, 7, 5, 27, tzinfo=timezone.utc),
        datetime(2026, 7, 9, 22, 26, tzinfo=timezone.utc),
    ]
    ids = [
        asyncio.run(
            ingest_email(sync_session, tenant, _email(f"t{i}", ts), _AlwaysExternalClassifier())
        )
        for i, ts in enumerate(times)
    ]
    rows = (
        sync_session.query(Interaction)
        .filter(Interaction.id.in_(ids))
        .order_by(Interaction.created_at)
        .all()
    )
    stamps = {r.created_at for r in rows}
    assert len(stamps) == 3
