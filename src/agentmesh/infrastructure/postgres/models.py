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


class CompanyRecord(Base):
    __tablename__ = "companies"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    mission: Mapped[str] = mapped_column(Text, nullable=False)
    owner_principal_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    risk_policy_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    default_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    operating_timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __mapper_args__ = {"version_id_col": version, "version_id_generator": False}
    __table_args__ = (
        Index(
            "uq_companies_tenant_active",
            "tenant_id",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
        Index("ix_companies_tenant_created", "tenant_id", "created_at"),
    )


class OrganizationUnitRecord(Base):
    __tablename__ = "organization_units"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    company_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(63), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    kind: Mapped[str] = mapped_column(String(63), nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    parent_unit_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("organization_units.id", ondelete="RESTRICT"), nullable=True
    )
    budget_policy_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    memory_namespace: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __mapper_args__ = {"version_id_col": version, "version_id_generator": False}
    __table_args__ = (
        UniqueConstraint("company_id", "key", name="uq_organization_units_company_key"),
        Index("ix_organization_units_company_status", "company_id", "status"),
    )


class CompanyPositionRecord(Base):
    __tablename__ = "company_positions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    company_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    primary_unit_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("organization_units.id", ondelete="RESTRICT"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(63), nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    responsibility_contract: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    required_capabilities: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    allowed_tool_capabilities: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    memory_policy_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    approval_scope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    budget_scope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    reports_to_position_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("company_positions.id", ondelete="RESTRICT"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __mapper_args__ = {"version_id_col": version, "version_id_generator": False}
    __table_args__ = (
        UniqueConstraint("company_id", "key", name="uq_company_positions_company_key"),
        Index("ix_company_positions_unit_status", "primary_unit_id", "status"),
    )


class AppointmentRecord(Base):
    __tablename__ = "company_appointments"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    company_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    position_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("company_positions.id", ondelete="RESTRICT"), nullable=False
    )
    agent_definition_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("agent_definitions.id", ondelete="RESTRICT"), nullable=False
    )
    agent_version_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("agent_versions.id", ondelete="RESTRICT"), nullable=False
    )
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    appointed_by: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index(
            "uq_company_appointments_position_active",
            "position_id",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
        Index("ix_company_appointments_company_status", "company_id", "status"),
        Index("ix_company_appointments_agent", "agent_definition_id", "status"),
    )


class OrganizationRelationshipRecord(Base):
    __tablename__ = "organization_relationships"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    company_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    relationship_type: Mapped[str] = mapped_column(String(63), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "relationship_type",
            "source_type",
            "source_id",
            "target_type",
            "target_id",
            "status",
            name="uq_organization_relationship_identity_status",
        ),
        Index("ix_organization_relationships_company_status", "company_id", "status"),
        Index("ix_organization_relationships_source", "source_type", "source_id"),
        Index("ix_organization_relationships_target", "target_type", "target_id"),
    )


class OperatingCycleRecord(Base):
    __tablename__ = "company_operating_cycles"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    company_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_schedule: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __mapper_args__ = {"version_id_col": version, "version_id_generator": False}
    __table_args__ = (
        Index(
            "uq_company_operating_cycles_active",
            "company_id",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
        Index("ix_company_operating_cycles_company_created", "company_id", "created_at"),
    )


class CompanyObjectiveRecord(Base):
    __tablename__ = "company_objectives"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    company_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    cycle_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("company_operating_cycles.id", ondelete="CASCADE"), nullable=False
    )
    owner_position_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("company_positions.id", ondelete="RESTRICT"), nullable=False
    )
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    target_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __mapper_args__ = {"version_id_col": version, "version_id_generator": False}
    __table_args__ = (
        CheckConstraint("priority >= 1 AND priority <= 5", name="ck_company_objective_priority"),
        Index("ix_company_objectives_cycle_status", "cycle_id", "status"),
    )


class CompanyKeyResultRecord(Base):
    __tablename__ = "company_key_results"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    company_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    objective_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("company_objectives.id", ondelete="CASCADE"), nullable=False
    )
    metric_key: Mapped[str] = mapped_column(String(128), nullable=False)
    unit: Mapped[str] = mapped_column(String(32), nullable=False)
    baseline: Mapped[str] = mapped_column(String(80), nullable=False)
    target: Mapped[str] = mapped_column(String(80), nullable=False)
    current_verified_value: Mapped[str | None] = mapped_column(String(80), nullable=True)
    current_estimated_value: Mapped[str | None] = mapped_column(String(80), nullable=True)
    measurement_source: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __mapper_args__ = {"version_id_col": version, "version_id_generator": False}
    __table_args__ = (
        UniqueConstraint(
            "objective_id", "metric_key", name="uq_company_key_results_objective_metric"
        ),
        Index("ix_company_key_results_objective_status", "objective_id", "status"),
    )


class CompanyInitiativeRecord(Base):
    __tablename__ = "company_initiatives"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    company_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    objective_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("company_objectives.id", ondelete="CASCADE"), nullable=False
    )
    owner_unit_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("organization_units.id", ondelete="RESTRICT"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    outcome_contract: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    budget_allocation_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __mapper_args__ = {"version_id_col": version, "version_id_generator": False}
    __table_args__ = (
        Index("ix_company_initiatives_objective_status", "objective_id", "status"),
    )


class InitiativeTaskLinkRecord(Base):
    __tablename__ = "company_initiative_tasks"

    initiative_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("company_initiatives.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False
    )
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint(
            "initiative_id", "task_id", name="pk_company_initiative_tasks"
        ),
        UniqueConstraint("task_id", name="uq_company_initiative_tasks_task"),
        Index("ix_company_initiative_tasks_created", "initiative_id", "created_at"),
    )


