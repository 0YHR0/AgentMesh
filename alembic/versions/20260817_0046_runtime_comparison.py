"""Persist immutable A2 path admission and comparison evidence."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260817_0046"
down_revision = "20260817_0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "task_runs",
        sa.Column(
            "runtime_authority", sa.String(length=16), nullable=False, server_default="legacy"
        ),
    )
    op.add_column(
        "task_runs",
        sa.Column("comparison_mode", sa.String(length=32), nullable=False, server_default="off"),
    )
    op.add_column("task_runs", sa.Column("runtime_execution_intent_id", sa.Uuid(), nullable=True))
    op.create_check_constraint(
        "ck_task_runs_runtime_authority",
        "task_runs",
        "runtime_authority IN ('legacy', 'managed')",
    )
    op.create_check_constraint(
        "ck_task_runs_comparison_mode",
        "task_runs",
        "comparison_mode IN ('off', 'deterministic_shadow')",
    )
    op.create_check_constraint(
        "ck_task_runs_comparison_pin",
        "task_runs",
        "comparison_mode = 'off' OR (runtime_authority = 'legacy' AND "
        "runtime_version_id IS NOT NULL AND "
        "(runtime_execution_id IS NOT NULL OR runtime_execution_intent_id IS NOT NULL))",
    )
    op.create_check_constraint(
        "ck_task_runs_runtime_execution_identity",
        "task_runs",
        "runtime_execution_intent_id IS NULL OR runtime_execution_id IS NULL OR "
        "runtime_execution_intent_id = runtime_execution_id",
    )
    op.create_table(
        "runtime_comparisons",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column(
            "task_id", sa.Uuid(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "run_id", sa.Uuid(), sa.ForeignKey("task_runs.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "attempt_id",
            sa.Uuid(),
            sa.ForeignKey("task_attempts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("authoritative_path", sa.String(length=16), nullable=False),
        sa.Column("authoritative_digest", sa.String(length=64), nullable=False),
        sa.Column("comparison_digest", sa.String(length=64), nullable=False),
        sa.Column("comparison_observation_id", sa.String(length=512), nullable=True),
        sa.Column("matches", sa.Boolean(), nullable=False),
        sa.Column("mismatches", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "authoritative_path = 'legacy'", name="ck_runtime_comparison_authority"
        ),
        sa.CheckConstraint(
            "authoritative_digest ~ '^[0-9a-f]{64}$' AND comparison_digest ~ '^[0-9a-f]{64}$'",
            name="ck_runtime_comparison_digests",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id", "attempt_id", name="uq_runtime_comparisons_run_attempt"
        ),
    )
    op.create_index(
        "ix_runtime_comparisons_tenant_created",
        "runtime_comparisons",
        ["tenant_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_runtime_comparisons_tenant_created", table_name="runtime_comparisons")
    op.drop_table("runtime_comparisons")
    op.drop_constraint("ck_task_runs_runtime_authority", "task_runs", type_="check")
    op.drop_constraint("ck_task_runs_comparison_mode", "task_runs", type_="check")
    op.drop_constraint("ck_task_runs_comparison_pin", "task_runs", type_="check")
    op.drop_constraint(
        "ck_task_runs_runtime_execution_identity", "task_runs", type_="check"
    )
    op.drop_column("task_runs", "comparison_mode")
    op.drop_column("task_runs", "runtime_execution_intent_id")
    op.drop_column("task_runs", "runtime_authority")
