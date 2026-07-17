"""Add durable coordinated Handoff lifecycle.

Revision ID: 20260717_0013
Revises: 20260717_0012
Create Date: 2026-07-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260717_0013"
down_revision: str | None = "20260717_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "handoffs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("source_subtask_id", sa.Uuid(), nullable=False),
        sa.Column("source_run_id", sa.Uuid(), nullable=False),
        sa.Column("source_trace_id", sa.String(length=32), nullable=False),
        sa.Column("causation_id", sa.Uuid(), nullable=False),
        sa.Column("source_agent_id", sa.String(length=63), nullable=False),
        sa.Column("target_subtask_id", sa.Uuid(), nullable=False),
        sa.Column("target_agent_id", sa.String(length=63), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("completed_work_summary", sa.Text(), nullable=False),
        sa.Column(
            "unresolved_questions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "constraints", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "acceptance_criteria",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("requested_by", sa.String(length=128), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_by", sa.String(length=128), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "status IN ('REQUESTED', 'ACCEPTED', 'REJECTED')",
            name="ck_handoffs_status",
        ),
        sa.CheckConstraint(
            "source_subtask_id <> target_subtask_id",
            name="ck_handoffs_distinct_subtasks",
        ),
        sa.CheckConstraint(
            "source_trace_id ~ '^[0-9a-f]{32}$'",
            name="ck_handoffs_source_trace_id",
        ),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["source_subtask_id"], ["subtasks.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["source_run_id"], ["task_runs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["target_subtask_id"], ["subtasks.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("causation_id"),
    )
    op.create_index(
        "ix_handoffs_task_requested", "handoffs", ["task_id", "requested_at"]
    )
    op.create_index(
        "ix_handoffs_target_status", "handoffs", ["target_subtask_id", "status"]
    )
    op.create_index(
        "uq_handoffs_one_accepted_target",
        "handoffs",
        ["target_subtask_id"],
        unique=True,
        postgresql_where=sa.text("status = 'ACCEPTED'"),
    )


def downgrade() -> None:
    op.drop_index("uq_handoffs_one_accepted_target", table_name="handoffs")
    op.drop_index("ix_handoffs_target_status", table_name="handoffs")
    op.drop_index("ix_handoffs_task_requested", table_name="handoffs")
    op.drop_table("handoffs")
