"""Durable per-step run ledger — the exactly-once backbone of the pipeline.

Design: docs/complexity/01-pipeline-exactly-once.md §7. Each paid or
non-idempotent pipeline step calls :func:`claim_step` before doing work.
The claim is atomic:

* No row for (interaction, step, input_hash) → INSERT + commit. A
  concurrent duplicate loses on the unique constraint (IntegrityError)
  and re-reads the winner's row.
* Row ``succeeded`` → ``REUSED``: the output is already persisted; the
  caller must skip the paid call and use the stored result.
* Row ``running`` with a live lease → ``HELD``: another worker is on it;
  the caller should defer (Celery retry) rather than proceed blind.
* Row ``failed``, or ``running`` with an expired lease → takeover via a
  compare-and-set UPDATE checked by rowcount, bumping ``attempt``.

The claim commits the session it is given (the claim must be durable and
visible to other workers *before* the money is spent). Callers therefore
invoke it at a point where the session holds no unrelated uncommitted
writes — in the pipeline that is immediately after the pre-analysis
commit that already exists for connection-idle reasons.

``complete_step`` deliberately does NOT commit by default: the caller
lands the step's output and the ``succeeded`` flip in one transaction
("persist-after-pay"), so there is no window where the money was spent
but the ledger forgot.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Canonical step keys. Keep in sync with the docstring on the model.
STEP_TRANSCRIPTION = "transcription"
STEP_SEGMENTATION = "segmentation"
STEP_ANALYSIS = "analysis"
STEP_SCORECARDS = "scorecards"
STEP_ENTITY_RESOLUTION = "entity_resolution"
STEP_PLAN_SYNTHESIS = "plan_synthesis"

# A pipeline run's longest single step is the 30-90s Sonnet analysis;
# 15 minutes comfortably covers the whole task including retries of
# transient DB blips, while still letting a takeover happen the same
# hour a worker OOMs mid-step.
DEFAULT_LEASE_SECONDS = 15 * 60

_HASH_SEP = b"\x1f"
_HASH_NONE = b"\x00none\x00"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(dt: Optional[datetime]) -> Optional[datetime]:
    """SQLite returns naive datetimes for timezone=True columns when the
    stored value had no offset; normalize to aware-UTC for comparisons."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def compute_input_hash(*parts: Any) -> str:
    """sha256 over the step's canonical inputs.

    Order-sensitive; ``None`` hashes distinctly from ``"None"`` and
    ``""`` so an absent input can't collide with a literal string.
    """
    h = hashlib.sha256()
    for part in parts:
        if part is None:
            h.update(_HASH_NONE)
        else:
            h.update(str(part).encode("utf-8", "replace"))
        h.update(_HASH_SEP)
    return h.hexdigest()


class StepClaim:
    """Outcome of :func:`claim_step`."""

    ACQUIRED = "acquired"
    REUSED = "reused"
    HELD = "held"

    def __init__(
        self,
        outcome: str,
        run_id: Optional[uuid.UUID] = None,
        attempt: int = 0,
        output_digest: Optional[str] = None,
    ) -> None:
        self.outcome = outcome
        self.run_id = run_id
        self.attempt = attempt
        self.output_digest = output_digest

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return "StepClaim(%s, run_id=%s, attempt=%s)" % (
            self.outcome, self.run_id, self.attempt,
        )


