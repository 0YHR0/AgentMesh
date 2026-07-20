"""Add MCP workload credential bindings and leases.

Revision ID: 20260720_0022
Revises: 20260720_0021
Create Date: 2026-07-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260720_0022"
down_revision: str | None = "20260720_0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "mcp_servers",
        sa.Column(
            "authentication_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.alter_column("mcp_servers", "authentication_required", server_default=None)
    op.create_table(
        "mcp_credential_bindings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("workload_principal_id", sa.Uuid(), nullable=False),
        sa.Column("server_id", sa.Uuid(), nullable=False),
        sa.Column("server_version_id", sa.Uuid(), nullable=False),
        sa.Column("configuration_digest", sa.String(length=80), nullable=False),
        sa.Column("secret_reference_id", sa.Uuid(), nullable=False),
        sa.Column("auth_scheme", sa.String(length=32), nullable=False),
        sa.Column("audience", sa.String(length=2048), nullable=False),
        sa.Column("scopes", postgresql.JSONB(), nullable=False),
        sa.Column("environment", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'REVOKED')", name="ck_mcp_credential_bindings_status"
        ),
        sa.ForeignKeyConstraint(["workload_principal_id"], ["principals.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["server_id"], ["mcp_servers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["server_version_id"], ["mcp_server_versions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["secret_reference_id"], ["secret_references.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_mcp_credential_bindings_tenant_status",
        "mcp_credential_bindings",
        ["tenant_id", "status", "created_at"],
    )
    op.create_index(
        "uq_mcp_credential_bindings_active_target",
        "mcp_credential_bindings",
        ["workload_principal_id", "server_version_id", "environment"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )
    op.create_table(
        "mcp_credential_leases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("binding_id", sa.Uuid(), nullable=False),
        sa.Column("secret_reference_id", sa.Uuid(), nullable=False),
        sa.Column("workload_principal_id", sa.Uuid(), nullable=False),
        sa.Column("server_id", sa.Uuid(), nullable=False),
        sa.Column("server_version_id", sa.Uuid(), nullable=False),
        sa.Column("tool_invocation_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("audience", sa.String(length=2048), nullable=False),
        sa.Column("scopes", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "status IN ('REQUESTED', 'ISSUED', 'USED', 'FAILED')",
            name="ck_mcp_credential_leases_status",
        ),
        sa.ForeignKeyConstraint(
            ["binding_id"], ["mcp_credential_bindings.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["secret_reference_id"], ["secret_references.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["workload_principal_id"], ["principals.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["server_id"], ["mcp_servers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["server_version_id"], ["mcp_server_versions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["tool_invocation_id"], ["tool_invocations.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["run_id"], ["task_runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_mcp_credential_leases_tenant_status",
        "mcp_credential_leases",
        ["tenant_id", "status", "created_at"],
    )
    op.create_index(
        "ix_mcp_credential_leases_invocation",
        "mcp_credential_leases",
        ["tool_invocation_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_mcp_credential_leases_invocation", table_name="mcp_credential_leases")
    op.drop_index("ix_mcp_credential_leases_tenant_status", table_name="mcp_credential_leases")
    op.drop_table("mcp_credential_leases")
    op.drop_index("uq_mcp_credential_bindings_active_target", table_name="mcp_credential_bindings")
    op.drop_index("ix_mcp_credential_bindings_tenant_status", table_name="mcp_credential_bindings")
    op.drop_table("mcp_credential_bindings")
    op.drop_column("mcp_servers", "authentication_required")
