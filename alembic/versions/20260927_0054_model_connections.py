"""Persist tenant-scoped provider configuration and encrypted model credentials."""

import sqlalchemy as sa

from alembic import op

revision = "20260927_0054"
down_revision = "20260915_0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_connections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("endpoint", sa.String(length=512), nullable=False),
        sa.Column("credential_source", sa.String(length=32), nullable=False),
        sa.Column("credential_reference", sa.String(length=255), nullable=False),
        sa.Column("encrypted_secret", sa.LargeBinary(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "provider IN ('openai', 'deepseek')", name="ck_model_connections_provider"
        ),
        sa.CheckConstraint(
            "credential_source IN ('encrypted', 'environment')",
            name="ck_model_connections_credential_source",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_model_connections_tenant_name"),
    )
    op.create_index(
        "ix_model_connections_tenant_enabled", "model_connections", ["tenant_id", "enabled"]
    )


def downgrade() -> None:
    op.drop_index("ix_model_connections_tenant_enabled", table_name="model_connections")
    op.drop_table("model_connections")
