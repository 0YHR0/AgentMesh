from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
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
    text,
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
    execution_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    acceptance_criteria: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    max_revisions: Mapped[int] = mapped_column(Integer, nullable=False)
    revision_count: Mapped[int] = mapped_column(Integer, nullable=False)
    review_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    candidate_output: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    latest_review: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    plan_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    plan_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    max_concurrency: Mapped[int] = mapped_column(Integer, nullable=False)
    budget: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    settled_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    reserved_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    settled_cost_micros: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    reserved_cost_micros: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    budget_exhausted_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    budget_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __mapper_args__ = {"version_id_col": version, "version_id_generator": False}
    __table_args__ = (
        CheckConstraint(
            "execution_mode IN ('DIRECT', 'REVIEWED', 'COORDINATED', 'FEDERATED')",
            name="ck_tasks_execution_mode",
        ),
        CheckConstraint(
            "max_revisions >= 0 AND revision_count >= 0 AND revision_count <= max_revisions",
            name="ck_tasks_review_revision_counts",
        ),
        CheckConstraint(
            "max_concurrency >= 1 AND max_concurrency <= 10",
            name="ck_tasks_max_concurrency",
        ),
        CheckConstraint(
            "settled_tokens >= 0 AND reserved_tokens >= 0 AND "
            "settled_cost_micros >= 0 AND reserved_cost_micros >= 0",
            name="ck_tasks_budget_counters",
        ),
        CheckConstraint("budget_revision >= 0", name="ck_tasks_budget_revision"),
        Index("ix_tasks_tenant_status_created_at", "tenant_id", "status", "created_at"),
    )


class TaskResolutionRecord(Base):
    __tablename__ = "task_resolutions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    task_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    action: Mapped[str] = mapped_column(String(48), nullable=False)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    previous_status: Mapped[str] = mapped_column(String(32), nullable=False)
    resulting_status: Mapped[str] = mapped_column(String(32), nullable=False)
    previous_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "action IN ('ACCEPT_CANDIDATE', 'REJECT_TASK', 'INCREASE_BUDGET_AND_RESUME')",
            name="ck_task_resolutions_action",
        ),
        Index("ix_task_resolutions_task_created", "task_id", "created_at"),
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
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    subtask_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("subtasks.id", ondelete="CASCADE"),
        nullable=True,
    )
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
        CheckConstraint(
            "role IN ('EXECUTOR', 'REVIEWER', 'SUPERVISOR')",
            name="ck_task_runs_role",
        ),
        CheckConstraint("revision_number >= 0", name="ck_task_runs_revision_number"),
        Index("ix_task_runs_task_id_queued_at", "task_id", "queued_at"),
        Index("ix_task_runs_agent_version_id", "agent_version_id"),
        Index("ix_task_runs_subtask_id", "subtask_id"),
    )


