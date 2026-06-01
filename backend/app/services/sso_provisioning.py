"""SSO/SCIM motion-scope provisioning.

Resolves a user's motion scopes from the set of IDP group names they
carry in their SSO claims (or SCIM ``groups`` payload). Every rule
in ``motion_provisioning_rule`` whose ``group_name`` is in the input
contributes its scopes; the result is the union across matching rules.

Used by:

* The Clerk JWT principal resolver — when the ``groups`` claim is
  present, apply matching rules and update the User row (JIT
  provisioning at first login + on every login to keep changes from
  the IDP reflected).
* The SCIM ``POST /scim/v2/Users`` endpoint — on create-or-update,
  apply the supplied groups so the IDP-driven push has the same
  effect as a JIT login.

Closed-by-default: a user with no matching groups gets ``agent_domains
= []``, ``manager_domains = []``, ``is_tenant_admin = False``. The
tenant default-motion does NOT apply here — that's an invite-time
fallback for users created manually through the UI.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Iterable, List

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models import MotionProvisioningRule, User

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedScopes:
    """Output of the rule resolver. Plain dataclass so the JIT hook can
    diff against the user's current scopes without round-tripping
    through a Pydantic model."""

    agent_domains: List[str]
    manager_domains: List[str]
    is_tenant_admin: bool
    matched_rule_count: int


def resolve_scopes_from_groups(
    session: Session,
    tenant_id: uuid.UUID,
    group_names: Iterable[str],
) -> ResolvedScopes:
    """Compute the motion-scope union for a user given their IDP groups.

    Inactive rules are ignored. Unknown group names (no matching rule)
    are silently skipped — they're not errors, just groups we don't
    map. Domain values are pinned to the canonical vocabulary by the
    rule-creation API; the resolver doesn't re-validate.
    """
    groups = [g for g in group_names if isinstance(g, str) and g]
    if not groups:
        return ResolvedScopes([], [], False, 0)

    rules = (
        session.execute(
            select(MotionProvisioningRule).where(
                MotionProvisioningRule.tenant_id == tenant_id,
                MotionProvisioningRule.group_name.in_(groups),
                MotionProvisioningRule.is_active.is_(True),
            )
        )
        .scalars()
        .all()
    )

    agent_set: List[str] = []
    manager_set: List[str] = []
    tenant_admin = False
    for r in rules:
        for d in r.agent_domains or []:
            if d not in agent_set:
                agent_set.append(d)
        for d in r.manager_domains or []:
            if d not in manager_set:
                manager_set.append(d)
        if r.grants_tenant_admin:
            tenant_admin = True
    return ResolvedScopes(
        agent_domains=agent_set,
        manager_domains=manager_set,
        is_tenant_admin=tenant_admin,
        matched_rule_count=len(rules),
    )


def apply_scopes_to_user(
    session: Session,
    user: User,
    resolved: ResolvedScopes,
    *,
    overwrite_admin: bool = True,
) -> bool:
    """Mutate ``user`` to match the resolved scopes. Returns True iff
    anything changed.

    ``overwrite_admin`` controls whether ``is_tenant_admin`` follows
    the resolution. The Clerk JIT hook sets it True so an IDP
    revocation drops the tenant-admin bit; the SCIM endpoint can pass
    False when an integration wants the rule set to grant admin but
    not revoke it (e.g. when admins live in a different group the
    integration doesn't see).
    """
    changed = False
    if list(user.agent_domains or []) != resolved.agent_domains:
        user.agent_domains = resolved.agent_domains
        changed = True
    if list(user.manager_domains or []) != resolved.manager_domains:
        user.manager_domains = resolved.manager_domains
        changed = True
    if overwrite_admin and bool(user.is_tenant_admin) != resolved.is_tenant_admin:
        user.is_tenant_admin = resolved.is_tenant_admin
        changed = True
    if changed:
        session.flush()
    return changed
