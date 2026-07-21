"""Add Agent Card snapshot discovery provenance.

Revision ID: 20260721_0025
Revises: 20260721_0024
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260721_0025"
down_revision: str | None = "20260721_0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "a2a_agent_card_snapshots",
        sa.Column("source", sa.String(length=32), nullable=False, server_default="MANUAL"),
    )
    op.add_column(
        "a2a_agent_card_snapshots",
        sa.Column("source_url", sa.String(length=2048), nullable=True),
    )
    op.create_check_constraint(
        "ck_a2a_card_snapshot_source",
        "a2a_agent_card_snapshots",
        "source IN ('MANUAL', 'DISCOVERED')",
    )
    op.alter_column("a2a_agent_card_snapshots", "source", server_default=None)


def downgrade() -> None:
    op.drop_constraint("ck_a2a_card_snapshot_source", "a2a_agent_card_snapshots", type_="check")
    op.drop_column("a2a_agent_card_snapshots", "source_url")
    op.drop_column("a2a_agent_card_snapshots", "source")
