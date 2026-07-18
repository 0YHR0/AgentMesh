"""Add governed actions and approval decisions.

Revision ID: 20260718_0016
Revises: 20260718_0015
Create Date: 2026-07-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260718_0016"
down_revision: str | None = "20260718_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "governed_actions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("requester_id", sa.String(length=128), nullable=False),
        sa.Column("action_type", sa.String(length=64), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("arguments", postgresql.JSONB(), nullable=False),
        sa.Column("canonicalization_version", sa.String(length=64), nullable=False),
        sa.Column("action_hash", sa.String(length=64), nullable=False),
        sa.Column("policy_result", sa.String(length=32), nullable=False),
        sa.Column("reason_code", sa.String(length=128), nullable=False),
        sa.Column("policy_bundle", sa.String(length=128), nullable=False),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("approval_id", sa.Uuid(), nullable=True),
        sa.Column("approval_status", sa.String(length=32), nullable=False),
        sa.Column("permit_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "policy_result IN ('ALLOW', 'DENY', 'REQUIRE_APPROVAL')",
            name="ck_governed_actions_policy_result",
        ),
        sa.CheckConstraint(
            "approval_status IN ('NOT_REQUIRED', 'PENDING', 'APPROVED', "
            "'REJECTED', 'EXPIRED', 'CONSUMED')",
            name="ck_governed_actions_approval_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("approval_id"),
        sa.UniqueConstraint("permit_id"),
    )
    op.create_index(
        "ix_governed_actions_tenant_status",
        "governed_actions",
        ["tenant_id", "approval_status", "created_at"],
    )
    op.create_index(
        "ix_governed_actions_resource",
        "governed_actions",
        ["tenant_id", "resource_type", "resource_id"],
    )
    op.create_table(
        "approval_decisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("governed_action_id", sa.Uuid(), nullable=False),
        sa.Column("approval_id", sa.Uuid(), nullable=False),
        sa.Column("approver_id", sa.String(length=128), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "outcome IN ('APPROVE', 'REJECT')",
            name="ck_approval_decisions_outcome",
        ),
        sa.ForeignKeyConstraint(
            ["governed_action_id"],
            ["governed_actions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("approval_id", name="uq_approval_decisions_approval"),
    )
    op.create_index(
        "ix_approval_decisions_action_created",
        "approval_decisions",
        ["governed_action_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_approval_decisions_action_created", table_name="approval_decisions")
    op.drop_table("approval_decisions")
    op.drop_index("ix_governed_actions_resource", table_name="governed_actions")
    op.drop_index("ix_governed_actions_tenant_status", table_name="governed_actions")
    op.drop_table("governed_actions")