class SubtaskRecord(Base):
    __tablename__ = "subtasks"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    task_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    input: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    required_capabilities: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    preferred_agent_id: Mapped[str | None] = mapped_column(String(63), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    current_run_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __mapper_args__ = {"version_id_col": version, "version_id_generator": False}
    __table_args__ = (
        UniqueConstraint("task_id", "key", name="uq_subtasks_task_key"),
        CheckConstraint(
            "status IN ('BLOCKED', 'READY', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELED')",
            name="ck_subtasks_status",
        ),
        Index("ix_subtasks_task_status_key", "task_id", "status", "key"),
    )


class SubtaskDependencyRecord(Base):
    __tablename__ = "subtask_dependencies"

    task_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    predecessor_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("subtasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    successor_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("subtasks.id", ondelete="CASCADE"),
        nullable=False,
    )

    __table_args__ = (
        PrimaryKeyConstraint(
            "task_id",
            "predecessor_id",
            "successor_id",
            name="pk_subtask_dependencies",
        ),
        CheckConstraint("predecessor_id <> successor_id", name="ck_subtask_dependencies_distinct"),
        Index("ix_subtask_dependencies_successor", "successor_id", "predecessor_id"),
    )


class HandoffRecord(Base):
    __tablename__ = "handoffs"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    task_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    source_subtask_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("subtasks.id", ondelete="CASCADE"), nullable=False
    )
    source_run_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("task_runs.id", ondelete="CASCADE"), nullable=False
    )
    source_trace_id: Mapped[str] = mapped_column(String(32), nullable=False)
    causation_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, unique=True)
    source_agent_id: Mapped[str] = mapped_column(String(63), nullable=False)
    target_subtask_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("subtasks.id", ondelete="CASCADE"), nullable=False
    )
    target_agent_id: Mapped[str] = mapped_column(String(63), nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    completed_work_summary: Mapped[str] = mapped_column(Text, nullable=False)
    unresolved_questions: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    constraints: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    acceptance_criteria: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    requested_by: Mapped[str] = mapped_column(String(128), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)

    __mapper_args__ = {"version_id_col": version, "version_id_generator": False}
    __table_args__ = (
        CheckConstraint(
            "status IN ('REQUESTED', 'ACCEPTED', 'REJECTED')",
            name="ck_handoffs_status",
        ),
        CheckConstraint(
            "source_subtask_id <> target_subtask_id",
            name="ck_handoffs_distinct_subtasks",
        ),
        CheckConstraint(
            "source_trace_id ~ '^[0-9a-f]{32}$'",
            name="ck_handoffs_source_trace_id",
        ),
        Index("ix_handoffs_task_requested", "task_id", "requested_at"),
        Index("ix_handoffs_target_status", "target_subtask_id", "status"),
        Index(
            "uq_handoffs_one_accepted_target",
            "target_subtask_id",
            unique=True,
            postgresql_where=text("status = 'ACCEPTED'"),
        ),
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
    reserved_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    reserved_cost_micros: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    settled_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    settled_cost_micros: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    budget_settlement_source: Mapped[str | None] = mapped_column(String(32), nullable=True)

    __table_args__ = (
        UniqueConstraint("run_id", "fencing_token", name="uq_attempt_run_fencing"),
        UniqueConstraint("trace_id", name="uq_task_attempts_trace_id"),
        CheckConstraint(
            "trace_id ~ '^[0-9a-f]{32}$' AND trace_id <> repeat('0', 32)",
            name="ck_task_attempts_trace_id",
        ),
        CheckConstraint(
            "reserved_tokens >= 0 AND reserved_cost_micros >= 0 AND "
            "(settled_tokens IS NULL OR settled_tokens >= 0) AND "
            "(settled_cost_micros IS NULL OR settled_cost_micros >= 0)",
            name="ck_task_attempts_budget_values",
        ),
        CheckConstraint(
            "budget_settlement_source IS NULL OR "
            "budget_settlement_source IN ('ACTUAL', 'CONSERVATIVE_ESTIMATE', 'RELEASED')",
            name="ck_task_attempts_budget_source",
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


class McpServerRecord(Base):
    __tablename__ = "mcp_servers"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    transport: Mapped[str] = mapped_column(String(32), nullable=False)
    endpoint_reference: Mapped[str] = mapped_column(String(512), nullable=False)
    authentication_required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)

    __mapper_args__ = {"version_id_col": revision, "version_id_generator": False}
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_mcp_servers_tenant_name"),
        CheckConstraint(
            "transport IN ('MANAGED_STDIO', 'STREAMABLE_HTTP')",
            name="ck_mcp_servers_transport",
        ),
        CheckConstraint("status IN ('DRAFT', 'ACTIVE', 'SUSPENDED')", name="ck_mcp_servers_status"),
        Index("ix_mcp_servers_tenant_status", "tenant_id", "status", "created_at"),
    )


class McpServerVersionRecord(Base):
    __tablename__ = "mcp_server_versions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    server_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("mcp_servers.id", ondelete="CASCADE"), nullable=False
    )
    semantic_version: Mapped[str] = mapped_column(String(64), nullable=False)
    protocol_version: Mapped[str] = mapped_column(String(32), nullable=False)
    configuration_digest: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoke_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)

    __mapper_args__ = {"version_id_col": revision, "version_id_generator": False}
    __table_args__ = (
        UniqueConstraint("server_id", "semantic_version", name="uq_mcp_server_versions_semantic"),
        CheckConstraint(
            "status IN ('DRAFT', 'PUBLISHED', 'REVOKED')",
            name="ck_mcp_server_versions_status",
        ),
        Index("ix_mcp_server_versions_server_status", "server_id", "status", "created_at"),
    )


class McpToolCapabilityRecord(Base):
    __tablename__ = "mcp_tool_capabilities"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    server_version_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("mcp_server_versions.id", ondelete="CASCADE"), nullable=False
    )
    logical_key: Mapped[str] = mapped_column(String(255), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    side_effect: Mapped[str] = mapped_column(String(32), nullable=False)
    input_schema: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    schema_digest: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "server_version_id", "logical_key", name="uq_mcp_tools_version_logical_key"
        ),
        CheckConstraint(
            "side_effect IN ('READ_ONLY', 'IDEMPOTENT_WRITE', "
            "'NON_IDEMPOTENT_WRITE', 'IRREVERSIBLE')",
            name="ck_mcp_tools_side_effect",
        ),
        Index("ix_mcp_tools_tenant_logical_key", "tenant_id", "logical_key"),
    )