class CompanyOperationRecord(Base):
    __tablename__ = "company_operations"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    company_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    organization_unit_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("organization_units.id", ondelete="RESTRICT"), nullable=False
    )
    initiative_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("company_initiatives.id", ondelete="SET NULL"), nullable=True
    )
    key: Mapped[str] = mapped_column(String(63), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    objective_template: Mapped[str] = mapped_column(Text, nullable=False)
    input_template: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    trigger_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    trigger_definition: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    missed_policy: Mapped[str] = mapped_column(String(32), nullable=False)
    catch_up_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    concurrency_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    maximum_runs_per_window: Mapped[int] = mapped_column(Integer, nullable=False)
    window_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    position_bindings: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    tool_capability_allowlist: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    budget_limit: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    approval_policy_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("company_id", "key", name="uq_company_operations_company_key"),
        Index("ix_company_operations_company_status", "company_id", "status"),
    )


class CompanyOperationTriggerStateRecord(Base):
    __tablename__ = "company_operation_trigger_states"

    operation_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("company_operations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    trigger_version: Mapped[int] = mapped_column(Integer, nullable=False)
    next_due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_evaluated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_fired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False)
    paused_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    fencing_token: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_company_operation_triggers_due", "next_due_at"),)


class CompanyOperationOccurrenceRecord(Base):
    __tablename__ = "company_operation_occurrences"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    operation_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("company_operations.id", ondelete="CASCADE"), nullable=False
    )
    operation_version: Mapped[int] = mapped_column(Integer, nullable=False)
    occurrence_key: Mapped[str] = mapped_column(String(512), nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    task_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=True
    )
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "operation_id",
            "occurrence_key",
            name="uq_company_operation_occurrences_key",
        ),
        Index(
            "ix_company_operation_occurrences_operation_scheduled",
            "operation_id",
            "scheduled_at",
        ),
        Index("ix_company_operation_occurrences_task", "task_id"),
    )


class CompanyOperationExceptionRecord(Base):
    __tablename__ = "company_operation_exceptions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    operation_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("company_operations.id", ondelete="CASCADE"), nullable=False
    )
    occurrence_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("company_operation_occurrences.id", ondelete="SET NULL"),
        nullable=True,
    )
    code: Mapped[str] = mapped_column(String(63), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index(
            "ix_company_operation_exceptions_unresolved",
            "operation_id",
            "resolved_at",
        ),
    )


class BusinessObjectTypeRecord(Base):
    __tablename__ = "business_object_types"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    company_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(63), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    json_schema: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    lifecycle_definition: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    sensitive_fields: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    ownership_rules: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    retention_policy: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "key",
            "schema_version",
            name="uq_business_object_types_company_key_version",
        ),
        Index(
            "uq_business_object_types_published",
            "company_id",
            "key",
            unique=True,
            postgresql_where=text("status = 'PUBLISHED'"),
        ),
        Index("ix_business_object_types_company_status", "company_id", "status"),
    )


class BusinessObjectRecord(Base):
    __tablename__ = "business_objects"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    company_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    type_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("business_object_types.id", ondelete="RESTRICT"), nullable=False
    )
    external_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    current_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    lifecycle_state: Mapped[str] = mapped_column(String(63), nullable=False)
    owner_position_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("company_positions.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __mapper_args__ = {
        "version_id_col": current_revision,
        "version_id_generator": False,
    }
    __table_args__ = (
        Index(
            "uq_business_objects_external_ref",
            "type_id",
            "external_ref",
            unique=True,
            postgresql_where=text("external_ref IS NOT NULL"),
        ),
        Index(
            "ix_business_objects_company_type_state",
            "company_id",
            "type_id",
            "lifecycle_state",
        ),
    )


class BusinessObjectRevisionRecord(Base):
    __tablename__ = "business_object_revisions"

    object_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("business_objects.id", ondelete="CASCADE"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(63), nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    data_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    evidence_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint(
            "object_id", "revision", name="pk_business_object_revisions"
        ),
        Index(
            "ix_business_object_revisions_created",
            "object_id",
            "created_at",
        ),
    )


class MemoryPolicyRecord(Base):
    __tablename__ = "memory_policies"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    company_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(63), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    readable_namespace_patterns: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    writable_namespace_patterns: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    allowed_memory_types: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    auto_accept_memory_types: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    forbidden_sensitivity_levels: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    maximum_retrieval_count: Mapped[int] = mapped_column(Integer, nullable=False)
    maximum_context_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    default_ttl_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    review_role: Mapped[str] = mapped_column(String(128), nullable=False)
    extraction_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "company_id", "key", "version", name="uq_memory_policies_company_key_version"
        ),
        Index(
            "uq_memory_policies_active",
            "company_id",
            "key",
            unique=True,
            postgresql_where=text("active IS TRUE"),
        ),
    )


