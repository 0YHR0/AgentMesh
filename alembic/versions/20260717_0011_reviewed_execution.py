"""Add reviewed execution state and durable run roles.

Revision ID: 20260717_0011
Revises: 20260717_0010
Create Date: 2026-07-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260717_0011"
down_revision: str | None = "20260717_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("execution_mode", sa.String(length=32), nullable=False, server_default="DIRECT"),
    )
    op.add_column(
        "tasks",
        sa.Column(
            "acceptance_criteria",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "tasks", sa.Column("max_revisions", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "tasks", sa.Column("revision_count", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "tasks", sa.Column("review_deadline", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "tasks",
        sa.Column("candidate_output", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "tasks",
        sa.Column("latest_review", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "task_runs",
        sa.Column("role", sa.String(length=32), nullable=False, server_default="EXECUTOR"),
    )
    op.add_column(
        "task_runs",
        sa.Column("revision_number", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        "ck_tasks_execution_mode", "tasks", "execution_mode IN ('DIRECT', 'REVIEWED')"
    )
    op.create_check_constraint(
        "ck_tasks_review_revision_counts",
        "tasks",
        "max_revisions >= 0 AND revision_count >= 0 AND revision_count <= max_revisions",
    )
    op.create_check_constraint(
        "ck_task_runs_role", "task_runs", "role IN ('EXECUTOR', 'REVIEWER')"
    )
    op.create_check_constraint(
        "ck_task_runs_revision_number", "task_runs", "revision_number >= 0"
    )
    op.alter_column("tasks", "execution_mode", server_default=None)
    op.alter_column("tasks", "acceptance_criteria", server_default=None)
    op.alter_column("tasks", "max_revisions", server_default=None)
    op.alter_column("tasks", "revision_count", server_default=None)
    op.alter_column("task_runs", "role", server_default=None)
    op.alter_column("task_runs", "revision_number", server_default=None)


def downgrade() -> None:
    op.drop_constraint("ck_task_runs_revision_number", "task_runs", type_="check")
    op.drop_constraint("ck_task_runs_role", "task_runs", type_="check")
    op.drop_constraint("ck_tasks_review_revision_counts", "tasks", type_="check")
    op.drop_constraint("ck_tasks_execution_mode", "tasks", type_="check")
    op.drop_column("task_runs", "revision_number")
    op.drop_column("task_runs", "role")
    op.drop_column("tasks", "latest_review")
    op.drop_column("tasks", "candidate_output")
    op.drop_column("tasks", "review_deadline")
    op.drop_column("tasks", "revision_count")
    op.drop_column("tasks", "max_revisions")
    op.drop_column("tasks", "acceptance_criteria")
    op.drop_column("tasks", "execution_mode")