class A2APeerRecord(Base):
    __tablename__ = "a2a_peers"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(63), nullable=False)
    discovery_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    allowed_endpoint_hosts: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    allowed_bindings: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    trust_tier: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    active_card_snapshot_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey(
            "a2a_agent_card_snapshots.id",
            ondelete="SET NULL",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
            name="fk_a2a_peer_active_card",
        ),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)

    __mapper_args__ = {"version_id_col": revision, "version_id_generator": False}
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_a2a_peers_tenant_name"),
        Index("ix_a2a_peers_tenant_status", "tenant_id", "status", "created_at"),
    )


class AgentCardSnapshotRecord(Base):
    __tablename__ = "a2a_agent_card_snapshots"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    peer_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("a2a_peers.id", ondelete="CASCADE"), nullable=False
    )
    digest: Mapped[str] = mapped_column(String(80), nullable=False)
    raw_card: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    agent_name: Mapped[str] = mapped_column(String(255), nullable=False)
    agent_description: Mapped[str] = mapped_column(Text, nullable=False)
    agent_version: Mapped[str] = mapped_column(String(128), nullable=False)
    endpoints: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    skills: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    security_schemes: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    signature_status: Mapped[str] = mapped_column(String(32), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_etag: Mapped[str | None] = mapped_column(String(512), nullable=True)

    __table_args__ = (
        Index("ix_a2a_cards_peer_digest", "peer_id", "digest"),
        Index("ix_a2a_cards_peer_fetched", "peer_id", "fetched_at"),
        Index("ix_a2a_cards_tenant_expiry", "tenant_id", "expires_at"),
    )


class RemoteTaskCorrelationRecord(Base):
    __tablename__ = "a2a_remote_task_correlations"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    task_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    run_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("task_runs.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    peer_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("a2a_peers.id", ondelete="RESTRICT"), nullable=False
    )
    card_snapshot_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("a2a_agent_card_snapshots.id", ondelete="RESTRICT"),
        nullable=False,
    )
    card_digest: Mapped[str] = mapped_column(String(80), nullable=False)
    endpoint_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    protocol_binding: Mapped[str] = mapped_column(String(32), nullable=False)
    protocol_version: Mapped[str] = mapped_column(String(32), nullable=False)
    endpoint_tenant: Mapped[str | None] = mapped_column(String(255), nullable=True)
    outbound_message_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, unique=True)
    request_digest: Mapped[str] = mapped_column(String(80), nullable=False)
    credential_binding_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("credential_bindings.id", ondelete="RESTRICT"), nullable=True
    )
    credential_scheme_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    credential_scopes: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    last_credential_lease_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    remote_task_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    remote_context_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    last_remote_state: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_response_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    poll_count: Mapped[int] = mapped_column(Integer, nullable=False)
    late_result: Mapped[bool] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    send_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)

    __mapper_args__ = {"version_id_col": revision, "version_id_generator": False}
    __table_args__ = (
        CheckConstraint("poll_count >= 0", name="ck_a2a_correlations_poll_count"),
        Index("ix_a2a_correlations_tenant_status", "tenant_id", "status", "updated_at"),
        Index(
            "uq_a2a_correlations_peer_remote_task",
            "peer_id",
            "remote_task_id",
            unique=True,
            postgresql_where=text("remote_task_id IS NOT NULL"),
        ),
    )


class SecretReferenceRecord(Base):
    __tablename__ = "secret_references"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    external_key: Mapped[str] = mapped_column(String(255), nullable=False)
    version_selector: Mapped[str | None] = mapped_column(String(128), nullable=True)
    purpose: Mapped[str] = mapped_column(String(64), nullable=False)
    allowed_audiences: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)

    __mapper_args__ = {"version_id_col": revision, "version_id_generator": False}
    __table_args__ = (
        CheckConstraint("status IN ('ACTIVE', 'REVOKED')", name="ck_secret_references_status"),
        Index("ix_secret_references_tenant_status", "tenant_id", "status", "created_at"),
    )