class MemoryRecordModel(Base):
    __tablename__ = "memory_records"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    company_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    namespace_type: Mapped[str] = mapped_column(String(32), nullable=False)
    namespace_id: Mapped[str] = mapped_column(String(255), nullable=False)
    memory_type: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    provenance_type: Mapped[str] = mapped_column(String(32), nullable=False)
    provenance_id: Mapped[str] = mapped_column(String(255), nullable=False)
    proposed_by_run_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("task_runs.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    confidence_basis_points: Mapped[int] = mapped_column(Integer, nullable=False)
    sensitivity: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    supersedes_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("memory_records.id", ondelete="RESTRICT"), nullable=True
    )
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "confidence_basis_points >= 0 AND confidence_basis_points <= 10000",
            name="ck_memory_records_confidence",
        ),
        Index(
            "ix_memory_records_search",
            "company_id",
            "namespace_type",
            "namespace_id",
            "memory_type",
            "status",
        ),
        Index("ix_memory_records_expiry", "status", "expires_at"),
    )


class MemoryEvidenceRecord(Base):
    __tablename__ = "memory_evidence"

    memory_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("memory_records.id", ondelete="CASCADE"), nullable=False
    )
    evidence_type: Mapped[str] = mapped_column(String(63), nullable=False)
    evidence_id: Mapped[str] = mapped_column(String(255), nullable=False)
    evidence_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint(
            "memory_id",
            "evidence_type",
            "evidence_id",
            name="pk_memory_evidence",
        ),
    )


class MemoryReviewRecord(Base):
    __tablename__ = "memory_reviews"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    memory_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("memory_records.id", ondelete="CASCADE"), nullable=False
    )
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    reviewer: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_memory_reviews_memory_created", "memory_id", "created_at"),)


class MemoryRetrievalRecord(Base):
    __tablename__ = "memory_retrievals"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    company_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    policy_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("memory_policies.id", ondelete="RESTRICT"), nullable=False
    )
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    query_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    namespace_keys: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    memory_types: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    result_memory_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    principal_id: Mapped[str] = mapped_column(String(128), nullable=False)
    task_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    run_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("task_runs.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_memory_retrievals_task_created", "task_id", "created_at"),
        Index("ix_memory_retrievals_run_created", "run_id", "created_at"),
        Index("ix_memory_retrievals_company_created", "company_id", "created_at"),
    )


class BudgetAllocationRecord(Base):
    __tablename__ = "budget_allocations"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    company_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    parent_allocation_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("budget_allocations.id", ondelete="RESTRICT"), nullable=True
    )
    scope_type: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(255), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    approved_limit_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint(
            "company_id", "scope_type", "scope_id", name="uq_budget_allocations_scope"
        ),
        CheckConstraint(
            "approved_limit_micros > 0", name="ck_budget_allocations_positive_limit"
        ),
        Index("ix_budget_allocations_company_status", "company_id", "status"),
    )


class BudgetLedgerEntryRecord(Base):
    __tablename__ = "budget_ledger_entries"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    allocation_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("budget_allocations.id", ondelete="CASCADE"), nullable=False
    )
    entry_type: Mapped[str] = mapped_column(String(16), nullable=False)
    amount_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)
    operation_key: Mapped[str] = mapped_column(String(128), nullable=False)
    task_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    evidence_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "allocation_id",
            "operation_key",
            name="uq_budget_ledger_allocation_operation",
        ),
        CheckConstraint("amount_micros > 0", name="ck_budget_ledger_positive_amount"),
        Index("ix_budget_ledger_allocation_created", "allocation_id", "created_at"),
    )


class EconomicEvidenceRecord(Base):
    __tablename__ = "economic_evidence"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    company_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    verification: Mapped[str] = mapped_column(String(16), nullable=False)
    amount_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    external_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_snapshot_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    organization_unit_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("organization_units.id", ondelete="SET NULL"), nullable=True
    )
    initiative_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("company_initiatives.id", ondelete="SET NULL"), nullable=True
    )
    operation_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("company_operations.id", ondelete="SET NULL"), nullable=True
    )
    task_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    attribution_method: Mapped[str] = mapped_column(String(63), nullable=False)
    recorded_by: Mapped[str] = mapped_column(String(128), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "company_id", "external_ref", name="uq_economic_evidence_external_ref"
        ),
        CheckConstraint("amount_micros > 0", name="ck_economic_evidence_positive_amount"),
        Index(
            "ix_economic_evidence_company_kind",
            "company_id",
            "kind",
            "verification",
        ),
    )


class ExpenseRequestRecord(Base):
    __tablename__ = "expense_requests"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    company_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    allocation_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("budget_allocations.id", ondelete="CASCADE"), nullable=False
    )
    requested_by: Mapped[str] = mapped_column(String(128), nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    vendor_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    amount_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    risk_tier: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    review_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint("amount_micros > 0", name="ck_expense_requests_positive_amount"),
        Index("ix_expense_requests_company_status", "company_id", "status"),
    )


class CompanyPackRecord(Base):
    __tablename__ = "company_packs"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    key: Mapped[str] = mapped_column(String(63), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    manifest: Mapped[dict] = mapped_column(JSONB, nullable=False)
    required_features: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    dependencies: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("key", "version", name="uq_company_packs_key_version"),
        UniqueConstraint("content_digest", name="uq_company_packs_digest"),
        Index("ix_company_packs_status_kind", "status", "kind"),
    )


