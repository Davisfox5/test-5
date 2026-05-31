"""First-class domain scopes: per-interaction domain + per-user agent/manager domain arrays.

Promotes ``domain`` from an Action-Plan-synthesis-only concept to a
first-class property of every interaction and every user.

Before this migration:

* ``Tenant.default_domain`` and ``User.default_domain`` exist as Action
  Plan synthesizer hints (``sales`` / ``customer_service`` / ``it_support``
  / ``generic``). The domain a call belongs to is inferred at synth time
  from triage's ``domain_prediction`` with tenant/user fallbacks.
* ``User.role`` is a single string (``agent`` / ``manager`` / ``admin``)
  that has to mean three things at once: which surfaces the user sees,
  whether they can manage anyone, and whether they administer the tenant.

After this migration:

* Every ``Interaction`` carries a persistent ``domain`` column. Backfilled
  to ``Tenant.default_domain`` for existing rows. New rows are stamped at
  create time (the API call or the ingestion pipeline picks the right
  value, with triage prediction as a per-call override).
* Every ``User`` carries two JSONB arrays:

    * ``agent_domains`` — the domains the user works calls/emails in
      (empty list = user does no front-line work).
    * ``manager_domains`` — the domains the user has manager-level
      visibility into (empty list = user is not a manager anywhere).

  Plus ``is_tenant_admin`` (boolean) — separates tenant-settings
  administration from per-domain manager visibility. A founder who
  manages all three domains and runs the tenant is
  ``is_tenant_admin=True`` AND ``manager_domains=[sales, customer_service,
  it_support]``; a dedicated Sales Manager who can't touch settings is
  ``manager_domains=["sales"]`` with ``is_tenant_admin=False``.

Backfill preserves current behaviour bit-for-bit:

* Every interaction's ``domain`` becomes its tenant's ``default_domain``.
* Every user inherits the same effective scope they have today:

  * ``role='admin'`` → ``is_tenant_admin=True``,
    ``manager_domains=[tenant.default_domain]``,
    ``agent_domains=[tenant.default_domain]``
  * ``role='manager'`` → ``manager_domains=[tenant.default_domain]``,
    ``agent_domains=[tenant.default_domain]``
  * ``role='agent'`` → ``agent_domains=[tenant.default_domain]``,
    empty ``manager_domains``

``User.role`` stays on the table as a backward-compat shim: existing
``require_role("manager")`` gates still work. The new
``require_domain_manager(domain)`` factory in ``backend.app.auth`` is
additive.

Revision ID: dom_001_first_class_domain_scopes
Revises: ab01c2d3e4f5
Create Date: 2026-05-31

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text
from sqlalchemy.dialects import postgresql


revision: str = "dom_001_first_class_domain_scopes"
down_revision: Union[str, None] = "ab01c2d3e4f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_CANONICAL_DOMAINS = ("sales", "customer_service", "it_support", "generic")


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"
    jsonb = postgresql.JSONB(astext_type=sa.Text()) if is_postgres else sa.JSON()

    # ── Interaction.domain ──────────────────────────────────────────────
    op.add_column(
        "interactions",
        sa.Column("domain", sa.String(), nullable=True),
    )
    # Backfill: every existing interaction inherits its tenant's domain.
    op.execute(
        text(
            """
            UPDATE interactions
            SET domain = COALESCE(t.default_domain, 'generic')
            FROM tenants t
            WHERE interactions.tenant_id = t.id
              AND interactions.domain IS NULL
            """
        )
    )
    # CHECK constraint — accept NULL (legacy rows or transient state) or
    # one of the canonical vocabulary values.
    op.create_check_constraint(
        "ck_interactions_domain",
        "interactions",
        "domain IS NULL OR domain IN ('sales', 'customer_service', 'it_support', 'generic')",
    )
    op.create_index(
        "ix_interactions_tenant_domain",
        "interactions",
        ["tenant_id", "domain"],
    )

    # ── User.agent_domains / manager_domains / is_tenant_admin ─────────
    op.add_column(
        "users",
        sa.Column(
            "agent_domains",
            jsonb,
            nullable=False,
            server_default="[]",
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "manager_domains",
            jsonb,
            nullable=False,
            server_default="[]",
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "is_tenant_admin",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )

    # Backfill: translate the legacy ``role`` column into the new scope
    # shape so the foundation migration is invisible to every existing
    # session and gate.
    if is_postgres:
        # Postgres-native JSON array literals.
        op.execute(
            text(
                """
                UPDATE users u
                SET
                    agent_domains = jsonb_build_array(COALESCE(t.default_domain, 'generic')),
                    manager_domains = CASE
                        WHEN u.role IN ('manager', 'admin')
                            THEN jsonb_build_array(COALESCE(t.default_domain, 'generic'))
                        ELSE '[]'::jsonb
                    END,
                    is_tenant_admin = (u.role = 'admin')
                FROM tenants t
                WHERE u.tenant_id = t.id
                """
            )
        )
    else:
        # SQLite (used by unit tests) — JSON_ARRAY() and bare 1/0 booleans.
        op.execute(
            text(
                """
                UPDATE users
                SET agent_domains = json_array(
                        COALESCE(
                            (SELECT default_domain FROM tenants WHERE tenants.id = users.tenant_id),
                            'generic'
                        )
                    ),
                    manager_domains = CASE
                        WHEN users.role IN ('manager', 'admin')
                            THEN json_array(
                                COALESCE(
                                    (SELECT default_domain FROM tenants WHERE tenants.id = users.tenant_id),
                                    'generic'
                                )
                            )
                        ELSE json_array()
                    END,
                    is_tenant_admin = CASE WHEN users.role = 'admin' THEN 1 ELSE 0 END
                """
            )
        )


def downgrade() -> None:
    op.drop_column("users", "is_tenant_admin")
    op.drop_column("users", "manager_domains")
    op.drop_column("users", "agent_domains")
    op.drop_index("ix_interactions_tenant_domain", table_name="interactions")
    op.drop_constraint("ck_interactions_domain", "interactions", type_="check")
    op.drop_column("interactions", "domain")
