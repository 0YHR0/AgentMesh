"""Expand Runtime storage for orchestrated reader compatibility.

This revision is deliberately expand-only.  It adds the immutable evidence
tables and the lifecycle worker columns, but does not schedule, claim, or
otherwise write any new lifecycle values.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260825_0049"
down_revision = "20260821_0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runtime_assignment_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column(
            "runtime_execution_id",
            sa.Uuid(),
            sa.ForeignKey("runtime_executions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("contract_name", sa.String(length=128), nullable=False),
        sa.Column("contract_major", sa.Integer(), nullable=False),
        sa.Column("assignment_id", sa.Uuid(), nullable=False),
        sa.Column("assignment_digest", sa.String(length=64), nullable=False),
        sa.Column("canonical_payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("contract_major >= 1", name="ck_runtime_assignment_snapshot_major"),
        sa.CheckConstraint(
            "assignment_digest ~ '^[0-9a-f]{64}$'",
            name="ck_runtime_assignment_snapshot_digest",
        ),
        sa.CheckConstraint(
            "octet_length(canonical_payload::text) <= 262144",
            name="ck_runtime_assignment_snapshot_payload_size",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "runtime_execution_id", name="uq_runtime_assignment_snapshot_execution"
        ),
    )
    op.create_index(
        "ix_runtime_assignment_snapshots_tenant_created",
        "runtime_assignment_snapshots",
        ["tenant_id", "created_at"],
    )

    op.create_table(
        "runtime_handle_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column(
            "runtime_execution_id",
            sa.Uuid(),
            sa.ForeignKey("runtime_executions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("handle_digest", sa.String(length=64), nullable=False),
        sa.Column("canonical_payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "handle_digest ~ '^[0-9a-f]{64}$'", name="ck_runtime_handle_snapshot_digest"
        ),
        sa.CheckConstraint(
            "octet_length(canonical_payload::text) <= 65536",
            name="ck_runtime_handle_snapshot_payload_size",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("runtime_execution_id", name="uq_runtime_handle_snapshot_execution"),
    )
    op.create_index(
        "ix_runtime_handle_snapshots_tenant_created",
        "runtime_handle_snapshots",
        ["tenant_id", "created_at"],
    )

    op.create_table(
        "runtime_integrity_incidents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column(
            "runtime_execution_id",
            sa.Uuid(),
            sa.ForeignKey("runtime_executions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("accepted_observation_id", sa.String(length=512), nullable=False),
        sa.Column("accepted_observation_digest", sa.String(length=64), nullable=False),
        sa.Column("accepted_phase", sa.String(length=32), nullable=False),
        sa.Column("conflicting_observation_id", sa.String(length=512), nullable=False),
        sa.Column("conflicting_observation_digest", sa.String(length=64), nullable=False),
        sa.Column("conflicting_phase", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=4096), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "accepted_observation_digest ~ '^[0-9a-f]{64}$' AND "
            "conflicting_observation_digest ~ '^[0-9a-f]{64}$'",
            name="ck_runtime_integrity_incident_digests",
        ),
        sa.CheckConstraint(
            "accepted_phase IN ('SUCCEEDED', 'FAILED', 'CANCELED', 'TIMED_OUT', 'LOST') AND "
            "conflicting_phase IN ('SUCCEEDED', 'FAILED', 'CANCELED', 'TIMED_OUT', 'LOST')",
            name="ck_runtime_integrity_incident_terminal_phases",
        ),
        sa.CheckConstraint(
            "status IN ('OPEN', 'ACKNOWLEDGED', 'ESCALATED')",
            name="ck_runtime_integrity_incident_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "runtime_execution_id",
            "accepted_observation_digest",
            "conflicting_observation_digest",
            name="uq_runtime_integrity_incident_conflict",
        ),
    )
    op.create_index(
        "ix_runtime_integrity_incidents_tenant_status",
        "runtime_integrity_incidents",
        ["tenant_id", "status", "created_at"],
    )
    op.create_index(
        "ix_runtime_integrity_incidents_execution_created",
        "runtime_integrity_incidents",
        ["runtime_execution_id", "created_at"],
    )

    op.add_column(
        "runtime_lifecycle_operations",
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "runtime_lifecycle_operations",
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "runtime_lifecycle_operations",
        sa.Column("claim_token", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "runtime_lifecycle_operations",
        sa.Column("claim_acquired_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "runtime_lifecycle_operations",
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "runtime_lifecycle_operations",
        sa.Column("last_error_code", sa.String(length=128), nullable=True),
    )
    op.create_check_constraint(
        "ck_runtime_lifecycle_attempt_count",
        "runtime_lifecycle_operations",
        "attempt_count >= 0",
    )
    op.create_check_constraint(
        "ck_runtime_lifecycle_claim_triple",
        "runtime_lifecycle_operations",
        "(claim_token IS NULL AND claim_acquired_at IS NULL AND claim_expires_at IS NULL) OR "
        "(claim_token IS NOT NULL AND claim_acquired_at IS NOT NULL AND "
        "claim_expires_at IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_runtime_lifecycle_claim_expiry",
        "runtime_lifecycle_operations",
        "claim_expires_at IS NULL OR claim_expires_at > claim_acquired_at",
    )
    op.create_index(
        "ix_runtime_lifecycle_due",
        "runtime_lifecycle_operations",
        ["status", "next_attempt_at", "deadline"],
    )


def _refuse_if_written() -> None:
    """Refuse loss of any A4.2a writer marker before changing the schema."""

    bind = op.get_bind()
    row = bind.execute(
        sa.text(
            "SELECT 1 FROM runtime_lifecycle_operations WHERE "
            "attempt_count <> 0 OR next_attempt_at IS NOT NULL OR "
            "claim_token IS NOT NULL OR claim_acquired_at IS NOT NULL OR "
            "claim_expires_at IS NOT NULL OR last_error_code IS NOT NULL LIMIT 1"
        )
    ).first()
    if row is not None:
        raise RuntimeError(
            "Cannot downgrade 0049: Runtime lifecycle writer markers exist; "
            "drain A4.2 lifecycle operations before retrying"
        )
    for table in (
        "runtime_assignment_snapshots",
        "runtime_handle_snapshots",
        "runtime_integrity_incidents",
    ):
        row = bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first()
        if row is not None:
            raise RuntimeError(f"Cannot downgrade 0049: {table} contains rows; refusing data loss")


def downgrade() -> None:
    _refuse_if_written()

    op.drop_index("ix_runtime_lifecycle_due", table_name="runtime_lifecycle_operations")
    op.drop_constraint(
        "ck_runtime_lifecycle_claim_expiry", "runtime_lifecycle_operations", type_="check"
    )
    op.drop_constraint(
        "ck_runtime_lifecycle_claim_triple", "runtime_lifecycle_operations", type_="check"
    )
    op.drop_constraint(
        "ck_runtime_lifecycle_attempt_count", "runtime_lifecycle_operations", type_="check"
    )
    for column in (
        "last_error_code",
        "claim_expires_at",
        "claim_acquired_at",
        "claim_token",
        "next_attempt_at",
        "attempt_count",
    ):
        op.drop_column("runtime_lifecycle_operations", column)

    op.drop_index(
        "ix_runtime_integrity_incidents_execution_created",
        table_name="runtime_integrity_incidents",
    )
    op.drop_index(
        "ix_runtime_integrity_incidents_tenant_status",
        table_name="runtime_integrity_incidents",
    )
    op.drop_table("runtime_integrity_incidents")
    op.drop_index(
        "ix_runtime_handle_snapshots_tenant_created", table_name="runtime_handle_snapshots"
    )
    op.drop_table("runtime_handle_snapshots")
    op.drop_index(
        "ix_runtime_assignment_snapshots_tenant_created",
        table_name="runtime_assignment_snapshots",
    )
    op.drop_table("runtime_assignment_snapshots")