class CompanyPackInstallationRecord(Base):
    __tablename__ = "company_pack_installations"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    company_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    pack_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("company_packs.id", ondelete="RESTRICT"), nullable=False
    )
    pack_key: Mapped[str] = mapped_column(String(63), nullable=False)
    pack_version: Mapped[str] = mapped_column(String(32), nullable=False)
    pack_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    installed_by: Mapped[str] = mapped_column(String(128), nullable=False)
    configuration: Mapped[dict] = mapped_column(JSONB, nullable=False)
    resource_refs: Mapped[list[dict]] = mapped_column(JSONB, nullable=False)
    installed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    upgraded_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    upgraded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "company_id", "pack_key", name="uq_company_pack_installations_key"
        ),
        Index(
            "ix_company_pack_installations_company",
            "company_id",
            "installed_at",
        ),
    )


class CompanyPackUpgradeRecord(Base):
    __tablename__ = "company_pack_upgrades"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    company_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    installation_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("company_pack_installations.id", ondelete="CASCADE"),
        nullable=False,
    )
    pack_key: Mapped[str] = mapped_column(String(63), nullable=False)
    from_version: Mapped[str] = mapped_column(String(32), nullable=False)
    from_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    to_version: Mapped[str] = mapped_column(String(32), nullable=False)
    to_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    upgraded_by: Mapped[str] = mapped_column(String(128), nullable=False)
    resource_changes: Mapped[list[dict]] = mapped_column(JSONB, nullable=False)
    migrated_object_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "installation_id", "to_digest", name="uq_company_pack_upgrades_target"
        ),
        CheckConstraint(
            "migrated_object_count >= 0", name="ck_company_pack_upgrades_object_count"
        ),
        Index("ix_company_pack_upgrades_company_created", "company_id", "created_at"),
    )

class OfficePlacementRecord(Base):
    __tablename__ = "office_employee_placements"

    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    grid_x: Mapped[int] = mapped_column(Integer, nullable=False)
    grid_z: Mapped[int] = mapped_column(Integer, nullable=False)
    department: Mapped[str] = mapped_column(String(63), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "agent_id", name="pk_office_employee_placements"),
        UniqueConstraint(
            "tenant_id",
            "grid_x",
            "grid_z",
            name="uq_office_employee_placements_cell",
        ),
        CheckConstraint("grid_x >= 0 AND grid_x < 35", name="ck_office_grid_x"),
        CheckConstraint("grid_z >= 0 AND grid_z < 12", name="ck_office_grid_z"),
        Index("ix_office_employee_placements_department", "tenant_id", "department"),
    )


class OfficeSpaceRecord(Base):
    __tablename__ = "office_custom_spaces"

    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    key: Mapped[str] = mapped_column(String(63), nullable=False)
    name: Mapped[str] = mapped_column(String(40), nullable=False)
    style: Mapped[str] = mapped_column(String(32), nullable=False)
    color: Mapped[str] = mapped_column(String(7), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "key", name="pk_office_custom_spaces"),
        UniqueConstraint(
            "tenant_id",
            "position",
            name="uq_office_custom_spaces_position",
        ),
        CheckConstraint(
            "position >= 0 AND position < 8",
            name="ck_office_custom_spaces_position",
        ),
    )


class ReplayBookmarkRecord(Base):
    __tablename__ = "replay_bookmarks"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    task_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "task_id", "event_id", name="uq_replay_bookmark_task_event"
        ),
        Index(
            "ix_replay_bookmarks_task_created",
            "tenant_id",
            "task_id",
            "created_at",
            "id",
        ),
    )


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
    content: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    storage_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("version_number >= 1", name="ck_artifact_versions_number"),
        CheckConstraint("size_bytes >= 1", name="ck_artifact_versions_size"),
        CheckConstraint(
            "(storage_class = 'INLINE_SMALL' AND content IS NOT NULL "
            "AND storage_key IS NULL AND octet_length(content) = size_bytes) OR "
            "(storage_class = 'FILESYSTEM' AND content IS NULL AND storage_key IS NOT NULL)",
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
    project_id: Mapped[str] = mapped_column(String(128), nullable=False, default="default")
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
        Index("ix_tasks_tenant_project_created_at", "tenant_id", "project_id", "created_at"),
    )


class GoalContractRecord(Base):
    __tablename__ = "task_goal_contracts"

    task_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("tasks.id", ondelete="CASCADE"),
        primary_key=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    constraints: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    success_criteria: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    digest: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_task_goal_contracts_version"),
        UniqueConstraint("task_id", "digest", name="uq_task_goal_contract_digest"),
    )


class PlanPatchRecord(Base):
    __tablename__ = "plan_patches"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    task_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    goal_digest: Mapped[str] = mapped_column(String(80), nullable=False)
    base_plan_version: Mapped[int] = mapped_column(Integer, nullable=False)
    base_plan_digest: Mapped[str] = mapped_column(String(80), nullable=False)
    proposed_plan_version: Mapped[int] = mapped_column(Integer, nullable=False)
    proposed_plan_digest: Mapped[str] = mapped_column(String(80), nullable=False)
    proposed_plan: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    requested_by: Mapped[str] = mapped_column(String(128), nullable=False)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "base_plan_version >= 1 AND proposed_plan_version = base_plan_version + 1",
            name="ck_plan_patches_versions",
        ),
        CheckConstraint("status IN ('VERIFIED', 'APPLIED')", name="ck_plan_patches_status"),
        Index("ix_plan_patches_task_created", "task_id", "created_at"),
    )


