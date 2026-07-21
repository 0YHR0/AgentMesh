"""goal contracts and plan patches

Revision ID: 20260721_0030
Revises: 20260721_0029
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260721_0030"
down_revision = "20260721_0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_goal_contracts",
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("constraints", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("success_criteria", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("digest", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_task_goal_contracts_version"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("task_id"),
        sa.UniqueConstraint("task_id", "digest", name="uq_task_goal_contract_digest"),
    )
    op.create_table(
        "plan_patches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("goal_digest", sa.String(length=80), nullable=False),
        sa.Column("base_plan_version", sa.Integer(), nullable=False),
        sa.Column("base_plan_digest", sa.String(length=80), nullable=False),
        sa.Column("proposed_plan_version", sa.Integer(), nullable=False),
        sa.Column("proposed_plan_digest", sa.String(length=80), nullable=False),
        sa.Column("proposed_plan", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("requested_by", sa.String(length=128), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "base_plan_version >= 1 AND proposed_plan_version = base_plan_version + 1",
            name="ck_plan_patches_versions",
        ),
        sa.CheckConstraint("status IN ('VERIFIED', 'APPLIED')", name="ck_plan_patches_status"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_plan_patches_task_created", "plan_patches", ["task_id", "created_at"])


def downgrade() -> None:
    op.drop_table("plan_patches")
    op.drop_table("task_goal_contracts")
