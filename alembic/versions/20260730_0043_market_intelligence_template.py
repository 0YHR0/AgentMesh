"""Store Company Pack installation configuration.

Revision ID: 20260730_0043
Revises: 20260729_0042
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260730_0043"
down_revision = "20260729_0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "company_pack_installations",
        sa.Column(
            "configuration",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.alter_column(
        "company_pack_installations", "configuration", server_default=None
    )


def downgrade() -> None:
    op.drop_column("company_pack_installations", "configuration")