class QuotaPolicyRecord(Base):
    __tablename__ = "quota_policies"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    project_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    max_concurrent_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    weight: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("scope IN ('TENANT', 'PROJECT')", name="ck_quota_policies_scope"),
        CheckConstraint(
            "(scope = 'TENANT' AND project_id IS NULL) OR "
            "(scope = 'PROJECT' AND project_id IS NOT NULL)",
            name="ck_quota_policies_scope_project",
        ),
        CheckConstraint(
            "max_concurrent_attempts BETWEEN 1 AND 100000",
            name="ck_quota_policies_concurrency",
        ),
        CheckConstraint("weight BETWEEN 1 AND 1000", name="ck_quota_policies_weight"),
        UniqueConstraint(
            "tenant_id", "scope", "project_id", "version", name="uq_quota_policy_version"
        ),
        Index(
            "uq_quota_policy_active_tenant",
            "tenant_id",
            unique=True,
            postgresql_where=text("active AND scope = 'TENANT'"),
        ),
        Index(
            "uq_quota_policy_active_project",
            "tenant_id",
            "project_id",
            unique=True,
            postgresql_where=text("active AND scope = 'PROJECT'"),
        ),
    )


class QuotaReservationRecord(Base):
    __tablename__ = "quota_reservations"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    policy_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("quota_policies.id", ondelete="RESTRICT"), nullable=False
    )
    attempt_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("task_attempts.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    project_id: Mapped[str] = mapped_column(String(128), nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("policy_id", "attempt_id", name="uq_quota_reservation_policy_attempt"),
        Index("ix_quota_reservations_policy_active", "policy_id", "released_at"),
        Index("ix_quota_reservations_attempt", "attempt_id"),
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
            "action IN ('ACCEPT_CANDIDATE', 'REJECT_TASK', 'INCREASE_BUDGET_AND_RESUME', "
            "'RECONCILE_MCP_SUCCEEDED', 'RECONCILE_MCP_FAILED', "
            "'BIND_A2A_REMOTE_TASK', 'RECONCILE_A2A_NOT_DELIVERED')",
            name="ck_task_resolutions_action",
        ),
        Index("ix_task_resolutions_task_created", "task_id", "created_at"),
    )


class RuntimeRegistrationRecord(Base):
    __tablename__ = "runtime_registrations"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    owner_principal_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("principals.id", ondelete="RESTRICT"), nullable=False
    )
    visibility: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    default_version_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey(
            "runtime_versions.id",
            ondelete="RESTRICT",
            name="fk_runtime_registrations_default_version",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __mapper_args__ = {"version_id_col": version, "version_id_generator": False}
    __table_args__ = (
        CheckConstraint(
            "visibility IN ('platform', 'tenant', 'private')",
            name="ck_runtime_registrations_visibility",
        ),
        CheckConstraint(
            "status IN ('ACTIVE', 'SUSPENDED', 'REVOKED')",
            name="ck_runtime_registrations_status",
        ),
        CheckConstraint("version >= 1", name="ck_runtime_registrations_version"),
        CheckConstraint(
            "(visibility = 'platform' AND tenant_id IS NULL) OR "
            "(visibility IN ('tenant', 'private') AND tenant_id IS NOT NULL)",
            name="ck_runtime_registrations_scope",
        ),
        Index(
            "uq_runtime_registrations_platform_name",
            "name",
            unique=True,
            postgresql_where=text("visibility = 'platform'"),
        ),
        Index(
            "uq_runtime_registrations_tenant_name",
            "tenant_id",
            "name",
            unique=True,
            postgresql_where=text("visibility = 'tenant'"),
        ),
        Index(
            "uq_runtime_registrations_private_owner_name",
            "owner_principal_id",
            "name",
            unique=True,
            postgresql_where=text("visibility = 'private'"),
        ),
        Index("ix_runtime_registrations_tenant_status", "tenant_id", "status", "created_at"),
    )


class RuntimeVersionRecord(Base):
    __tablename__ = "runtime_versions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    runtime_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("runtime_registrations.id", ondelete="RESTRICT"), nullable=False
    )
    api_version: Mapped[int] = mapped_column(Integer, nullable=False)
    adapter_kind: Mapped[str] = mapped_column(String(128), nullable=False)
    artifact_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    configuration_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    descriptor: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    trust_profile: Mapped[str] = mapped_column(String(32), nullable=False)
    compatibility: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("api_version = 1", name="ck_runtime_versions_api_version"),
        CheckConstraint(
            "artifact_digest ~ '^[0-9a-f]{64}$' AND configuration_digest ~ '^[0-9a-f]{64}$'",
            name="ck_runtime_versions_digests",
        ),
        CheckConstraint(
            "trust_profile IN ('built_in', 'trusted_process', 'isolated', 'remote')",
            name="ck_runtime_versions_trust_profile",
        ),
        CheckConstraint(
            "status IN ('DRAFT', 'PUBLISHED', 'DEPRECATED', 'REVOKED')",
            name="ck_runtime_versions_status",
        ),
        CheckConstraint(
            "(status = 'PUBLISHED' AND published_at IS NOT NULL) OR "
            "(status <> 'PUBLISHED')",
            name="ck_runtime_versions_publication",
        ),
        UniqueConstraint(
            "runtime_id",
            "artifact_digest",
            "configuration_digest",
            name="uq_runtime_versions_immutable_identity",
        ),
        Index("ix_runtime_versions_runtime_status", "runtime_id", "status", "created_at"),
    )


