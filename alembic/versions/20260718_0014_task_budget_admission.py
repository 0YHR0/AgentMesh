"""Add Task budget policy and Attempt reservation settlement fields.

Revision ID: 20260718_0014
Revises: 20260717_0013
Create Date: 2026-07-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260718_0014"
down_revision: str | None = "20260717_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("budget", postgresql.JSONB(), nullable=True))
    op.add_column(
        "tasks", sa.Column("settled_tokens", sa.BigInteger(), server_default="0", nullable=False)
    )
    op.add_column(
        "tasks", sa.Column("reserved_tokens", sa.BigInteger(), server_default="0", nullable=False)
    )
    op.add_column(
        "tasks",
        sa.Column("settled_cost_micros", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.add_column(
        "tasks",
        sa.Column("reserved_cost_micros", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.add_column(
        "tasks", sa.Column("budget_exhausted_reason", sa.String(length=64), nullable=True)
    )
    op.create_check_constraint(
        "ck_tasks_budget_counters",
        "tasks",
        "settled_tokens >= 0 AND reserved_tokens >= 0 AND "
        "settled_cost_micros >= 0 AND reserved_cost_micros >= 0",
    )
    op.add_column(
        "task_attempts",
        sa.Column("reserved_tokens", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.add_column(
        "task_attempts",
        sa.Column("reserved_cost_micros", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.add_column("task_attempts", sa.Column("settled_tokens", sa.BigInteger(), nullable=True))
    op.add_column(
        "task_attempts", sa.Column("settled_cost_micros", sa.BigInteger(), nullable=True)
    )
    op.add_column(
        "task_attempts",
        sa.Column("budget_settlement_source", sa.String(length=32), nullable=True),
    )
    op.create_check_constraint(
        "ck_task_attempts_budget_values",
        "task_attempts",
        "reserved_tokens >= 0 AND reserved_cost_micros >= 0 AND "
        "(settled_tokens IS NULL OR settled_tokens >= 0) AND "
        "(settled_cost_micros IS NULL OR settled_cost_micros >= 0)",
    )
    op.create_check_constraint(
        "ck_task_attempts_budget_source",
        "task_attempts",
        "budget_settlement_source IS NULL OR "
        "budget_settlement_source IN ('ACTUAL', 'CONSERVATIVE_ESTIMATE', 'RELEASED')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_task_attempts_budget_source", "task_attempts", type_="check")
    op.drop_constraint("ck_task_attempts_budget_values", "task_attempts", type_="check")
    op.drop_column("task_attempts", "budget_settlement_source")
    op.drop_column("task_attempts", "settled_cost_micros")
    op.drop_column("task_attempts", "settled_tokens")
    op.drop_column("task_attempts", "reserved_cost_micros")
    op.drop_column("task_attempts", "reserved_tokens")
    op.drop_constraint("ck_tasks_budget_counters", "tasks", type_="check")
    op.drop_column("tasks", "budget_exhausted_reason")
    op.drop_column("tasks", "reserved_cost_micros")
    op.drop_column("tasks", "settled_cost_micros")
    op.drop_column("tasks", "reserved_tokens")
    op.drop_column("tasks", "settled_tokens")
    op.drop_column("tasks", "budget")
