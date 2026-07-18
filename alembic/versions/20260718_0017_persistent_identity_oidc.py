"""Add persistent principals, external identities, and role bindings.

Revision ID: 20260718_0017
Revises: 20260718_0016
Create Date: 2026-07-18
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260718_0017"
down_revision: str | None = "20260718_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "principals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("principal_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "principal_type IN ('USER', 'SERVICE', 'AGENT', 'EXTERNAL_PEER')",
            name="ck_principals_type",
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'SUSPENDED', 'DEACTIVATED')",
            name="ck_principals_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_principals_tenant_created", "principals", ["tenant_id", "created_at", "id"]
    )
    op.create_table(
        "external_identities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("issuer", sa.String(length=512), nullable=False),
        sa.Column("subject", sa.String(length=512), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.ForeignKeyConstraint(["principal_id"], ["principals.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "issuer", "subject", name="uq_external_identities_tenant_subject"
        ),
    )
    op.create_index(
        "ix_external_identities_principal",
        "external_identities",
        ["tenant_id", "principal_id"],
    )
    op.create_table(
        "role_bindings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.String(length=128), nullable=True),
        sa.Column("revoke_reason", sa.Text(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.CheckConstraint("status IN ('ACTIVE', 'REVOKED')", name="ck_role_bindings_status"),
        sa.ForeignKeyConstraint(["principal_id"], ["principals.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_role_bindings_principal_status",
        "role_bindings",
        ["tenant_id", "principal_id", "status"],
    )
    op.create_index("ix_role_bindings_expiry", "role_bindings", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_role_bindings_expiry", table_name="role_bindings")
    op.drop_index("ix_role_bindings_principal_status", table_name="role_bindings")
    op.drop_table("role_bindings")
    op.drop_index("ix_external_identities_principal", table_name="external_identities")
    op.drop_table("external_identities")
    op.drop_index("ix_principals_tenant_created", table_name="principals")
    op.drop_table("principals")
