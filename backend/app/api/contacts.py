"""Contacts & Customers API — CRM-like directory for managing contacts and customers."""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.auth import (
    AuthPrincipal,
    get_current_principal,
    get_current_tenant,
    require_scope,
)
from backend.app.db import get_db
from backend.app.models import (
    ActionItem,
    Commitment,
    Contact,
    Customer,
    CustomerNote,
    CustomerOwner,
    CustomerWarning,
    Interaction,
    Tenant,
    User,
)
from backend.app.services.audit_log import audit_log
from backend.app.services.kb.context_dispatch import schedule_customer_brief_rebuild
from backend.app.services.kb.customer_brief_builder import CustomerBriefBuilder

router = APIRouter()


# ── Favicon proxy ────────────────────────────────────────
#
# Customer cards render upstream favicons (Google's S2 service today).
# Hitting them direct from the browser means N DNS lookups + N HTTP
# round-trips per page render. The proxy below adds a server-side cache
# (24h immutable) and a single canonical URL so the browser dedupes
# domain hits and the upstream is queried once per domain per day.

import re as _re
import httpx as _httpx
from fastapi import Response as _Response

_FAVICON_DOMAIN_RE = _re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}$", _re.IGNORECASE)
_FAVICON_UPSTREAM = (
    "https://www.google.com/s2/favicons?domain={domain}&sz=64"
)
_FAVICON_TTL_SECONDS = 24 * 60 * 60


@router.get("/favicons", response_class=_Response)
async def proxy_favicon(
    domain: str = Query(..., min_length=3, max_length=253),
):
    """Tenant-agnostic favicon proxy with a 24h browser cache header.

    Lightweight enough to skip auth — favicons are public assets, and
    the validation regex prevents the endpoint from being abused as a
    generic open-proxy.
    """
    if not _FAVICON_DOMAIN_RE.match(domain):
        raise HTTPException(status_code=400, detail="invalid domain")
    url = _FAVICON_UPSTREAM.format(domain=domain)
    try:
        async with _httpx.AsyncClient(timeout=5.0, follow_redirects=True) as c:
            up = await c.get(url)
            up.raise_for_status()
            content = up.content
            content_type = up.headers.get("content-type", "image/png")
    except _httpx.HTTPError:
        # Return a tiny 1×1 transparent PNG so the browser doesn't show
        # a broken-image icon on upstream failure.
        content = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\x00\x01"
            b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        content_type = "image/png"
    return _Response(
        content=content,
        media_type=content_type,
        headers={
            "Cache-Control": f"public, max-age={_FAVICON_TTL_SECONDS}, immutable",
        },
    )


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
    parent_customer_id: Optional[uuid.UUID] = None
    timezone: Optional[str] = None
    strongest_connection_user_id: Optional[uuid.UUID] = None

    model_config = {"from_attributes": True}


class CustomerOwnerOut(BaseModel):
    """One row of the multi-owner avatar stack on a customer card."""

    user_id: uuid.UUID
    name: Optional[str]
    email: Optional[str]
    role: str  # primary | secondary
    assigned_via: str

    model_config = {"from_attributes": True}


class CustomerListItem(BaseModel):
    """Rich customer row for the list page.

    Adds the per-row signals the SPA's table/grid/kanban views need:
    multi-owner stack, latest-interaction summary, open-action-item
    count, multithreading count (distinct contacts in last 90 days),
    and the most-recent sentiment + churn-risk numbers. Computed once
    per row in a small bounded query budget — no N+1.
    """

    id: uuid.UUID
    name: str
    domain: Optional[str]
    industry: Optional[str]
    parent_customer_id: Optional[uuid.UUID] = None
    timezone: Optional[str] = None

    # Multi-owner stack
    owners: List[CustomerOwnerOut] = []

    # Activity
    contact_count: int = 0
    multithreading_90d: int = 0
    latest_interaction_at: Optional[datetime] = None
    latest_interaction_id: Optional[uuid.UUID] = None
    latest_interaction_title: Optional[str] = None

    # Health
    sentiment_score: Optional[float] = None  # most-recent analyzed call
    churn_risk: Optional[float] = None       # most-recent analyzed call
    open_action_items: int = 0


class CustomerListResponse(BaseModel):
    items: List[CustomerListItem]
    total: int


class CustomerInteractionSummary(BaseModel):
    """A row in the customer detail page's recent-interactions list."""

    id: uuid.UUID
    title: Optional[str]
    channel: str
    direction: Optional[str]
    status: str
    created_at: datetime
    sentiment_score: Optional[float] = None
    summary_excerpt: Optional[str] = None  # First 240 chars of insights.summary

    model_config = {"from_attributes": True}


class CustomerActionItemSummary(BaseModel):
    """One pending action item rolled up to the customer detail page."""

    id: uuid.UUID
    interaction_id: uuid.UUID
    title: str
    description: Optional[str]
    category: Optional[str]
    priority: Optional[str]
    status: str
    created_at: datetime


