"""Add governed recurring Company Operations.

Revision ID: 20260729_0038
Revises: 20260729_0037
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260729_0038"
down_revision = "20260729_0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "company_operations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("organization_unit_id", sa.Uuid(), nullable=False),
        sa.Column("initiative_id", sa.Uuid(), nullable=True),
        sa.Column("key", sa.String(63), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("objective_template", sa.Text(), nullable=False),
        sa.Column("input_template", postgresql.JSONB(), nullable=False),
        sa.Column("trigger_kind", sa.String(32), nullable=False),
        sa.Column("trigger_definition", postgresql.JSONB(), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("missed_policy", sa.String(32), nullable=False),
        sa.Column("catch_up_limit", sa.Integer(), nullable=False),
        sa.Column("concurrency_limit", sa.Integer(), nullable=False),
        sa.Column("maximum_runs_per_window", sa.Integer(), nullable=False),
        sa.Column("window_seconds", sa.Integer(), nullable=False),
        sa.Column("position_bindings", postgresql.JSONB(), nullable=False),
        sa.Column("tool_capability_allowlist", postgresql.JSONB(), nullable=False),
        sa.Column("budget_limit", postgresql.JSONB(), nullable=False),
        sa.Column("approval_policy_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["organization_unit_id"], ["organization_units.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["initiative_id"], ["company_initiatives.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "company_id", "key", name="uq_company_operations_company_key"
        ),
    )
    op.create_index(
        "ix_company_operations_company_status",
        "company_operations",
        ["company_id", "status"],
    )
    op.create_table(
        "company_operation_trigger_states",
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("trigger_version", sa.Integer(), nullable=False),
        sa.Column("next_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_fired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("paused_reason", sa.String(255), nullable=True),
        sa.Column("fencing_token", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["operation_id"], ["company_operations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("operation_id"),
    )
    op.create_index(
        "ix_company_operation_triggers_due",
        "company_operation_trigger_states",
        ["next_due_at"],
    )
    op.create_table(
        "company_operation_occurrences",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("operation_version", sa.Integer(), nullable=False),
        sa.Column("occurrence_key", sa.String(512), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("detail", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["operation_id"], ["company_operations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "operation_id",
            "occurrence_key",
            name="uq_company_operation_occurrences_key",
        ),
    )
    op.create_index(
        "ix_company_operation_occurrences_operation_scheduled",
        "company_operation_occurrences",
        ["operation_id", "scheduled_at"],
    )
    op.create_index(
        "ix_company_operation_occurrences_task",
        "company_operation_occurrences",
        ["task_id"],
    )
    op.create_table(
        "company_operation_exceptions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("occurrence_id", sa.Uuid(), nullable=True),
        sa.Column("code", sa.String(63), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("retryable", sa.Boolean(), nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["operation_id"], ["company_operations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["occurrence_id"],
            ["company_operation_occurrences.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_company_operation_exceptions_unresolved",
        "company_operation_exceptions",
        ["operation_id", "resolved_at"],
    )


def downgrade() -> None:
    op.drop_table("company_operation_exceptions")
    op.drop_table("company_operation_occurrences")
    op.drop_table("company_operation_trigger_states")
    op.drop_table("company_operations")
