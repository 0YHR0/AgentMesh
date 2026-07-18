"""Add governed MCP registry and capability snapshots.

Revision ID: 20260718_0018
Revises: 20260718_0017
Create Date: 2026-07-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260718_0018"
down_revision: str | None = "20260718_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mcp_servers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("owner_id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("transport", sa.String(length=32), nullable=False),
        sa.Column("endpoint_reference", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "transport IN ('MANAGED_STDIO', 'STREAMABLE_HTTP')",
            name="ck_mcp_servers_transport",
        ),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'ACTIVE', 'SUSPENDED')", name="ck_mcp_servers_status"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_mcp_servers_tenant_name"),
    )
    op.create_index(
        "ix_mcp_servers_tenant_status", "mcp_servers", ["tenant_id", "status", "created_at"]
    )
    op.create_table(
        "mcp_server_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("server_id", sa.Uuid(), nullable=False),
        sa.Column("semantic_version", sa.String(length=64), nullable=False),
        sa.Column("protocol_version", sa.String(length=32), nullable=False),
        sa.Column("configuration_digest", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoke_reason", sa.Text(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'PUBLISHED', 'REVOKED')",
            name="ck_mcp_server_versions_status",
        ),
        sa.ForeignKeyConstraint(["server_id"], ["mcp_servers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "server_id", "semantic_version", name="uq_mcp_server_versions_semantic"
        ),
    )
    op.create_index(
        "ix_mcp_server_versions_server_status",
        "mcp_server_versions",
        ["server_id", "status", "created_at"],
    )
    op.create_table(
        "mcp_tool_capabilities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("server_version_id", sa.Uuid(), nullable=False),
        sa.Column("logical_key", sa.String(length=255), nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("side_effect", sa.String(length=32), nullable=False),
        sa.Column("input_schema", postgresql.JSONB(), nullable=False),
        sa.Column("schema_digest", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "side_effect IN ('READ_ONLY', 'IDEMPOTENT_WRITE', "
            "'NON_IDEMPOTENT_WRITE', 'IRREVERSIBLE')",
            name="ck_mcp_tools_side_effect",
        ),
        sa.ForeignKeyConstraint(
            ["server_version_id"], ["mcp_server_versions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "server_version_id", "logical_key", name="uq_mcp_tools_version_logical_key"
        ),
    )
    op.create_index(
        "ix_mcp_tools_tenant_logical_key",
        "mcp_tool_capabilities",
        ["tenant_id", "logical_key"],
    )


def downgrade() -> None:
    op.drop_index("ix_mcp_tools_tenant_logical_key", table_name="mcp_tool_capabilities")
    op.drop_table("mcp_tool_capabilities")
    op.drop_index("ix_mcp_server_versions_server_status", table_name="mcp_server_versions")
    op.drop_table("mcp_server_versions")
    op.drop_index("ix_mcp_servers_tenant_status", table_name="mcp_servers")
    op.drop_table("mcp_servers")
