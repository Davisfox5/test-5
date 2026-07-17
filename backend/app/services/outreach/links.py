"""Per-recipient click tracking for outreach sends (``out_002``).

Send side — when ``config.track_clicks`` is on, the scheduler passes
:func:`build_link_rewriter`'s closure into ``render_email_html``. It
swaps each body link's destination for an opaque ``{base}/t/{token}``
redirect (one token per distinct URL per send) and collects the
(token, original_url) pairs. Nothing is persisted until the provider
accepts the send: :func:`persist_links` then writes the
``outreach_links`` rows keyed to that touch's ``campaign_recipients``
row, in the same transaction as the send bookkeeping — so a failed
send leaves no orphan tokens, and a click can never arrive before its
row is committed. The text/plain part keeps the original URLs.

Click side — the public ``GET /t/{token}`` endpoint
(``backend.app.api.outreach_links``) resolves the token and calls
:func:`record_click`: bind the tenant (the lookup itself ran pre-tenant
via the AUTH_BOOTSTRAP_TABLES read policy), insert a ``click``
CampaignEvent, emit ``outreach.link_clicked``. Every hit is recorded;
likely scanner prefetches (bot user-agents, hits within seconds of
delivery) are flagged in metadata rather than dropped, so analytics
can dedupe down to first human clicks (see CampaignRollup.unique_clicks).
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from backend.app.config import get_settings
from backend.app.models import (
    Campaign,
    CampaignEvent,
    CampaignRecipient,
    Customer,
    OutreachLink,
    OutreachMember,
)
from backend.app.services.webhook_dispatcher import emit_event
from backend.app.tenant_ctx import bind_tenant_async

logger = logging.getLogger(__name__)

# UA fragments that mark a hit as a security scanner / prefetcher rather
# than a human. Case-insensitive substring match; deliberately broad —
# a false "suspected_bot" only demotes the hit in unique-click analytics,
# the event itself is always recorded.
_BOT_UA_MARKERS = (
    "bot",
    "spider",
    "crawl",
    "curl",
    "wget",
    "python-requests",
    "httpclient",
    "headless",
    "phantomjs",
    "slurp",
    "validator",
    "monitor",
    "preview",
    "scanner",
    "proofpoint",
    "mimecast",
    "barracuda",
    "urldefense",
    "safelinks",
    "trendmicro",
    "symantec",
    "sophos",
    "fireeye",
)

# A click this soon after the send is almost certainly a mail-gateway
# link scanner following every URL on delivery, not a human reading.
BOT_CLICK_WINDOW_SECONDS = 10


def new_click_token() -> str:
    """Opaque, unguessable redirect token (32 url-safe chars)."""
    return secrets.token_urlsafe(24)


def tracking_base_url() -> Optional[str]:
    """Public https base the rewritten links point at, or None if the
    deployment hasn't configured one (in which case sends keep their
    original links — a tracked link that can't resolve is worse than an
    untracked one)."""
    s = get_settings()
    base = (s.OUTREACH_TRACKING_BASE_URL or s.PUBLIC_WEBHOOK_BASE_URL or "").rstrip("/")
    return base or None


def fallback_redirect_url() -> str:
    """Where /t/{token} bounces when the token is unknown — never an
    error page, and never a destination taken from the request."""
    return get_settings().OUTREACH_CLICK_FALLBACK_URL


def build_link_rewriter(
    collected: List[Tuple[str, str]],
) -> Optional[Callable[[str], Optional[str]]]:
    """Rewriter closure for one send, or None when no base URL is
    configured. Appends (token, original_url) pairs to ``collected`` —
    the caller persists them only after the provider accepts the send."""
    base = tracking_base_url()
    if base is None:
        logger.warning(
            "track_clicks is on but neither OUTREACH_TRACKING_BASE_URL nor "
            "PUBLIC_WEBHOOK_BASE_URL is set — sending original links"
        )
        return None
    tokens: Dict[str, str] = {}

    def _rewrite(url: str) -> str:
        token = tokens.get(url)
        if token is None:
            token = new_click_token()
            tokens[url] = token
            collected.append((token, url))
        return "{0}/t/{1}".format(base, token)

    return _rewrite


def persist_links(
    session: Session,
    links: List[Tuple[str, str]],
    *,
    tenant_id,
    campaign_id,
    member_id,
    recipient_id,
) -> None:
    """Write the collected (token, original_url) pairs for one delivered
    touch. Rides the caller's transaction — commits with the send."""
    for token, url in links:
        session.add(
            OutreachLink(
                token=token,
                tenant_id=tenant_id,
                campaign_id=campaign_id,
                member_id=member_id,
                recipient_id=recipient_id,
                original_url=url,
            )
        )


