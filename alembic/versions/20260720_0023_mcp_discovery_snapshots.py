"""Add immutable MCP capability discovery snapshots.

Revision ID: 20260720_0023
Revises: 20260720_0022
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260720_0023"
down_revision: str | None = "20260720_0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mcp_discovery_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("server_id", sa.Uuid(), nullable=False),
        sa.Column("server_version_id", sa.Uuid(), nullable=False),
        sa.Column("configuration_digest", sa.String(length=80), nullable=False),
        sa.Column("protocol_version", sa.String(length=32), nullable=False),
        sa.Column("server_name", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("capability_digest", sa.String(length=80), nullable=True),
        sa.Column(
            "discovered_tools",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.CheckConstraint(
            "status IN ('COMPATIBLE', 'EXPANDED', 'INCOMPATIBLE', 'FAILED')",
            name="ck_mcp_discovery_snapshots_status",
        ),
        sa.ForeignKeyConstraint(["server_id"], ["mcp_servers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["server_version_id"], ["mcp_server_versions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_mcp_discovery_snapshots_version_fetched",
        "mcp_discovery_snapshots",
        ["server_version_id", "fetched_at"],
    )
    op.create_index(
        "ix_mcp_discovery_snapshots_tenant_status",
        "mcp_discovery_snapshots",
        ["tenant_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_mcp_discovery_snapshots_tenant_status",
        table_name="mcp_discovery_snapshots",
    )
    op.drop_index(
        "ix_mcp_discovery_snapshots_version_fetched",
        table_name="mcp_discovery_snapshots",
    )
    op.drop_table("mcp_discovery_snapshots")