class RuntimeExecutionRecord(Base):
    __tablename__ = "runtime_executions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    run_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("task_runs.id", ondelete="RESTRICT"), nullable=False
    )
    runtime_version_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("runtime_versions.id", ondelete="RESTRICT"), nullable=False
    )
    assignment_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    assignment_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    dispatch_key: Mapped[str] = mapped_column(String(512), nullable=False)
    dispatch_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_execution_ref: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    provider_generation: Mapped[str | None] = mapped_column(String(256), nullable=True)
    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    current_owner_attempt_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey(
            "task_attempts.id",
            ondelete="RESTRICT",
            name="fk_runtime_executions_owner_attempt",
            use_alter=True,
        ),
        nullable=True,
    )
    current_fencing_token: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider_sequence: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    checkpoint_ref: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    workspace_ref: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __mapper_args__ = {"version_id_col": version, "version_id_generator": False}
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_runtime_executions_version"),
        CheckConstraint(
            "assignment_digest ~ '^[0-9a-f]{64}$' AND dispatch_digest ~ '^[0-9a-f]{64}$'",
            name="ck_runtime_executions_digests",
        ),
        CheckConstraint(
            "phase IN ('PREPARED', 'DISPATCHING', 'ACCEPTED', 'RUNNING', 'WAITING_INPUT', "
            "'WAITING_APPROVAL', 'PAUSE_REQUESTED', 'PAUSED', 'CANCEL_REQUESTED', "
            "'SUCCEEDED', 'FAILED', 'CANCELED', 'TIMED_OUT', 'LOST', 'OUTCOME_UNKNOWN')",
            name="ck_runtime_executions_phase",
        ),
        CheckConstraint(
            "current_fencing_token IS NULL OR current_fencing_token >= 0",
            name="ck_runtime_executions_fencing",
        ),
        CheckConstraint(
            "provider_sequence IS NULL OR provider_sequence >= 0",
            name="ck_runtime_executions_provider_sequence",
        ),
        CheckConstraint(
            "(phase IN ('SUCCEEDED', 'FAILED', 'CANCELED', 'TIMED_OUT', 'LOST', 'OUTCOME_UNKNOWN') "
            "AND terminal_at IS NOT NULL) OR "
            "(phase NOT IN ('SUCCEEDED', 'FAILED', 'CANCELED', 'TIMED_OUT', 'LOST', "
            "'OUTCOME_UNKNOWN') "
            "AND terminal_at IS NULL)",
            name="ck_runtime_executions_terminal_at",
        ),
        UniqueConstraint("tenant_id", "dispatch_key", name="uq_runtime_executions_dispatch_key"),
        Index(
            "uq_runtime_executions_one_active_per_run",
            "run_id",
            unique=True,
            postgresql_where=text(
                "phase NOT IN ('SUCCEEDED', 'FAILED', 'CANCELED', 'TIMED_OUT', 'LOST', "
                "'OUTCOME_UNKNOWN')"
            ),
        ),
        Index("ix_runtime_executions_tenant_updated", "tenant_id", "updated_at"),
    )


class RuntimeOwnershipHistoryRecord(Base):
    __tablename__ = "runtime_ownership_history"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    runtime_execution_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("runtime_executions.id", ondelete="CASCADE"), nullable=False
    )
    attempt_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("task_attempts.id", ondelete="RESTRICT"), nullable=False
    )
    fencing_token: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_attempt_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("task_attempts.id", ondelete="RESTRICT"), nullable=True
    )
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    release_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claim_reason: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "runtime_execution_id",
            "fencing_token",
            name="uq_runtime_ownership_execution_fence",
        ),
        Index("ix_runtime_ownership_execution_claimed", "runtime_execution_id", "claimed_at"),
    )


class RuntimeObservationRecord(Base):
    __tablename__ = "runtime_observations"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    runtime_execution_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("runtime_executions.id", ondelete="CASCADE"), nullable=False
    )
    observation_id: Mapped[str] = mapped_column(String(512), nullable=False)
    observation_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    assignment_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    assignment_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_event_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    provider_sequence: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    safe_summary: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    processing_outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    processing_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __table_args__ = (
        CheckConstraint(
            "observation_digest ~ '^[0-9a-f]{64}$' AND assignment_digest ~ '^[0-9a-f]{64}$'",
            name="ck_runtime_observations_digests",
        ),
        CheckConstraint(
            "phase IN ('PREPARED', 'DISPATCHING', 'ACCEPTED', 'RUNNING', 'WAITING_INPUT', "
            "'WAITING_APPROVAL', 'PAUSE_REQUESTED', 'PAUSED', 'CANCEL_REQUESTED', "
            "'SUCCEEDED', 'FAILED', 'CANCELED', 'TIMED_OUT', 'LOST', 'OUTCOME_UNKNOWN')",
            name="ck_runtime_observations_phase",
        ),
        CheckConstraint(
            "processing_outcome IN ('APPLIED', 'DUPLICATE', 'GAP', 'STALE_OWNER', 'CONFLICT', "
            "'RECONCILED')",
            name="ck_runtime_observations_outcome",
        ),
        CheckConstraint(
            "provider_sequence IS NULL OR provider_sequence >= 0",
            name="ck_runtime_observations_sequence",
        ),
        Index("ix_runtime_observations_execution_received", "runtime_execution_id", "received_at"),
        Index(
            "ix_runtime_observations_execution_identity", "runtime_execution_id", "observation_id"
        ),
        Index(
            "ix_runtime_observations_execution_digest", "runtime_execution_id", "observation_digest"
        ),
    )


