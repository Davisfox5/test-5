"""Historical mailbox import — "sync the last N days".

The poller/push pipeline only moves forward from the moment a mailbox
is connected. A backfill job walks the trailing ``window_days`` of
Inbox + Sent via provider list APIs and hands every message to the
same :func:`ingest_email` path (classifier, threading, dedupe on
``provider_message_id``, analysis enqueue), so re-running a window is
safe and past outreach gets the identical treatment as new mail.

Runs inside the ``email_backfill_run`` Celery task with a synchronous
Session; progress is persisted onto the :class:`EmailBackfillJob` row
every ``_COMMIT_EVERY`` messages so the status endpoint shows live
counts, and each checkpoint stamps ``heartbeat_at`` so a redelivered
task (or the start endpoint) can tell a live run from a dead one.

Throughput: the fetchers are synchronous (googleapiclient / sync
httpx), and classification awaits an LLM call per message. Rather than
serializing every provider round trip with every classifier call, the
run loop keeps a one-message lookahead — the fetch for message N+1 runs
in a worker thread while message N is classified/ingested on the event
loop. The fetch thread gets its own read-only DB session (for the
dedupe lookups) and, on Gmail, its own service object, so the two
threads never share a Session or an HTTP client.

Duplicate handling: before fetching message bodies, each provider page
is checked against the tenant's existing ``provider_message_id``s in
one batched query. Already-imported messages are yielded as
:class:`SkippedRef` — counted, but never paid for with a full-body GET
(Gmail) or an attachment listing (Graph) — which is what makes a
re-run a genuinely cheap catch-up pass over the skipped prefix.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, List, Optional, Set, Union

import httpx
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from backend.app.models import (
    EMAIL_BACKFILL_HEARTBEAT_STALE_AFTER,
    EmailBackfillJob,
    Integration,
    Interaction,
    Tenant,
    User,
)
from backend.app.services.email_classifier import EmailClassifier
from backend.app.services.email_ingest import graph as graph_fetcher
from backend.app.services.email_ingest.graph import GRAPH_BASE

# NOTE: the gmail fetcher module is imported lazily inside
# fetch_window_gmail — it drags in googleapiclient at module level,
# which only the worker environment is guaranteed to have.
from backend.app.services.email_ingest.ingest import (
    IngestCaches,
    NormalizedEmail,
    ingest_email,
)
from backend.app.services.email_ingest.poller import (
    IntegrationAuthError,
    mark_needs_reauth,
    refresh_if_expired_sync,
)

logger = logging.getLogger(__name__)

# Hard ceiling per job — a 90-day window on a busy mailbox can be huge,
# and each message costs a provider GET + a classifier call. Anything
# beyond this is better served by re-running the job (dedupe makes that
# a fast catch-up pass over the skipped prefix).
MAX_MESSAGES_PER_JOB = 2000

# Provider page size for list calls.
_PAGE_SIZE = 100

# Persist counters every N messages so status polling shows progress.
_COMMIT_EVERY = 25

# A ``running`` job whose worker hasn't checkpointed within this window
# is treated as dead (crashed worker / lost task): a redelivered task
# may take it over, and the start endpoint may supersede it. Checkpoints
# land every ``_COMMIT_EVERY`` messages and a single message is seconds
# of work, so a live run always heartbeats well inside this. Defined on
# the models module so the API can share it without importing this
# module's provider-SDK dependency chain.
HEARTBEAT_STALE_AFTER = EMAIL_BACKFILL_HEARTBEAT_STALE_AFTER

# Batched "which of these ids do we already have?" lookup, called once
# per provider list page.
KnownIdsLookup = Callable[[List[str]], Set[str]]


@dataclass
class SkippedRef:
    """A message recognized as already ingested from its provider id
    alone — yielded instead of a :class:`NormalizedEmail` so the caller
    can count it without the fetcher paying for the message body."""

    provider_message_id: str


def _folder_cap(index: int, max_messages: int, yielded: int) -> int:
    """Per-folder message budget.

    The first folder (inbox) is capped at half the job budget so a busy
    inbox can't starve the sent folder out of the job entirely —
    outbound history is half the point of a backfill. The second folder
    gets everything the first didn't use.
    """
    if index == 0:
        return max_messages - (max_messages // 2)
    return max_messages - yielded


def fetch_window_gmail(
    access_token: str,
    agent_email: Optional[str],
    days: int,
    max_messages: int = MAX_MESSAGES_PER_JOB,
    known_of: Optional[KnownIdsLookup] = None,
) -> Iterable[Union[NormalizedEmail, SkippedRef]]:
    """Yield normalized Gmail messages from the trailing ``days`` window.

    Uses ``q=after:<epoch>`` (Gmail accepts epoch seconds) over INBOX and
    SENT with page-token pagination — unlike the poller this ignores the
    history cursor entirely, since we want the historical window, not the
    delta.

    Error posture (mirrors the Graph fetcher): a failed list call logs
    and abandons that folder, preserving the rest of the job; a failed
    per-message get logs and skips just that message. A 401 means the
    access token died mid-run and is raised as
    :class:`IntegrationAuthError` so the job can surface an actionable
    "reconnect" error instead of an opaque provider trace.
    """
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    from backend.app.services.email_ingest import gmail as gmail_fetcher

    def _status(exc: HttpError) -> Optional[int]:
        status = getattr(exc, "status_code", None)
        if status is None:
            status = getattr(getattr(exc, "resp", None), "status", None)
        try:
            return int(status) if status is not None else None
        except (TypeError, ValueError):
            return None

    service = build(
        "gmail", "v1",
        credentials=gmail_fetcher.build_credentials(access_token),
        cache_discovery=False,
    )
    # Separate service for the NormalizedEmail's lazy attachment fetcher:
    # that callback fires from the consumer thread (post-classification,
    # inside ingest_email) while this generator may be listing the next
    # page on its own thread, and googleapiclient service objects are not
    # safe to share across threads.
    attachment_service = build(
        "gmail", "v1",
        credentials=gmail_fetcher.build_credentials(access_token),
        cache_discovery=False,
    )
    after_epoch = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())

    yielded = 0
    for idx, (label, direction) in enumerate((("INBOX", "inbound"), ("SENT", "outbound"))):
        cap = _folder_cap(idx, max_messages, yielded)
        folder_yielded = 0
        page_token: Optional[str] = None
        while folder_yielded < cap:
            try:
                resp = (
                    service.users()
                    .messages()
                    .list(
                        userId="me",
                        labelIds=[label],
                        q=f"after:{after_epoch}",
                        maxResults=min(_PAGE_SIZE, cap - folder_yielded),
                        pageToken=page_token,
                    )
                    .execute()
                )
            except HttpError as exc:
                if _status(exc) == 401:
                    raise IntegrationAuthError(
                        f"Gmail returned 401 mid-backfill (label={label})"
                    ) from exc
                logger.exception(
                    "Gmail backfill list failed label=%s (abandoning folder)", label
                )
                break
            except Exception:  # noqa: BLE001 — transport errors degrade, not abort
                logger.exception(
                    "Gmail backfill list failed label=%s (abandoning folder)", label
                )
                break

            ids = [m["id"] for m in resp.get("messages", [])]
            known = known_of(ids) if (known_of and ids) else set()
            for mid in ids:
                if folder_yielded >= cap:
                    break
                folder_yielded += 1
                yielded += 1
                if mid in known:
                    # Already on file — count it without buying the body.
                    yield SkippedRef(mid)
                    continue
                try:
                    raw = (
                        service.users()
                        .messages()
                        .get(userId="me", id=mid, format="full")
                        .execute()
                    )
                except HttpError as exc:
                    if _status(exc) == 401:
                        raise IntegrationAuthError(
                            f"Gmail returned 401 mid-backfill (message={mid})"
                        ) from exc
                    logger.exception(
                        "Gmail backfill get failed message=%s (skipping)", mid
                    )
                    continue
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "Gmail backfill get failed message=%s (skipping)", mid
                    )
                    continue
                yield gmail_fetcher.normalize_message(
                    raw, agent_email, direction, service=attachment_service
                )
            page_token = resp.get("nextPageToken")
            if not page_token:
                break


def fetch_window_graph(
    access_token: str,
    agent_email: Optional[str],
    days: int,
    max_messages: int = MAX_MESSAGES_PER_JOB,
    known_of: Optional[KnownIdsLookup] = None,
) -> Iterable[Union[NormalizedEmail, SkippedRef]]:
    """Yield normalized Graph messages from the trailing ``days`` window.

    Plain ``$filter=receivedDateTime ge <iso>`` listing over Inbox and
    SentItems, following ``@odata.nextLink`` — no delta cursor involved.
    Graph list responses already carry full bodies, so the dedupe skip
    here mostly saves the per-message attachment listing and the
    classifier hand-off. A 401 raises :class:`IntegrationAuthError`
    (token died mid-run); other HTTP errors abandon the folder.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    yielded = 0
    with httpx.Client(
        timeout=30, headers=graph_fetcher.auth_headers(access_token)
    ) as client:
        for idx, (folder, direction) in enumerate(
            (("Inbox", "inbound"), ("SentItems", "outbound"))
        ):
            cap = _folder_cap(idx, max_messages, yielded)
            folder_yielded = 0
            url: Optional[str] = (
                f"{GRAPH_BASE}/me/mailFolders/{folder}/messages"
                f"?$filter=receivedDateTime ge {since}"
                f"&$orderby=receivedDateTime desc&$top={_PAGE_SIZE}"
            )
            while url and folder_yielded < cap:
                try:
                    resp = client.get(url)
                    resp.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 401:
                        raise IntegrationAuthError(
                            f"Graph returned 401 mid-backfill (folder={folder})"
                        ) from exc
                    logger.exception(
                        "Graph backfill fetch failed folder=%s url=%s", folder, url
                    )
                    break
                except httpx.HTTPError:
                    logger.exception(
                        "Graph backfill fetch failed folder=%s url=%s", folder, url
                    )
                    break
                data = resp.json()
                items = data.get("value", [])
                ids = [raw.get("id", "") for raw in items if raw.get("id")]
                known = known_of(ids) if (known_of and ids) else set()
                for raw in items:
                    if folder_yielded >= cap:
                        break
                    folder_yielded += 1
                    yielded += 1
                    mid = raw.get("id", "")
                    if mid and mid in known:
                        yield SkippedRef(mid)
                        continue
                    yield graph_fetcher.normalize_message(
                        raw, agent_email, direction, access_token=access_token
                    )
                url = data.get("@odata.nextLink")