def _hash_ip(ip: Optional[str]) -> Optional[str]:
    """Keyed one-way hash — enough to correlate repeat hits without
    storing the prospect's IP itself."""
    if not ip:
        return None
    secret = get_settings().SESSION_JWT_SECRET or ""
    return hashlib.sha256(
        "{0}|{1}".format(secret, ip).encode("utf-8")
    ).hexdigest()[:32]


def _bot_signals(
    user_agent: str, sent_at: Optional[datetime], now: datetime
) -> List[str]:
    signals: List[str] = []
    if not user_agent:
        signals.append("empty_user_agent")
    else:
        lowered = user_agent.lower()
        if any(marker in lowered for marker in _BOT_UA_MARKERS):
            signals.append("bot_user_agent")
    if sent_at is not None:
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=timezone.utc)
        if (now - sent_at).total_seconds() <= BOT_CLICK_WINDOW_SECONDS:
            signals.append("immediate_after_delivery")
    return signals


async def record_click(
    db: AsyncSession,
    link: OutreachLink,
    *,
    user_agent: Optional[str],
    client_ip: Optional[str],
) -> None:
    """One tracked-link hit: bind the tenant, insert the ``click``
    CampaignEvent, emit ``outreach.link_clicked``, commit.

    Every hit is recorded — repeat clicks included. Suspected scanner
    prefetches are flagged (``suspected_bot`` + ``bot_signals`` in the
    event metadata), never dropped: unique-click analytics filters on
    the flag, and a misjudged human click still 302s normally.
    """
    # The token lookup ran with no tenant bound (bootstrap read policy);
    # bind the tenant on the already-open transaction for the writes below.
    await bind_tenant_async(db, link.tenant_id)

    now = datetime.now(timezone.utc)
    recipient = (
        await db.get(CampaignRecipient, link.recipient_id)
        if link.recipient_id
        else None
    )
    ua = (user_agent or "").strip()
    signals = _bot_signals(ua, recipient.sent_at if recipient else None, now)

    metadata = {
        "url": link.original_url,
        "user_agent": ua[:300] or None,
        "ip_hash": _hash_ip(client_ip),
        "suspected_bot": bool(signals),
    }
    if signals:
        metadata["bot_signals"] = signals

    db.add(
        CampaignEvent(
            campaign_id=link.campaign_id,
            tenant_id=link.tenant_id,
            recipient_id=link.recipient_id,
            contact_id=recipient.contact_id if recipient else None,
            event_type="click",
            occurred_at=now,
            metadata_=metadata,
        )
    )

    campaign = await db.get(Campaign, link.campaign_id)
    member = (
        await db.get(OutreachMember, link.member_id) if link.member_id else None
    )
    customer = (
        await db.get(Customer, member.customer_id) if member is not None else None
    )
    await emit_event(
        db,
        link.tenant_id,
        "outreach.link_clicked",
        {
            "prospect_id": str(customer.id) if customer else None,
            "prospect_name": customer.name if customer else None,
            "campaign_id": str(link.campaign_id),
            "campaign_name": campaign.name if campaign else None,
            "member_id": str(link.member_id) if link.member_id else None,
            "recipient_id": str(link.recipient_id) if link.recipient_id else None,
            "url": link.original_url,
            "suspected_bot": bool(signals),
            "occurred_at": now.isoformat(),
        },
    )
    await db.commit()
