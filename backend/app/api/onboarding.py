"""Onboarding interview API.

Endpoints:

* ``POST /onboarding/sessions``          — start (or resume) an interview.
* ``GET  /onboarding/sessions/current``  — read the in-progress session, if any.
* ``POST /onboarding/sessions/{id}/reply`` — submit a user reply and get the
  next assistant message back.
* ``POST /onboarding/sessions/{id}/complete`` — finalise the interview and
  splice the collected answers into ``Tenant.tenant_context``.
* ``POST /onboarding/sessions/{id}/abandon`` — mark as abandoned (cleanup).

The interview agent (``OnboardingInterview``) is stateful in the DB row; the
HTTP layer is stateless.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import get_current_tenant
from backend.app.db import get_db
from backend.app.models import OnboardingSession, Tenant
from backend.app.services.kb.onboarding_interview import (
    OnboardingInterview,
    apply_completed_interview,
)

router = APIRouter()


class ReplyIn(BaseModel):
    message: str = ""


class OnboardingSessionOut(BaseModel):
    id: uuid.UUID
    status: str
    started_at: datetime
    completed_at: Optional[datetime]
    history: List[Dict[str, str]]
    answers: Dict[str, Any]
    completed_sections: List[str]
    next_section: Optional[str]
    done: bool
    assistant_message: Optional[str] = None


def _serialise(sess: OnboardingSession, last_assistant: Optional[str] = None) -> OnboardingSessionOut:
    state = sess.state or {}
    history = state.get("history") or []
    answers = state.get("answers") or {}
    completed = state.get("completed_sections") or []
    return OnboardingSessionOut(
        id=sess.id,
        status=sess.status,
        started_at=sess.started_at,
        completed_at=sess.completed_at,
        history=history,
        answers=answers,
        completed_sections=completed,
        next_section=state.get("next_section"),
        done=bool(state.get("done", False)),
        assistant_message=last_assistant
        or (history[-1]["content"] if history and history[-1]["role"] == "assistant" else None),
    )


async def _latest_active_session(
    db: AsyncSession, tenant_id: uuid.UUID
) -> Optional[OnboardingSession]:
    stmt = (
        select(OnboardingSession)
        .where(
            OnboardingSession.tenant_id == tenant_id,
            OnboardingSession.status == "active",
        )
        .order_by(desc(OnboardingSession.started_at))
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


@router.post("/onboarding/sessions", response_model=OnboardingSessionOut)
async def start_or_resume(
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Create a fresh onboarding session, or resume the tenant's active one."""
    existing = await _latest_active_session(db, tenant.id)
    if existing is not None:
        return _serialise(existing)

    agent = OnboardingInterview()
    state = OnboardingInterview.new_state()
    # Run the opening turn (empty user reply) so the tenant sees the first
    # question immediately rather than having to poke the interview.
    turn = await agent.step(state, user_reply="")
    state = OnboardingInterview.update_state(state, turn)

    sess = OnboardingSession(
        tenant_id=tenant.id,
        status="active",
        state=state,
    )
    db.add(sess)
    await db.flush()
    return _serialise(sess, last_assistant=turn.assistant_message)


@router.get("/onboarding/sessions/current", response_model=OnboardingSessionOut)
async def get_current(
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    sess = await _latest_active_session(db, tenant.id)
    if sess is None:
        raise HTTPException(status_code=404, detail="No active onboarding session")
    return _serialise(sess)


@router.post(
    "/onboarding/sessions/{session_id}/reply",
    response_model=OnboardingSessionOut,
)
async def submit_reply(
    session_id: uuid.UUID,
    body: ReplyIn,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    sess = await db.get(OnboardingSession, session_id)
    if sess is None or sess.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Session not found")
    if sess.status != "active":
        raise HTTPException(status_code=400, detail=f"Session is {sess.status}")

    agent = OnboardingInterview()
    turn = await agent.step(sess.state or OnboardingInterview.new_state(), user_reply=body.message)
    sess.state = OnboardingInterview.update_state(sess.state or {}, turn)
    # Don't auto-complete — completion is an explicit admin action so the
    # tenant can review the collected answers before they land in the brief.
    return _serialise(sess, last_assistant=turn.assistant_message)


@router.post("/onboarding/sessions/{session_id}/complete")
async def complete_session(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    sess = await db.get(OnboardingSession, session_id)
    if sess is None or sess.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Session not found")
    answers = (sess.state or {}).get("answers") or {}
    brief = await apply_completed_interview(db, tenant.id, answers)
    sess.status = "completed"
    sess.completed_at = datetime.now(timezone.utc)
    return {
        "session_id": str(session_id),
        "status": "completed",
        "applied_keys": [k for k in ("goals", "kpis", "strategies", "org_structure", "personal_touches") if k in answers],
        "brief": brief,
    }


@router.post("/onboarding/sessions/{session_id}/abandon", status_code=204)
async def abandon_session(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    sess = await db.get(OnboardingSession, session_id)
    if sess is None or sess.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Session not found")
    sess.status = "abandoned"
