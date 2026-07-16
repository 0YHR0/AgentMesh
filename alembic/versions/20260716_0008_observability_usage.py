"""Add Attempt trace correlation and durable usage/cost records.

Revision ID: 20260716_0008
Revises: 20260716_0007
Create Date: 2026-07-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260716_0008"
down_revision: str | None = "20260716_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "task_attempts",
        sa.Column("trace_id", sa.String(length=32), nullable=True),
    )
    op.execute("UPDATE task_attempts SET trace_id = replace(id::text, '-', '')")
    op.alter_column("task_attempts", "trace_id", nullable=False)
    op.create_unique_constraint("uq_task_attempts_trace_id", "task_attempts", ["trace_id"])
    op.create_check_constraint(
        "ck_task_attempts_trace_id",
        "task_attempts",
        "trace_id ~ '^[0-9a-f]{32}$' AND trace_id <> repeat('0', 32)",
    )

    op.create_table(
        "usage_records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("trace_id", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=128), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("usage_details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "cost_details_micros",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("pricing_version", sa.String(length=128), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source IN ('PROVIDER', 'ESTIMATED')",
            name="ck_usage_records_source",
        ),
        sa.CheckConstraint(
            "currency ~ '^[A-Z]{3}$'",
            name="ck_usage_records_currency",
        ),
        sa.CheckConstraint(
            "trace_id ~ '^[0-9a-f]{32}$' AND trace_id <> repeat('0', 32)",
            name="ck_usage_records_trace_id",
        ),
        sa.ForeignKeyConstraint(["attempt_id"], ["task_attempts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["task_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_usage_records_task_recorded",
        "usage_records",
        ["task_id", "recorded_at"],
    )
    op.create_index(
        "ix_usage_records_run_recorded",
        "usage_records",
        ["run_id", "recorded_at"],
    )
    op.create_index("ix_usage_records_attempt", "usage_records", ["attempt_id"])
    op.create_index(
        "ix_usage_records_tenant_provider",
        "usage_records",
        ["tenant_id", "provider"],
    )


def downgrade() -> None:
    op.drop_index("ix_usage_records_tenant_provider", table_name="usage_records")
    op.drop_index("ix_usage_records_attempt", table_name="usage_records")
    op.drop_index("ix_usage_records_run_recorded", table_name="usage_records")
    op.drop_index("ix_usage_records_task_recorded", table_name="usage_records")
    op.drop_table("usage_records")
    op.drop_constraint("ck_task_attempts_trace_id", "task_attempts", type_="check")
    op.drop_constraint("uq_task_attempts_trace_id", "task_attempts", type_="unique")
    op.drop_column("task_attempts", "trace_id")
