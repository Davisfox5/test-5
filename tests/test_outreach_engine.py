"""Engine tests: the outreach scheduler tick + ingest-side hooks.

Uses a *sync* SQLite session (the engine runs inside Celery on sync
sessions). The provider sender is faked at the transport seam
(scheduler.build_sender) so no HTTP/OAuth is involved; Celery enqueues
inside dispatch_sync fail quietly with no broker, which is exactly the
production no-webhook-configured posture.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

import tests.db_fixtures  # noqa: F401 — registers JSONB/UUID sqlite compilers

from backend.app.models import (
    Campaign,
    CampaignEvent,
    CampaignRecipient,
    Contact,
    Customer,
    EmailSend,
    Integration,
    Interaction,
    OutreachMember,
    Tenant,
)
from backend.app.services.outreach import replies, scheduler
from backend.app.services.outreach.common import parse_config

# Wednesday 15:00 UTC == 11:00 New York — inside the default send window.
IN_WINDOW = datetime(2026, 7, 8, 15, 0, tzinfo=timezone.utc)
# Sunday — outside.
OUT_OF_WINDOW = datetime(2026, 7, 12, 15, 0, tzinfo=timezone.utc)

CONFIG = {
    "template": {
        "subject": "Quick question about {business_name}",
        "body": "Hi — saw you run {business_name}.",
        "sender_name": "Davis Fox",
        "sender_business": "Flex",
        "physical_address": "123 Main St, Nashville, TN 37201",
    },
    "daily_limit": 2,
    "max_touches": 2,
    "steps": [{"offset_days": 0}, {"offset_days": 3}],
    "mode": "review",
}


@dataclass
class FakeSendResult:
    provider: str = "google"
    message_id: Optional[str] = None
    provider_message_id: Optional[str] = None
    raw_snippet: str = ""


class FakeSender:
    """Stands in for GmailSender at the scheduler's build_sender seam."""

    calls: List[dict] = []
    fail = False

    def __init__(self) -> None:
        self.provider = "google"

    async def send(self, **kwargs):
        if FakeSender.fail:
            raise RuntimeError("provider exploded")
        FakeSender.calls.append(kwargs)
        n = len(FakeSender.calls)
        return FakeSendResult(
            message_id=f"<out-{n}@mail.example>", provider_message_id=f"gm-{n}"
        )

    async def close(self) -> None:
        pass


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite://")
    from backend.app.db import Base
    import backend.app.models  # noqa: F401

    Base.metadata.create_all(engine)
    session = sessionmaker(engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def fake_sender(monkeypatch):
    FakeSender.calls = []
    FakeSender.fail = False
    monkeypatch.setattr(scheduler, "build_sender", lambda integ, from_address_hint=None: FakeSender())
    return FakeSender


def _seed(db: Session, n_members: int = 1, config: Optional[dict] = None):
    tenant = Tenant(name="T", slug=f"t-{uuid.uuid4().hex[:8]}")
    db.add(tenant)
    db.flush()
    db.add(
        Integration(
            tenant_id=tenant.id, provider="google",
            access_token="enc", refresh_token="enc",
        )
    )
    campaign = Campaign(
        tenant_id=tenant.id,
        name="Sweep",
        channel="email",
        kind="outreach",
        status="active",
        config=parse_config(config or CONFIG).model_dump(mode="json"),
    )
    db.add(campaign)
    db.flush()
    members = []
    for i in range(n_members):
        customer = Customer(
            tenant_id=tenant.id,
            name=f"Gym {i}",
            domain=f"gym-{i}.com",
            pipeline_status="new",
            metadata_={"outreach": {"hook": f"Hook {i}", "segment": "boutique"}},
        )
        db.add(customer)
        db.flush()
        contact = Contact(
            tenant_id=tenant.id, customer_id=customer.id,
            email=f"owner{i}@gym-{i}.com", name=f"Owner {i}",
        )
        db.add(contact)
        db.flush()
        member = OutreachMember(
            tenant_id=tenant.id,
            campaign_id=campaign.id,
            customer_id=customer.id,
            contact_id=contact.id,
            state="queued",
            draft_status="approved",
            draft_subject=f"Subject {i}",
            draft_body=f"Body {i}",
            next_send_at=IN_WINDOW - timedelta(minutes=5),
        )
        db.add(member)
        members.append(member)
    db.commit()
    return tenant, campaign, members


# ── Sending ────────────────────────────────────────────────────────────


def test_tick_sends_due_member_and_writes_all_rows(db, fake_sender):
    tenant, campaign, (member,) = _seed(db)
    result = scheduler.run_campaign_tick(db, tenant, campaign, now_utc=IN_WINDOW)
    assert result["sent"] == 1 and result["failed"] == 0

    sent_kwargs = fake_sender.calls[0]
    assert sent_kwargs["to"] == ["owner0@gym-0.com"]
    # CAN-SPAM footer appended to the approved draft body.
    assert "Body 0" in sent_kwargs["body"]
    assert "123 Main St" in sent_kwargs["body"]
    assert "unsubscribe" in sent_kwargs["body"].lower()

    send = db.execute(select(EmailSend)).scalars().one()
    assert send.status == "sent"
    assert send.campaign_id == campaign.id
    assert send.customer_id == member.customer_id
    assert send.provider_message_id == "gm-1"

    recipient = db.execute(select(CampaignRecipient)).scalars().one()
    assert recipient.rfc822_message_id == "<out-1@mail.example>"
    assert recipient.customer_id == member.customer_id
    assert recipient.step == 0

    interaction = db.execute(select(Interaction)).scalars().one()
    assert interaction.direction == "outbound"
    assert interaction.campaign_id == campaign.id
    assert interaction.customer_id == member.customer_id
    assert interaction.provider_message_id == "gm-1"

    db.refresh(member)
    assert member.state == "in_sequence"
    assert member.touches_sent == 1
    assert member.current_step == 1
    assert member.thread_message_ids == ["<out-1@mail.example>"]
    assert member.next_send_at is not None  # bump scheduled

    customer = db.get(Customer, member.customer_id)
    assert customer.pipeline_status == "contacted"


def test_tick_respects_daily_limit_and_window(db, fake_sender):
    tenant, campaign, members = _seed(db, n_members=3)

    # daily_limit=2 → only 2 of 3 send this "day".
    result = scheduler.run_campaign_tick(db, tenant, campaign, now_utc=IN_WINDOW)
    assert result["sent"] == 2
    assert len(fake_sender.calls) == 2

    again = scheduler.run_campaign_tick(
        db, tenant, campaign, now_utc=IN_WINDOW + timedelta(minutes=10)
    )
    assert again["sent"] == 0  # quota exhausted

    # Outside the window nothing sends, quota or not.
    fake_sender.calls.clear()
    outside = scheduler.run_campaign_tick(db, tenant, campaign, now_utc=OUT_OF_WINDOW)
    assert outside["sent"] == 0
    assert fake_sender.calls == []


def test_tick_skips_dnc_and_survives_provider_failure(db, fake_sender):
    tenant, campaign, members = _seed(db, n_members=2)
    # First member goes DNC between approval and send.
    customer0 = db.get(Customer, members[0].customer_id)
    customer0.do_not_contact = True
    db.commit()

    FakeSender.fail = True
    result = scheduler.run_campaign_tick(db, tenant, campaign, now_utc=IN_WINDOW)
    assert result["sent"] == 0
    # one halted (DNC, not a failure), one failed on the provider
    db.refresh(members[0])
    db.refresh(members[1])
    assert members[0].state == "halted" and members[0].halt_reason == "do_not_contact"
    assert members[1].state == "failed"
    failed_send = db.execute(select(EmailSend)).scalars().one()
    assert failed_send.status == "failed"
    assert "provider exploded" in failed_send.error


def test_sequence_completes_after_max_touches(db, fake_sender):
    tenant, campaign, (member,) = _seed(db)
    scheduler.run_campaign_tick(db, tenant, campaign, now_utc=IN_WINDOW)
    db.refresh(member)

    # Fast-forward past the bump offset; regenerate inline via a stub.
    member.state = "queued"
    member.draft_status = "approved"
    member.draft_subject = "Bump"
    member.draft_body = "Bump body"
    member.next_send_at = IN_WINDOW
    db.commit()
    day2 = IN_WINDOW + timedelta(days=1)  # fresh throttle day
    scheduler.run_campaign_tick(db, tenant, campaign, now_utc=day2)

    db.refresh(member)
    assert member.touches_sent == 2
    assert member.state == "completed"  # max_touches=2 exhausted
    assert member.next_send_at is None
    # Second send threads on the first.
    threading = fake_sender.calls[1]
    assert threading["in_reply_to"] == "<out-1@mail.example>"
    assert threading["references"] == ["<out-1@mail.example>"]

    db.refresh(campaign)
    assert campaign.status == "completed"  # no actionable members left
    assert campaign.sent_count == 2


def test_bump_reenters_draft_flow(db, fake_sender, monkeypatch):
    tenant, campaign, (member,) = _seed(db)
    scheduler.run_campaign_tick(db, tenant, campaign, now_utc=IN_WINDOW)
    db.refresh(member)
    assert member.state == "in_sequence"

    # Stub the LLM personalization for the bump draft.
    monkeypatch.setattr(
        scheduler.drafts_mod,
        "generate_member_draft",
        lambda campaign, config, member, customer, step_index=None: {
            "subject": "Bump subject", "body": "Bump body", "facts": {}
        },
    )
    after_bump_due = member.next_send_at + timedelta(hours=1)
    scheduler.run_campaign_tick(db, tenant, campaign, now_utc=after_bump_due)
    db.refresh(member)
    # review mode → bump waits for human approval, not auto-sent
    assert member.state == "needs_approval"
    assert member.draft_subject == "Bump subject"
    assert member.draft_status == "ready"


# ── Reply / bounce / opt-out hooks ─────────────────────────────────────


def _reply_interaction(db, tenant, member, text: str) -> Interaction:
    contact = db.get(Contact, member.contact_id)
    interaction = Interaction(
        tenant_id=tenant.id,
        contact_id=contact.id,
        channel="email",
        direction="inbound",
        from_address=contact.email,
        subject="Re: Quick question",
        raw_text=text,
        message_id=f"<reply-{uuid.uuid4().hex[:6]}@gym>",
        in_reply_to="<out-1@mail.example>",
    )
    db.add(interaction)
    db.flush()
    return interaction


def test_reply_halts_sequence_and_flips_status(db, fake_sender):
    tenant, campaign, (member,) = _seed(db)
    scheduler.run_campaign_tick(db, tenant, campaign, now_utc=IN_WINDOW)

    recipient = replies.find_recipient_for_reply(
        db, tenant.id,
        in_reply_to="<out-1@mail.example>", references=None, from_address=None,
    )
    assert recipient is not None

    interaction = _reply_interaction(db, tenant, member, "Sounds interesting — tell me more!")
    replies.handle_outreach_reply(db, tenant.id, interaction, recipient, db.get(Contact, member.contact_id))
    db.flush()

    db.refresh(member)
    assert member.state == "replied"
    assert member.next_send_at is None
    customer = db.get(Customer, member.customer_id)
    assert customer.pipeline_status == "replied"
    assert interaction.customer_id == customer.id  # hung onto the prospect


def test_reply_matches_via_references_chain_and_address_fallback(db, fake_sender):
    tenant, campaign, (member,) = _seed(db)
    scheduler.run_campaign_tick(db, tenant, campaign, now_utc=IN_WINDOW)

    by_refs = replies.find_recipient_for_reply(
        db, tenant.id,
        in_reply_to=None,
        references=["<out-1@mail.example>", "<other@x>"],
        from_address=None,
    )
    assert by_refs is not None

    # Outlook path: no Message-ID we know — match by sender address while
    # the member is awaiting a reply.
    by_addr = replies.find_recipient_for_reply(
        db, tenant.id,
        in_reply_to=None, references=None, from_address="owner0@gym-0.com",
    )
    assert by_addr is not None

    assert replies.find_recipient_for_reply(
        db, tenant.id, in_reply_to=None, references=None,
        from_address="stranger@nowhere.com",
    ) is None


def test_opt_out_reply_sets_dnc_and_halts_everything(db, fake_sender):
    tenant, campaign, (member,) = _seed(db)
    scheduler.run_campaign_tick(db, tenant, campaign, now_utc=IN_WINDOW)

    # A second campaign enrollment for the same prospect must halt too.
    other = Campaign(
        tenant_id=tenant.id, name="Other", channel="email",
        kind="outreach", status="active",
        config=parse_config(CONFIG).model_dump(mode="json"),
    )
    db.add(other)
    db.flush()
    sibling = OutreachMember(
        tenant_id=tenant.id, campaign_id=other.id,
        customer_id=member.customer_id, contact_id=member.contact_id,
        state="queued", draft_status="approved",
    )
    db.add(sibling)
    db.commit()

    recipient = replies.find_recipient_for_reply(
        db, tenant.id, in_reply_to="<out-1@mail.example>",
        references=None, from_address=None,
    )
    interaction = _reply_interaction(db, tenant, member, "Please remove me from your list.")
    replies.handle_outreach_reply(
        db, tenant.id, interaction, recipient, db.get(Contact, member.contact_id)
    )
    db.flush()

    db.refresh(member)
    db.refresh(sibling)
    assert member.state == "opted_out"
    assert sibling.state == "halted"
    customer = db.get(Customer, member.customer_id)
    assert customer.do_not_contact is True
    assert customer.pipeline_status == "do_not_contact"
    events = db.execute(select(CampaignEvent)).scalars().all()
    assert "unsubscribe" in {e.event_type for e in events}


def test_bounce_detection_marks_member(db, fake_sender):
    tenant, campaign, (member,) = _seed(db)
    scheduler.run_campaign_tick(db, tenant, campaign, now_utc=IN_WINDOW)

    handled = replies.handle_possible_bounce(
        db, tenant.id,
        from_address="MAILER-DAEMON@googlemail.com",
        subject="Delivery Status Notification (Failure)",
        body_text="The message <out-1@mail.example> to owner0@gym-0.com bounced.",
        in_reply_to=None,
        references=None,
    )
    assert handled is True
    db.refresh(member)
    assert member.state == "bounced"
    events = db.execute(select(CampaignEvent)).scalars().all()
    assert "bounce" in {e.event_type for e in events}

    # A random newsletter that merely *looks* auto-generated is ignored.
    assert not replies.handle_possible_bounce(
        db, tenant.id,
        from_address="mailer-daemon@elsewhere.com",
        subject="Undeliverable",
        body_text="no known ids here",
        in_reply_to=None,
        references=None,
    )


def test_reply_never_demotes_advanced_prospect(db, fake_sender):
    tenant, campaign, (member,) = _seed(db)
    scheduler.run_campaign_tick(db, tenant, campaign, now_utc=IN_WINDOW)
    customer = db.get(Customer, member.customer_id)
    customer.pipeline_status = "demo"  # rep already booked the demo
    db.commit()

    recipient = replies.find_recipient_for_reply(
        db, tenant.id, in_reply_to="<out-1@mail.example>",
        references=None, from_address=None,
    )
    interaction = _reply_interaction(db, tenant, member, "See you at the demo")
    replies.handle_outreach_reply(
        db, tenant.id, interaction, recipient, db.get(Contact, member.contact_id)
    )
    db.refresh(customer)
    assert customer.pipeline_status == "demo"  # monotonic — not demoted


# ── HTML rendering / logo / attachments in the send path ───────────────


class FakeStore:
    """Stands in for the S3 attachment store at the scheduler seam."""

    def __init__(self, objects=None):
        self.objects = objects or {}

    def get(self, key):
        return self.objects.get(key)


def test_send_passes_html_alternative_and_strips_markers(db, fake_sender):
    tenant, campaign, (member,) = _seed(db)
    member.draft_body = "Hi **Sam**, try *this* and _that_."
    db.commit()

    scheduler.run_campaign_tick(db, tenant, campaign, now_utc=IN_WINDOW)
    kw = fake_sender.calls[0]
    # Plain part: markers stripped, footer appended.
    assert "Hi Sam, try this and that." in kw["body"]
    assert "**" not in kw["body"]
    # HTML part: markers rendered, footer + no logo (none uploaded).
    assert "<b>Sam</b>" in kw["body_html"]
    assert "<i>this</i>" in kw["body_html"]
    assert "<u>that</u>" in kw["body_html"]
    assert "123 Main St" in kw["body_html"]
    assert "cid:" not in kw["body_html"]
    assert kw["attachments"] is None

    interaction = db.execute(select(Interaction)).scalars().one()
    assert "<b>Sam</b>" in interaction.body_html


def test_send_embeds_tenant_logo_inline(db, fake_sender, monkeypatch):
    tenant, campaign, (member,) = _seed(db)
    logo_key = f"tenants/{tenant.id}/branding/ab12-logo.png"
    tenant.branding_config = {
        "email_logo": {
            "s3_key": logo_key,
            "filename": "logo.png",
            "content_type": "image/png",
        }
    }
    db.commit()
    store = FakeStore({logo_key: (b"\x89PNGDATA", "image/png")})
    monkeypatch.setattr(scheduler, "get_store", lambda: store)

    scheduler.run_campaign_tick(db, tenant, campaign, now_utc=IN_WINDOW)
    kw = fake_sender.calls[0]
    (logo,) = kw["attachments"]
    assert logo.content_id == "tenant-logo"
    assert logo.data == b"\x89PNGDATA"
    assert logo.content_type == "image/png"
    assert 'src="cid:tenant-logo"' in kw["body_html"]


def test_send_attaches_campaign_files(db, fake_sender, monkeypatch):
    tenant, campaign, (member,) = _seed(db)
    key = f"tenants/{tenant.id}/outreach/ab12-deck.pdf"
    campaign.config = {
        **campaign.config,
        "attachments": [
            {"s3_key": key, "filename": "deck.pdf", "content_type": "application/pdf"}
        ],
    }
    db.commit()
    store = FakeStore({key: (b"%PDF", "application/pdf")})
    monkeypatch.setattr(scheduler, "get_store", lambda: store)

    scheduler.run_campaign_tick(db, tenant, campaign, now_utc=IN_WINDOW)
    (att,) = fake_sender.calls[0]["attachments"]
    assert att.filename == "deck.pdf" and att.data == b"%PDF"
    assert att.content_id is None

    send = db.execute(select(EmailSend)).scalars().one()
    assert send.attachments[0]["filename"] == "deck.pdf"


def test_send_fails_member_when_attachment_unavailable(db, fake_sender, monkeypatch):
    tenant, campaign, (member,) = _seed(db)
    campaign.config = {
        **campaign.config,
        "attachments": [
            # Wrong tenant prefix — must never be fetched or sent.
            {"s3_key": "tenants/other/outreach/x-deck.pdf", "filename": "deck.pdf"}
        ],
    }
    db.commit()
    monkeypatch.setattr(scheduler, "get_store", lambda: FakeStore())

    scheduler.run_campaign_tick(db, tenant, campaign, now_utc=IN_WINDOW)
    assert fake_sender.calls == []
    db.refresh(member)
    assert member.state == "failed"
    assert member.halt_reason == "attachment_unavailable"
