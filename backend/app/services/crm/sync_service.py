"""Orchestrates a CRM sync run for one tenant + provider.

Responsibilities:

1. Resolve the tenant's ``Integration`` row and build the right adapter.
2. Iterate customers → upsert ``Customer`` rows by ``(tenant_id, crm_id,
   crm_source)``.
3. Iterate contacts → upsert ``Contact`` rows, linking to the customer we
   just wrote (by external_id).
4. For each new customer we inserted, schedule a ``CustomerBriefBuilder``
   rebuild so LINDA has a day-one dossier.
5. Record a ``CrmSyncLog`` row with counts, duration, and any errors.

Idempotent: re-running on the same data produces no duplicates.

Safety: errors on a single row don't abort the whole sync — we log + skip
and mark the CrmSyncLog status ``partial`` if any skip fires.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import (
    Contact,
    CrmDealRecord,
    CrmSyncLog,
    Customer,
    Integration,
)
from backend.app.services.crm.base import (
    CrmAdapter,
    CrmAuthError,
    CrmCapabilityMissing,
    CrmDeal,
    CrmError,
)
from backend.app.services.kb.context_dispatch import schedule_customer_brief_rebuild
from backend.app.services.token_crypto import decrypt_token, encrypt_token

logger = logging.getLogger(__name__)


SUPPORTED_PROVIDERS = {"hubspot", "salesforce", "pipedrive"}


@dataclass
class SyncSummary:
    provider: str
    status: str
    customers_upserted: int
    contacts_upserted: int
    briefs_rebuilt: int
    error: Optional[str] = None


async def sync_crm_for_tenant(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    provider: str,
    *,
    rebuild_briefs_for_new_customers: bool = True,
) -> SyncSummary:
    """Run a full sync for ``(tenant_id, provider)``. Returns the summary and
    writes a CrmSyncLog row."""

    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(f"Unsupported CRM provider: {provider}")

    log = CrmSyncLog(
        tenant_id=tenant_id,
        provider=provider,
        status="running",
        # Explicit zeros — SQLAlchemy column defaults don't fire on the in-
        # memory object until flush, and our increment loop assumes they're
        # already numbers.
        customers_upserted=0,
        contacts_upserted=0,
        briefs_rebuilt=0,
    )
    db.add(log)
    await db.flush()

    try:
        adapter = await _build_adapter(db, tenant_id, provider)
    except CrmAuthError as exc:
        log.status = "failed"
        log.error = str(exc)
        log.finished_at = datetime.now(timezone.utc)
        return SyncSummary(provider, "failed", 0, 0, 0, error=str(exc))
    except NotImplementedError as exc:
        log.status = "failed"
        log.error = str(exc)
        log.finished_at = datetime.now(timezone.utc)
        return SyncSummary(provider, "failed", 0, 0, 0, error=str(exc))

    try:
        ext_id_to_internal: Dict[str, uuid.UUID] = {}
        new_customer_ids: list[uuid.UUID] = []
        partial = False

        # ── 1. Customers ───────────────────────────────────────────
        async for cust in adapter.iter_customers():
            try:
                internal_id, created = await _upsert_customer(
                    db, tenant_id, provider, cust
                )
                ext_id_to_internal[cust.external_id] = internal_id
                if created:
                    new_customer_ids.append(internal_id)
                log.customers_upserted += 1
            except Exception:
                logger.exception(
                    "Failed to upsert customer %s for tenant %s",
                    cust.external_id,
                    tenant_id,
                )
                partial = True

        # ── 2. Contacts ────────────────────────────────────────────
        contact_ext_to_internal: Dict[str, uuid.UUID] = {}
        async for ct in adapter.iter_contacts():
            try:
                internal_contact_id = await _upsert_contact(
                    db, tenant_id, provider, ct, ext_id_to_internal
                )
                if internal_contact_id is not None:
                    contact_ext_to_internal[ct.external_id] = internal_contact_id
                log.contacts_upserted += 1
            except Exception:
                logger.exception(
                    "Failed to upsert contact %s for tenant %s",
                    ct.external_id,
                    tenant_id,
                )
                partial = True

        # ── 2b. Deals (providers that support it) ─────────────────
        try:
            async for deal in adapter.iter_deals():
                try:
                    await _upsert_deal(
                        db,
                        tenant_id,
                        provider,
                        deal,
                        ext_id_to_internal,
                        contact_ext_to_internal,
                    )
                    log.deals_upserted += 1
                except Exception:
                    logger.exception(
                        "Failed to upsert deal %s for tenant %s",
                        deal.external_id,
                        tenant_id,
                    )
                    partial = True
        except CrmCapabilityMissing:
            logger.info(
                "%s does not support deal pull; skipping deals pass", provider
            )

        # ── 3. Schedule brief rebuilds for net-new customers ───────
        if rebuild_briefs_for_new_customers:
            for cid in new_customer_ids:
                try:
                    await schedule_customer_brief_rebuild(tenant_id, cid)
                    log.briefs_rebuilt += 1
                except Exception:
                    logger.exception(
                        "Failed to schedule brief rebuild for %s", cid
                    )

        log.status = "partial" if partial else "success"
    except (CrmAuthError, CrmError) as exc:
        logger.exception("CRM sync for %s/%s failed", tenant_id, provider)
        log.status = "failed"
        log.error = str(exc)
    except Exception as exc:  # pragma: no cover — defensive
        logger.exception(
            "Unexpected error during CRM sync for %s/%s", tenant_id, provider
        )
        log.status = "failed"
        log.error = f"unexpected: {exc}"
    finally:
        try:
            await adapter.close()
        except Exception:
            logger.debug("adapter.close failed", exc_info=True)
        log.finished_at = datetime.now(timezone.utc)

    try:
        from backend.app.services.metrics import CRM_SYNC_OUTCOMES

        CRM_SYNC_OUTCOMES.labels(provider=provider, status=log.status).inc()
    except Exception:
        logger.debug("sync metric emission failed", exc_info=True)

    return SyncSummary(
        provider=provider,
        status=log.status,
        customers_upserted=log.customers_upserted,
        contacts_upserted=log.contacts_upserted,
        briefs_rebuilt=log.briefs_rebuilt,
        error=log.error,
    )


# ── Helpers ────────────────────────────────────────────────────────────


async def _build_adapter(
    db: AsyncSession, tenant_id: uuid.UUID, provider: str
) -> CrmAdapter:
    """Load the Integration row and construct the provider's adapter.

    Tokens live on ``Integration.access_token`` / ``refresh_token``. We
    currently store them as plaintext (the schema comment says AES-256 but
    the encryption layer isn't wired up yet); any future decryption should
    happen here before we hand tokens to the adapter.
    """
    stmt = (
        select(Integration)
        .where(
            Integration.tenant_id == tenant_id,
            Integration.provider == provider,
        )
        .order_by(Integration.created_at.desc())
        .limit(1)
    )
    integ = (await db.execute(stmt)).scalar_one_or_none()
    if integ is None:
        raise CrmAuthError(f"No {provider} integration for tenant {tenant_id}")

    # Decrypt tokens before handing them to the adapter. ``decrypt_token`` is
    # tolerant of legacy plaintext rows — they pass through with a warning,
    # and the next refresh will rewrite in the encrypted form.
    access_token_plain = decrypt_token(integ.access_token) or ""
    refresh_token_plain = decrypt_token(integ.refresh_token)

    # Token-refresh callback: persist the new tokens + expiry back to the
    # Integration row so the next sync doesn't start stale. Values are
    # encrypted again before they hit the database.
    async def on_refresh(
        access_token: str,
        refresh_token: Optional[str],
        expires_in: Optional[int],
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        integ.access_token = encrypt_token(access_token)
        if refresh_token:
            integ.refresh_token = encrypt_token(refresh_token)
        if expires_in:
            from datetime import timedelta as _td

            integ.expires_at = datetime.now(timezone.utc) + _td(seconds=int(expires_in))
        if extra:
            cfg = dict(integ.provider_config or {})
            cfg.update(extra)
            integ.provider_config = cfg

    if provider == "hubspot":
        from backend.app.services.crm.hubspot import HubSpotAdapter

        cfg = integ.provider_config or {}
        return HubSpotAdapter(
            access_token=access_token_plain,
            refresh_token=refresh_token_plain,
            field_map=cfg.get("field_map") or {},
            on_token_refresh=on_refresh,
        )
    if provider == "salesforce":
        from backend.app.services.crm.salesforce import SalesforceAdapter

        cfg = integ.provider_config or {}
        return SalesforceAdapter(
            access_token=access_token_plain,
            instance_url=cfg.get("instance_url", ""),
            refresh_token=refresh_token_plain,
            field_map=cfg.get("field_map") or {},
            on_token_refresh=on_refresh,
        )
    if provider == "pipedrive":
        from backend.app.services.crm.pipedrive import PipedriveAdapter

        cfg = integ.provider_config or {}
        return PipedriveAdapter(
            access_token=access_token_plain,
            refresh_token=refresh_token_plain,
            api_domain=cfg.get("api_domain", ""),
            auth_mode=cfg.get("auth_mode") or "bearer",
            field_map=cfg.get("field_map") or {},
            on_token_refresh=on_refresh,
        )
    raise ValueError(f"Unhandled provider: {provider}")


async def _upsert_customer(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    provider: str,
    cust,
) -> tuple[uuid.UUID, bool]:
    """Upsert a customer by (tenant_id, crm_id, crm_source). Returns the
    internal id and whether this was a net-new insert."""
    existing = (
        await db.execute(
            select(Customer).where(
                Customer.tenant_id == tenant_id,
                Customer.crm_id == cust.external_id,
            )
        )
    ).scalar_one_or_none()

    if existing is None:
        row = Customer(
            tenant_id=tenant_id,
            name=cust.name,
            domain=cust.domain,
            crm_id=cust.external_id,
            industry=cust.industry,
            metadata_={**(cust.metadata or {}), "crm_source": provider},
        )
        db.add(row)
        await db.flush()
        return row.id, True

    # Merge: refresh scalar fields if the CRM has more data than we do.
    existing.name = cust.name or existing.name
    existing.domain = cust.domain or existing.domain
    existing.industry = cust.industry or existing.industry
    merged_meta = dict(existing.metadata_ or {})
    merged_meta.update(cust.metadata or {})
    merged_meta["crm_source"] = provider
    existing.metadata_ = merged_meta
    return existing.id, False


async def _upsert_contact(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    provider: str,
    ct,
    ext_to_internal: Dict[str, uuid.UUID],
) -> Optional[uuid.UUID]:
    """Upsert a contact by (tenant_id, crm_id, crm_source). Returns the
    internal Contact.id so callers can resolve deal↔contact links
    against the contacts we just touched."""
    existing = (
        await db.execute(
            select(Contact).where(
                Contact.tenant_id == tenant_id,
                Contact.crm_id == ct.external_id,
                Contact.crm_source == provider,
            )
        )
    ).scalar_one_or_none()

    customer_id = (
        ext_to_internal.get(ct.customer_external_id) if ct.customer_external_id else None
    )

    if existing is None:
        new_contact = Contact(
            tenant_id=tenant_id,
            name=ct.name,
            email=ct.email,
            phone=ct.phone,
            customer_id=customer_id,
            crm_id=ct.external_id,
            crm_source=provider,
            metadata_=ct.metadata or {},
        )
        db.add(new_contact)
        await db.flush()
        return new_contact.id

    existing.name = ct.name or existing.name
    existing.email = ct.email or existing.email
    existing.phone = ct.phone or existing.phone
    if customer_id and existing.customer_id != customer_id:
        existing.customer_id = customer_id
    merged_meta = dict(existing.metadata_ or {})
    merged_meta.update(ct.metadata or {})
    existing.metadata_ = merged_meta
    return existing.id


async def _upsert_deal(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    provider: str,
    deal: CrmDeal,
    customer_ext_to_internal: Dict[str, uuid.UUID],
    contact_ext_to_internal: Dict[str, uuid.UUID],
) -> None:
    """Upsert a deal by (tenant_id, provider, external_id). Resolves
    customer/contact references against the ids populated during the
    first two passes of this sync."""
    existing = (
        await db.execute(
            select(CrmDealRecord).where(
                CrmDealRecord.tenant_id == tenant_id,
                CrmDealRecord.provider == provider,
                CrmDealRecord.external_id == deal.external_id,
            )
        )
    ).scalar_one_or_none()

    customer_id = (
        customer_ext_to_internal.get(deal.customer_external_id)
        if deal.customer_external_id
        else None
    )
    contact_id = (
        contact_ext_to_internal.get(deal.contact_external_id)
        if deal.contact_external_id
        else None
    )

    if existing is None:
        db.add(
            CrmDealRecord(
                tenant_id=tenant_id,
                provider=provider,
                external_id=deal.external_id,
                title=deal.title,
                stage=deal.stage,
                status=deal.status,
                amount=deal.amount,
                currency=deal.currency,
                probability=deal.probability,
                close_date=deal.close_date,
                customer_id=customer_id,
                contact_id=contact_id,
                owner_name=deal.owner_name,
                metadata_json=deal.metadata or {},
                last_synced_at=datetime.now(timezone.utc),
            )
        )
        return

    existing.title = deal.title or existing.title
    existing.stage = deal.stage or existing.stage
    existing.status = deal.status or existing.status
    existing.amount = deal.amount if deal.amount is not None else existing.amount
    existing.currency = deal.currency or existing.currency
    existing.probability = (
        deal.probability if deal.probability is not None else existing.probability
    )
    existing.close_date = deal.close_date or existing.close_date
    if customer_id:
        existing.customer_id = customer_id
    if contact_id:
        existing.contact_id = contact_id
    existing.owner_name = deal.owner_name or existing.owner_name
    merged_meta = dict(existing.metadata_json or {})
    merged_meta.update(deal.metadata or {})
    existing.metadata_json = merged_meta
    existing.last_synced_at = datetime.now(timezone.utc)