class CredentialBindingRecord(Base):
    __tablename__ = "credential_bindings"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    workload_principal_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("principals.id", ondelete="RESTRICT"), nullable=False
    )
    peer_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("a2a_peers.id", ondelete="RESTRICT"), nullable=False
    )
    card_snapshot_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("a2a_agent_card_snapshots.id", ondelete="RESTRICT"),
        nullable=False,
    )
    card_digest: Mapped[str] = mapped_column(String(80), nullable=False)
    secret_reference_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("secret_references.id", ondelete="RESTRICT"), nullable=False
    )
    scheme_name: Mapped[str] = mapped_column(String(128), nullable=False)
    auth_scheme: Mapped[str] = mapped_column(String(32), nullable=False)
    audience: Mapped[str] = mapped_column(String(2048), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    environment: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)

    __mapper_args__ = {"version_id_col": revision, "version_id_generator": False}
    __table_args__ = (
        CheckConstraint("status IN ('ACTIVE', 'REVOKED')", name="ck_credential_bindings_status"),
        Index("ix_credential_bindings_tenant_status", "tenant_id", "status", "created_at"),
        Index(
            "uq_credential_bindings_active_target",
            "workload_principal_id",
            "peer_id",
            "card_snapshot_id",
            "scheme_name",
            "environment",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
    )


class CredentialLeaseRecord(Base):
    __tablename__ = "credential_leases"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    binding_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("credential_bindings.id", ondelete="RESTRICT"), nullable=False
    )
    secret_reference_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("secret_references.id", ondelete="RESTRICT"), nullable=False
    )
    workload_principal_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("principals.id", ondelete="RESTRICT"), nullable=False
    )
    peer_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("a2a_peers.id", ondelete="RESTRICT"), nullable=False
    )
    card_snapshot_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("a2a_agent_card_snapshots.id", ondelete="RESTRICT"),
        nullable=False,
    )
    task_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False
    )
    run_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("task_runs.id", ondelete="RESTRICT"), nullable=False
    )
    audience: Mapped[str] = mapped_column(String(2048), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)

    __mapper_args__ = {"version_id_col": revision, "version_id_generator": False}
    __table_args__ = (
        CheckConstraint(
            "status IN ('REQUESTED', 'ISSUED', 'USED', 'FAILED')",
            name="ck_credential_leases_status",
        ),
        Index("ix_credential_leases_tenant_status", "tenant_id", "status", "created_at"),
        Index("ix_credential_leases_run", "run_id", "created_at"),
    )


class McpCredentialBindingRecord(Base):
    __tablename__ = "mcp_credential_bindings"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    workload_principal_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("principals.id", ondelete="RESTRICT"), nullable=False
    )
    server_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("mcp_servers.id", ondelete="RESTRICT"), nullable=False
    )
    server_version_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("mcp_server_versions.id", ondelete="RESTRICT"), nullable=False
    )
    configuration_digest: Mapped[str] = mapped_column(String(80), nullable=False)
    secret_reference_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("secret_references.id", ondelete="RESTRICT"), nullable=False
    )
    auth_scheme: Mapped[str] = mapped_column(String(32), nullable=False)
    audience: Mapped[str] = mapped_column(String(2048), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    environment: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)

    __mapper_args__ = {"version_id_col": revision, "version_id_generator": False}
    __table_args__ = (
        CheckConstraint(
            "status IN ('ACTIVE', 'REVOKED')", name="ck_mcp_credential_bindings_status"
        ),
        Index("ix_mcp_credential_bindings_tenant_status", "tenant_id", "status", "created_at"),
        Index(
            "uq_mcp_credential_bindings_active_target",
            "workload_principal_id",
            "server_version_id",
            "environment",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
    )


class McpCredentialLeaseRecord(Base):
    __tablename__ = "mcp_credential_leases"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    binding_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("mcp_credential_bindings.id", ondelete="RESTRICT"), nullable=False
    )
    secret_reference_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("secret_references.id", ondelete="RESTRICT"), nullable=False
    )
    workload_principal_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("principals.id", ondelete="RESTRICT"), nullable=False
    )
    server_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("mcp_servers.id", ondelete="RESTRICT"), nullable=False
    )
    server_version_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("mcp_server_versions.id", ondelete="RESTRICT"), nullable=False
    )
    tool_invocation_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("tool_invocations.id", ondelete="RESTRICT"), nullable=False
    )
    task_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False
    )
    run_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("task_runs.id", ondelete="RESTRICT"), nullable=False
    )
    audience: Mapped[str] = mapped_column(String(2048), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)

    __mapper_args__ = {"version_id_col": revision, "version_id_generator": False}
    __table_args__ = (
        CheckConstraint(
            "status IN ('REQUESTED', 'ISSUED', 'USED', 'FAILED')",
            name="ck_mcp_credential_leases_status",
        ),
        Index("ix_mcp_credential_leases_tenant_status", "tenant_id", "status", "created_at"),
        Index("ix_mcp_credential_leases_invocation", "tool_invocation_id", "created_at"),
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
            "(status = 'PUBLISHED' AND published_at IS NOT NULL) "
            "OR (status <> 'PUBLISHED' AND published_at IS NULL)",
            name="ck_outbox_published_timestamp",
        ),
        CheckConstraint(
            "(status = 'QUARANTINED' AND quarantined_at IS NOT NULL) "
            "OR (status <> 'QUARANTINED' AND quarantined_at IS NULL)",
            name="ck_outbox_quarantine_timestamp",
        ),
        Index("ix_outbox_pending_available", "status", "available_at", "created_at"),
        Index(
            "ix_outbox_published_retention",
            "published_at",
            "id",
            postgresql_where=text("status = 'PUBLISHED' AND published_at IS NOT NULL"),
        ),
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
        PrimaryKeyConstraint("tenant_id", "consumer_name", "message_id"),
        Index(
            "ix_inbox_retention",
            "processed_at",
            "tenant_id",
            "consumer_name",
            "message_id",
        ),
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


