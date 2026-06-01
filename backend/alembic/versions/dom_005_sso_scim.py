"""SSO/SCIM provisioning: motion-mapping rules + per-IDP user link.

Adds two tables that power the v2 SSO/SCIM build:

* ``motion_provisioning_rule`` — tenant admin maps an IDP group name
  (Okta / Azure AD / Google Workspace security group) to a set of
  motion scopes. On JIT-provisioned login or SCIM push, every rule
  whose ``group_name`` is in the user's IDP claims contributes to the
  resolved motion scopes. Closed-by-default: a user with no matching
  rules gets nothing (the tenant default-motion only applies to
  invite-time creation, not SSO-driven).
* ``scim_account_link`` — maps a SCIM external_id (the IDP's stable
  user identifier) to a local User. SCIM PUT/PATCH operations look
  up the User through this table so an IDP-side rename or email
  change doesn't strand the link.

Revision ID: dom_005_sso_scim
Revises: dom_004_cross_motion_notifs
Create Date: 2026-06-01

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "dom_005_sso_scim"
down_revision: Union[str, None] = "dom_004_cross_motion_notifs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"
    jsonb = postgresql.JSONB(astext_type=sa.Text()) if is_postgres else sa.JSON()
    uuid_t = postgresql.UUID(as_uuid=True) if is_postgres else sa.String(36)

    # ── motion_provisioning_rule ───────────────────────────────────────
    op.create_table(
        "motion_provisioning_rule",
        sa.Column("id", uuid_t, primary_key=True),
        sa.Column(
            "tenant_id",
            uuid_t,
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # IDP-side identifier the user carries in their claims. Examples:
        # Okta group name (``linda-sales-agents``), Azure AD group
        # object id, Google Workspace org-unit path. We store whatever
        # the customer's IDP emits and match exact-string at apply time.
        sa.Column("group_name", sa.String(length=255), nullable=False),
        sa.Column("agent_domains", jsonb, nullable=False, server_default="[]"),
        sa.Column("manager_domains", jsonb, nullable=False, server_default="[]"),
        sa.Column(
            "grants_tenant_admin",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # One rule per (tenant, group_name) so a duplicate insert
        # surfaces the existing rule rather than silently shadowing.
        sa.UniqueConstraint(
            "tenant_id",
            "group_name",
            name="uq_motion_rule_tenant_group",
        ),
    )
    op.create_index(
        "ix_motion_rule_tenant_active",
        "motion_provisioning_rule",
        ["tenant_id", "is_active"],
    )

    # ── scim_account_link ──────────────────────────────────────────────
    op.create_table(
        "scim_account_link",
        sa.Column("id", uuid_t, primary_key=True),
        sa.Column(
            "tenant_id",
            uuid_t,
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            uuid_t,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # IDP's stable identifier for the user (Okta user id, AAD object
        # id). Unique within a tenant so a single IDP user can't be
        # mapped to two local users; a duplicate POST returns the
        # existing link.
        sa.Column("external_id", sa.String(length=255), nullable=False),
        # Provider tag for observability + future per-IDP quirks.
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "external_id",
            name="uq_scim_link_tenant_external",
        ),
        sa.UniqueConstraint(
            "user_id", name="uq_scim_link_user"
        ),
    )


def downgrade() -> None:
    op.drop_table("scim_account_link")
    op.drop_index(
        "ix_motion_rule_tenant_active", table_name="motion_provisioning_rule"
    )
    op.drop_table("motion_provisioning_rule")