class RuntimeLifecycleOperationRecord(Base):
    __tablename__ = "runtime_lifecycle_operations"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    runtime_execution_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("runtime_executions.id", ondelete="CASCADE"), nullable=False
    )
    operation_id: Mapped[str] = mapped_column(String(512), nullable=False)
    operation: Mapped[str] = mapped_column(String(16), nullable=False)
    intent_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    receipt_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __mapper_args__ = {"version_id_col": version, "version_id_generator": False}
    __table_args__ = (
        CheckConstraint(
            "operation IN ('cancel', 'pause', 'resume')",
            name="ck_runtime_lifecycle_operation",
        ),
        CheckConstraint(
            "status IN ('REQUESTED', 'ACCEPTED', 'REJECTED', 'EXPIRED')",
            name="ck_runtime_lifecycle_status",
        ),
        CheckConstraint(
            "intent_digest ~ '^[0-9a-f]{64}$'",
            name="ck_runtime_lifecycle_digest",
        ),
        UniqueConstraint(
            "runtime_execution_id", "operation_id", name="uq_runtime_lifecycle_operation"
        ),
        Index("ix_runtime_lifecycle_tenant_status", "tenant_id", "status", "deadline"),
    )


class RuntimeComparisonRecord(Base):
    """Durable parity evidence; it never changes Task/Run authority."""

    __tablename__ = "runtime_comparisons"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    task_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("task_runs.id", ondelete="CASCADE"), nullable=False
    )
    attempt_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("task_attempts.id", ondelete="RESTRICT"), nullable=False
    )
    authoritative_path: Mapped[str] = mapped_column(String(16), nullable=False)
    authoritative_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    comparison_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    comparison_observation_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    matches: Mapped[bool] = mapped_column(Boolean, nullable=False)
    mismatches: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("authoritative_path = 'legacy'", name="ck_runtime_comparison_authority"),
        CheckConstraint(
            "authoritative_digest ~ '^[0-9a-f]{64}$' AND comparison_digest ~ '^[0-9a-f]{64}$'",
            name="ck_runtime_comparison_digests",
        ),
        UniqueConstraint("run_id", "attempt_id", name="uq_runtime_comparisons_run_attempt"),
        Index("ix_runtime_comparisons_tenant_created", "tenant_id", "created_at"),
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
    runtime_version_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey(
            "runtime_versions.id",
            ondelete="RESTRICT",
            name="fk_task_runs_runtime_version",
        ),
        nullable=True,
    )
    runtime_execution_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey(
            "runtime_executions.id",
            ondelete="RESTRICT",
            name="fk_task_runs_runtime_execution",
            use_alter=True,
        ),
        nullable=True,
    )
    runtime_execution_intent_id: Mapped[UUID | None] = mapped_column(
        Uuid, nullable=True
    )
    runtime_authority: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="legacy"
    )
    comparison_mode: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="off"
    )
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
        CheckConstraint(
            "runtime_authority IN ('legacy', 'managed')",
            name="ck_task_runs_runtime_authority",
        ),
        CheckConstraint(
            "comparison_mode IN ('off', 'deterministic_shadow')",
            name="ck_task_runs_comparison_mode",
        ),
        CheckConstraint(
            "(runtime_authority = 'managed' AND comparison_mode = 'off' AND "
            "runtime_version_id IS NOT NULL AND (runtime_execution_id IS NOT NULL OR "
            "runtime_execution_intent_id IS NOT NULL)) OR "
            "(runtime_authority = 'legacy' AND (comparison_mode = 'off' OR "
            "(runtime_version_id IS NOT NULL AND (runtime_execution_id IS NOT NULL OR "
            "runtime_execution_intent_id IS NOT NULL))))",
            name="ck_task_runs_comparison_pin",
        ),
        CheckConstraint(
            "runtime_execution_intent_id IS NULL OR runtime_execution_id IS NULL OR "
            "runtime_execution_intent_id = runtime_execution_id",
            name="ck_task_runs_runtime_execution_identity",
        ),
        Index("ix_task_runs_task_id_queued_at", "task_id", "queued_at"),
        Index("ix_task_runs_agent_version_id", "agent_version_id"),
        Index("ix_task_runs_subtask_id", "subtask_id"),
        Index("ix_task_runs_runtime_version", "runtime_version_id"),
        Index("ix_task_runs_runtime_execution", "runtime_execution_id"),
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
            "status IN ('RUNNING', 'SUCCEEDED', 'FAILED', 'OUTCOME_UNKNOWN')",
            name="ck_tool_invocations_status",
        ),
        CheckConstraint(
            "result_bytes IS NULL OR result_bytes >= 0",
            name="ck_tool_invocations_result_bytes",
        ),
        Index("ix_tool_invocations_task_started", "task_id", "started_at"),
        Index("ix_tool_invocations_run_started", "run_id", "started_at"),
        Index("ix_tool_invocations_tenant_status", "tenant_id", "status"),
    )


