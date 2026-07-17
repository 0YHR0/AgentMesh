"""Add durable quarantine metadata for malformed Outbox rows.

Revision ID: 20260717_0009
Revises: 20260716_0008
Create Date: 2026-07-17
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260717_0009"
down_revision: str | None = "20260716_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "outbox_events",
        sa.Column("quarantined_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_outbox_quarantine_timestamp",
        "outbox_events",
        "(status = 'QUARANTINED' AND quarantined_at IS NOT NULL) "
        "OR (status <> 'QUARANTINED' AND quarantined_at IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_outbox_quarantine_timestamp",
        "outbox_events",
        type_="check",
    )
    op.drop_column("outbox_events", "quarantined_at")