class GovernedActionRecord(Base):
    __tablename__ = "governed_actions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    requester_id: Mapped[str] = mapped_column(String(128), nullable=False)
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    arguments: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    canonicalization_version: Mapped[str] = mapped_column(String(64), nullable=False)
    action_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_result: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(128), nullable=False)
    policy_bundle: Mapped[str] = mapped_column(String(128), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    approval_id: Mapped[UUID | None] = mapped_column(Uuid, unique=True, nullable=True)
    approval_status: Mapped[str] = mapped_column(String(32), nullable=False)
    permit_id: Mapped[UUID | None] = mapped_column(Uuid, unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)

    __mapper_args__ = {"version_id_col": revision, "version_id_generator": False}
    __table_args__ = (
        CheckConstraint(
            "policy_result IN ('ALLOW', 'DENY', 'REQUIRE_APPROVAL')",
            name="ck_governed_actions_policy_result",
        ),
        CheckConstraint(
            "approval_status IN ('NOT_REQUIRED', 'PENDING', 'APPROVED', "
            "'REJECTED', 'EXPIRED', 'CONSUMED')",
            name="ck_governed_actions_approval_status",
        ),
        Index("ix_governed_actions_tenant_status", "tenant_id", "approval_status", "created_at"),
        Index("ix_governed_actions_resource", "tenant_id", "resource_type", "resource_id"),
    )


class ApprovalDecisionRecord(Base):
    __tablename__ = "approval_decisions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    governed_action_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("governed_actions.id", ondelete="CASCADE"),
        nullable=False,
    )
    approval_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    approver_id: Mapped[str] = mapped_column(String(128), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "outcome IN ('APPROVE', 'REJECT')",
            name="ck_approval_decisions_outcome",
        ),
        UniqueConstraint("approval_id", name="uq_approval_decisions_approval"),
        Index("ix_approval_decisions_action_created", "governed_action_id", "created_at"),
    )


class PrincipalRecord(Base):
    __tablename__ = "principals"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    principal_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)

    __mapper_args__ = {"version_id_col": revision, "version_id_generator": False}
    __table_args__ = (
        CheckConstraint(
            "principal_type IN ('USER', 'SERVICE', 'AGENT', 'EXTERNAL_PEER')",
            name="ck_principals_type",
        ),
        CheckConstraint(
            "status IN ('ACTIVE', 'SUSPENDED', 'DEACTIVATED')",
            name="ck_principals_status",
        ),
        Index("ix_principals_tenant_created", "tenant_id", "created_at", "id"),
    )


class ExternalIdentityRecord(Base):
    __tablename__ = "external_identities"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    principal_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("principals.id", ondelete="CASCADE"), nullable=False
    )
    issuer: Mapped[str] = mapped_column(String(512), nullable=False)
    subject: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "issuer", "subject", name="uq_external_identities_tenant_subject"
        ),
        Index("ix_external_identities_principal", "tenant_id", "principal_id"),
    )


class RoleBindingRecord(Base):
    __tablename__ = "role_bindings"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    principal_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("principals.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    revoke_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)

    __mapper_args__ = {"version_id_col": revision, "version_id_generator": False}
    __table_args__ = (
        CheckConstraint("status IN ('ACTIVE', 'REVOKED')", name="ck_role_bindings_status"),
        Index("ix_role_bindings_principal_status", "tenant_id", "principal_id", "status"),
        Index("ix_role_bindings_expiry", "expires_at"),
    )
