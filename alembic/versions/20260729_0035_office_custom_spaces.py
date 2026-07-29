"""Add shared custom Office spaces.

Revision ID: 20260729_0035
Revises: 20260729_0034
"""

import sqlalchemy as sa

from alembic import op

revision = "20260729_0035"
down_revision = "20260729_0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "office_custom_spaces",
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("key", sa.String(length=63), nullable=False),
        sa.Column("name", sa.String(length=40), nullable=False),
        sa.Column("style", sa.String(length=32), nullable=False),
        sa.Column("color", sa.String(length=7), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "position >= 0 AND position < 8",
            name="ck_office_custom_spaces_position",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "key",
            name="pk_office_custom_spaces",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "position",
            name="uq_office_custom_spaces_position",
        ),
    )


def downgrade() -> None:
    op.drop_table("office_custom_spaces")
