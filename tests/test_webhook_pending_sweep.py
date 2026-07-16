"""webhook_pending_sweep: orphan re-enqueue + stale dead-letter.

Pins the safety net for the enqueue-race defect: delivery rows whose
webhook_deliver task fired before the caller's commit sit in ``pending``
with attempt_count=0 forever unless the sweep re-enqueues them — and
rows older than the dead-letter window must be parked, never delivered
(stale payloads, e.g. pre-fix churn values, must not reach consumers).
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import tests.db_fixtures  # noqa: F401 — registers JSONB/UUID sqlite shims
from backend.app.db import Base
from backend.app.models import Webhook, WebhookDelivery

TENANT = uuid.uuid4()


def _now():
    return datetime.now(timezone.utc)


@pytest.fixture()
def sweep_env(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    hook_id = uuid.uuid4()
    seed = Session()
    seed.add(
        Webhook(
            id=hook_id,
            tenant_id=TENANT,
            url="https://example.com/hook",
            events=["*"],
            secret="s",
            active=True,
        )
    )
    seed.commit()
    seed.close()

    from backend.app import tasks

    monkeypatch.setattr(tasks, "_get_sync_session", Session)
    monkeypatch.setattr(tasks, "_all_tenant_ids", lambda: [str(TENANT)])

    delayed = []
    monkeypatch.setattr(
        tasks.webhook_deliver, "delay", lambda *a: delayed.append(a)
    )

    def add_delivery(**kw):
        s = Session()
        d = WebhookDelivery(
            id=uuid.uuid4(),
            webhook_id=hook_id,
            tenant_id=TENANT,
            event="interaction.analyzed",
            payload={"event": "interaction.analyzed", "data": {}},
            status=kw.pop("status", "pending"),
            attempts=[],
            attempt_count=kw.pop("attempt_count", 0),
            **kw,
        )
        s.add(d)
        s.commit()
        did = d.id
        s.close()
        return did

    return {"Session": Session, "add": add_delivery, "delayed": delayed}


def test_sweep_requeues_never_attempted_orphan(sweep_env):
    from backend.app.tasks import webhook_pending_sweep

    did = sweep_env["add"](created_at=_now() - timedelta(minutes=10))
    result = webhook_pending_sweep()
    assert result == {"requeued": 1, "dead_lettered": 0}
    assert sweep_env["delayed"] == [(str(did), str(TENANT))]


def test_sweep_leaves_fresh_rows_alone(sweep_env):
    from backend.app.tasks import webhook_pending_sweep

    sweep_env["add"](created_at=_now() - timedelta(minutes=1))
    result = webhook_pending_sweep()
    assert result == {"requeued": 0, "dead_lettered": 0}
    assert sweep_env["delayed"] == []


def test_sweep_requeues_overdue_retry(sweep_env):
    from backend.app.tasks import webhook_pending_sweep

    did = sweep_env["add"](
        created_at=_now() - timedelta(minutes=30),
        attempt_count=2,
        next_retry_at=_now() - timedelta(minutes=10),
    )
    result = webhook_pending_sweep()
    assert result["requeued"] == 1
    assert sweep_env["delayed"] == [(str(did), str(TENANT))]


def test_sweep_dead_letters_stale_rows_without_delivering(sweep_env):
    from backend.app.tasks import webhook_pending_sweep

    did = sweep_env["add"](created_at=_now() - timedelta(days=3))
    result = webhook_pending_sweep()
    assert result == {"requeued": 0, "dead_lettered": 1}
    assert sweep_env["delayed"] == []

    s = sweep_env["Session"]()
    row = s.get(WebhookDelivery, did)
    assert row.status == "dead_letter"
    assert "stale" in (row.last_error or "")
    s.close()


def test_sweep_ignores_attempted_rows_without_scheduled_retry(sweep_env):
    from backend.app.tasks import webhook_pending_sweep

    # attempt_count > 0 and no next_retry_at → a retry chain the
    # dispatcher still owns (countdown task in flight); leave it alone.
    sweep_env["add"](
        created_at=_now() - timedelta(minutes=10), attempt_count=1
    )
    result = webhook_pending_sweep()
    assert result == {"requeued": 0, "dead_lettered": 0}
