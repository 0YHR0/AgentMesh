from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class AgentDefinitionRecord(Base):
    __tablename__ = "agent_definitions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(63), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    visibility: Mapped[str] = mapped_column(String(32), nullable=False)
    lifecycle: Mapped[str] = mapped_column(String(32), nullable=False)
    default_version_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey(
            "agent_versions.id",
            ondelete="SET NULL",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
            name="fk_agent_definition_default_version",
        ),
        nullable=True,
    )
    tags: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __mapper_args__ = {"version_id_col": version, "version_id_generator": False}
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_agent_definition_tenant_name"),
        Index("ix_agent_definitions_tenant_created", "tenant_id", "created_at"),
    )


class AgentVersionRecord(Base):
    __tablename__ = "agent_versions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    definition_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("agent_definitions.id", ondelete="CASCADE"),
        nullable=False,
    )
    semantic_version: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    content_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    instructions: Mapped[str] = mapped_column(Text, nullable=False)
    declared_capabilities: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    verified_capabilities: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    input_schema: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    output_schema: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    model_policy: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    tool_profile: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    knowledge_profile: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    policy_profile: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    risk_class: Mapped[str] = mapped_column(String(32), nullable=False)
    data_classification_ceiling: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_defaults: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    runtime_adapter: Mapped[str] = mapped_column(String(128), nullable=False)
    artifact_digest: Mapped[str | None] = mapped_column(String(255), nullable=True)
    execution_modes: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    compatibility: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoke_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "definition_id",
            "semantic_version",
            name="uq_agent_version_definition_semver",
        ),
        Index("ix_agent_versions_content_digest", "content_digest"),
        Index("ix_agent_versions_definition_status", "definition_id", "status"),
    )


class CapabilityRecord(Base):
    __tablename__ = "capabilities"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    input_schema: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    output_schema: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    evidence_requirements: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("tenant_id", "key", "version", name="uq_capability_tenant_key_version"),
        Index("ix_capabilities_tenant_key", "tenant_id", "key"),
    )


class AgentDeploymentRecord(Base):
    __tablename__ = "agent_deployments"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    agent_version_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("agent_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    environment: Mapped[str] = mapped_column(String(128), nullable=False)
    runtime_kind: Mapped[str] = mapped_column(String(128), nullable=False)
    remote_peer_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    endpoint_reference: Mapped[str | None] = mapped_column(String(512), nullable=True)
    desired_status: Mapped[str] = mapped_column(String(32), nullable=False)
    current_status: Mapped[str] = mapped_column(String(32), nullable=False)
    traffic_weight: Mapped[int] = mapped_column(Integer, nullable=False)
    region: Mapped[str | None] = mapped_column(String(128), nullable=True)
    rollout_policy: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index(
            "ix_agent_deployments_version_environment",
            "agent_version_id",
            "environment",
        ),
    )


class AgentInstanceRecord(Base):
    __tablename__ = "agent_instances"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    deployment_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("agent_deployments.id", ondelete="CASCADE"),
        nullable=False,
    )
    external_instance_id: Mapped[str] = mapped_column(String(255), nullable=False)
    health: Mapped[str] = mapped_column(String(32), nullable=False)
    last_heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    capacity_slots: Mapped[int] = mapped_column(Integer, nullable=False)
    active_slots: Mapped[int] = mapped_column(Integer, nullable=False)
    protocol_endpoint: Mapped[str | None] = mapped_column(String(512), nullable=True)
    lease_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "deployment_id",
            "external_instance_id",
            name="uq_agent_instance_deployment_external",
        ),
        Index("ix_agent_instances_deployment_health", "deployment_id", "health"),
    )


