"""Add atomic Company Pack upgrade audit state.

Revision ID: 20260803_0044
Revises: 20260730_0043
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260803_0044"
down_revision = "20260730_0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "company_pack_installations",
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "company_pack_installations",
        sa.Column("upgraded_by", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "company_pack_installations",
        sa.Column("upgraded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.alter_column("company_pack_installations", "revision", server_default=None)
    op.create_table(
        "company_pack_upgrades",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("installation_id", sa.Uuid(), nullable=False),
        sa.Column("pack_key", sa.String(length=63), nullable=False),
        sa.Column("from_version", sa.String(length=32), nullable=False),
        sa.Column("from_digest", sa.String(length=64), nullable=False),
        sa.Column("to_version", sa.String(length=32), nullable=False),
        sa.Column("to_digest", sa.String(length=64), nullable=False),
        sa.Column("upgraded_by", sa.String(length=128), nullable=False),
        sa.Column("resource_changes", postgresql.JSONB(), nullable=False),
        sa.Column("migrated_object_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "migrated_object_count >= 0", name="ck_company_pack_upgrades_object_count"
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["installation_id"], ["company_pack_installations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "installation_id", "to_digest", name="uq_company_pack_upgrades_target"
        ),
    )
    op.create_index(
        "ix_company_pack_upgrades_company_created",
        "company_pack_upgrades",
        ["company_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_company_pack_upgrades_company_created", table_name="company_pack_upgrades"
    )
    op.drop_table("company_pack_upgrades")
    op.drop_column("company_pack_installations", "upgraded_at")
    op.drop_column("company_pack_installations", "upgraded_by")
    op.drop_column("company_pack_installations", "revision")
