"""Add durable Task Run pause and resume timestamps.

Revision ID: 20260716_0006
Revises: 20260716_0005
Create Date: 2026-07-16
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260716_0006"
down_revision: str | None = "20260716_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "task_runs",
        sa.Column("pause_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "task_runs",
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "task_runs",
        sa.Column("resumed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "task_runs",
        sa.Column("paused_from_status", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.execute("ALTER TABLE task_runs DROP COLUMN IF EXISTS paused_from_status")
    op.drop_column("task_runs", "resumed_at")
    op.drop_column("task_runs", "paused_at")
    op.drop_column("task_runs", "pause_requested_at")
