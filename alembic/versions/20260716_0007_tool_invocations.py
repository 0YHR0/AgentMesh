"""Add durable MCP Tool Invocation audit records.

Revision ID: 20260716_0007
Revises: 20260716_0006
Create Date: 2026-07-16
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260716_0007"
down_revision: str | None = "20260716_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tool_invocations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("server_name", sa.String(length=128), nullable=False),
        sa.Column("tool_key", sa.String(length=255), nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("side_effect", sa.String(length=32), nullable=False),
        sa.Column("protocol_version", sa.String(length=32), nullable=True),
        sa.Column("schema_digest", sa.String(length=80), nullable=True),
        sa.Column("arguments_digest", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("result_digest", sa.String(length=80), nullable=True),
        sa.Column("result_bytes", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "result_bytes IS NULL OR result_bytes >= 0",
            name="ck_tool_invocations_result_bytes",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["task_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_tool_invocations_run_started",
        "tool_invocations",
        ["run_id", "started_at"],
    )
    op.create_index(
        "ix_tool_invocations_task_started",
        "tool_invocations",
        ["task_id", "started_at"],
    )
    op.create_index(
        "ix_tool_invocations_tenant_status",
        "tool_invocations",
        ["tenant_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_tool_invocations_tenant_status", table_name="tool_invocations")
    op.drop_index("ix_tool_invocations_task_started", table_name="tool_invocations")
    op.drop_index("ix_tool_invocations_run_started", table_name="tool_invocations")
    op.drop_table("tool_invocations")