class CustomerWarningOut(BaseModel):
    """A Deal Warning chip on the customer page (Phase 4)."""

    id: uuid.UUID
    kind: str
    severity: str
    label: Optional[str] = None
    evidence_text: Optional[str]
    evidence_interaction_id: Optional[uuid.UUID]
    first_detected_at: datetime
    last_detected_at: datetime
    dismissed_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class CommitmentOut(BaseModel):
    """One row in the commitments list on the customer detail page."""

    id: uuid.UUID
    interaction_id: uuid.UUID
    actor_side: str  # rep | customer | unknown
    actor_user_id: Optional[uuid.UUID] = None
    actor_user_name: Optional[str] = None
    actor_contact_id: Optional[uuid.UUID] = None
    actor_contact_name: Optional[str] = None
    target_user_id: Optional[uuid.UUID] = None
    target_contact_id: Optional[uuid.UUID] = None
    text: str
    evidence_excerpt: Optional[str]
    due_date: Optional[datetime]
    status: str  # pending | done | overdue | dismissed
    completed_at: Optional[datetime] = None
    completed_via: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class CustomerDetail(BaseModel):
    """Full customer record for the detail page (Layout 1-4 all consume this).

    Note: ``contacts`` is a forward reference because ``ContactOut`` is
    defined further down in this file. PR #65 originally embedded the
    bare class name and broke API startup with NameError when the
    module was imported in the right order. The forward ref + the
    explicit ``model_rebuild()`` call after ``ContactOut`` lands keeps
    the schema layout we want without requiring a hot reorder.
    """

    # Base
    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    domain: Optional[str]
    industry: Optional[str]
    parent_customer_id: Optional[uuid.UUID]
    timezone: Optional[str]
    metadata: Optional[Dict]

    # People
    owners: List[CustomerOwnerOut] = []
    contacts: List["ContactOut"] = []
    multithreading_90d: int = 0

    # Activity
    recent_interactions: List[CustomerInteractionSummary] = []
    open_action_items: List[CustomerActionItemSummary] = []

    # Health (latest call)
    sentiment_score: Optional[float] = None
    churn_risk: Optional[float] = None
    upsell_score: Optional[float] = None

    # Customer brief (for the dossier-style layout)
    customer_brief: Optional[Dict] = None

    # Phase 4 surfaces — Deal Warnings + Commitments
    warnings: List[CustomerWarningOut] = []
    commitments: List[CommitmentOut] = []


class CustomerUpdate(BaseModel):
    name: Optional[str] = None
    domain: Optional[str] = None
    crm_id: Optional[str] = None
    industry: Optional[str] = None
    metadata: Optional[Dict] = None
    parent_customer_id: Optional[uuid.UUID] = None
    timezone: Optional[str] = None


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
    # Buying-group role inferred from call dialogue. ``role`` is null
    # until entity_resolution gets enough confidence; ``role_confidence``
    # carries the LLM's most-recent score so the SPA can render
    # confirmed (>=0.8) vs suggested (0.6-0.8) chip styling.
    role: Optional[str] = None
    role_confidence: Optional[float] = None
    interaction_count: int
    last_seen_at: Optional[datetime]
    sentiment_trend: list
    metadata: Optional[Dict]
    created_at: datetime

    model_config = {"from_attributes": True}


# Resolve the forward reference on CustomerDetail now that ContactOut
# exists. Without this Pydantic v2 raises ``PydanticUndefinedAnnotation``
# the first time the schema is used (e.g. on first request hitting
# ``GET /customers/{id}/detail``). This is the line that was missing
# in PR #65 and is what took staging down with the deploy timeout.
CustomerDetail.model_rebuild()


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
        parent_customer_id=c.parent_customer_id,
        timezone=c.timezone,
        strongest_connection_user_id=c.strongest_connection_user_id,
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
        # role + role_confidence were added to the ContactOut schema in
        # PR #65 but forgotten here in _contact_to_out. The fields had
        # ``Optional[...] = None`` defaults, so every API response
        # returned role=None even though the DB column was correctly
        # populated by entity_resolution. The Phase 3.5 v3 SQL readback
        # nailed this: trace["role_before"] showed "champion" pulled
        # via SQLAlchemy, but the SPA still saw null. Fix is one line
        # per field.
        role=c.role,
        role_confidence=c.role_confidence,
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


