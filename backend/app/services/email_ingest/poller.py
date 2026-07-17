"""Per-integration poller — runs inside a Celery task.

Scans every active Integration, refreshes its token if needed, pulls
recent messages via the provider-specific fetcher, and hands each one
to :func:`ingest_email`.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from backend.app.models import EmailSyncCursor, Integration, Tenant, User
from backend.app.services.email_ingest import gmail as gmail_fetcher
from backend.app.services.email_ingest import graph as graph_fetcher
from backend.app.services.email_ingest.ingest import IngestCaches, ingest_email
from backend.app.services.email_classifier import EmailClassifier
from backend.app.services.llm_circuit_breaker import LLMCallsSuspended
from backend.app.services.token_crypto import decrypt_token, encrypt_token

logger = logging.getLogger(__name__)


class IntegrationAuthError(Exception):
    """The integration's OAuth credentials are no longer usable.

    Raised for non-retryable auth failures — a revoked/expired refresh
    token (Google ``invalid_grant`` / Microsoft ``invalid_grant``), a
    missing refresh token, or a client-config problem. Callers handle
    this by flagging the integration for re-auth and skipping it, rather
    than letting it surface as an unhandled error on every poll (which
    floods Sentry every 15 minutes for a credential a retry can't fix).
    """


def mark_needs_reauth(session: Session, integration: Integration) -> None:
    """Flag an integration so future polls skip it until it's re-authed.

    Stored in the freeform ``provider_config`` JSONB (reassigned, not
    mutated in place, so SQLAlchemy detects the change). Committed on its
    own so the flag survives even when surrounding work is rolled back.

    Public API: also used by the push (tasks.py) and backfill paths —
    any consumer of :func:`refresh_if_expired_sync` should call this on
    :class:`IntegrationAuthError` so the re-auth flag is set consistently.
    """
    try:
        integration.provider_config = {
            **(integration.provider_config or {}),
            "needs_reauth": True,
        }
        session.commit()
    except Exception:  # noqa: BLE001 — flagging is best-effort
        logger.debug(
            "Could not flag integration %s for re-auth", integration.id, exc_info=True
        )
        session.rollback()


def refresh_if_expired_sync(session: Session, integration: Integration) -> str:
    """Return a valid access token, refreshing via provider API if needed.

    Mirrors the async version in api/oauth.py but runs inside Celery's
    synchronous world.  Updates the Integration row in place; caller
    commits — and should commit *promptly*: Microsoft can rotate the
    refresh token during this call, so a later rollback that discards the
    pending token attributes leaves the DB holding a dead refresh token.

    Public API: consumed by the poller, the push path (tasks.py), and the
    backfill service. Raises :class:`IntegrationAuthError` for
    non-retryable credential failures; transient transport errors (5xx
    from the token endpoint, network failures, unexpected MSAL errors)
    bubble as their native exception types and callers must handle them.
    """
    token = decrypt_token(integration.access_token)
    now = datetime.now(timezone.utc)
    if integration.expires_at and integration.expires_at > now + timedelta(seconds=30):
        return token

    refresh = decrypt_token(integration.refresh_token)
    if not refresh:
        raise IntegrationAuthError(
            f"Integration {integration.id} expired and has no refresh token"
        )

    if integration.provider == "google":
        import requests

        from backend.app.config import get_settings

        s = get_settings()
        resp = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": s.GOOGLE_CLIENT_ID,
                "client_secret": s.GOOGLE_CLIENT_SECRET,
                "refresh_token": refresh,
                "grant_type": "refresh_token",
            },
            timeout=10,
        )
        if 400 <= resp.status_code < 500:
            # 4xx from the token endpoint is almost always invalid_grant
            # (refresh token revoked/expired) or a client-config error —
            # not retryable. Surface as an auth error rather than a raw
            # HTTPError that floods Sentry every poll. 5xx is transient,
            # so let it bubble (and retry next cycle).
            try:
                err_detail = resp.json().get("error", "")
            except Exception:  # noqa: BLE001
                err_detail = resp.text[:200]
            raise IntegrationAuthError(
                f"Google token refresh failed for integration "
                f"{integration.id}: {resp.status_code} {err_detail}"
            )
        resp.raise_for_status()
        j = resp.json()
        new_access = j["access_token"]
        new_refresh = j.get("refresh_token")
        expires_at = now + timedelta(seconds=int(j.get("expires_in", 3600)))
    else:
        import msal

        from backend.app.config import get_settings

        s = get_settings()
        app = msal.ConfidentialClientApplication(
            s.MICROSOFT_CLIENT_ID,
            authority="https://login.microsoftonline.com/common",
            client_credential=s.MICROSOFT_CLIENT_SECRET,
        )
        # NOTE: do NOT pass reserved scopes (``offline_access``, ``openid``,
        # ``profile``) here — MSAL rejects them with
        # "You cannot use any scope value that is reserved" and adds
        # offline_access itself for refresh-token grants.
        result = app.acquire_token_by_refresh_token(
            refresh,
            scopes=[
                "Mail.Send", "Mail.Read", "Calendars.ReadWrite",
                "Contacts.Read",
            ],
        )
        if "error" in result:
            err = result.get("error")
            if err in {"invalid_grant", "interaction_required", "invalid_client"}:
                raise IntegrationAuthError(
                    f"Microsoft refresh failed for integration "
                    f"{integration.id}: {err}"
                )
            raise RuntimeError(f"Microsoft refresh failed: {result['error']}")
        new_access = result["access_token"]
        new_refresh = result.get("refresh_token")
        expires_at = now + timedelta(seconds=int(result.get("expires_in", 3600)))

    integration.access_token = encrypt_token(new_access)
    if new_refresh:
        integration.refresh_token = encrypt_token(new_refresh)
    integration.expires_at = expires_at
    return new_access


# Backwards-compat aliases — these predate the helpers being promoted to
# the module's public API; new callers should use the public names.
_mark_needs_reauth = mark_needs_reauth
_refresh_if_expired_sync = refresh_if_expired_sync


def poll_integration(session: Session, integration: Integration) -> int:
    """Poll a single integration.  Returns the number of emails ingested."""
    tenant = session.query(Tenant).filter(Tenant.id == integration.tenant_id).first()
    if tenant is None:
        logger.warning("Integration %s has no tenant; skipping", integration.id)
        return 0

    # Agent email = the User attached to the integration (set on OAuth callback).
    user = (
        session.query(User).filter(User.id == integration.user_id).first()
        if integration.user_id else None
    )
    agent_email = user.email if user else None

    cursor = (
        session.query(EmailSyncCursor)
        .filter(EmailSyncCursor.integration_id == integration.id)
        .first()
    )
    if cursor is None:
        cursor = EmailSyncCursor(
            integration_id=integration.id,
            tenant_id=integration.tenant_id,
            provider=integration.provider,
        )
        session.add(cursor)
        session.flush()

    access_token = refresh_if_expired_sync(session, integration)
    # Persist a rotated token IMMEDIATELY (see refresh_if_expired_sync's
    # docstring): Microsoft can rotate the refresh token during that
    # call, and any later rollback in this cycle (ingest failure, LLM
    # breaker deferral) must not discard it — that leaves a dead refresh
    # token in the DB and bricks the integration.
    session.commit()

    if integration.provider == "google":
        stream = gmail_fetcher.fetch_recent(integration, cursor, access_token, agent_email)
    elif integration.provider == "microsoft":
        stream = graph_fetcher.fetch_recent(integration, cursor, access_token, agent_email)
    else:
        logger.info("Skipping non-email integration %s (%s)", integration.id, integration.provider)
        return 0

    # Poll windows overlap by design, so most fetched messages already
    # exist. Dedupe the whole batch with ONE query up front instead of
    # letting ingest_email run its per-message existence SELECT for every
    # already-seen message (the N+1 Sentry flags on this transaction).
    # ingest_email keeps its own check for the messages that get through
    # — it stays the idempotency backstop for the push/backfill paths.
    from backend.app.models import Interaction

    msgs = [m for m in stream]
    provider_ids = [m.provider_message_id for m in msgs if m.provider_message_id]
    already_ingested = (
        {
            pid
            for (pid,) in session.query(Interaction.provider_message_id).filter(
                Interaction.tenant_id == integration.tenant_id,
                Interaction.provider_message_id.in_(provider_ids),
            )
        }
        if provider_ids
        else set()
    )

    classifier = EmailClassifier()
    caches = IngestCaches()
    ingested = 0

    async def _run(msgs):
        nonlocal ingested
        for msg in msgs:
            if msg.provider_message_id in already_ingested:
                continue
            if await ingest_email(session, tenant, msg, classifier, caches=caches) is not None:
                ingested += 1

    asyncio.run(_run(msgs))
    session.commit()
    return ingested


def poll_all(session: Session) -> dict:
    """Poll every email-capable integration *that doesn't have push configured*.

    Real-time delivery comes from Gmail Pub/Sub + Microsoft Graph push
    webhooks. When those are configured globally (``GMAIL_PUBSUB_TOPIC`` /
    ``GRAPH_CLIENT_STATE``) we skip the corresponding provider here — the
    poll is only a safety net for environments that can't receive
    webhooks (local dev, air-gapped installs, pre-verification tenants).

    To force a full poll regardless of push config (smoke tests), pass
    ``settings.EMAIL_POLL_FORCE_ALL = True`` in the env.
    """
    from backend.app.config import get_settings
    from backend.app.tenant_ctx import tenant_context

    # Credit-balance breaker: when open, defer the WHOLE cycle up front.
    # Classification is fail-closed, so polling without LLM access would
    # ingest-then-rollback every 2 minutes (and re-enqueue analysis work
    # for rows the rollback discards). The guard doubles as the resume
    # path — one poller per probe interval issues the cheap probe, and
    # when it succeeds the breaker closes and this same cycle proceeds.
    from backend.app.services import llm_circuit_breaker as _breaker
    from backend.app.services.llm_client import get_async_anthropic

    if _breaker.is_open():
        try:
            asyncio.run(_breaker.guard(get_async_anthropic()))
        except LLMCallsSuspended:
            return {
                "integrations": 0,
                "emails_ingested": 0,
                "skipped_reason": "llm_credit_breaker_open",
            }

    settings = get_settings()
    force_all = bool(getattr(settings, "EMAIL_POLL_FORCE_ALL", False))

    providers: list[str] = []
    if force_all or not getattr(settings, "GMAIL_PUBSUB_TOPIC", ""):
        providers.append("google")
    if force_all or not getattr(settings, "GRAPH_CLIENT_STATE", ""):
        providers.append("microsoft")

    if not providers:
        return {
            "integrations": 0,
            "emails_ingested": 0,
            "skipped_reason": "push_configured_for_all_providers",
        }

    integrations = (
        session.query(Integration)
        .filter(Integration.provider.in_(providers))
        .all()
    )

    # Per-integration push gating: even when push is globally enabled
    # we may have individual integrations whose subscription is still
    # healthy — those don't need polling. We materialise the cursor
    # state into a dict so the loop below stays cheap.
    from datetime import datetime, timezone

    from backend.app.models import EmailSyncCursor

    cursors_by_integration = {
        c.integration_id: c
        for c in session.query(EmailSyncCursor)
        .filter(
            EmailSyncCursor.integration_id.in_([i.id for i in integrations])
        )
        .all()
    } if integrations else {}
    now = datetime.now(timezone.utc)

    summary = {
        "integrations": 0,
        "emails_ingested": 0,
        "polled_providers": providers,
        "skipped_healthy_push": 0,
        "skipped_needs_reauth": 0,
        "needs_reauth": 0,
    }
    for integ in integrations:
        # Skip integrations already flagged for re-auth — their refresh
        # token is dead and retrying just re-hits the token endpoint
        # (and re-floods logs) every cycle until a human reconnects.
        if (integ.provider_config or {}).get("needs_reauth"):
            summary["skipped_needs_reauth"] += 1
            continue
        cursor = cursors_by_integration.get(integ.id)
        # Skip if this integration's push subscription is still healthy.
        # ``force_all`` is the smoke-test escape hatch — when set we ignore
        # the cursor entirely and poll everything.
        if not force_all and cursor is not None:
            expires = getattr(cursor, "push_subscription_expires_at", None)
            if expires is not None and expires > now:
                summary["skipped_healthy_push"] += 1
                continue
        try:
            with tenant_context(integ.tenant_id, session):
                count = poll_integration(session, integ)
        except LLMCallsSuspended:
            # Expected state: the credit-balance breaker is open. Roll
            # back so this integration's cursor does NOT advance past
            # messages we couldn't classify — the next poll after the
            # breaker closes re-fetches the same window (ingest is
            # idempotent on provider_message_id). Quiet by design: the
            # breaker reports once per transition.
            session.rollback()
            summary["deferred_llm_paused"] = True
            logger.info(
                "email poll deferred (LLM credit breaker open); "
                "will retry next cycle"
            )
            break
        except IntegrationAuthError as exc:
            # Expected, non-retryable: log at WARNING (not ERROR, so the
            # Sentry logging integration doesn't turn it into an event)
            # and flag the integration so we stop polling it.
            logger.warning("Integration %s needs re-auth: %s", integ.id, exc)
            session.rollback()
            with tenant_context(integ.tenant_id, session):
                mark_needs_reauth(session, integ)
            summary["needs_reauth"] += 1
            continue
        except Exception:
            logger.exception("Poll failed for integration %s", integ.id)
            session.rollback()
            continue
        summary["integrations"] += 1
        summary["emails_ingested"] += count
    return summary
