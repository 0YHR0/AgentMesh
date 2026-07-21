"""Add durable A2A remote cancellation state.

Revision ID: 20260721_0027
Revises: 20260721_0026
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260721_0027"
down_revision: str | None = "20260721_0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_a2a_correlations_status", "a2a_remote_task_correlations", type_="check"
    )
    op.create_check_constraint(
        "ck_a2a_correlations_status",
        "a2a_remote_task_correlations",
        "status IN ('PREPARED', 'SENDING', 'WAITING_REMOTE', 'OUTCOME_UNKNOWN', "
        "'INTERVENTION_REQUIRED', 'CANCELING', 'CANCEL_PENDING', "
        "'CANCEL_OUTCOME_UNKNOWN', 'COMPLETED', 'FAILED', 'REJECTED', 'CANCELED')",
    )
    op.add_column(
        "a2a_remote_task_correlations",
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "a2a_remote_task_correlations",
        sa.Column("cancel_request_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "a2a_remote_task_correlations",
        sa.Column("cancel_request_digest", sa.String(length=80), nullable=True),
    )
    op.create_check_constraint(
        "ck_a2a_correlations_cancel_count",
        "a2a_remote_task_correlations",
        "cancel_request_count >= 0",
    )
    op.drop_index("ix_a2a_correlations_due_poll", table_name="a2a_remote_task_correlations")
    op.create_index(
        "ix_a2a_correlations_due_poll",
        "a2a_remote_task_correlations",
        ["tenant_id", "next_poll_at", "poll_lease_expires_at"],
        unique=False,
        postgresql_where=sa.text(
            "status IN ('WAITING_REMOTE', 'CANCELING', 'CANCEL_PENDING', "
            "'CANCEL_OUTCOME_UNKNOWN') AND remote_task_id IS NOT NULL"
        ),
    )
    op.alter_column("a2a_remote_task_correlations", "cancel_request_count", server_default=None)


def downgrade() -> None:
    op.execute(
        "UPDATE a2a_remote_task_correlations SET status = 'WAITING_REMOTE', "
        "poll_lease_owner = NULL, poll_lease_expires_at = NULL "
        "WHERE status IN ('CANCELING', 'CANCEL_PENDING', 'CANCEL_OUTCOME_UNKNOWN')"
    )
    op.drop_index("ix_a2a_correlations_due_poll", table_name="a2a_remote_task_correlations")
    op.create_index(
        "ix_a2a_correlations_due_poll",
        "a2a_remote_task_correlations",
        ["tenant_id", "next_poll_at", "poll_lease_expires_at"],
        unique=False,
        postgresql_where=sa.text("status = 'WAITING_REMOTE' AND remote_task_id IS NOT NULL"),
    )
    op.drop_constraint(
        "ck_a2a_correlations_cancel_count",
        "a2a_remote_task_correlations",
        type_="check",
    )
    op.drop_constraint(
        "ck_a2a_correlations_status", "a2a_remote_task_correlations", type_="check"
    )
    op.create_check_constraint(
        "ck_a2a_correlations_status",
        "a2a_remote_task_correlations",
        "status IN ('PREPARED', 'SENDING', 'WAITING_REMOTE', 'OUTCOME_UNKNOWN', "
        "'INTERVENTION_REQUIRED', 'COMPLETED', 'FAILED', 'REJECTED', 'CANCELED')",
    )
    op.drop_column("a2a_remote_task_correlations", "cancel_request_digest")
    op.drop_column("a2a_remote_task_correlations", "cancel_request_count")
    op.drop_column("a2a_remote_task_correlations", "cancel_requested_at")
