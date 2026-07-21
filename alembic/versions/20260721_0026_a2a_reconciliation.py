"""Add durable A2A reconciliation scheduling and leases.

Revision ID: 20260721_0026
Revises: 20260721_0025
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260721_0026"
down_revision: str | None = "20260721_0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "a2a_remote_task_correlations",
        sa.Column("poll_failure_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "a2a_remote_task_correlations",
        sa.Column("next_poll_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "a2a_remote_task_correlations",
        sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "a2a_remote_task_correlations",
        sa.Column("poll_lease_owner", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "a2a_remote_task_correlations",
        sa.Column("poll_lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_a2a_correlations_poll_failure_count",
        "a2a_remote_task_correlations",
        "poll_failure_count >= 0",
    )
    op.create_index(
        "ix_a2a_correlations_due_poll",
        "a2a_remote_task_correlations",
        ["tenant_id", "next_poll_at", "poll_lease_expires_at"],
        unique=False,
        postgresql_where=sa.text("status = 'WAITING_REMOTE' AND remote_task_id IS NOT NULL"),
    )
    op.execute(
        "UPDATE a2a_remote_task_correlations "
        "SET next_poll_at = updated_at "
        "WHERE status = 'WAITING_REMOTE' AND remote_task_id IS NOT NULL"
    )
    op.alter_column("a2a_remote_task_correlations", "poll_failure_count", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_a2a_correlations_due_poll", table_name="a2a_remote_task_correlations")
    op.drop_constraint(
        "ck_a2a_correlations_poll_failure_count",
        "a2a_remote_task_correlations",
        type_="check",
    )
    op.drop_column("a2a_remote_task_correlations", "poll_lease_expires_at")
    op.drop_column("a2a_remote_task_correlations", "poll_lease_owner")
    op.drop_column("a2a_remote_task_correlations", "last_polled_at")
    op.drop_column("a2a_remote_task_correlations", "next_poll_at")
    op.drop_column("a2a_remote_task_correlations", "poll_failure_count")
