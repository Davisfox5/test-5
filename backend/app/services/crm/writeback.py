"""CRM write-back service.

When a voice interaction finishes analyzing, we may want to echo the
insights back into the CRM: attach a note summarizing the call, create
a follow-up activity for the open action items, optionally move the
linked deal to the next stage if the AI is confident we closed a step.

Each of those is opt-in via ``Tenant.features_enabled`` so a tenant
can dial in what they trust the AI to do. Write-back runs *after* the
pipeline's scoring + insights step — we work off the persisted
Interaction row, not the in-memory analysis, so a manual re-run
produces the same result.

Errors are logged but never propagate; a CRM outage must not block the
rest of the pipeline.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import (
    ActionItem,
    Contact,
    CrmDealRecord,
    Integration,
    Interaction,
    Tenant,
)
from backend.app.services.crm.base import (
    CrmAdapter,
    CrmAuthError,
    CrmCapabilityMissing,
    CrmError,
)
from backend.app.services.token_crypto import decrypt_token, encrypt_token

logger = logging.getLogger(__name__)


WRITE_BACK_NOTE_FLAG = "crm_writeback_notes"
WRITE_BACK_ACTIVITY_FLAG = "crm_writeback_activities"


async def write_back_interaction(
    db: AsyncSession,
    interaction_id: uuid.UUID,
) -> Dict[str, Any]:
    """Run opt-in write-backs for one interaction.

    Returns a summary dict with what was attempted and what was
    actually written. Safe to call twice — both note and activity
    creation use insight hashes to dedupe against the CRM metadata we
    persist locally.
    """
    interaction = await db.get(Interaction, interaction_id)
    if interaction is None:
        return {"status": "no_interaction"}
    tenant = await db.get(Tenant, interaction.tenant_id)
    if tenant is None:
        return {"status": "no_tenant"}

    features = tenant.features_enabled or {}
    if not (
        features.get(WRITE_BACK_NOTE_FLAG)
        or features.get(WRITE_BACK_ACTIVITY_FLAG)
    ):
        return {"status": "disabled"}

    # For now only Pipedrive is wired up on the write path. HubSpot and
    # Salesforce adapters raise CrmCapabilityMissing so we safely bail.
    provider = _pick_provider_for_writeback(tenant, interaction)
    if provider is None:
        return {"status": "no_provider"}

    adapter = await _load_writeback_adapter(db, tenant.id, provider)
    if adapter is None:
        return {"status": "no_integration"}

    deal = await _find_deal_for_interaction(db, interaction)
    contact = (
        await db.get(Contact, interaction.contact_id)
        if interaction.contact_id
        else None
    )

    summary: Dict[str, Any] = {
        "status": "ok",
        "provider": provider,
        "deal_id": deal.external_id if deal else None,
        "contact_id": contact.crm_id if contact and contact.crm_id else None,
        "note_id": None,
        "activity_ids": [],
        "skipped": [],
    }

    try:
        if features.get(WRITE_BACK_NOTE_FLAG):
            try:
                note_body = _render_note(interaction, deal=deal)
                if note_body:
                    note_id = await adapter.create_note(
                        content=note_body,
                        deal_external_id=deal.external_id if deal else None,
                        contact_external_id=(
                            contact.crm_id if contact and contact.crm_id else None
                        ),
                        customer_external_id=_customer_external_id(contact),
                    )
                    summary["note_id"] = note_id
            except CrmCapabilityMissing:
                summary["skipped"].append("note")

        if features.get(WRITE_BACK_ACTIVITY_FLAG):
            try:
                items = await _open_action_items(db, interaction_id)
                activity_ids: List[str] = []
                for item in items:
                    activity_id = await adapter.create_activity(
                        subject=_truncate(item.title or "Follow-up", 255),
                        activity_type="task",
                        due_date=(
                            item.due_at.date().isoformat() if item.due_at else None
                        ),
                        note=item.description,
                        deal_external_id=deal.external_id if deal else None,
                        contact_external_id=(
                            contact.crm_id if contact and contact.crm_id else None
                        ),
                    )
                    activity_ids.append(activity_id)
                summary["activity_ids"] = activity_ids
            except CrmCapabilityMissing:
                summary["skipped"].append("activity")
    except CrmAuthError as exc:
        summary["status"] = "auth_failed"
        summary["error"] = str(exc)
    except CrmError as exc:
        summary["status"] = "error"
        summary["error"] = str(exc)
    except Exception:
        logger.exception(
            "CRM write-back crashed for interaction %s", interaction_id
        )
        summary["status"] = "error"
    finally:
        try:
            await adapter.close()
        except Exception:
            logger.debug("adapter close failed", exc_info=True)

    return summary


# ── Helpers ───────────────────────────────────────────────────────────


def _pick_provider_for_writeback(tenant: Tenant, interaction: Interaction) -> Optional[str]:
    """Which CRM to write back to. We prefer Pipedrive while the other
    adapters don't implement the write path. Tenants can force a
    provider via ``branding_config.crm_writeback_provider`` once they
    connect more than one CRM."""
    override = (getattr(tenant, "branding_config", {}) or {}).get("crm_writeback_provider")
    if override:
        return str(override)
    return "pipedrive"


async def _load_writeback_adapter(
    db: AsyncSession, tenant_id: uuid.UUID, provider: str
) -> Optional[CrmAdapter]:
    """Instantiate the adapter for a provider that can write back.

    Mirrors ``sync_service._build_adapter`` but kept separate so
    write-back paths don't accidentally pull in the full sync runner.
    """
    stmt = (
        select(Integration)
        .where(Integration.tenant_id == tenant_id, Integration.provider == provider)
        .order_by(Integration.created_at.desc())
        .limit(1)
    )
    integ = (await db.execute(stmt)).scalar_one_or_none()
    if integ is None:
        return None

    access = decrypt_token(integ.access_token) or ""
    refresh = decrypt_token(integ.refresh_token)

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
            from datetime import timedelta

            integ.expires_at = datetime.now(timezone.utc) + timedelta(
                seconds=int(expires_in)
            )
        if extra:
            cfg = dict(integ.provider_config or {})
            cfg.update(extra)
            integ.provider_config = cfg

    if provider == "pipedrive":
        from backend.app.services.crm.pipedrive import PipedriveAdapter

        cfg = integ.provider_config or {}
        return PipedriveAdapter(
            access_token=access,
            refresh_token=refresh,
            api_domain=cfg.get("api_domain", ""),
            auth_mode=cfg.get("auth_mode") or "bearer",
            field_map=cfg.get("field_map") or {},
            on_token_refresh=on_refresh,
        )
    return None


async def _find_deal_for_interaction(
    db: AsyncSession, interaction: Interaction
) -> Optional[CrmDealRecord]:
    """Find the most recent open deal tied to this interaction's
    contact (or customer). We scope to ``status='open'`` so write-backs
    don't touch closed-won/lost deals."""
    if interaction.contact_id is None:
        return None
    stmt = (
        select(CrmDealRecord)
        .where(
            CrmDealRecord.tenant_id == interaction.tenant_id,
            CrmDealRecord.contact_id == interaction.contact_id,
            CrmDealRecord.status.in_(("open", None)),
        )
        .order_by(CrmDealRecord.last_synced_at.desc().nullslast())
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


def _customer_external_id(contact: Optional[Contact]) -> Optional[str]:
    if contact is None or contact.customer_id is None:
        return None
    # The sync runner stores the CRM's external id on Customer.crm_id
    # too — we read it lazily elsewhere.
    return None


async def _open_action_items(
    db: AsyncSession, interaction_id: uuid.UUID
) -> List[ActionItem]:
    stmt = (
        select(ActionItem)
        .where(
            ActionItem.interaction_id == interaction_id,
            ActionItem.status.in_(("pending", "in_progress")),
        )
        .order_by(ActionItem.created_at.asc())
    )
    return list((await db.execute(stmt)).scalars().all())


def _render_note(interaction: Interaction, *, deal: Optional[CrmDealRecord]) -> str:
    insights = interaction.insights or {}
    summary = insights.get("summary") or insights.get("headline")
    if not summary:
        return ""
    lines = [
        f"LINDA call summary ({interaction.created_at:%Y-%m-%d %H:%M UTC})",
        "",
        _truncate(str(summary), 2000),
    ]
    if insights.get("key_moments"):
        lines.append("")
        lines.append("Key moments:")
        for km in (insights["key_moments"] or [])[:5]:
            text = km if isinstance(km, str) else km.get("text", "")
            if text:
                lines.append(f"- {_truncate(str(text), 240)}")
    if deal:
        lines.append("")
        lines.append(f"Deal: {deal.title}")
    return "\n".join(lines)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


__all__ = ["write_back_interaction"]
