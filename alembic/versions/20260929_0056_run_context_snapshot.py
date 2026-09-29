"""Pin coordinated Run input and dependency-transfer evidence."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260929_0056"
down_revision = "20260929_0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "task_runs", sa.Column("work_item_snapshot", postgresql.JSONB(), nullable=True)
    )
    op.add_column(
        "task_runs", sa.Column("work_item_pinned_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("task_runs", "work_item_pinned_at")
    op.drop_column("task_runs", "work_item_snapshot")
