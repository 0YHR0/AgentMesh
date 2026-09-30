"""Add an optional explicit subject for conservative memory conflict assessment."""

import sqlalchemy as sa

from alembic import op

revision = "20260930_0057"
down_revision = "20260929_0056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("memory_records", sa.Column("subject_key", sa.String(128), nullable=True))


def downgrade() -> None:
    op.drop_column("memory_records", "subject_key")
