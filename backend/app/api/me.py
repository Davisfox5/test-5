"""Current-tenant / current-user context for the SPA.

Returns everything the frontend needs on boot to decide what to render:
identity, plan tier, trial state, feature limits, and Ask-Linda
availability. Maps the ``TierSpec`` returned by ``limits_for(tenant)``
into the flat-feature shape the SPA's ``useMe()`` consumes.

Also hosts the sandbox-only preview-role switcher
(``POST /me/preview-role``). The override is a render-time overlay
gated at two layers (sandbox tier + role validity); the underlying
``users.role`` column is never mutated by this endpoint, so the
override never relaxes a security boundary. The trial-active gate
that used to wrap this was removed so a sandbox tenant can keep
previewing roles after the 14-day window — useful when a buying
decision drags past the trial. See the principal resolver in
:mod:`backend.app.auth` for the load-bearing gate.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import (
    AuthPrincipal,
    _tenant_allows_role_preview,
    get_current_principal,
)
from backend.app.db import get_db
from backend.app.models import User
from backend.app.plans import TierSpec, limits_for, trial_is_active, trial_is_expired
from backend.app.services.audit_log import audit_log

router = APIRouter()


PreviewRole = Literal["agent", "manager", "admin"]
Domain = Literal["sales", "customer_service", "it_support", "generic"]


class PlanLimitsOut(BaseModel):
    real_time_transcription: bool
    live_coaching: bool
    crm_push: bool
    custom_scorecards: bool
    custom_branding: bool
    ask_linda: bool
    api_access: bool
    max_users: Optional[int]
    max_monthly_minutes: Optional[int]
    max_uploads_per_day: Optional[int]
    # Pin the literal so the OpenAPI client + the SPA's narrowed
    # `"haiku"|"sonnet"|"opus"` union stay in lockstep. A future tier
    # ("haiku-fast") needs to widen this on both sides — silently drifting
    # would break the SPA's exhaustive-switch type-checks.
    ai_model_tier: Literal["haiku", "sonnet", "opus"]


class TenantOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    plan_tier: str
    is_white_label: bool
    trial_ends_at: Optional[datetime]
    trial_active: bool
    trial_expired: bool
    # True iff the tenant has an active Stripe subscription. The SPA
    # uses this to decide between the first-time plan picker (false)
    # and the Stripe billing portal (true) on /billing.
    has_subscription: bool
    # True iff this tenant may render the role-preview pill regardless
    # of tier. Mirrors the backend predicate so the SPA doesn't have to
    # reimplement "sandbox OR override-on" in two places.
    role_preview_enabled: bool
    limits: PlanLimitsOut


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    name: Optional[str]
    # The *effective* role: the preview-role overlay if it's currently
    # being applied, otherwise the user's real ``users.role``. Sidebar
    # nav + role-gated UI should read this directly.
    role: str
    # The user's stored override (``users.preview_role``). Surfaced so
    # the switcher can render which option is checked.
    preview_role: Optional[PreviewRole]
    # The user's underlying ``users.role`` (no preview overlay). Lets
    # the SPA render "Switch back to admin" when the preview differs.
    real_role: str
    # True iff the principal resolver applied the preview overlay on
    # this request — drives the "you're in preview mode" banner.
    is_previewing: bool
    # ── Domain scopes (added with ``dom_001``) ──────────────────────────
    # The motions this user works front-line in. Drives which agent
    # surfaces (inbox, action plans, coaching) the SPA shows. Empty list
    # = no agent surfaces; common for a dedicated Sales Manager who
    # only consumes dashboards.
    agent_domains: List[Domain]
    # The motions this user has manager-level visibility into. Drives
    # which Manager sub-pages render; ``len >= 2`` unlocks the
    # cross-motion Journey view.
    manager_domains: List[Domain]
    # Tenant-settings/admin gate, orthogonal to manager scope. A
    # dedicated Sales Manager has ``manager_domains=["sales"]`` and
    # ``is_tenant_admin=False``; the founder has both.
    is_tenant_admin: bool


class MeOut(BaseModel):
    tenant: TenantOut
    user: Optional[UserOut]


class PreviewRoleIn(BaseModel):
    """Body for ``POST /me/preview-role``.

    ``role: null`` clears the override. Anything outside the literal
    set is rejected by Pydantic with HTTP 422.
    """

    role: Optional[PreviewRole] = None


def _plan_limits_out(spec: TierSpec) -> PlanLimitsOut:
    """Project ``TierSpec`` into the flat shape the SPA expects.

    ``TierSpec.features`` is a free-form dict; the API surfaces only
    the seven gates the dashboard actually reads (UI-facing toggles).
    Adding a new gate? Add it both here and in
    ``apps/app/src/lib/me.ts:PlanLimits``.
    """
    f = spec.features
    return PlanLimitsOut(
        real_time_transcription=bool(f.get("real_time_transcription", False)),
        live_coaching=bool(f.get("live_coaching", False)),
        crm_push=bool(f.get("crm_push", False)),
        custom_scorecards=bool(f.get("custom_scorecards", False)),
        custom_branding=bool(f.get("custom_branding", False)),
        ask_linda=bool(f.get("ask_linda", False)),
        api_access=bool(f.get("api_access", False)),
        max_users=spec.seat_limit,
        max_monthly_minutes=spec.max_monthly_minutes,
        max_uploads_per_day=spec.max_uploads_per_day,
        ai_model_tier=spec.ai_model_tier,
    )


def _user_out(principal: AuthPrincipal) -> Optional[UserOut]:
    user = principal.user
    if user is None:
        return None
    raw_preview = user.preview_role
    preview_role: Optional[PreviewRole] = (
        raw_preview if raw_preview in {"agent", "manager", "admin"} else None
    )
    # Coerce any out-of-vocabulary value (corrupt JSON from a hand-edit,
    # a future domain not yet declared in the Literal) to ``"generic"``
    # rather than 500'ing the SPA boot. Pydantic would otherwise reject
    # the response model.
    def _coerce_domain_list(raw: Any) -> List[Domain]:
        if not isinstance(raw, list):
            return []
        out: List[Domain] = []
        for v in raw:
            if v in ("sales", "customer_service", "it_support", "generic"):
                out.append(v)  # type: ignore[arg-type]
        return out

    return UserOut(
        id=user.id,
        email=user.email,
        name=user.name,
        role=principal.role,
        preview_role=preview_role,
        real_role=principal.real_role,
        is_previewing=principal.is_previewing,
        agent_domains=_coerce_domain_list(principal.agent_domains),
        manager_domains=_coerce_domain_list(principal.manager_domains),
        is_tenant_admin=principal.is_tenant_admin,
    )


@router.get("/me", response_model=MeOut)
async def me(
    principal: AuthPrincipal = Depends(get_current_principal),
) -> MeOut:
    tenant = principal.tenant
    return MeOut(
        tenant=TenantOut(
            id=tenant.id,
            name=tenant.name,
            slug=tenant.slug,
            plan_tier=tenant.plan_tier,
            is_white_label=tenant.is_white_label,
            trial_ends_at=tenant.trial_ends_at,
            trial_active=trial_is_active(tenant),
            trial_expired=trial_is_expired(tenant),
            # Treat a whitespace-only id as "no subscription" — guards
            # against a stale string that survived a cancel webhook.
            has_subscription=bool(
                tenant.stripe_subscription_id
                and tenant.stripe_subscription_id.strip()
            ),
            role_preview_enabled=_tenant_allows_role_preview(tenant),
            limits=_plan_limits_out(limits_for(tenant)),
        ),
        user=_user_out(principal),
    )


@router.post("/me/preview-role")
async def set_preview_role(
    body: PreviewRoleIn,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(get_current_principal),
) -> Dict[str, Any]:
    """Set or clear the calling user's preview role.

    Allowed on sandbox tenants or any tenant with
    ``role_preview_enabled = True``. The override is render-time only —
    the user's real role is unchanged and remains the source of truth
    for security. ``role: null`` clears.
    """
    # API-key callers don't have a human user behind them; preview is
    # an interactive-session feature only.
    if principal.source == "api_key" or principal.user is None:
        raise HTTPException(
            status_code=403,
            detail="preview role only applies to interactive sessions",
        )

    tenant = principal.tenant
    if not _tenant_allows_role_preview(tenant):
        raise HTTPException(
            status_code=403,
            detail="preview role is not enabled for this tenant",
        )

    # Look the user up in *this* request's DB session — the principal's
    # ``.user`` came from the principal-resolver's session and isn't
    # attached here, so mutations on it wouldn't reach this commit.
    db_user = (
        await db.execute(select(User).where(User.id == principal.user.id))
    ).scalar_one()

    before_value = db_user.preview_role
    new_value = body.role  # Pydantic already validated the literal set.

    db_user.preview_role = new_value
    await audit_log(
        db,
        principal,
        action="user.preview_role_set",
        resource_type="user",
        resource_id=str(db_user.id),
        before={"preview_role": before_value},
        after={"preview_role": new_value},
    )
    await db.commit()

    return {
        "role": new_value,
        "real_role": db_user.role or "agent",
    }


class CalendarProviderStatus(BaseModel):
    """One provider's serve-readiness for the current user.

    The SPA uses this to render a "Connect calendar" CTA on the Action
    Item card when no real provider is available, instead of letting
    the user click "Schedule" and discover the stub fallback after the
    fact. ``ok=True`` means ``can_serve()`` returned True for this
    user/tenant pair.
    """

    name: str
    ok: bool
    reason: Optional[str] = None


class CalendarProvidersOut(BaseModel):
    providers: list[CalendarProviderStatus]
    active_provider: Optional[str]
    """``active_provider`` is whichever provider would serve a Schedule
    Meeting click right now, in scheduler preference order. ``None``
    means the stub would fire (no real provider can serve)."""


@router.get("/me/calendar-providers", response_model=CalendarProvidersOut)
async def get_calendar_providers(
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(get_current_principal),
) -> CalendarProvidersOut:
    """Return which calendar providers can serve a Schedule Meeting
    click for the current user.

    The frontend uses this to gate the Schedule button: when no real
    provider can serve, the button changes to a "Connect calendar"
    CTA pointing at Settings -> Integrations. Without this pre-flight
    the user discovers the stub fallback only after clicking, which
    surfaces a confusing "no calendar provider connected" error.
    """
    # Import inside the handler so the meeting_scheduler module's
    # provider registry isn't a startup-time hard dependency.
    from backend.app.services.meeting_scheduler.google_calendar import (
        GoogleCalendarProvider,
    )
    from backend.app.services.meeting_scheduler.microsoft_graph import (
        MicrosoftGraphProvider,
    )
    from backend.app.services.meeting_scheduler.zoom import ZoomMeetingProvider
    from backend.app.services.meeting_scheduler.cal_com import CalcomProvider

    tenant_id = principal.tenant.id
    user_id = principal.user.id if principal.user else None

    candidates = [
        GoogleCalendarProvider,
        MicrosoftGraphProvider,
        ZoomMeetingProvider,
        CalcomProvider,
    ]
    statuses: list[CalendarProviderStatus] = []
    active: Optional[str] = None
    for cls in candidates:
        try:
            ok = await cls.can_serve(db, tenant_id=tenant_id, user_id=user_id)
        except Exception:
            ok = False
        statuses.append(CalendarProviderStatus(name=cls.name, ok=ok))
        if ok and active is None:
            active = cls.name
    return CalendarProvidersOut(providers=statuses, active_provider=active)


class EmailProviderStatus(BaseModel):
    name: str
    ok: bool


class EmailProvidersOut(BaseModel):
    providers: list[EmailProviderStatus]
    active_provider: Optional[str]


@router.get("/me/email-providers", response_model=EmailProvidersOut)
async def get_email_providers(
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(get_current_principal),
) -> EmailProvidersOut:
    """Return which email providers can send for the current tenant.

    Mirrors ``/me/calendar-providers`` so the SPA can pre-flight a Send
    button: connected provider → Send; no provider → Connect-email CTA.
    """
    from backend.app.models import Integration as _Integration
    from sqlalchemy import select as _select

    tenant_id = principal.tenant.id
    statuses: list[EmailProviderStatus] = []
    active: Optional[str] = None
    for name in ("google", "microsoft"):
        stmt = _select(_Integration).where(
            _Integration.tenant_id == tenant_id,
            _Integration.provider == name,
        ).limit(1)
        row = (await db.execute(stmt)).scalar_one_or_none()
        ok = row is not None
        # Refuse to claim "ok" when the integration exists but doesn't
        # have the relevant send scope. Both providers expose it as the
        # same logical capability under different scope URLs.
        if ok:
            scopes = row.scopes or []
            if name == "google":
                ok = any(
                    s in scopes for s in (
                        "https://www.googleapis.com/auth/gmail.send",
                        "gmail.send",
                    )
                )
            elif name == "microsoft":
                ok = any(
                    s in scopes for s in (
                        "https://graph.microsoft.com/Mail.Send",
                        "Mail.Send",
                    )
                )
        statuses.append(EmailProviderStatus(name=name, ok=ok))
        if ok and active is None:
            active = name
    return EmailProvidersOut(providers=statuses, active_provider=active)
