"""Persist the Drain provenance required for safe coordinated budget resume."""

import sqlalchemy as sa

from alembic import op

revision = "20260915_0053"
down_revision = "20260909_0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subtasks",
        sa.Column("cancellation_source", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "subtasks",
        sa.Column("canceled_by_drain_id", sa.Uuid(), nullable=True),
    )
    op.create_check_constraint(
        "ck_subtasks_cancellation_source",
        "subtasks",
        "cancellation_source IS NULL OR cancellation_source IN "
        "('budget_drain', 'control_drain', 'runtime_reconciliation', 'user')",
    )
    op.create_check_constraint(
        "ck_subtasks_cancellation_provenance",
        "subtasks",
        "(cancellation_source IS NULL AND canceled_by_drain_id IS NULL) OR "
        "(cancellation_source IS NOT NULL AND status = 'CANCELED' AND ((cancellation_source IN "
        "('budget_drain', 'control_drain') AND canceled_by_drain_id IS NOT NULL) OR "
        "(cancellation_source IN ('runtime_reconciliation', 'user') AND "
        "canceled_by_drain_id IS NULL)))",
    )
    op.create_unique_constraint(
        "uq_coordination_runtime_drains_id_task",
        "coordination_runtime_drains",
        ["id", "task_id"],
    )
    op.create_foreign_key(
        "fk_subtasks_canceled_by_drain_task",
        "subtasks",
        "coordination_runtime_drains",
        ["canceled_by_drain_id", "task_id"],
        ["id", "task_id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_subtasks_canceled_by_drain",
        "subtasks",
        ["canceled_by_drain_id", "id"],
        unique=False,
        postgresql_where=sa.text("canceled_by_drain_id IS NOT NULL"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    row = bind.execute(
        sa.text(
            "SELECT 1 FROM subtasks "
            "WHERE cancellation_source IS NOT NULL "
            "OR canceled_by_drain_id IS NOT NULL LIMIT 1"
        )
    ).first()
    if row is not None:
        raise RuntimeError(
            "Cannot downgrade 0053: Subtask cancellation provenance is in use"
        )
    op.drop_index("ix_subtasks_canceled_by_drain", table_name="subtasks")
    op.drop_constraint(
        "fk_subtasks_canceled_by_drain_task", "subtasks", type_="foreignkey"
    )
    op.drop_constraint(
        "uq_coordination_runtime_drains_id_task",
        "coordination_runtime_drains",
        type_="unique",
    )
    op.drop_constraint(
        "ck_subtasks_cancellation_provenance", "subtasks", type_="check"
    )
    op.drop_constraint(
        "ck_subtasks_cancellation_source", "subtasks", type_="check"
    )
    op.drop_column("subtasks", "canceled_by_drain_id")
    op.drop_column("subtasks", "cancellation_source")
