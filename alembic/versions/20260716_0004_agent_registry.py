"""Add the Agent Registry and immutable Run bindings.

Revision ID: 20260716_0004
Revises: 20260716_0003
Create Date: 2026-07-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260716_0004"
down_revision: str | None = "20260716_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_definitions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("owner_id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=63), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("visibility", sa.String(length=32), nullable=False),
        sa.Column("lifecycle", sa.String(length=32), nullable=False),
        sa.Column("default_version_id", sa.Uuid(), nullable=True),
        sa.Column("tags", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_agent_definition_tenant_name"),
    )
    op.create_index(
        "ix_agent_definitions_tenant_created",
        "agent_definitions",
        ["tenant_id", "created_at"],
    )

    op.create_table(
        "agent_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("definition_id", sa.Uuid(), nullable=False),
        sa.Column("semantic_version", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("content_digest", sa.String(length=80), nullable=True),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=False),
        sa.Column(
            "declared_capabilities",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "verified_capabilities",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("input_schema", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("output_schema", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("model_policy", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("tool_profile", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("knowledge_profile", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("policy_profile", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("risk_class", sa.String(length=32), nullable=False),
        sa.Column("data_classification_ceiling", sa.String(length=32), nullable=False),
        sa.Column("resource_defaults", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("runtime_adapter", sa.String(length=128), nullable=False),
        sa.Column("artifact_digest", sa.String(length=255), nullable=True),
        sa.Column("execution_modes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("compatibility", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoke_reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["definition_id"], ["agent_definitions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "definition_id",
            "semantic_version",
            name="uq_agent_version_definition_semver",
        ),
    )
    op.create_index(
        "ix_agent_versions_definition_status",
        "agent_versions",
        ["definition_id", "status"],
    )
    op.create_index("ix_agent_versions_content_digest", "agent_versions", ["content_digest"])
    op.create_foreign_key(
        "fk_agent_definition_default_version",
        "agent_definitions",
        "agent_versions",
        ["default_version_id"],
        ["id"],
        ondelete="SET NULL",
        deferrable=True,
        initially="DEFERRED",
    )

    op.create_table(
        "capabilities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("version", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("input_schema", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("output_schema", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "evidence_requirements",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "key", "version", name="uq_capability_tenant_key_version"),
    )
    op.create_index("ix_capabilities_tenant_key", "capabilities", ["tenant_id", "key"])

    op.create_table(
        "agent_deployments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("agent_version_id", sa.Uuid(), nullable=False),
        sa.Column("environment", sa.String(length=128), nullable=False),
        sa.Column("runtime_kind", sa.String(length=128), nullable=False),
        sa.Column("remote_peer_id", sa.String(length=255), nullable=True),
        sa.Column("endpoint_reference", sa.String(length=512), nullable=True),
        sa.Column("desired_status", sa.String(length=32), nullable=False),
        sa.Column("current_status", sa.String(length=32), nullable=False),
        sa.Column("traffic_weight", sa.Integer(), nullable=False),
        sa.Column("region", sa.String(length=128), nullable=True),
        sa.Column("rollout_policy", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_version_id"], ["agent_versions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_agent_deployments_version_environment",
        "agent_deployments",
        ["agent_version_id", "environment"],
    )

    op.create_table(
        "agent_instances",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("deployment_id", sa.Uuid(), nullable=False),
        sa.Column("external_instance_id", sa.String(length=255), nullable=False),
        sa.Column("health", sa.String(length=32), nullable=False),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("capacity_slots", sa.Integer(), nullable=False),
        sa.Column("active_slots", sa.Integer(), nullable=False),
        sa.Column("protocol_endpoint", sa.String(length=512), nullable=True),
        sa.Column("lease_epoch", sa.Integer(), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(["deployment_id"], ["agent_deployments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "deployment_id",
            "external_instance_id",
            name="uq_agent_instance_deployment_external",
        ),
    )
    op.create_index(
        "ix_agent_instances_deployment_health",
        "agent_instances",
        ["deployment_id", "health"],
    )

    op.add_column("task_runs", sa.Column("agent_version_id", sa.Uuid(), nullable=True))
    op.add_column(
        "task_runs", sa.Column("agent_version_digest", sa.String(length=80), nullable=True)
    )
    op.create_foreign_key(
        "fk_task_runs_agent_version",
        "task_runs",
        "agent_versions",
        ["agent_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_task_runs_agent_version_id", "task_runs", ["agent_version_id"])


def downgrade() -> None:
    op.drop_index("ix_task_runs_agent_version_id", table_name="task_runs")
    op.drop_constraint("fk_task_runs_agent_version", "task_runs", type_="foreignkey")
    op.drop_column("task_runs", "agent_version_digest")
    op.drop_column("task_runs", "agent_version_id")

    op.drop_index("ix_agent_instances_deployment_health", table_name="agent_instances")
    op.drop_table("agent_instances")
    op.drop_index("ix_agent_deployments_version_environment", table_name="agent_deployments")
    op.drop_table("agent_deployments")
    op.drop_index("ix_capabilities_tenant_key", table_name="capabilities")
    op.drop_table("capabilities")
    op.drop_constraint(
        "fk_agent_definition_default_version",
        "agent_definitions",
        type_="foreignkey",
    )
    op.drop_index("ix_agent_versions_definition_status", table_name="agent_versions")
    op.execute("DROP INDEX IF EXISTS ix_agent_versions_content_digest")
    op.drop_table("agent_versions")
    op.drop_index("ix_agent_definitions_tenant_created", table_name="agent_definitions")
    op.drop_table("agent_definitions")
