"""Add trusted A2A peers and immutable Agent Card snapshots.

Revision ID: 20260720_0019
Revises: 20260718_0018
Create Date: 2026-07-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260720_0019"
down_revision: str | None = "20260718_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "a2a_peers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("owner_id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=63), nullable=False),
        sa.Column("discovery_url", sa.String(length=2048), nullable=False),
        sa.Column("allowed_endpoint_hosts", postgresql.JSONB(), nullable=False),
        sa.Column("allowed_bindings", postgresql.JSONB(), nullable=False),
        sa.Column("trust_tier", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("active_card_snapshot_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "trust_tier IN ('RESTRICTED', 'TRUSTED', 'HIGH_ASSURANCE')",
            name="ck_a2a_peers_trust_tier",
        ),
        sa.CheckConstraint(
            "status IN ('REGISTERED', 'ACTIVE', 'SUSPENDED')",
            name="ck_a2a_peers_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_a2a_peers_tenant_name"),
    )
    op.create_index(
        "ix_a2a_peers_tenant_status", "a2a_peers", ["tenant_id", "status", "created_at"]
    )
    op.create_table(
        "a2a_agent_card_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("peer_id", sa.Uuid(), nullable=False),
        sa.Column("digest", sa.String(length=80), nullable=False),
        sa.Column("raw_card", postgresql.JSONB(), nullable=False),
        sa.Column("agent_name", sa.String(length=255), nullable=False),
        sa.Column("agent_description", sa.Text(), nullable=False),
        sa.Column("agent_version", sa.String(length=128), nullable=False),
        sa.Column("endpoints", postgresql.JSONB(), nullable=False),
        sa.Column("skills", postgresql.JSONB(), nullable=False),
        sa.Column("capabilities", postgresql.JSONB(), nullable=False),
        sa.Column("security_schemes", postgresql.JSONB(), nullable=False),
        sa.Column("signature_status", sa.String(length=32), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_etag", sa.String(length=512), nullable=True),
        sa.CheckConstraint(
            "signature_status IN ('UNSIGNED', 'PRESENT_UNVERIFIED')",
            name="ck_a2a_cards_signature_status",
        ),
        sa.ForeignKeyConstraint(["peer_id"], ["a2a_peers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_a2a_cards_peer_digest", "a2a_agent_card_snapshots", ["peer_id", "digest"]
    )
    op.create_index(
        "ix_a2a_cards_peer_fetched", "a2a_agent_card_snapshots", ["peer_id", "fetched_at"]
    )
    op.create_index(
        "ix_a2a_cards_tenant_expiry", "a2a_agent_card_snapshots", ["tenant_id", "expires_at"]
    )
    op.create_foreign_key(
        "fk_a2a_peer_active_card",
        "a2a_peers",
        "a2a_agent_card_snapshots",
        ["active_card_snapshot_id"],
        ["id"],
        ondelete="SET NULL",
        deferrable=True,
        initially="DEFERRED",
    )


def downgrade() -> None:
    op.drop_constraint("fk_a2a_peer_active_card", "a2a_peers", type_="foreignkey")
    op.drop_index("ix_a2a_cards_peer_digest", table_name="a2a_agent_card_snapshots")
    op.drop_index("ix_a2a_cards_tenant_expiry", table_name="a2a_agent_card_snapshots")
    op.drop_index("ix_a2a_cards_peer_fetched", table_name="a2a_agent_card_snapshots")
    op.drop_table("a2a_agent_card_snapshots")
    op.drop_index("ix_a2a_peers_tenant_status", table_name="a2a_peers")
    op.drop_table("a2a_peers")