class ArtifactRecord(Base):
    __tablename__ = "artifacts"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    classification: Mapped[str] = mapped_column(String(32), nullable=False)
    version_count: Mapped[int] = mapped_column(Integer, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __mapper_args__ = {"version_id_col": revision, "version_id_generator": False}
    __table_args__ = (
        CheckConstraint("version_count >= 0", name="ck_artifacts_version_count"),
        CheckConstraint("revision >= 1", name="ck_artifacts_revision"),
        Index("ix_artifacts_tenant_created", "tenant_id", "created_at"),
        Index("ix_artifacts_tenant_kind", "tenant_id", "kind"),
    )


class ArtifactVersionRecord(Base):
    __tablename__ = "artifact_versions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    artifact_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("artifacts.id", ondelete="CASCADE"),
        nullable=False,
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    media_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_class: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    scan_status: Mapped[str] = mapped_column(String(32), nullable=False)
    producer_run_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("task_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("version_number >= 1", name="ck_artifact_versions_number"),
        CheckConstraint("size_bytes >= 1", name="ck_artifact_versions_size"),
        CheckConstraint(
            "octet_length(content) = size_bytes",
            name="ck_artifact_versions_content_size",
        ),
        UniqueConstraint(
            "artifact_id",
            "version_number",
            name="uq_artifact_version_number",
        ),
        Index("ix_artifact_versions_artifact_created", "artifact_id", "created_at"),
        Index("ix_artifact_versions_sha256", "sha256"),
        Index("ix_artifact_versions_producer_run", "producer_run_id"),
    )


class TaskRecord(Base):
    __tablename__ = "tasks"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    input: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    current_run_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __mapper_args__ = {"version_id_col": version, "version_id_generator": False}
    __table_args__ = (
        Index("ix_tasks_tenant_status_created_at", "tenant_id", "status", "created_at"),
    )


class TaskRunRecord(Base):
    __tablename__ = "task_runs"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    task_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    agent_id: Mapped[str] = mapped_column(String(128), nullable=False)
    agent_version_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("agent_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    agent_version_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    pause_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paused_from_status: Mapped[str | None] = mapped_column(String(32), nullable=True)

    __table_args__ = (
        Index("ix_task_runs_task_id_queued_at", "task_id", "queued_at"),
        Index("ix_task_runs_agent_version_id", "agent_version_id"),
    )


class TaskAttemptRecord(Base):
    __tablename__ = "task_attempts"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    run_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("task_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    trace_id: Mapped[str] = mapped_column(String(32), nullable=False)
    worker_id: Mapped[str] = mapped_column(String(128), nullable=False)
    lease_token: Mapped[UUID] = mapped_column(Uuid, nullable=False, unique=True)
    fencing_token: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("run_id", "fencing_token", name="uq_attempt_run_fencing"),
        UniqueConstraint("trace_id", name="uq_task_attempts_trace_id"),
        CheckConstraint(
            "trace_id ~ '^[0-9a-f]{32}$' AND trace_id <> repeat('0', 32)",
            name="ck_task_attempts_trace_id",
        ),
        Index("ix_task_attempts_run_started_at", "run_id", "started_at"),
        Index("ix_task_attempts_status_lease", "status", "lease_expires_at"),
    )


class UsageRecordModel(Base):
    __tablename__ = "usage_records"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    task_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    run_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("task_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    attempt_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("task_attempts.id", ondelete="CASCADE"),
        nullable=False,
    )
    trace_id: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(128), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    usage_details: Mapped[dict[str, int]] = mapped_column(JSONB, nullable=False)
    cost_details_micros: Mapped[dict[str, int]] = mapped_column(JSONB, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    pricing_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "source IN ('PROVIDER', 'ESTIMATED')",
            name="ck_usage_records_source",
        ),
        CheckConstraint(
            "currency ~ '^[A-Z]{3}$'",
            name="ck_usage_records_currency",
        ),
        CheckConstraint(
            "trace_id ~ '^[0-9a-f]{32}$' AND trace_id <> repeat('0', 32)",
            name="ck_usage_records_trace_id",
        ),
        Index("ix_usage_records_task_recorded", "task_id", "recorded_at"),
        Index("ix_usage_records_run_recorded", "run_id", "recorded_at"),
        Index("ix_usage_records_attempt", "attempt_id"),
        Index("ix_usage_records_tenant_provider", "tenant_id", "provider"),
    )


class ToolInvocationRecord(Base):
    __tablename__ = "tool_invocations"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    task_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    run_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("task_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    server_name: Mapped[str] = mapped_column(String(128), nullable=False)
    tool_key: Mapped[str] = mapped_column(String(255), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    side_effect: Mapped[str] = mapped_column(String(32), nullable=False)
    protocol_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    schema_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    arguments_digest: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    result_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    result_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "result_bytes IS NULL OR result_bytes >= 0",
            name="ck_tool_invocations_result_bytes",
        ),
        Index("ix_tool_invocations_task_started", "task_id", "started_at"),
        Index("ix_tool_invocations_run_started", "run_id", "started_at"),
        Index("ix_tool_invocations_tenant_status", "tenant_id", "status"),
    )


class OutboxEventRecord(Base):
    __tablename__ = "outbox_events"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    topic: Mapped[str] = mapped_column(String(255), nullable=False)
    envelope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claimed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    claimed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    quarantined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "(status = 'QUARANTINED' AND quarantined_at IS NOT NULL) "
            "OR (status <> 'QUARANTINED' AND quarantined_at IS NULL)",
            name="ck_outbox_quarantine_timestamp",
        ),
        Index("ix_outbox_pending_available", "status", "available_at", "created_at"),
    )


class InboxMessageRecord(Base):
    __tablename__ = "inbox_messages"

    consumer_name: Mapped[str] = mapped_column(String(128), nullable=False)
    message_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    schema_name: Mapped[str] = mapped_column(String(255), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("consumer_name", "message_id"),
        Index("ix_inbox_processed_at", "processed_at"),
    )


class IdempotencyRecordModel(Base):
    __tablename__ = "idempotency_records"

    scope: Mapped[str] = mapped_column(String(255), nullable=False)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("scope", "key"),
        Index("ix_idempotency_expires_at", "expires_at"),
    )