def claim_step(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    interaction_id: uuid.UUID,
    step_key: str,
    input_hash: str,
    worker_id: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> StepClaim:
    """Atomically claim (interaction, step, input_hash). Commits.

    Returns a :class:`StepClaim` whose ``outcome`` is ``ACQUIRED`` (run
    the step, then ``complete_step``/``fail_step``), ``REUSED`` (output
    already persisted — skip the paid call), or ``HELD`` (another worker
    holds a live lease — defer).
    """
    from backend.app.models import InteractionStepRun

    now = _utcnow()
    lease = now + timedelta(seconds=lease_seconds)

    row = (
        session.query(InteractionStepRun)
        .filter(
            InteractionStepRun.interaction_id == interaction_id,
            InteractionStepRun.step_key == step_key,
            InteractionStepRun.input_hash == input_hash,
        )
        .first()
    )

    if row is None:
        run = InteractionStepRun(
            tenant_id=tenant_id,
            interaction_id=interaction_id,
            step_key=step_key,
            input_hash=input_hash,
            status="running",
            attempt=1,
            claimed_by=worker_id,
            claimed_at=now,
            lease_expires_at=lease,
        )
        session.add(run)
        try:
            session.commit()
            return StepClaim(StepClaim.ACQUIRED, run.id, attempt=1)
        except IntegrityError:
            # Lost the INSERT race — re-read the winner's row and fall
            # through to the row-exists handling below.
            session.rollback()
            row = (
                session.query(InteractionStepRun)
                .filter(
                    InteractionStepRun.interaction_id == interaction_id,
                    InteractionStepRun.step_key == step_key,
                    InteractionStepRun.input_hash == input_hash,
                )
                .first()
            )
            if row is None:  # pragma: no cover — constraint said it exists
                raise

    if row.status == "succeeded":
        return StepClaim(
            StepClaim.REUSED, row.id, attempt=row.attempt,
            output_digest=row.output_digest,
        )

    lease_expired = (
        row.lease_expires_at is None or _as_aware(row.lease_expires_at) <= now
    )
    if row.status == "running" and not lease_expired:
        return StepClaim(StepClaim.HELD, row.id, attempt=row.attempt)

    # failed, or running-with-expired-lease → compare-and-set takeover.
    # The WHERE re-checks what we just read so a concurrent takeover
    # makes rowcount 0 instead of double-claiming. Capture the read
    # values first: ORM update() synchronizes the in-memory row, so
    # ``row.attempt`` is already bumped after execute().
    prior_attempt = row.attempt
    new_attempt = prior_attempt + 1
    result = session.execute(
        update(InteractionStepRun)
        .where(
            InteractionStepRun.id == row.id,
            InteractionStepRun.attempt == prior_attempt,
            InteractionStepRun.status == row.status,
        )
        .values(
            status="running",
            attempt=new_attempt,
            claimed_by=worker_id,
            claimed_at=now,
            lease_expires_at=lease,
            error=None,
            finished_at=None,
        )
    )
    session.commit()
    if result.rowcount == 1:
        return StepClaim(StepClaim.ACQUIRED, row.id, attempt=new_attempt)
    return StepClaim(StepClaim.HELD, row.id, attempt=prior_attempt)


def complete_step(
    session: Session,
    run_id: uuid.UUID,
    *,
    output_digest: Optional[str] = None,
    commit: bool = True,
) -> None:
    """Flip a claimed run to ``succeeded``.

    With ``commit=True`` (default) this commits the session — including
    any output the caller staged on it, which is exactly the
    persist-after-pay contract: output + ledger land atomically. Pass
    ``commit=False`` only when the caller owns a larger transaction and
    commits immediately itself.
    """
    from backend.app.models import InteractionStepRun

    session.execute(
        update(InteractionStepRun)
        .where(InteractionStepRun.id == run_id)
        .values(
            status="succeeded",
            finished_at=_utcnow(),
            output_digest=output_digest,
            error=None,
        )
    )
    if commit:
        session.commit()


def fail_step(
    session: Session,
    run_id: uuid.UUID,
    *,
    error: Optional[str] = None,
    commit: bool = True,
) -> None:
    """Flip a claimed run to ``failed`` (retryable by the next claim)."""
    from backend.app.models import InteractionStepRun

    session.execute(
        update(InteractionStepRun)
        .where(InteractionStepRun.id == run_id)
        .values(
            status="failed",
            finished_at=_utcnow(),
            error=(error or "")[:2000] or None,
        )
    )
    if commit:
        session.commit()
