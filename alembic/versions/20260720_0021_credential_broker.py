"""Add workload credential references, bindings, and leases.

Revision ID: 20260720_0021
Revises: 20260720_0020
Create Date: 2026-07-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260720_0021"
down_revision: str | None = "20260720_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "secret_references",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("external_key", sa.String(length=255), nullable=False),
        sa.Column("version_selector", sa.String(length=128), nullable=True),
        sa.Column("purpose", sa.String(length=64), nullable=False),
        sa.Column("allowed_audiences", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.CheckConstraint("status IN ('ACTIVE', 'REVOKED')", name="ck_secret_references_status"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_secret_references_tenant_status",
        "secret_references",
        ["tenant_id", "status", "created_at"],
    )
    op.create_table(
        "credential_bindings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("workload_principal_id", sa.Uuid(), nullable=False),
        sa.Column("peer_id", sa.Uuid(), nullable=False),
        sa.Column("card_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("card_digest", sa.String(length=80), nullable=False),
        sa.Column("secret_reference_id", sa.Uuid(), nullable=False),
        sa.Column("scheme_name", sa.String(length=128), nullable=False),
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
        sa.CheckConstraint("status IN ('ACTIVE', 'REVOKED')", name="ck_credential_bindings_status"),
        sa.ForeignKeyConstraint(["workload_principal_id"], ["principals.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["peer_id"], ["a2a_peers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["card_snapshot_id"], ["a2a_agent_card_snapshots.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["secret_reference_id"], ["secret_references.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_credential_bindings_tenant_status",
        "credential_bindings",
        ["tenant_id", "status", "created_at"],
    )
    op.create_index(
        "uq_credential_bindings_active_target",
        "credential_bindings",
        [
            "workload_principal_id",
            "peer_id",
            "card_snapshot_id",
            "scheme_name",
            "environment",
        ],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )
    op.create_table(
        "credential_leases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("binding_id", sa.Uuid(), nullable=False),
        sa.Column("secret_reference_id", sa.Uuid(), nullable=False),
        sa.Column("workload_principal_id", sa.Uuid(), nullable=False),
        sa.Column("peer_id", sa.Uuid(), nullable=False),
        sa.Column("card_snapshot_id", sa.Uuid(), nullable=False),
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
            name="ck_credential_leases_status",
        ),
        sa.ForeignKeyConstraint(["binding_id"], ["credential_bindings.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["secret_reference_id"], ["secret_references.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["workload_principal_id"], ["principals.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["peer_id"], ["a2a_peers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["card_snapshot_id"], ["a2a_agent_card_snapshots.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["run_id"], ["task_runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_credential_leases_tenant_status",
        "credential_leases",
        ["tenant_id", "status", "created_at"],
    )
    op.create_index("ix_credential_leases_run", "credential_leases", ["run_id", "created_at"])
    op.add_column(
        "a2a_remote_task_correlations",
        sa.Column("credential_binding_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "a2a_remote_task_correlations",
        sa.Column("credential_scheme_name", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "a2a_remote_task_correlations",
        sa.Column(
            "credential_scopes",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "a2a_remote_task_correlations",
        sa.Column("last_credential_lease_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_a2a_correlations_credential_binding",
        "a2a_remote_task_correlations",
        "credential_bindings",
        ["credential_binding_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_a2a_correlations_credential_binding",
        "a2a_remote_task_correlations",
        type_="foreignkey",
    )
    op.drop_column("a2a_remote_task_correlations", "last_credential_lease_id")
    op.drop_column("a2a_remote_task_correlations", "credential_scopes")
    op.drop_column("a2a_remote_task_correlations", "credential_scheme_name")
    op.drop_column("a2a_remote_task_correlations", "credential_binding_id")
    op.drop_index("ix_credential_leases_run", table_name="credential_leases")
    op.drop_index("ix_credential_leases_tenant_status", table_name="credential_leases")
    op.drop_table("credential_leases")
    op.drop_index("uq_credential_bindings_active_target", table_name="credential_bindings")
    op.drop_index("ix_credential_bindings_tenant_status", table_name="credential_bindings")
    op.drop_table("credential_bindings")
    op.drop_index("ix_secret_references_tenant_status", table_name="secret_references")
    op.drop_table("secret_references")
