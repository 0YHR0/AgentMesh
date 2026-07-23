"""artifact filesystem blob storage

Revision ID: 20260723_0033
Revises: 20260723_0032
"""

import sqlalchemy as sa

from alembic import op

revision = "20260723_0033"
down_revision = "20260723_0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "artifact_versions",
        sa.Column("storage_key", sa.String(length=512), nullable=True),
    )
    op.alter_column("artifact_versions", "content", nullable=True)
    op.drop_constraint(
        "ck_artifact_versions_content_size", "artifact_versions", type_="check"
    )
    op.create_check_constraint(
        "ck_artifact_versions_content_size",
        "artifact_versions",
        "(storage_class = 'INLINE_SMALL' AND content IS NOT NULL "
        "AND storage_key IS NULL AND octet_length(content) = size_bytes) OR "
        "(storage_class = 'FILESYSTEM' AND content IS NULL AND storage_key IS NOT NULL)",
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM artifact_versions WHERE storage_class = 'FILESYSTEM'"
    )
    op.drop_constraint(
        "ck_artifact_versions_content_size", "artifact_versions", type_="check"
    )
    op.create_check_constraint(
        "ck_artifact_versions_content_size",
        "artifact_versions",
        "octet_length(content) = size_bytes",
    )
    op.alter_column("artifact_versions", "content", nullable=False)
    op.drop_column("artifact_versions", "storage_key")
