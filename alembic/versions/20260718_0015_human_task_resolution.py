"""Add durable human Task resolution audit ledger.

Revision ID: 20260718_0015
Revises: 20260718_0014
Create Date: 2026-07-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260718_0015"
down_revision: str | None = "20260718_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("budget_revision", sa.Integer(), server_default="0", nullable=False),
    )
    op.execute("UPDATE tasks SET budget_revision = 1 WHERE budget IS NOT NULL")
    op.create_check_constraint(
        "ck_tasks_budget_revision",
        "tasks",
        "budget_revision >= 0",
    )
    op.create_table(
        "task_resolutions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(length=48), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("previous_status", sa.String(length=32), nullable=False),
        sa.Column("resulting_status", sa.String(length=32), nullable=False),
        sa.Column("previous_error", sa.Text(), nullable=True),
        sa.Column("details", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "action IN ('ACCEPT_CANDIDATE', 'REJECT_TASK', "
            "'INCREASE_BUDGET_AND_RESUME')",
            name="ck_task_resolutions_action",
        ),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_task_resolutions_task_created",
        "task_resolutions",
        ["task_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_task_resolutions_task_created", table_name="task_resolutions")
    op.drop_table("task_resolutions")
    op.drop_constraint("ck_tasks_budget_revision", "tasks", type_="check")
    op.drop_column("tasks", "budget_revision")