class ToolExecutionAuthorizationRecord(Base):
    __tablename__ = "tool_execution_authorizations"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    task_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    governed_action_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("governed_actions.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    principal_id: Mapped[str] = mapped_column(String(255), nullable=False)
    server_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("mcp_servers.id", ondelete="RESTRICT"), nullable=False
    )
    server_version_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("mcp_server_versions.id", ondelete="RESTRICT"), nullable=False
    )
    configuration_digest: Mapped[str] = mapped_column(String(80), nullable=False)
    tool_key: Mapped[str] = mapped_column(String(255), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    side_effect: Mapped[str] = mapped_column(String(32), nullable=False)
    schema_digest: Mapped[str] = mapped_column(String(80), nullable=False)
    arguments_digest: Mapped[str] = mapped_column(String(80), nullable=False)
    idempotency_key_digest: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    invocation_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("tool_invocations.id", ondelete="RESTRICT"), nullable=True, unique=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "side_effect = 'IDEMPOTENT_WRITE'",
            name="ck_tool_execution_authorizations_side_effect",
        ),
        CheckConstraint(
            "status IN ('AUTHORIZED', 'EXECUTING', 'SUCCEEDED', 'FAILED', 'OUTCOME_UNKNOWN')",
            name="ck_tool_execution_authorizations_status",
        ),
        Index("ix_tool_execution_authorizations_tenant_status", "tenant_id", "status"),
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


class McpDiscoverySnapshotRecord(Base):
    __tablename__ = "mcp_discovery_snapshots"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    server_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("mcp_servers.id", ondelete="CASCADE"), nullable=False
    )
    server_version_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("mcp_server_versions.id", ondelete="CASCADE"), nullable=False
    )
    configuration_digest: Mapped[str] = mapped_column(String(80), nullable=False)
    protocol_version: Mapped[str] = mapped_column(String(32), nullable=False)
    server_name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    capability_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    discovered_tools: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status IN ('COMPATIBLE', 'EXPANDED', 'INCOMPATIBLE', 'FAILED')",
            name="ck_mcp_discovery_snapshots_status",
        ),
        Index(
            "ix_mcp_discovery_snapshots_version_fetched",
            "server_version_id",
            "fetched_at",
        ),
        Index("ix_mcp_discovery_snapshots_tenant_status", "tenant_id", "status"),
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
        CheckConstraint(
            "trust_tier IN ('RESTRICTED', 'TRUSTED', 'HIGH_ASSURANCE')",
            name="ck_a2a_peers_trust_tier",
        ),
        CheckConstraint(
            "status IN ('REGISTERED', 'ACTIVE', 'SUSPENDED')",
            name="ck_a2a_peers_status",
        ),
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
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "signature_status IN ('UNSIGNED', 'PRESENT_UNVERIFIED')",
            name="ck_a2a_cards_signature_status",
        ),
        CheckConstraint(
            "source IN ('MANUAL', 'DISCOVERED')",
            name="ck_a2a_card_snapshot_source",
        ),
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
    poll_failure_count: Mapped[int] = mapped_column(Integer, nullable=False)
    next_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    poll_lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    poll_lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancel_request_count: Mapped[int] = mapped_column(Integer, nullable=False)
    cancel_request_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    late_result: Mapped[bool] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    send_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)

    __mapper_args__ = {"version_id_col": revision, "version_id_generator": False}
    __table_args__ = (
        CheckConstraint(
            "status IN ('PREPARED', 'SENDING', 'WAITING_REMOTE', 'OUTCOME_UNKNOWN', "
            "'INTERVENTION_REQUIRED', 'CANCELING', 'CANCEL_PENDING', "
            "'CANCEL_OUTCOME_UNKNOWN', 'COMPLETED', 'FAILED', 'REJECTED', 'CANCELED')",
            name="ck_a2a_correlations_status",
        ),
        CheckConstraint("poll_count >= 0", name="ck_a2a_correlations_poll_count"),
        CheckConstraint("poll_failure_count >= 0", name="ck_a2a_correlations_poll_failure_count"),
        CheckConstraint("cancel_request_count >= 0", name="ck_a2a_correlations_cancel_count"),
        Index("ix_a2a_correlations_tenant_status", "tenant_id", "status", "updated_at"),
        Index(
            "ix_a2a_correlations_due_poll",
            "tenant_id",
            "next_poll_at",
            "poll_lease_expires_at",
            postgresql_where=text(
                "status IN ('WAITING_REMOTE', 'CANCELING', 'CANCEL_PENDING', "
                "'CANCEL_OUTCOME_UNKNOWN') AND remote_task_id IS NOT NULL"
            ),
        ),
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
    obligations: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    approval_stages: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    current_stage: Mapped[int] = mapped_column(Integer, nullable=False)
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
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "outcome IN ('APPROVE', 'REJECT')",
            name="ck_approval_decisions_outcome",
        ),
        UniqueConstraint(
            "approval_id",
            "stage",
            "approver_id",
            name="uq_approval_decisions_stage_approver",
        ),
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
