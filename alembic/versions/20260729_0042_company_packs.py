"""Add declarative Company Packs.

Revision ID: 20260729_0042
Revises: 20260729_0041
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260729_0042"
down_revision = "20260729_0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "company_packs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(63), nullable=False),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("manifest", postgresql.JSONB(), nullable=False),
        sa.Column("required_features", postgresql.JSONB(), nullable=False),
        sa.Column("dependencies", postgresql.JSONB(), nullable=False),
        sa.Column("content_digest", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key", "version", name="uq_company_packs_key_version"),
        sa.UniqueConstraint("content_digest", name="uq_company_packs_digest"),
    )
    op.create_index(
        "ix_company_packs_status_kind", "company_packs", ["status", "kind"]
    )
    op.create_table(
        "company_pack_installations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("pack_id", sa.Uuid(), nullable=False),
        sa.Column("pack_key", sa.String(63), nullable=False),
        sa.Column("pack_version", sa.String(32), nullable=False),
        sa.Column("pack_digest", sa.String(64), nullable=False),
        sa.Column("installed_by", sa.String(128), nullable=False),
        sa.Column("resource_refs", postgresql.JSONB(), nullable=False),
        sa.Column("installed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["pack_id"], ["company_packs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "company_id", "pack_key", name="uq_company_pack_installations_key"
        ),
    )
    op.create_index(
        "ix_company_pack_installations_company",
        "company_pack_installations",
        ["company_id", "installed_at"],
    )


def downgrade() -> None:
    op.drop_table("company_pack_installations")
    op.drop_table("company_packs")
