"""Contacts & Companies API — CRM-like directory for managing contacts and companies."""

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
from backend.app.models import Company, Contact, Interaction, Tenant

router = APIRouter()


# ── Pydantic Schemas ─────────────────────────────────────


class CompanyCreate(BaseModel):
    name: str
    domain: Optional[str] = None
    crm_id: Optional[str] = None
    industry: Optional[str] = None
    metadata: Optional[Dict] = None


class CompanyOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    domain: Optional[str]
    crm_id: Optional[str]
    industry: Optional[str]
    metadata: Optional[Dict]

    model_config = {"from_attributes": True}


class CompanyUpdate(BaseModel):
    name: Optional[str] = None
    domain: Optional[str] = None
    crm_id: Optional[str] = None
    industry: Optional[str] = None
    metadata: Optional[Dict] = None


class ContactCreate(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    company_id: Optional[uuid.UUID] = None
    crm_id: Optional[str] = None
    crm_source: Optional[str] = None
    metadata: Optional[Dict] = None


class ContactOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    name: Optional[str]
    email: Optional[str]
    phone: Optional[str]
    company_id: Optional[uuid.UUID]
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
    company: Optional[CompanyOut] = None
    recent_interactions: List[InteractionSummary] = []


class ContactUpdate(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    company_id: Optional[uuid.UUID] = None
    crm_id: Optional[str] = None
    crm_source: Optional[str] = None
    metadata: Optional[Dict] = None


# ── Helper ───────────────────────────────────────────────


def _company_to_out(c: Company) -> CompanyOut:
    return CompanyOut(
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
        company_id=c.company_id,
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
    company_id: Optional[uuid.UUID] = Query(None),
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
    if company_id:
        stmt = stmt.where(Contact.company_id == company_id)

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
        .options(selectinload(Contact.company))
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

    company_out = _company_to_out(contact.company) if contact.company else None

    return ContactDetail(
        **_contact_to_out(contact).model_dump(),
        company=company_out,
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
        company_id=body.company_id,
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
    if body.company_id is not None:
        contact.company_id = body.company_id
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


# ── Company Endpoints ────────────────────────────────────


@router.get("/companies", response_model=List[CompanyOut])
async def list_companies(
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    stmt = (
        select(Company)
        .where(Company.tenant_id == tenant.id)
        .order_by(Company.name)
        .limit(limit)
        .offset(offset)
    )
    result = await db.execute(stmt)
    companies = result.scalars().all()
    return [_company_to_out(c) for c in companies]


@router.post("/companies", response_model=CompanyOut, status_code=201)
async def create_company(
    body: CompanyCreate,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    company = Company(
        tenant_id=tenant.id,
        name=body.name,
        domain=body.domain,
        crm_id=body.crm_id,
        industry=body.industry,
        metadata_=body.metadata or {},
    )
    db.add(company)
    await db.flush()
    return _company_to_out(company)


@router.patch("/companies/{company_id}", response_model=CompanyOut)
async def update_company(
    company_id: uuid.UUID,
    body: CompanyUpdate,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    stmt = select(Company).where(Company.id == company_id, Company.tenant_id == tenant.id)
    result = await db.execute(stmt)
    company = result.scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    if body.name is not None:
        company.name = body.name
    if body.domain is not None:
        company.domain = body.domain
    if body.crm_id is not None:
        company.crm_id = body.crm_id
    if body.industry is not None:
        company.industry = body.industry
    if body.metadata is not None:
        company.metadata_ = body.metadata

    return _company_to_out(company)


@router.delete("/companies/{company_id}", status_code=204)
async def delete_company(
    company_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    stmt = select(Company).where(Company.id == company_id, Company.tenant_id == tenant.id)
    result = await db.execute(stmt)
    company = result.scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    await db.delete(company)