def _claim_job(session: Session, job: EmailBackfillJob) -> bool:
    """Atomically take ownership of the job row. Returns False if a live
    worker already owns it.

    Flips ``queued → running``, or takes over a ``running`` job whose
    heartbeat has gone stale (crashed worker being resumed via broker
    redelivery). A duplicate delivery while the original worker is still
    heartbeating — e.g. a Redis visibility-timeout replay on a job that
    legitimately runs longer than the timeout — sees a fresh heartbeat
    and bows out instead of double-sweeping the mailbox.
    """
    from sqlalchemy import func as sa_func

    now = datetime.now(timezone.utc)
    stale_before = now - HEARTBEAT_STALE_AFTER
    claimed = (
        session.query(EmailBackfillJob)
        .filter(
            EmailBackfillJob.id == job.id,
            or_(
                EmailBackfillJob.status == "queued",
                and_(
                    EmailBackfillJob.status == "running",
                    or_(
                        EmailBackfillJob.heartbeat_at.is_(None),
                        EmailBackfillJob.heartbeat_at < stale_before,
                    ),
                ),
            ),
        )
        .update(
            {
                "status": "running",
                "started_at": sa_func.coalesce(EmailBackfillJob.started_at, now),
                "heartbeat_at": now,
            },
            synchronize_session=False,
        )
    )
    session.commit()
    session.refresh(job)
    return bool(claimed)


