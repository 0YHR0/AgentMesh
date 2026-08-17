"""Expand schema for Runtime Registry and execution evidence.

This migration is additive. Existing TaskRun rows remain valid with nullable runtime bindings;
the runtime tables are not used by the legacy worker while the managed_agent_runtime gate is off.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260817_0045"
down_revision = "20260803_0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runtime_registrations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=True),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column(
            "owner_principal_id",
            sa.Uuid(),
            sa.ForeignKey("principals.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("visibility", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("default_version_id", sa.Uuid(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "visibility IN ('platform', 'tenant', 'private')",
            name="ck_runtime_registrations_visibility",
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'SUSPENDED', 'REVOKED')",
            name="ck_runtime_registrations_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_runtime_registrations_version"),
        sa.CheckConstraint(
            "(visibility = 'platform' AND tenant_id IS NULL) OR "
            "(visibility IN ('tenant', 'private') AND tenant_id IS NOT NULL)",
            name="ck_runtime_registrations_scope",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_runtime_registrations_platform_name",
        "runtime_registrations",
        ["name"],
        unique=True,
        postgresql_where=sa.text("visibility = 'platform'"),
    )
    op.create_index(
        "uq_runtime_registrations_tenant_name",
        "runtime_registrations",
        ["tenant_id", "name"],
        unique=True,
        postgresql_where=sa.text("visibility = 'tenant'"),
    )
    op.create_index(
        "uq_runtime_registrations_private_owner_name",
        "runtime_registrations",
        ["owner_principal_id", "name"],
        unique=True,
        postgresql_where=sa.text("visibility = 'private'"),
    )
    op.create_index(
        "ix_runtime_registrations_tenant_status",
        "runtime_registrations",
        ["tenant_id", "status", "created_at"],
    )

    op.create_table(
        "runtime_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "runtime_id",
            sa.Uuid(),
            sa.ForeignKey("runtime_registrations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("api_version", sa.Integer(), nullable=False),
        sa.Column("adapter_kind", sa.String(length=128), nullable=False),
        sa.Column("artifact_digest", sa.String(length=64), nullable=False),
        sa.Column("configuration_digest", sa.String(length=64), nullable=False),
        sa.Column("descriptor", postgresql.JSONB(), nullable=False),
        sa.Column("trust_profile", sa.String(length=32), nullable=False),
        sa.Column("compatibility", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("api_version = 1", name="ck_runtime_versions_api_version"),
        sa.CheckConstraint(
            "artifact_digest ~ '^[0-9a-f]{64}$' AND configuration_digest ~ '^[0-9a-f]{64}$'",
            name="ck_runtime_versions_digests",
        ),
        sa.CheckConstraint(
            "trust_profile IN ('built_in', 'trusted_process', 'isolated', 'remote')",
            name="ck_runtime_versions_trust_profile",
        ),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'PUBLISHED', 'DEPRECATED', 'REVOKED')",
            name="ck_runtime_versions_status",
        ),
        sa.CheckConstraint(
            "(status = 'PUBLISHED' AND published_at IS NOT NULL) OR (status <> 'PUBLISHED')",
            name="ck_runtime_versions_publication",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "runtime_id",
            "artifact_digest",
            "configuration_digest",
            name="uq_runtime_versions_immutable_identity",
        ),
    )
    op.create_foreign_key(
        "fk_runtime_registrations_default_version",
        "runtime_registrations",
        "runtime_versions",
        ["default_version_id"],
        ["id"],
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_index(
        "ix_runtime_versions_runtime_status",
        "runtime_versions",
        ["runtime_id", "status", "created_at"],
    )

    op.create_table(
        "runtime_executions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column(
            "run_id", sa.Uuid(), sa.ForeignKey("task_runs.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column(
            "runtime_version_id",
            sa.Uuid(),
            sa.ForeignKey("runtime_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("assignment_id", sa.Uuid(), nullable=False),
        sa.Column("assignment_digest", sa.String(length=64), nullable=False),
        sa.Column("dispatch_key", sa.String(length=512), nullable=False),
        sa.Column("dispatch_digest", sa.String(length=64), nullable=False),
        sa.Column("provider_execution_ref", sa.String(length=4096), nullable=True),
        sa.Column("provider_generation", sa.String(length=256), nullable=True),
        sa.Column("phase", sa.String(length=32), nullable=False),
        sa.Column("current_owner_attempt_id", sa.Uuid(), nullable=True),
        sa.Column("current_fencing_token", sa.Integer(), nullable=True),
        sa.Column("provider_sequence", sa.BigInteger(), nullable=True),
        sa.Column("checkpoint_ref", sa.String(length=4096), nullable=True),
        sa.Column("workspace_ref", sa.String(length=4096), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("version >= 1", name="ck_runtime_executions_version"),
        sa.CheckConstraint(
            "assignment_digest ~ '^[0-9a-f]{64}$' AND dispatch_digest ~ '^[0-9a-f]{64}$'",
            name="ck_runtime_executions_digests",
        ),
        sa.CheckConstraint(
            "phase IN ('PREPARED', 'DISPATCHING', 'ACCEPTED', 'RUNNING', 'WAITING_INPUT', "
            "'WAITING_APPROVAL', 'PAUSE_REQUESTED', 'PAUSED', 'CANCEL_REQUESTED', "
            "'SUCCEEDED', 'FAILED', 'CANCELED', 'TIMED_OUT', 'LOST', 'OUTCOME_UNKNOWN')",
            name="ck_runtime_executions_phase",
        ),
        sa.CheckConstraint(
            "current_fencing_token IS NULL OR current_fencing_token >= 0",
            name="ck_runtime_executions_fencing",
        ),
        sa.CheckConstraint(
            "provider_sequence IS NULL OR provider_sequence >= 0",
            name="ck_runtime_executions_provider_sequence",
        ),
        sa.CheckConstraint(
            "(phase IN ('SUCCEEDED', 'FAILED', 'CANCELED', 'TIMED_OUT', 'LOST', 'OUTCOME_UNKNOWN') "
            "AND terminal_at IS NOT NULL) OR "
            "(phase NOT IN ('SUCCEEDED', 'FAILED', 'CANCELED', 'TIMED_OUT', 'LOST', "
            "'OUTCOME_UNKNOWN') "
            "AND terminal_at IS NULL)",
            name="ck_runtime_executions_terminal_at",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "dispatch_key", name="uq_runtime_executions_dispatch_key"),
    )
    op.create_foreign_key(
        "fk_runtime_executions_owner_attempt",
        "runtime_executions",
        "task_attempts",
        ["current_owner_attempt_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "uq_runtime_executions_one_active_per_run",
        "runtime_executions",
        ["run_id"],
        unique=True,
        postgresql_where=sa.text(
            "phase NOT IN ('SUCCEEDED', 'FAILED', 'CANCELED', 'TIMED_OUT', 'LOST', "
            "'OUTCOME_UNKNOWN')"
        ),
    )
    op.create_index(
        "ix_runtime_executions_tenant_updated",
        "runtime_executions",
        ["tenant_id", "updated_at"],
    )

    op.create_table(
        "runtime_ownership_history",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column(
            "runtime_execution_id",
            sa.Uuid(),
            sa.ForeignKey("runtime_executions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "attempt_id",
            sa.Uuid(),
            sa.ForeignKey("task_attempts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("fencing_token", sa.Integer(), nullable=False),
        sa.Column(
            "previous_attempt_id",
            sa.Uuid(),
            sa.ForeignKey("task_attempts.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("release_reason", sa.String(length=64), nullable=True),
        sa.Column("claim_reason", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "runtime_execution_id", "fencing_token", name="uq_runtime_ownership_execution_fence"
        ),
    )
    op.create_index(
        "ix_runtime_ownership_execution_claimed",
        "runtime_ownership_history",
        ["runtime_execution_id", "claimed_at"],
    )

    op.create_table(
        "runtime_observations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column(
            "runtime_execution_id",
            sa.Uuid(),
            sa.ForeignKey("runtime_executions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("observation_id", sa.String(length=512), nullable=False),
        sa.Column("observation_digest", sa.String(length=64), nullable=False),
        sa.Column("assignment_id", sa.Uuid(), nullable=False),
        sa.Column("assignment_digest", sa.String(length=64), nullable=False),
        sa.Column("provider_event_id", sa.String(length=512), nullable=True),
        sa.Column("provider_sequence", sa.BigInteger(), nullable=True),
        sa.Column("phase", sa.String(length=32), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("safe_summary", sa.String(length=4096), nullable=True),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
        sa.Column("processing_outcome", sa.String(length=32), nullable=False),
        sa.Column("processing_version", sa.Integer(), nullable=False, server_default="1"),
        sa.CheckConstraint(
            "processing_outcome IN ('APPLIED', 'DUPLICATE', 'GAP', 'STALE_OWNER', 'CONFLICT')",
            name="ck_runtime_observations_outcome",
        ),
        sa.CheckConstraint(
            "provider_sequence IS NULL OR provider_sequence >= 0",
            name="ck_runtime_observations_sequence",
        ),
        sa.CheckConstraint(
            "observation_digest ~ '^[0-9a-f]{64}$' AND assignment_digest ~ '^[0-9a-f]{64}$'",
            name="ck_runtime_observations_digests",
        ),
        sa.CheckConstraint(
            "phase IN ('PREPARED', 'DISPATCHING', 'ACCEPTED', 'RUNNING', 'WAITING_INPUT', "
            "'WAITING_APPROVAL', 'PAUSE_REQUESTED', 'PAUSED', 'CANCEL_REQUESTED', "
            "'SUCCEEDED', 'FAILED', 'CANCELED', 'TIMED_OUT', 'LOST', 'OUTCOME_UNKNOWN')",
            name="ck_runtime_observations_phase",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_runtime_observations_execution_received",
        "runtime_observations",
        ["runtime_execution_id", "received_at"],
    )
    op.create_index(
        "ix_runtime_observations_execution_identity",
        "runtime_observations",
        ["runtime_execution_id", "observation_id"],
    )
    op.create_index(
        "ix_runtime_observations_execution_digest",
        "runtime_observations",
        ["runtime_execution_id", "observation_digest"],
    )

    op.create_table(
        "runtime_lifecycle_operations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column(
            "runtime_execution_id",
            sa.Uuid(),
            sa.ForeignKey("runtime_executions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("operation_id", sa.String(length=512), nullable=False),
        sa.Column("operation", sa.String(length=16), nullable=False),
        sa.Column("intent_digest", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("receipt_summary", postgresql.JSONB(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "operation IN ('cancel', 'pause', 'resume')", name="ck_runtime_lifecycle_operation"
        ),
        sa.CheckConstraint(
            "status IN ('REQUESTED', 'ACCEPTED', 'REJECTED', 'EXPIRED')",
            name="ck_runtime_lifecycle_status",
        ),
        sa.CheckConstraint(
            "intent_digest ~ '^[0-9a-f]{64}$'", name="ck_runtime_lifecycle_digest"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "runtime_execution_id", "operation_id", name="uq_runtime_lifecycle_operation"
        ),
    )
    op.create_index(
        "ix_runtime_lifecycle_tenant_status",
        "runtime_lifecycle_operations",
        ["tenant_id", "status", "deadline"],
    )

    op.add_column("task_runs", sa.Column("runtime_version_id", sa.Uuid(), nullable=True))
    op.add_column("task_runs", sa.Column("runtime_execution_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_task_runs_runtime_version",
        "task_runs",
        "runtime_versions",
        ["runtime_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_task_runs_runtime_execution",
        "task_runs",
        "runtime_executions",
        ["runtime_execution_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_task_runs_runtime_version", "task_runs", ["runtime_version_id"])
    op.create_index("ix_task_runs_runtime_execution", "task_runs", ["runtime_execution_id"])


def downgrade() -> None:
    op.drop_index("ix_task_runs_runtime_execution", table_name="task_runs")
    op.drop_index("ix_task_runs_runtime_version", table_name="task_runs")
    op.drop_constraint("fk_task_runs_runtime_execution", "task_runs", type_="foreignkey")
    op.drop_constraint("fk_task_runs_runtime_version", "task_runs", type_="foreignkey")
    op.drop_column("task_runs", "runtime_execution_id")
    op.drop_column("task_runs", "runtime_version_id")
    op.drop_index("ix_runtime_lifecycle_tenant_status", table_name="runtime_lifecycle_operations")
    op.drop_table("runtime_lifecycle_operations")
    op.drop_index("ix_runtime_observations_execution_digest", table_name="runtime_observations")
    op.drop_index("ix_runtime_observations_execution_identity", table_name="runtime_observations")
    op.drop_index("ix_runtime_observations_execution_received", table_name="runtime_observations")
    op.drop_table("runtime_observations")
    op.drop_index("ix_runtime_ownership_execution_claimed", table_name="runtime_ownership_history")
    op.drop_table("runtime_ownership_history")
    op.drop_index("ix_runtime_executions_tenant_updated", table_name="runtime_executions")
    op.drop_index("uq_runtime_executions_one_active_per_run", table_name="runtime_executions")
    op.drop_constraint(
        "fk_runtime_executions_owner_attempt", "runtime_executions", type_="foreignkey"
    )
    op.drop_table("runtime_executions")
    op.drop_index("ix_runtime_versions_runtime_status", table_name="runtime_versions")
    op.drop_constraint(
        "fk_runtime_registrations_default_version", "runtime_registrations", type_="foreignkey"
    )
    op.drop_table("runtime_versions")
    op.drop_index("ix_runtime_registrations_tenant_status", table_name="runtime_registrations")
    op.drop_index("uq_runtime_registrations_private_owner_name", table_name="runtime_registrations")
    op.drop_index("uq_runtime_registrations_tenant_name", table_name="runtime_registrations")
    op.drop_index("uq_runtime_registrations_platform_name", table_name="runtime_registrations")
    op.drop_table("runtime_registrations")