@router.post(
    "/contacts",
    response_model=ContactOut,
    status_code=201,
    dependencies=[Depends(require_scope("contacts:write"))],
)
async def create_contact(
    body: ContactCreate,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    principal: AuthPrincipal = Depends(get_current_principal),
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
    await audit_log(
        db,
        principal,
        action="contact.created",
        resource_type="contact",
        resource_id=str(contact.id),
        after={"name": contact.name, "email": contact.email},
    )
    return _contact_to_out(contact)


@router.patch(
    "/contacts/{contact_id}",
    response_model=ContactOut,
    dependencies=[Depends(require_scope("contacts:write"))],
)
async def update_contact(
    contact_id: uuid.UUID,
    body: ContactUpdate,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    stmt = select(Contact).where(Contact.id == contact_id, Contact.tenant_id == tenant.id)
    result = await db.execute(stmt)
    contact = result.scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    before = {"name": contact.name, "email": contact.email, "phone": contact.phone}

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

    await db.flush()
    await audit_log(
        db,
        principal,
        action="contact.updated",
        resource_type="contact",
        resource_id=str(contact.id),
        before=before,
        after={"name": contact.name, "email": contact.email, "phone": contact.phone},
    )
    return _contact_to_out(contact)


@router.delete(
    "/contacts/{contact_id}",
    status_code=204,
    dependencies=[Depends(require_scope("contacts:write"))],
)
async def delete_contact(
    contact_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    stmt = select(Contact).where(Contact.id == contact_id, Contact.tenant_id == tenant.id)
    result = await db.execute(stmt)
    contact = result.scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    snapshot = {"name": contact.name, "email": contact.email}
    await db.delete(contact)
    await db.flush()
    await audit_log(
        db,
        principal,
        action="contact.deleted",
        resource_type="contact",
        resource_id=str(contact_id),
        before=snapshot,
    )


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
    """Bare customer list (legacy shape).

    The Customers list page calls the richer ``/customers/list`` below.
    This endpoint stays as-is so existing callers (CRM sync utilities,
    older SPA hooks) don't break.
    """
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


@router.get("/customers/list", response_model=CustomerListResponse)
async def list_customers_rich(
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    name: Optional[str] = Query(
        None,
        description="Case-insensitive partial match on customer name.",
    ),
    owner_user_id: Optional[uuid.UUID] = Query(
        None,
        description="Filter to customers owned (primary or secondary) by this user.",
    ),
    sort: str = Query(
        "latest_interaction",
        description=(
            "Sort key: latest_interaction (default), name, churn_risk, "
            "open_action_items, multithreading_90d."
        ),
    ),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Rich customer list for the Customers page.

    Returns one row per customer with everything the table / grid /
    kanban variants need to render without further fetches: multi-
    owner avatars, latest-interaction summary, open-action-item count,
    multithreading count over the last 90 days, and the most-recent
    sentiment + churn-risk signals.

    Per-row computation is bounded: one base query + a handful of
    per-tenant aggregates joined back. Pagination is on the base
    query so a tenant with thousands of customers paginates cheaply.
    """
    # ── Base customer query ─────────────────────────────────
    base_stmt = select(Customer).where(Customer.tenant_id == tenant.id)
    if name:
        base_stmt = base_stmt.where(Customer.name.ilike(f"%{name}%"))
    if owner_user_id:
        base_stmt = base_stmt.where(
            Customer.id.in_(
                select(CustomerOwner.customer_id).where(
                    CustomerOwner.tenant_id == tenant.id,
                    CustomerOwner.user_id == owner_user_id,
                )
            )
        )

    # Total count for pagination — separate query because the .count()
    # on the same statement collides with the limit/offset below.
    total_stmt = select(func.count()).select_from(base_stmt.subquery())
    total = (await db.execute(total_stmt)).scalar_one()

    base_stmt = base_stmt.order_by(Customer.name).limit(limit).offset(offset)
    customers = (await db.execute(base_stmt)).scalars().all()
    if not customers:
        return CustomerListResponse(items=[], total=total)

    customer_ids = [c.id for c in customers]
    cutoff_90d = datetime.now(timezone.utc) - timedelta(days=90)

    # ── Owners (joined to users for name/email) ──────────────
    owners_rows = (
        await db.execute(
            select(
                CustomerOwner.customer_id,
                CustomerOwner.user_id,
                CustomerOwner.role,
                CustomerOwner.assigned_via,
                User.name,
                User.email,
            )
            .join(User, User.id == CustomerOwner.user_id)
            .where(CustomerOwner.customer_id.in_(customer_ids))
            .order_by(CustomerOwner.role.desc(), CustomerOwner.assigned_at)
        )
    ).all()
    owners_by_customer: Dict[uuid.UUID, List[CustomerOwnerOut]] = {}
    for row in owners_rows:
        owners_by_customer.setdefault(row.customer_id, []).append(
            CustomerOwnerOut(
                user_id=row.user_id,
                name=row.name,
                email=row.email,
                role=row.role,
                assigned_via=row.assigned_via,
            )
        )

    # ── Contact counts (total + last-90d distinct via interactions) ──
    contact_count_rows = (
        await db.execute(
            select(Contact.customer_id, func.count(Contact.id))
            .where(
                Contact.tenant_id == tenant.id,
                Contact.customer_id.in_(customer_ids),
            )
            .group_by(Contact.customer_id)
        )
    ).all()
    contact_count_by_customer = {row[0]: int(row[1]) for row in contact_count_rows}

    multithreading_rows = (
        await db.execute(
            select(
                Interaction.customer_id,
                func.count(func.distinct(Interaction.contact_id)),
            )
            .where(
                Interaction.tenant_id == tenant.id,
                Interaction.customer_id.in_(customer_ids),
                Interaction.contact_id.is_not(None),
                Interaction.created_at >= cutoff_90d,
            )
            .group_by(Interaction.customer_id)
        )
    ).all()
    multithreading_by_customer = {row[0]: int(row[1]) for row in multithreading_rows}

    # ── Open action items per customer ──────────────────────
    # Joined via interaction → customer because action_items don't
    # carry a customer_id directly today. (Phase 5 may denormalize.)
    action_item_rows = (
        await db.execute(
            select(Interaction.customer_id, func.count(ActionItem.id))
            .join(ActionItem, ActionItem.interaction_id == Interaction.id)
            .where(
                Interaction.tenant_id == tenant.id,
                Interaction.customer_id.in_(customer_ids),
                ActionItem.status == "pending",
            )
            .group_by(Interaction.customer_id)
        )
    ).all()
    open_items_by_customer = {row[0]: int(row[1]) for row in action_item_rows}

    # ── Latest interaction per customer ─────────────────────
    # Use a window-style subquery (DISTINCT ON is Postgres-specific
    # but we're on PG; use a per-customer max + join-back for
    # cross-DB safety with SQLite tests).
    latest_subq = (
        select(
            Interaction.customer_id.label("cid"),
            func.max(Interaction.created_at).label("max_created"),
        )
        .where(
            Interaction.tenant_id == tenant.id,
            Interaction.customer_id.in_(customer_ids),
        )
        .group_by(Interaction.customer_id)
        .subquery()
    )
    latest_rows = (
        await db.execute(
            select(
                Interaction.id,
                Interaction.customer_id,
                Interaction.title,
                Interaction.created_at,
                Interaction.insights,
            )
            .join(
                latest_subq,
                (Interaction.customer_id == latest_subq.c.cid)
                & (Interaction.created_at == latest_subq.c.max_created),
            )
            .where(Interaction.tenant_id == tenant.id)
        )
    ).all()
    latest_by_customer: Dict[uuid.UUID, Dict] = {}
    for row in latest_rows:
        ins = row.insights or {}
        latest_by_customer[row.customer_id] = {
            "id": row.id,
            "title": row.title,
            "at": row.created_at,
            "sentiment_score": ins.get("sentiment_score"),
            "churn_risk": ins.get("churn_risk"),
        }

    # ── Assemble rows ───────────────────────────────────────
    items: List[CustomerListItem] = []
    for c in customers:
        latest = latest_by_customer.get(c.id) or {}
        items.append(
            CustomerListItem(
                id=c.id,
                name=c.name,
                domain=c.domain,
                industry=c.industry,
                parent_customer_id=c.parent_customer_id,
                timezone=c.timezone,
                owners=owners_by_customer.get(c.id, []),
                contact_count=contact_count_by_customer.get(c.id, 0),
                multithreading_90d=multithreading_by_customer.get(c.id, 0),
                latest_interaction_at=latest.get("at"),
                latest_interaction_id=latest.get("id"),
                latest_interaction_title=latest.get("title"),
                sentiment_score=latest.get("sentiment_score"),
                churn_risk=latest.get("churn_risk"),
                open_action_items=open_items_by_customer.get(c.id, 0),
            )
        )

    # ── Optional sort overlay ───────────────────────────────
    # Default base SQL order is name; if the caller asked for a
    # signal sort, re-order in Python. Cheap because limit is bounded.
    if sort == "latest_interaction":
        items.sort(
            key=lambda it: (it.latest_interaction_at or datetime.min.replace(tzinfo=timezone.utc)),
            reverse=True,
        )
    elif sort == "churn_risk":
        items.sort(key=lambda it: (it.churn_risk or 0.0), reverse=True)
    elif sort == "open_action_items":
        items.sort(key=lambda it: it.open_action_items, reverse=True)
    elif sort == "multithreading_90d":
        items.sort(key=lambda it: it.multithreading_90d, reverse=True)
    # 'name' uses the SQL order already.

    return CustomerListResponse(items=items, total=total)


@router.get("/customers/{customer_id}/detail", response_model=CustomerDetail)
async def get_customer_detail(
    customer_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Full customer record for the detail page (all 4 layout variants).

    Returns the customer plus rolled-up people / activity / health
    signals so the SPA can render any of the four layout variants
    without further fetches. Pagination is fixed at the most-recent
    25 interactions and 50 open action items — anything more is the
    Interactions and Action Items lists' job.
    """
    cust = (
        await db.execute(
            select(Customer).where(
                Customer.id == customer_id,
                Customer.tenant_id == tenant.id,
            )
        )
    ).scalar_one_or_none()
    if cust is None:
        raise HTTPException(status_code=404, detail="Customer not found")

    cutoff_90d = datetime.now(timezone.utc) - timedelta(days=90)

    # Owners
    owners_rows = (
        await db.execute(
            select(
                CustomerOwner.user_id,
                CustomerOwner.role,
                CustomerOwner.assigned_via,
                User.name,
                User.email,
            )
            .join(User, User.id == CustomerOwner.user_id)
            .where(CustomerOwner.customer_id == customer_id)
            .order_by(CustomerOwner.role.desc(), CustomerOwner.assigned_at)
        )
    ).all()
    owners = [
        CustomerOwnerOut(
            user_id=row.user_id,
            name=row.name,
            email=row.email,
            role=row.role,
            assigned_via=row.assigned_via,
        )
        for row in owners_rows
    ]

    # Contacts
    contact_rows = (
        await db.execute(
            select(Contact)
            .where(
                Contact.tenant_id == tenant.id,
                Contact.customer_id == customer_id,
            )
            .order_by(Contact.last_seen_at.desc().nullslast(), Contact.created_at.desc())
        )
    ).scalars().all()
    contacts = [_contact_to_out(c) for c in contact_rows]

    # Recent interactions (max 25)
    interaction_rows = (
        await db.execute(
            select(Interaction)
            .where(
                Interaction.tenant_id == tenant.id,
                Interaction.customer_id == customer_id,
            )
            .order_by(Interaction.created_at.desc())
            .limit(25)
        )
    ).scalars().all()
    recent_interactions = [
        CustomerInteractionSummary(
            id=ix.id,
            title=ix.title,
            channel=ix.channel,
            direction=ix.direction,
            status=ix.status,
            created_at=ix.created_at,
            sentiment_score=(ix.insights or {}).get("sentiment_score"),
            summary_excerpt=(((ix.insights or {}).get("summary")) or None) and (
                ((ix.insights or {}).get("summary") or "")[:240]
            ),
        )
        for ix in interaction_rows
    ]

    # Multithreading: distinct contacts on this customer's calls in 90d
    multithreading_90d = (
        await db.execute(
            select(func.count(func.distinct(Interaction.contact_id))).where(
                Interaction.tenant_id == tenant.id,
                Interaction.customer_id == customer_id,
                Interaction.contact_id.is_not(None),
                Interaction.created_at >= cutoff_90d,
            )
        )
    ).scalar_one() or 0

    # Open action items rolled up via interaction → customer (max 50)
    action_item_rows = (
        await db.execute(
            select(ActionItem)
            .join(Interaction, ActionItem.interaction_id == Interaction.id)
            .where(
                Interaction.tenant_id == tenant.id,
                Interaction.customer_id == customer_id,
                ActionItem.status == "pending",
            )
            .order_by(ActionItem.created_at.desc())
            .limit(50)
        )
    ).scalars().all()
    open_action_items = [
        CustomerActionItemSummary(
            id=ai.id,
            interaction_id=ai.interaction_id,
            title=ai.title,
            description=ai.description,
            category=ai.category,
            priority=ai.priority,
            status=ai.status,
            created_at=ai.created_at,
        )
        for ai in action_item_rows
    ]

    # Latest-call health signals
    latest_health = recent_interactions[0] if recent_interactions else None
    sentiment_score = latest_health.sentiment_score if latest_health else None
    churn_risk = None
    upsell_score = None
    if interaction_rows:
        latest_insights = interaction_rows[0].insights or {}
        churn_risk = latest_insights.get("churn_risk")
        upsell_score = latest_insights.get("upsell_score")

    # Phase 4 — active (non-dismissed) Deal Warnings, severity-then-recency.
    warning_rows = (
        await db.execute(
            select(CustomerWarning)
            .where(
                CustomerWarning.tenant_id == tenant.id,
                CustomerWarning.customer_id == customer_id,
                CustomerWarning.dismissed_at.is_(None),
            )
            .order_by(
                # Manual severity order so 'high' > 'medium' > 'low' beats
                # alphabetical. ``case`` would be cleaner but we keep the
                # order client-side too — sort here is best-effort.
                CustomerWarning.last_detected_at.desc(),
            )
        )
    ).scalars().all()
    warnings = [_warning_to_out(w) for w in warning_rows]
    # Stable severity sort (high → medium → low) on top of recency.
    _SEV_RANK = {"high": 0, "medium": 1, "low": 2}
    warnings.sort(key=lambda w: _SEV_RANK.get(w.severity, 3))

    # Phase 4 — open + recent-done commitments. Limit to 50; the
    # detail page's commitments section paginates older ones from
    # the dedicated /commitments endpoint.
    commit_rows = (
        await db.execute(
            select(Commitment)
            .where(
                Commitment.tenant_id == tenant.id,
                Commitment.customer_id == customer_id,
                Commitment.status.in_(("pending", "overdue", "done")),
            )
            .order_by(
                # Pending/overdue first (NULL completed_at) then by due
                # date ascending — overdue surfaces above future-pending.
                Commitment.completed_at.asc().nullsfirst(),
                Commitment.due_date.asc().nullslast(),
                Commitment.created_at.desc(),
            )
            .limit(50)
        )
    ).scalars().all()
    actor_user_ids = {c.actor_user_id for c in commit_rows if c.actor_user_id}
    actor_contact_ids = {c.actor_contact_id for c in commit_rows if c.actor_contact_id}
    user_name_map: Dict[uuid.UUID, str] = {}
    if actor_user_ids:
        rows = (
            await db.execute(
                select(User.id, User.name).where(User.id.in_(actor_user_ids))
            )
        ).all()
        user_name_map = {r.id: (r.name or "") for r in rows}
    contact_name_map: Dict[uuid.UUID, str] = {}
    if actor_contact_ids:
        rows = (
            await db.execute(
                select(Contact.id, Contact.name).where(Contact.id.in_(actor_contact_ids))
            )
        ).all()
        contact_name_map = {r.id: (r.name or "") for r in rows}
    commitments = [
        _commitment_to_out(
            c,
            actor_user_name=user_name_map.get(c.actor_user_id),
            actor_contact_name=contact_name_map.get(c.actor_contact_id),
        )
        for c in commit_rows
    ]

    return CustomerDetail(
        id=cust.id,
        tenant_id=cust.tenant_id,
        name=cust.name,
        domain=cust.domain,
        industry=cust.industry,
        parent_customer_id=cust.parent_customer_id,
        timezone=cust.timezone,
        metadata=cust.metadata_,
        owners=owners,
        contacts=contacts,
        multithreading_90d=int(multithreading_90d),
        recent_interactions=recent_interactions,
        open_action_items=open_action_items,
        sentiment_score=sentiment_score,
        churn_risk=churn_risk,
        upsell_score=upsell_score,
        customer_brief=cust.customer_brief or {},
        warnings=warnings,
        commitments=commitments,
    )


def _warning_to_out(w: CustomerWarning) -> CustomerWarningOut:
    label = None
    meta = w.metadata_ or {}
    if isinstance(meta, dict):
        label = meta.get("label")
    return CustomerWarningOut(
        id=w.id,
        kind=w.kind,
        severity=w.severity,
        label=label if isinstance(label, str) else None,
        evidence_text=w.evidence_text,
        evidence_interaction_id=w.evidence_interaction_id,
        first_detected_at=w.first_detected_at,
        last_detected_at=w.last_detected_at,
        dismissed_at=w.dismissed_at,
    )


def _commitment_to_out(
    c: Commitment,
    *,
    actor_user_name: Optional[str] = None,
    actor_contact_name: Optional[str] = None,
) -> CommitmentOut:
    return CommitmentOut(
        id=c.id,
        interaction_id=c.interaction_id,
        actor_side=c.actor_side,
        actor_user_id=c.actor_user_id,
        actor_user_name=actor_user_name or None,
        actor_contact_id=c.actor_contact_id,
        actor_contact_name=actor_contact_name or None,
        target_user_id=c.target_user_id,
        target_contact_id=c.target_contact_id,
        text=c.text,
        evidence_excerpt=c.evidence_excerpt,
        due_date=c.due_date,
        status=c.status,
        completed_at=c.completed_at,
        completed_via=c.completed_via,
        created_at=c.created_at,
    )


@router.post(
    "/customers",
    response_model=CustomerOut,
    status_code=201,
    dependencies=[Depends(require_scope("contacts:write"))],
)
async def create_customer(
    body: CustomerCreate,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    principal: AuthPrincipal = Depends(get_current_principal),
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
    await audit_log(
        db,
        principal,
        action="customer.created",
        resource_type="customer",
        resource_id=str(customer.id),
        after={"name": customer.name, "domain": customer.domain},
    )
    return _customer_to_out(customer)


@router.patch(
    "/customers/{customer_id}",
    response_model=CustomerOut,
    dependencies=[Depends(require_scope("contacts:write"))],
)
async def update_customer(
    customer_id: uuid.UUID,
    body: CustomerUpdate,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    stmt = select(Customer).where(Customer.id == customer_id, Customer.tenant_id == tenant.id)
    result = await db.execute(stmt)
    customer = result.scalar_one_or_none()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")

    before = {"name": customer.name, "domain": customer.domain, "industry": customer.industry}

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

    await db.flush()
    await audit_log(
        db,
        principal,
        action="customer.updated",
        resource_type="customer",
        resource_id=str(customer.id),
        before=before,
        after={"name": customer.name, "domain": customer.domain, "industry": customer.industry},
    )
    return _customer_to_out(customer)


@router.delete(
    "/customers/{customer_id}",
    status_code=204,
    dependencies=[Depends(require_scope("contacts:write"))],
)
async def delete_customer(
    customer_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    stmt = select(Customer).where(Customer.id == customer_id, Customer.tenant_id == tenant.id)
    result = await db.execute(stmt)
    customer = result.scalar_one_or_none()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    snapshot = {"name": customer.name, "domain": customer.domain}
    await db.delete(customer)
    await db.flush()
    await audit_log(
        db,
        principal,
        action="customer.deleted",
        resource_type="customer",
        resource_id=str(customer_id),
        before=snapshot,
    )


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
    principal: AuthPrincipal = Depends(get_current_principal),
):
    """Attach a note to a customer. Fed as evidence into the next
    CustomerBriefBuilder run (which is automatically debounced)."""
    tenant = principal.tenant
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
        # Audit — which agent authored the note.
        author_user_id=principal.user_id,
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


# ── Phase 4 — Deal Warnings ───────────────────────────────────────


@router.get(
    "/customers/{customer_id}/warnings",
    response_model=List[CustomerWarningOut],
)
async def list_customer_warnings(
    customer_id: uuid.UUID,
    include_dismissed: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """List Deal Warnings on a customer (Phase 4)."""
    cust = await db.get(Customer, customer_id)
    if cust is None or cust.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Customer not found")
    stmt = select(CustomerWarning).where(
        CustomerWarning.tenant_id == tenant.id,
        CustomerWarning.customer_id == customer_id,
    )
    if not include_dismissed:
        stmt = stmt.where(CustomerWarning.dismissed_at.is_(None))
    stmt = stmt.order_by(CustomerWarning.last_detected_at.desc())
    rows = (await db.execute(stmt)).scalars().all()
    out = [_warning_to_out(w) for w in rows]
    _SEV = {"high": 0, "medium": 1, "low": 2}
    out.sort(key=lambda w: _SEV.get(w.severity, 3))
    return out


@router.post(
    "/customers/{customer_id}/warnings/{warning_id}/dismiss",
    response_model=CustomerWarningOut,
    dependencies=[Depends(require_scope("contacts:write"))],
)
async def dismiss_customer_warning(
    customer_id: uuid.UUID,
    warning_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    """Dismiss a Deal Warning. Re-detection by the pipeline can re-raise it."""
    w = await db.get(CustomerWarning, warning_id)
    if (
        w is None
        or w.tenant_id != tenant.id
        or w.customer_id != customer_id
    ):
        raise HTTPException(status_code=404, detail="Warning not found")
    w.dismissed_at = datetime.now(timezone.utc)
    w.dismissed_by = principal.user_id
    await db.commit()
    await db.refresh(w)
    return _warning_to_out(w)


@router.post(
    "/customers/{customer_id}/warnings/{warning_id}/restore",
    response_model=CustomerWarningOut,
    dependencies=[Depends(require_scope("contacts:write"))],
)
async def restore_customer_warning(
    customer_id: uuid.UUID,
    warning_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Reverse a dismissal — useful if a user clicked dismiss by mistake."""
    w = await db.get(CustomerWarning, warning_id)
    if (
        w is None
        or w.tenant_id != tenant.id
        or w.customer_id != customer_id
    ):
        raise HTTPException(status_code=404, detail="Warning not found")
    w.dismissed_at = None
    w.dismissed_by = None
    await db.commit()
    await db.refresh(w)
    return _warning_to_out(w)


# ── Phase 4 — Commitments ─────────────────────────────────────────


@router.get(
    "/customers/{customer_id}/commitments",
    response_model=List[CommitmentOut],
)
async def list_customer_commitments(
    customer_id: uuid.UUID,
    status: Optional[str] = Query(
        None, description="Filter: pending | done | overdue | dismissed"
    ),
    limit: int = Query(100, le=500),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    cust = await db.get(Customer, customer_id)
    if cust is None or cust.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Customer not found")
    stmt = select(Commitment).where(
        Commitment.tenant_id == tenant.id,
        Commitment.customer_id == customer_id,
    )
    if status:
        stmt = stmt.where(Commitment.status == status)
    stmt = stmt.order_by(
        Commitment.completed_at.asc().nullsfirst(),
        Commitment.due_date.asc().nullslast(),
        Commitment.created_at.desc(),
    ).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    user_ids = {c.actor_user_id for c in rows if c.actor_user_id}
    contact_ids = {c.actor_contact_id for c in rows if c.actor_contact_id}
    user_map: Dict[uuid.UUID, str] = {}
    if user_ids:
        urows = (
            await db.execute(
                select(User.id, User.name).where(User.id.in_(user_ids))
            )
        ).all()
        user_map = {r.id: (r.name or "") for r in urows}
    contact_map: Dict[uuid.UUID, str] = {}
    if contact_ids:
        crows = (
            await db.execute(
                select(Contact.id, Contact.name).where(Contact.id.in_(contact_ids))
            )
        ).all()
        contact_map = {r.id: (r.name or "") for r in crows}
    return [
        _commitment_to_out(
            c,
            actor_user_name=user_map.get(c.actor_user_id),
            actor_contact_name=contact_map.get(c.actor_contact_id),
        )
        for c in rows
    ]


class CommitmentStatusUpdate(BaseModel):
    status: str  # done | dismissed | pending


@router.patch(
    "/commitments/{commitment_id}",
    response_model=CommitmentOut,
    dependencies=[Depends(require_scope("contacts:write"))],
)
async def update_commitment_status(
    commitment_id: uuid.UUID,
    body: CommitmentStatusUpdate,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Mark a commitment done / dismissed / re-open. ``overdue`` is computed,
    not user-set — restore goes to ``pending`` and the daily job
    re-derives overdue based on ``due_date``."""
    if body.status not in ("done", "dismissed", "pending"):
        raise HTTPException(
            status_code=400,
            detail="status must be one of: done, dismissed, pending",
        )
    c = await db.get(Commitment, commitment_id)
    if c is None or c.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Commitment not found")
    c.status = body.status
    if body.status == "done":
        c.completed_at = datetime.now(timezone.utc)
        c.completed_via = "manual"
    elif body.status == "pending":
        c.completed_at = None
        c.completed_via = None
        c.completed_evidence_interaction_id = None
    await db.commit()
    await db.refresh(c)
    user_name = None
    contact_name = None
    if c.actor_user_id:
        u = await db.get(User, c.actor_user_id)
        user_name = u.name if u else None
    if c.actor_contact_id:
        ct = await db.get(Contact, c.actor_contact_id)
        contact_name = ct.name if ct else None
    return _commitment_to_out(c, actor_user_name=user_name, actor_contact_name=contact_name)


# ── Customer behavior signals (Phase 5C) ─────────────────────────────────


class BehaviorRadarOut(BaseModel):
    commitment: float
    openness: float
    engagement: float
    trust: float
    decision_urgency: float
    friction: float


class ChangeReadinessOut(BaseModel):
    score: int
    confidence: str
    contributing: Dict[str, float]


class CustomerBehaviorSignalsOut(BaseModel):
    customer_id: uuid.UUID
    radar: BehaviorRadarOut
    change_readiness: ChangeReadinessOut
    signal_density: int
    source_interaction_count: int


@router.get(
    "/customers/{customer_id}/behavior-signals",
    response_model=CustomerBehaviorSignalsOut,
)
async def get_customer_behavior_signals(
    customer_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Aggregated Customer Behavior Radar + Change-Readiness Index.

    Pulls ``customer_signals`` blocks from every analyzed interaction
    on this customer, merges them, and runs the radar + readiness
    services. Returned shape mirrors the dataclasses with one extra
    field — ``source_interaction_count`` — so the UI can show how
    many calls' worth of signal informed the score.
    """
    from backend.app.services.customer_behavior import (
        compute_behavior_radar,
        compute_change_readiness,
        signal_density_from,
    )

    cust = (
        await db.execute(
            select(Customer).where(
                Customer.id == customer_id,
                Customer.tenant_id == tenant.id,
            )
        )
    ).scalar_one_or_none()
    if cust is None:
        raise HTTPException(status_code=404, detail="Customer not found")

    rows = (
        await db.execute(
            select(Interaction.insights).where(
                Interaction.customer_id == customer_id,
                Interaction.tenant_id == tenant.id,
                Interaction.insights.isnot(None),
            )
        )
    ).all()

    merged = {
        "commitment_language": [],
        "change_talk": [],
        "sustain_talk": [],
        "trust_signals": [],
        "urgency_language": [],
        "objections": [],
    }
    interaction_count = 0
    for (insights,) in rows:
        if not isinstance(insights, dict):
            continue
        cs = insights.get("customer_signals")
        if not isinstance(cs, dict):
            continue
        interaction_count += 1
        for key in merged.keys():
            v = cs.get(key)
            if isinstance(v, list):
                merged[key].extend(v)

    radar = compute_behavior_radar(merged)
    density = signal_density_from(merged)
    readiness = compute_change_readiness(radar, signal_density=density)

    return CustomerBehaviorSignalsOut(
        customer_id=customer_id,
        radar=BehaviorRadarOut(**radar.as_dict()),
        change_readiness=ChangeReadinessOut(**readiness.as_dict()),
        signal_density=density,
        source_interaction_count=interaction_count,
    )