def run_backfill(session: Session, job_id: str) -> dict:
    """Execute one backfill job to completion. Returns a summary dict.

    Owns the job row's full lifecycle (claim → running → done/error)
    including commits — callers just hand over a session. Every failure
    path lands the row in a terminal state with a human-readable reason
    (and flags the integration for re-auth when the failure is a
    credential problem), so the start endpoint's in-flight guard can
    never wedge on a phantom ``running`` job.
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

    if not _claim_job(session, job):
        logger.info(
            "Backfill %s is already owned by a live worker; ignoring duplicate delivery",
            job.id,
        )
        return {"status": "already_running", "note": "live heartbeat — duplicate delivery"}

    integration = (
        session.query(Integration)
        .filter(Integration.id == job.integration_id)
        .first()
    )
    tenant = (
        session.query(Tenant).filter(Tenant.id == job.tenant_id).first()
    )

    def _fail(reason: str) -> dict:
        # Roll back any half-done transaction state first so the error
        # write itself can't fail (or drag unrelated pending changes in).
        session.rollback()
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

    if integration.provider not in ("google", "microsoft"):
        return _fail(f"Provider {integration.provider!r} is not an email provider.")

    try:
        access_token = refresh_if_expired_sync(session, integration)
        # Persist immediately: Microsoft can rotate the refresh token
        # during the call above, and any later rollback would silently
        # discard the rotated token — leaving the DB with a dead one and
        # the mailbox connection permanently broken.
        session.commit()
    except IntegrationAuthError as exc:
        logger.warning("Backfill %s: integration needs re-auth: %s", job.id, exc)
        session.rollback()
        mark_needs_reauth(session, integration)
        return _fail("Mailbox credentials expired — reconnect the mailbox and retry.")
    except Exception:  # noqa: BLE001 — transient token-endpoint/transport failure
        # refresh_if_expired_sync deliberately lets 5xx/network errors
        # bubble as their native types; if this escaped, the job would be
        # stuck 'running' forever and the in-flight guard would lock the
        # tenant out of backfill entirely.
        logger.exception("Backfill %s: token refresh failed (transient)", job.id)
        return _fail(
            "Could not reach the mail provider to refresh credentials — try again shortly."
        )

    # The fetch generator runs on a lookahead thread (see _run below), so
    # its dedupe lookups get their own session — SQLAlchemy Sessions are
    # not thread-safe, and the main session is busy ingesting. Reads only
    # committed rows; a message ingested-but-not-yet-checkpointed slips
    # past this probe and is caught by ingest_email's in-transaction guard.
    fetch_session = Session(bind=session.get_bind())

    def _known_of(ids: List[str]) -> Set[str]:
        """One batched dedupe probe per provider list page (≤100 ids)."""
        if not ids:
            return set()
        try:
            rows = (
                fetch_session.query(Interaction.provider_message_id)
                .filter(
                    Interaction.tenant_id == tenant.id,
                    Interaction.provider_message_id.in_(ids),
                )
                .all()
            )
            return {r[0] for r in rows}
        finally:
            # Release the read transaction between pages — this session
            # lives for the whole job and must not sit idle-in-transaction.
            fetch_session.rollback()

    if integration.provider == "google":
        stream = fetch_window_gmail(
            access_token, agent_email, job.window_days, known_of=_known_of
        )
    elif integration.provider == "microsoft":
        stream = fetch_window_graph(
            access_token, agent_email, job.window_days, known_of=_known_of
        )
    else:
        return _fail(f"Provider {integration.provider!r} is not an email provider.")

    classifier = EmailClassifier()

    async def _run() -> None:
        # Local counters are the source of truth; they're *assigned* (not
        # incremented) onto the row at each checkpoint, so a rollback from
        # a failed message can never silently reset progress accounting.
        # Initialized from the row so a takeover of a crashed run resumes
        # its counts instead of restarting them.
        fetched = job.fetched
        ingested = job.ingested
        skipped = job.skipped
        since_commit = 0
        caches = IngestCaches()

        def _checkpoint() -> None:
            job.fetched = fetched
            job.ingested = ingested
            job.skipped = skipped
            job.heartbeat_at = datetime.now(timezone.utc)
            session.commit()

        # One-message lookahead: the provider fetch for message N+1 runs
        # on a worker thread while message N is classified/ingested here.
        # Generator exceptions (including the fetchers' 401 →
        # IntegrationAuthError translation) surface through the awaited
        # future exactly as they would from a plain `for` loop.
        sentinel = object()
        it = iter(stream)
        with ThreadPoolExecutor(max_workers=1) as fetch_pool:
            next_item = fetch_pool.submit(next, it, sentinel)
            while True:
                item = await asyncio.wrap_future(next_item)
                if item is sentinel:
                    break
                next_item = fetch_pool.submit(next, it, sentinel)
                fetched += 1
                if isinstance(item, SkippedRef):
                    skipped += 1
                else:
                    try:
                        # Savepoint: one malformed message must not kill a
                        # 2000-message job — and, just as important, its
                        # rollback must not drag down the flushed-but-not-
                        # yet-committed rows of the earlier messages in
                        # this checkpoint batch.
                        with session.begin_nested():
                            result = await ingest_email(
                                session, tenant, item, classifier, caches=caches
                            )
                        if result is not None:
                            ingested += 1
                    except Exception:
                        logger.exception(
                            "Backfill %s: ingest failed for message %s (non-fatal)",
                            job.id, item.provider_message_id,
                        )
                        # Objects created inside the rolled-back savepoint
                        # are gone from the DB; drop them from the caches so
                        # later messages don't reference phantom rows.
                        caches.clear()
                since_commit += 1
                if since_commit >= _COMMIT_EVERY:
                    _checkpoint()
                    since_commit = 0
        _checkpoint()

    try:
        asyncio.run(_run())
    except IntegrationAuthError as exc:
        # Reachable via the fetchers' 401 translation — the access token
        # died mid-run (long job outliving the ~1h token TTL, or a
        # mid-run revocation).
        logger.warning("Backfill %s: auth expired mid-run: %s", job.id, exc)
        session.rollback()
        mark_needs_reauth(session, integration)
        return _fail("Mailbox credentials expired mid-sync — reconnect and retry.")
    except Exception as exc:  # noqa: BLE001 — job row is the error channel
        logger.exception("Backfill %s failed", job.id)
        return _fail(f"Sync failed: {exc}")
    finally:
        fetch_session.close()

    job.status = "done"
    job.finished_at = datetime.now(timezone.utc)
    session.commit()
    return {
        "status": "done",
        "fetched": job.fetched,
        "ingested": job.ingested,
        "skipped": job.skipped,
    }
