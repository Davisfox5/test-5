"""Contacts & Customers API — CRM-like directory for managing contacts and customers."""

import uuid
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.auth import get_current_tenant
from backend.app.db import get_db
from backend.app.models import Customer, CustomerNote, Contact, Interaction, Tenant
from backend.app.services.kb.context_dispatch import schedule_customer_brief_rebuild
from backend.app.services.kb.customer_brief_builder import CustomerBriefBuilder

router = APIRouter()


# ── Pydantic Schemas ─────────────────────────────────────


class CustomerCreate(BaseModel):
    name: str
    domain: Optional[str] = None
    crm_id: Optional[str] = None
    industry: Optional[str] = None
    metadata: Optional[Dict] = None


class CustomerOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    domain: Optional[str]
    crm_id: Optional[str]
    industry: Optional[str]
    metadata: Optional[Dict]

    model_config = {"from_attributes": True}


class CustomerUpdate(BaseModel):
    name: Optional[str] = None
    domain: Optional[str] = None
    crm_id: Optional[str] = None
    industry: Optional[str] = None
    metadata: Optional[Dict] = None


class ContactCreate(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    customer_id: Optional[uuid.UUID] = None
    crm_id: Optional[str] = None
    crm_source: Optional[str] = None
    metadata: Optional[Dict] = None


class ContactOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    name: Optional[str]
    email: Optional[str]
    phone: Optional[str]
    customer_id: Optional[uuid.UUID]
    crm_id: Optional[str]
    crm_source: Optional[str]
    interaction_count: int
    last_seen_at: Optional[datetime]
    sentiment_trend: list
    metadata: Optional[Dict]
    created_at: datetime

    model_config = {"from_attributes": True}


class InteractionSummary(BaseModel):
    id: uuid.UUID
    channel: str
    title: Optional[str]
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ContactDetail(ContactOut):
    customer: Optional[CustomerOut] = None
    recent_interactions: List[InteractionSummary] = []


class ContactUpdate(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    customer_id: Optional[uuid.UUID] = None
    crm_id: Optional[str] = None
    crm_source: Optional[str] = None
    metadata: Optional[Dict] = None


# ── Helper ───────────────────────────────────────────────


def _customer_to_out(c: Customer) -> CustomerOut:
    return CustomerOut(
        id=c.id,
        tenant_id=c.tenant_id,
        name=c.name,
        domain=c.domain,
        crm_id=c.crm_id,
        industry=c.industry,
        metadata=c.metadata_,
    )


def _contact_to_out(c: Contact) -> ContactOut:
    return ContactOut(
        id=c.id,
        tenant_id=c.tenant_id,
        name=c.name,
        email=c.email,
        phone=c.phone,
        customer_id=c.customer_id,
        crm_id=c.crm_id,
        crm_source=c.crm_source,
        interaction_count=c.interaction_count,
        last_seen_at=c.last_seen_at,
        sentiment_trend=c.sentiment_trend,
        metadata=c.metadata_,
        created_at=c.created_at,
    )


# ── Contact Endpoints ───────────────────────────────────


@router.get("/contacts", response_model=List[ContactOut])
async def list_contacts(
    name: Optional[str] = Query(None, description="Filter by name (case-insensitive partial match)"),
    phone: Optional[str] = Query(None),
    email: Optional[str] = Query(None),
    customer_id: Optional[uuid.UUID] = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    stmt = (
        select(Contact)
        .where(Contact.tenant_id == tenant.id)
        .order_by(Contact.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if name:
        stmt = stmt.where(Contact.name.ilike(f"%{name}%"))
    if phone:
        stmt = stmt.where(Contact.phone == phone)
    if email:
        stmt = stmt.where(Contact.email == email)
    if customer_id:
        stmt = stmt.where(Contact.customer_id == customer_id)

    result = await db.execute(stmt)
    contacts = result.scalars().all()
    return [_contact_to_out(c) for c in contacts]


@router.get("/contacts/{contact_id}", response_model=ContactDetail)
async def get_contact(
    contact_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    stmt = (
        select(Contact)
        .options(selectinload(Contact.customer))
        .where(Contact.id == contact_id, Contact.tenant_id == tenant.id)
    )
    result = await db.execute(stmt)
    contact = result.scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    # Fetch recent interactions
    interactions_stmt = (
        select(Interaction)
        .where(Interaction.contact_id == contact_id, Interaction.tenant_id == tenant.id)
        .order_by(Interaction.created_at.desc())
        .limit(10)
    )
    interactions_result = await db.execute(interactions_stmt)
    interactions = interactions_result.scalars().all()

    customer_out = _customer_to_out(contact.customer) if contact.customer else None

    return ContactDetail(
        **_contact_to_out(contact).model_dump(),
        customer=customer_out,
        recent_interactions=[
            InteractionSummary(
                id=i.id,
                channel=i.channel,
                title=i.title,
                status=i.status,
                created_at=i.created_at,
            )
            for i in interactions
        ],
    )


@router.post("/contacts", response_model=ContactOut, status_code=201)
async def create_contact(
    body: ContactCreate,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    contact = Contact(
        tenant_id=tenant.id,
        name=body.name,
        email=body.email,
        phone=body.phone,
        customer_id=body.customer_id,
        crm_id=body.crm_id,
        crm_source=body.crm_source,
        metadata_=body.metadata or {},
    )
    db.add(contact)
    await db.flush()
    return _contact_to_out(contact)


@router.patch("/contacts/{contact_id}", response_model=ContactOut)
async def update_contact(
    contact_id: uuid.UUID,
    body: ContactUpdate,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    stmt = select(Contact).where(Contact.id == contact_id, Contact.tenant_id == tenant.id)
    result = await db.execute(stmt)
    contact = result.scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    if body.name is not None:
        contact.name = body.name
    if body.email is not None:
        contact.email = body.email
    if body.phone is not None:
        contact.phone = body.phone
    if body.customer_id is not None:
        contact.customer_id = body.customer_id
    if body.crm_id is not None:
        contact.crm_id = body.crm_id
    if body.crm_source is not None:
        contact.crm_source = body.crm_source
    if body.metadata is not None:
        contact.metadata_ = body.metadata

    return _contact_to_out(contact)


@router.delete("/contacts/{contact_id}", status_code=204)
async def delete_contact(
    contact_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    stmt = select(Contact).where(Contact.id == contact_id, Contact.tenant_id == tenant.id)
    result = await db.execute(stmt)
    contact = result.scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    await db.delete(contact)


@router.get("/contacts/{contact_id}/interactions", response_model=List[InteractionSummary])
async def list_contact_interactions(
    contact_id: uuid.UUID,
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    # Verify contact exists and belongs to tenant
    contact_stmt = select(Contact.id).where(Contact.id == contact_id, Contact.tenant_id == tenant.id)
    contact_result = await db.execute(contact_stmt)
    if not contact_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Contact not found")

    stmt = (
        select(Interaction)
        .where(Interaction.contact_id == contact_id, Interaction.tenant_id == tenant.id)
        .order_by(Interaction.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await db.execute(stmt)
    return result.scalars().all()


# ── Customer Endpoints ────────────────────────────────────


@router.get("/customers", response_model=List[CustomerOut])
async def list_customers(
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    stmt = (
        select(Customer)
        .where(Customer.tenant_id == tenant.id)
        .order_by(Customer.name)
        .limit(limit)
        .offset(offset)
    )
    result = await db.execute(stmt)
    customers = result.scalars().all()
    return [_customer_to_out(c) for c in customers]


@router.post("/customers", response_model=CustomerOut, status_code=201)
async def create_customer(
    body: CustomerCreate,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    customer = Customer(
        tenant_id=tenant.id,
        name=body.name,
        domain=body.domain,
        crm_id=body.crm_id,
        industry=body.industry,
        metadata_=body.metadata or {},
    )
    db.add(customer)
    await db.flush()
    return _customer_to_out(customer)


@router.patch("/customers/{customer_id}", response_model=CustomerOut)
async def update_customer(
    customer_id: uuid.UUID,
    body: CustomerUpdate,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    stmt = select(Customer).where(Customer.id == customer_id, Customer.tenant_id == tenant.id)
    result = await db.execute(stmt)
    customer = result.scalar_one_or_none()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")

    if body.name is not None:
        customer.name = body.name
    if body.domain is not None:
        customer.domain = body.domain
    if body.crm_id is not None:
        customer.crm_id = body.crm_id
    if body.industry is not None:
        customer.industry = body.industry
    if body.metadata is not None:
        customer.metadata_ = body.metadata

    return _customer_to_out(customer)


@router.delete("/customers/{customer_id}", status_code=204)
async def delete_customer(
    customer_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    stmt = select(Customer).where(Customer.id == customer_id, Customer.tenant_id == tenant.id)
    result = await db.execute(stmt)
    customer = result.scalar_one_or_none()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    await db.delete(customer)


# ── Customer brief (LINDA's per-customer dossier) ─────────────────────


class CustomerBriefOut(BaseModel):
    customer_id: uuid.UUID
    brief: Dict


@router.get("/customers/{customer_id}/brief", response_model=CustomerBriefOut)
async def get_customer_brief(
    customer_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Return the customer brief LINDA uses at call time for this customer."""
    customer = await db.get(Customer, customer_id)
    if not customer or customer.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Customer not found")
    return CustomerBriefOut(customer_id=customer.id, brief=dict(customer.customer_brief or {}))


@router.post("/customers/{customer_id}/brief/rebuild", status_code=202)
async def rebuild_customer_brief_endpoint(
    customer_id: uuid.UUID,
    sync: bool = False,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Trigger a rebuild of the customer brief.

    ``sync=true`` runs inline and returns the new brief. Otherwise enqueues a
    debounced Celery task so a burst of triggers collapses into one run.
    """
    customer = await db.get(Customer, customer_id)
    if not customer or customer.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Customer not found")

    if sync:
        builder = CustomerBriefBuilder()
        brief = await builder.build(db, tenant.id, customer_id)
        return {"customer_id": str(customer_id), "brief": brief}

    await schedule_customer_brief_rebuild(tenant.id, customer_id)
    return {"customer_id": str(customer_id), "scheduled": True}


# ── Customer notes (agent-authored, feed into brief rebuilds) ─────────


class CustomerNoteIn(BaseModel):
    body: str
    interaction_id: Optional[uuid.UUID] = None


class CustomerNoteOut(BaseModel):
    id: uuid.UUID
    customer_id: uuid.UUID
    interaction_id: Optional[uuid.UUID]
    body: str
    created_at: datetime
    reviewed_at: Optional[datetime]

    model_config = {"from_attributes": True}


@router.post(
    "/customers/{customer_id}/notes",
    response_model=CustomerNoteOut,
    status_code=201,
)
async def add_customer_note(
    customer_id: uuid.UUID,
    body: CustomerNoteIn,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Attach a note to a customer. Fed as evidence into the next
    CustomerBriefBuilder run (which is automatically debounced)."""
    customer = await db.get(Customer, customer_id)
    if not customer or customer.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Customer not found")
    text = (body.body or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Note body is required")

    note = CustomerNote(
        tenant_id=tenant.id,
        customer_id=customer_id,
        interaction_id=body.interaction_id,
        body=text[:4000],
    )
    db.add(note)
    await db.flush()

    # Schedule a brief rebuild so the note gets folded in soon.
    await schedule_customer_brief_rebuild(tenant.id, customer_id)

    return note


@router.get(
    "/customers/{customer_id}/notes",
    response_model=List[CustomerNoteOut],
)
async def list_customer_notes(
    customer_id: uuid.UUID,
    limit: int = Query(50, le=200),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    customer = await db.get(Customer, customer_id)
    if not customer or customer.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Customer not found")
    stmt = (
        select(CustomerNote)
        .where(
            CustomerNote.tenant_id == tenant.id,
            CustomerNote.customer_id == customer_id,
        )
        .order_by(CustomerNote.created_at.desc())
        .limit(limit)
    )
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows)


# ── Historical sentiment (non-live-tier fallback) ─────────────────────


@router.get("/contacts/{contact_id}/sentiment-history")
async def get_contact_sentiment_history(
    contact_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Return the contact's rolling sentiment scores across past interactions.

    Tenants on the live-sentiment package receive updates via the live
    WebSocket; everyone else renders a static sparkline from this endpoint.
    """
    contact = await db.get(Contact, contact_id)
    if not contact or contact.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Contact not found")
    return {
        "contact_id": str(contact_id),
        "points": list(contact.sentiment_trend or []),
        "interaction_count": contact.interaction_count or 0,
    }
