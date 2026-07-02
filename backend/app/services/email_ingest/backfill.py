"""Historical mailbox import — "sync the last N days".

The poller/push pipeline only moves forward from the moment a mailbox
is connected.  A backfill job walks the trailing ``window_days`` of
mail via provider list APIs and hands every message to the same
:func:`ingest_email` path (classifier, threading, dedupe on
``provider_message_id``, analysis enqueue), so re-running a window is
safe and past outreach gets the identical treatment as new mail.

Provider strategy:

* **Gmail** — ``newer_than:Nd -in:chats -in:spam -in:trash`` (see
  ``gmail.backfill_query``), which covers received, sent AND archived
  mail.  Message ids are listed first and deduped against existing
  Interactions BEFORE the per-message ``get()`` call, so a re-run
  spends near-zero quota on the already-imported prefix.  All Gmail
  calls go through ``gmail._execute_with_backoff`` (429/5xx retry).
* **Microsoft Graph** — plain ``$filter=receivedDateTime ge <iso>``
  listing over Inbox and SentItems following ``@odata.nextLink``.
  Graph list responses already include the message body, so dedupe
  happens on the listed id before normalization (no extra fetch to
  save).  Transient 429/5xx responses are retried with backoff.

Runs inside the ``email_backfill_run`` Celery task with a synchronous
Session; progress is persisted onto the :class:`EmailBackfillJob` row
every ``_COMMIT_EVERY`` messages so the status endpoint shows live
counts.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Iterator, Optional, Tuple

import httpx
from sqlalchemy.orm import Session

from backend.app.models import (
    EmailBackfillJob,
    Integration,
    Interaction,
    Tenant,
    User,
)
from backend.app.services.email_classifier import EmailClassifier
from backend.app.services.email_ingest import gmail as gmail_fetcher
from backend.app.services.email_ingest import graph as graph_fetcher
from backend.app.services.email_ingest.graph import GRAPH_BASE
from backend.app.services.email_ingest.ingest import NormalizedEmail, ingest_email
from backend.app.services.email_ingest.poller import (
    IntegrationAuthError,
    _refresh_if_expired_sync,
)

logger = logging.getLogger(__name__)

# Hard ceiling on messages actually *imported* per job (dedupe-skipped
# ids don't count).  A 90-day window on a busy mailbox can be huge, and
# each imported message costs a provider GET + a classifier call.  Since
# skips are free, re-running a capped job is a cheap catch-up pass that
# picks up where the last run stopped.
MAX_MESSAGES_PER_JOB = 2000

# Provider page size for list calls.
_PAGE_SIZE = 100

# Persist counters every N messages so status polling shows progress.
_COMMIT_EVERY = 25

# A candidate is (provider_message_id, thunk-returning-NormalizedEmail).
# The thunk is only invoked for messages that survive dedupe, so listing
# stays cheap and the expensive fetch/normalize is skipped for dupes.
_Candidate = Tuple[str, Callable[[], NormalizedEmail]]


def _iter_gmail_candidates(
    access_token: str, agent_email: Optional[str], days: int
) -> Iterator[_Candidate]:
    service = gmail_fetcher.build_service(access_token)
    for mid in gmail_fetcher.list_backfill_ids(service, days, page_size=_PAGE_SIZE):
        yield (
            mid,
            lambda mid=mid: gmail_fetcher.get_backfill_message(
                service, mid, agent_email
            ),
        )


def _graph_get_with_backoff(
    client: httpx.Client, url: str, max_attempts: int = 5
) -> httpx.Response:
    """GET a Graph URL, retrying transient rate-limit / 5xx responses."""
    delay = 1.0
    for attempt in range(1, max_attempts + 1):
        resp = client.get(url)
        if resp.status_code not in (429, 500, 502, 503, 504) or attempt == max_attempts:
            resp.raise_for_status()
            return resp
        retry_after = resp.headers.get("Retry-After")
        try:
            wait = float(retry_after) if retry_after else delay
        except ValueError:
            wait = delay
        logger.warning(
            "Graph backfill request hit %s (attempt %s/%s); backing off %.1fs",
            resp.status_code,
            attempt,
            max_attempts,
            wait,
        )
        time.sleep(wait)
        delay = min(delay * 2, 30.0)
    raise RuntimeError("unreachable")  # pragma: no cover


def _iter_graph_candidates(
    access_token: str, agent_email: Optional[str], days: int
) -> Iterator[_Candidate]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    with httpx.Client(
        timeout=30, headers=graph_fetcher._headers(access_token)
    ) as client:
        for folder, direction in (("Inbox", "inbound"), ("SentItems", "outbound")):
            url: Optional[str] = (
                f"{GRAPH_BASE}/me/mailFolders/{folder}/messages"
                f"?$filter=receivedDateTime ge {since}"
                f"&$orderby=receivedDateTime desc&$top={_PAGE_SIZE}"
            )
            while url:
                resp = _graph_get_with_backoff(client, url)
                data = resp.json()
                for raw in data.get("value", []):
                    yield (
                        raw["id"],
                        lambda raw=raw, direction=direction: graph_fetcher._normalize(
                            raw, agent_email, direction, access_token=access_token
                        ),
                    )
                url = data.get("@odata.nextLink")


def run_backfill(session: Session, job_id: str) -> dict:
    """Execute one backfill job to completion.  Returns a summary dict.

    Owns the job row's full lifecycle (running → done/error) including
    commits — callers just hand over a session.  Auth failures flag the
    job as ``error`` with a human-readable reason instead of raising, so
    Celery doesn't retry a credential problem a retry can't fix.
    """
    job = (
        session.query(EmailBackfillJob)
        .filter(EmailBackfillJob.id == uuid.UUID(str(job_id)))
        .first()
    )
    if job is None:
        return {"status": "job_missing"}
    if job.status not in ("queued", "running"):
        return {"status": job.status, "note": "already finished"}

    integration = (
        session.query(Integration)
        .filter(Integration.id == job.integration_id)
        .first()
    )
    tenant = session.query(Tenant).filter(Tenant.id == job.tenant_id).first()

    def _fail(reason: str) -> dict:
        job.status = "error"
        job.error = reason
        job.finished_at = datetime.now(timezone.utc)
        session.commit()
        return {"status": "error", "error": reason}

    if integration is None:
        return _fail("Mailbox integration no longer exists — reconnect it and retry.")
    if tenant is None:
        return _fail("Tenant not found.")

    user = (
        session.query(User).filter(User.id == integration.user_id).first()
        if integration.user_id
        else None
    )
    agent_email = user.email if user else None

    job.status = "running"
    job.started_at = datetime.now(timezone.utc)
    session.commit()

    try:
        access_token = _refresh_if_expired_sync(session, integration)
    except IntegrationAuthError as exc:
        logger.warning("Backfill %s: integration needs re-auth: %s", job.id, exc)
        return _fail("Mailbox credentials expired — reconnect the mailbox and retry.")

    if integration.provider == "google":
        candidates: Iterable[_Candidate] = _iter_gmail_candidates(
            access_token, agent_email, job.window_days
        )
    elif integration.provider == "microsoft":
        candidates = _iter_graph_candidates(access_token, agent_email, job.window_days)
    else:
        return _fail(f"Provider {integration.provider!r} is not an email provider.")

    classifier = EmailClassifier()
    capped = False

    async def _run() -> None:
        nonlocal capped
        since_commit = 0
        imported = 0  # non-skipped messages this run — what the cap bounds
        for mid, fetch in candidates:
            job.fetched += 1
            # Dedupe on the listed id BEFORE the expensive fetch/normalize —
            # a re-run spends near-zero quota on the already-imported prefix.
            existing = (
                session.query(Interaction.id)
                .filter(
                    Interaction.tenant_id == tenant.id,
                    Interaction.provider_message_id == mid,
                )
                .first()
            )
            if existing is not None:
                job.skipped += 1
            else:
                imported += 1
                try:
                    msg = fetch()
                    if await ingest_email(session, tenant, msg, classifier) is not None:
                        job.ingested += 1
                except Exception:
                    # One malformed message must not kill a 2000-message job.
                    logger.exception(
                        "Backfill %s: ingest failed for message %s (non-fatal)",
                        job.id,
                        mid,
                    )
                    session.rollback()
            since_commit += 1
            if since_commit >= _COMMIT_EVERY:
                session.commit()
                since_commit = 0
            if imported >= MAX_MESSAGES_PER_JOB:
                capped = True
                logger.info(
                    "Backfill %s: hit %s-message cap; re-run to continue",
                    job.id,
                    MAX_MESSAGES_PER_JOB,
                )
                break
        session.commit()

    try:
        asyncio.run(_run())
    except IntegrationAuthError as exc:
        logger.warning("Backfill %s: auth expired mid-run: %s", job.id, exc)
        return _fail("Mailbox credentials expired mid-sync — reconnect and retry.")
    except Exception as exc:  # noqa: BLE001 — job row is the error channel
        logger.exception("Backfill %s failed", job.id)
        session.rollback()
        return _fail(f"Sync failed: {exc}")

    job.status = "done"
    job.finished_at = datetime.now(timezone.utc)
    # Surface a partial import without inventing a new status: the job is
    # done, but there is more history than one run imports.
    job.error = (
        f"Imported the {MAX_MESSAGES_PER_JOB}-message maximum for one sync — "
        "run the sync again to continue where it left off."
        if capped
        else None
    )
    session.commit()
    return {
        "status": "done",
        "fetched": job.fetched,
        "ingested": job.ingested,
        "skipped": job.skipped,
        "capped": capped,
    }
