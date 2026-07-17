"""Add durable coordinated Subtask DAG execution.

Revision ID: 20260717_0012
Revises: 20260717_0011
Create Date: 2026-07-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260717_0012"
down_revision: str | None = "20260717_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("plan_version", sa.Integer(), nullable=True))
    op.add_column("tasks", sa.Column("plan_digest", sa.String(length=80), nullable=True))
    op.add_column(
        "tasks",
        sa.Column("max_concurrency", sa.Integer(), nullable=False, server_default="1"),
    )
    op.drop_constraint("ck_tasks_execution_mode", "tasks", type_="check")
    op.create_check_constraint(
        "ck_tasks_execution_mode",
        "tasks",
        "execution_mode IN ('DIRECT', 'REVIEWED', 'COORDINATED')",
    )
    op.create_check_constraint(
        "ck_tasks_max_concurrency",
        "tasks",
        "max_concurrency >= 1 AND max_concurrency <= 10",
    )
    op.create_table(
        "subtasks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("input", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "required_capabilities",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("preferred_agent_id", sa.String(length=63), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("current_run_id", sa.Uuid(), nullable=True),
        sa.Column("output", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('BLOCKED', 'READY', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELED')",
            name="ck_subtasks_status",
        ),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "key", name="uq_subtasks_task_key"),
    )
    op.create_index(
        "ix_subtasks_task_status_key", "subtasks", ["task_id", "status", "key"]
    )
    op.create_table(
        "subtask_dependencies",
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("predecessor_id", sa.Uuid(), nullable=False),
        sa.Column("successor_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "predecessor_id <> successor_id",
            name="ck_subtask_dependencies_distinct",
        ),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["predecessor_id"], ["subtasks.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["successor_id"], ["subtasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint(
            "task_id",
            "predecessor_id",
            "successor_id",
            name="pk_subtask_dependencies",
        ),
    )
    op.create_index(
        "ix_subtask_dependencies_successor",
        "subtask_dependencies",
        ["successor_id", "predecessor_id"],
    )
    op.add_column("task_runs", sa.Column("subtask_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_task_runs_subtask_id",
        "task_runs",
        "subtasks",
        ["subtask_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_task_runs_subtask_id", "task_runs", ["subtask_id"])
    op.drop_constraint("ck_task_runs_role", "task_runs", type_="check")
    op.create_check_constraint(
        "ck_task_runs_role",
        "task_runs",
        "role IN ('EXECUTOR', 'REVIEWER', 'SUPERVISOR')",
    )
    op.alter_column("tasks", "max_concurrency", server_default=None)


def downgrade() -> None:
    # The previous schema cannot represent coordinated Tasks or Supervisor Runs,
    # and dropping the Subtask tables is inherently lossy. Remove only aggregates
    # introduced by this revision before restoring the narrower check constraints;
    # task-owned Runs and Attempts follow their existing ON DELETE CASCADE rules.
    op.execute("DELETE FROM tasks WHERE execution_mode = 'COORDINATED'")
    op.drop_constraint("ck_task_runs_role", "task_runs", type_="check")
    op.create_check_constraint(
        "ck_task_runs_role", "task_runs", "role IN ('EXECUTOR', 'REVIEWER')"
    )
    op.drop_index("ix_task_runs_subtask_id", table_name="task_runs")
    op.drop_constraint("fk_task_runs_subtask_id", "task_runs", type_="foreignkey")
    op.drop_column("task_runs", "subtask_id")
    op.drop_index("ix_subtask_dependencies_successor", table_name="subtask_dependencies")
    op.drop_table("subtask_dependencies")
    op.drop_index("ix_subtasks_task_status_key", table_name="subtasks")
    op.drop_table("subtasks")
    op.drop_constraint("ck_tasks_max_concurrency", "tasks", type_="check")
    op.drop_constraint("ck_tasks_execution_mode", "tasks", type_="check")
    op.create_check_constraint(
        "ck_tasks_execution_mode", "tasks", "execution_mode IN ('DIRECT', 'REVIEWED')"
    )
    op.drop_column("tasks", "max_concurrency")
    op.drop_column("tasks", "plan_digest")
    op.drop_column("tasks", "plan_version")
