from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from agentmesh.application.registry_services import AgentCandidate, AgentDefinitionAggregate
from agentmesh.domain.registry import (
    AgentDefinitionLifecycle,
    AgentDeployment,
    AgentInstance,
    AgentVersion,
    AgentVersionStatus,
    AgentVisibility,
    Capability,
    DeploymentStatus,
    InstanceHealth,
)
from agentmesh.domain.tasks import RunStatus, TaskRun


class CreateAgentDefinitionRequest(BaseModel):
    owner_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=3, max_length=63)
    description: str = Field(default="", max_length=20_000)
    visibility: AgentVisibility = AgentVisibility.PRIVATE
    tags: list[str] = Field(default_factory=list, max_length=100)


class CreateAgentVersionRequest(BaseModel):
    semantic_version: str = Field(min_length=5, max_length=128)
    role: str = Field(min_length=1, max_length=10_000)
    instructions: str = Field(min_length=1, max_length=100_000)
    declared_capabilities: list[str] = Field(min_length=1, max_length=200)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    model_policy: dict[str, Any] = Field(default_factory=dict)
    tool_profile: dict[str, Any] = Field(default_factory=dict)
    knowledge_profile: dict[str, Any] = Field(default_factory=dict)
    policy_profile: dict[str, Any] = Field(default_factory=dict)
    risk_class: str = Field(default="LOW", max_length=32)
    data_classification_ceiling: str = Field(default="INTERNAL", max_length=32)
    resource_defaults: dict[str, Any] = Field(default_factory=dict)
    runtime_adapter: str = Field(default="local", min_length=1, max_length=128)
    artifact_digest: str | None = Field(default=None, max_length=255)
    execution_modes: list[str] = Field(default_factory=lambda: ["async"], min_length=1)
    compatibility: dict[str, Any] = Field(default_factory=dict)


class PublishAgentVersionRequest(BaseModel):
    verified_capabilities: list[str] = Field(default_factory=list, max_length=200)
    make_default: bool = True


class RevokeAgentVersionRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2_000)


class SetDefaultVersionRequest(BaseModel):
    agent_version_id: UUID


class AgentVersionResponse(BaseModel):
    id: UUID
    definition_id: UUID
    semantic_version: str
    status: AgentVersionStatus
    content_digest: str | None
    role: str
    instructions: str
    declared_capabilities: list[str]
    verified_capabilities: list[str]
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    model_policy: dict[str, Any]
    tool_profile: dict[str, Any]
    knowledge_profile: dict[str, Any]
    policy_profile: dict[str, Any]
    risk_class: str
    data_classification_ceiling: str
    resource_defaults: dict[str, Any]
    runtime_adapter: str
    artifact_digest: str | None
    execution_modes: list[str]
    compatibility: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    published_at: datetime | None
    revoked_at: datetime | None
    revoke_reason: str | None

    @classmethod
    def from_domain(cls, value: AgentVersion) -> "AgentVersionResponse":
        return cls(
            id=value.id,
            definition_id=value.definition_id,
            semantic_version=value.semantic_version,
            status=value.status,
            content_digest=value.content_digest,
            role=value.role,
            instructions=value.instructions,
            declared_capabilities=list(value.declared_capabilities),
            verified_capabilities=list(value.verified_capabilities),
            input_schema=dict(value.input_schema),
            output_schema=dict(value.output_schema),
            model_policy=dict(value.model_policy),
            tool_profile=dict(value.tool_profile),
            knowledge_profile=dict(value.knowledge_profile),
            policy_profile=dict(value.policy_profile),
            risk_class=value.risk_class,
            data_classification_ceiling=value.data_classification_ceiling,
            resource_defaults=dict(value.resource_defaults),
            runtime_adapter=value.runtime_adapter,
            artifact_digest=value.artifact_digest,
            execution_modes=list(value.execution_modes),
            compatibility=dict(value.compatibility),
            created_at=value.created_at,
            updated_at=value.updated_at,
            published_at=value.published_at,
            revoked_at=value.revoked_at,
            revoke_reason=value.revoke_reason,
        )


class AgentDefinitionResponse(BaseModel):
    id: UUID
    tenant_id: str
    owner_id: str
    name: str
    description: str
    visibility: AgentVisibility
    lifecycle: AgentDefinitionLifecycle
    default_version_id: UUID | None
    tags: list[str]
    version: int
    created_at: datetime
    updated_at: datetime
    versions: list[AgentVersionResponse]

    @classmethod
    def from_aggregate(cls, value: AgentDefinitionAggregate) -> "AgentDefinitionResponse":
        definition = value.definition
        return cls(
            id=definition.id,
            tenant_id=definition.tenant_id,
            owner_id=definition.owner_id,
            name=definition.name,
            description=definition.description,
            visibility=definition.visibility,
            lifecycle=definition.lifecycle,
            default_version_id=definition.default_version_id,
            tags=list(definition.tags),
            version=definition.version,
            created_at=definition.created_at,
            updated_at=definition.updated_at,
            versions=[AgentVersionResponse.from_domain(item) for item in value.versions],
        )


class AgentDefinitionListResponse(BaseModel):
    items: list[AgentDefinitionResponse]
    limit: int
    offset: int


class CreateCapabilityRequest(BaseModel):
    key: str = Field(min_length=3, max_length=255)
    version: str = Field(min_length=5, max_length=128)
    description: str = Field(default="", max_length=20_000)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    evidence_requirements: list[str] = Field(default_factory=list, max_length=100)


class CapabilityResponse(BaseModel):
    id: UUID
    tenant_id: str
    key: str
    version: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    evidence_requirements: list[str]
    created_at: datetime

    @classmethod
    def from_domain(cls, value: Capability) -> "CapabilityResponse":
        return cls(
            id=value.id,
            tenant_id=value.tenant_id,
            key=value.key,
            version=value.version,
            description=value.description,
            input_schema=dict(value.input_schema),
            output_schema=dict(value.output_schema),
            evidence_requirements=list(value.evidence_requirements),
            created_at=value.created_at,
        )


class CapabilityListResponse(BaseModel):
    items: list[CapabilityResponse]
    limit: int
    offset: int


class CreateAgentDeploymentRequest(BaseModel):
    environment: str = Field(min_length=1, max_length=128)
    runtime_kind: str = Field(min_length=1, max_length=128)
    endpoint_reference: str | None = Field(default=None, max_length=512)
    remote_peer_id: str | None = Field(default=None, max_length=255)
    traffic_weight: int = Field(default=100, ge=0, le=100)
    region: str | None = Field(default=None, max_length=128)
    rollout_policy: dict[str, Any] = Field(default_factory=dict)


class UpdateAgentDeploymentStatusRequest(BaseModel):
    desired_status: DeploymentStatus | None = None
    current_status: DeploymentStatus | None = None


class AgentDeploymentResponse(BaseModel):
    id: UUID
    agent_version_id: UUID
    environment: str
    runtime_kind: str
    remote_peer_id: str | None
    endpoint_reference: str | None
    desired_status: DeploymentStatus
    current_status: DeploymentStatus
    traffic_weight: int
    region: str | None
    rollout_policy: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, value: AgentDeployment) -> "AgentDeploymentResponse":
        return cls(
            id=value.id,
            agent_version_id=value.agent_version_id,
            environment=value.environment,
            runtime_kind=value.runtime_kind,
            remote_peer_id=value.remote_peer_id,
            endpoint_reference=value.endpoint_reference,
            desired_status=value.desired_status,
            current_status=value.current_status,
            traffic_weight=value.traffic_weight,
            region=value.region,
            rollout_policy=dict(value.rollout_policy),
            created_at=value.created_at,
            updated_at=value.updated_at,
        )


class CandidateSearchRequest(BaseModel):
    required_capabilities: list[str] = Field(default_factory=list, max_length=200)
    execution_mode: str | None = Field(default=None, max_length=64)


class AgentCandidateResponse(BaseModel):
    agent_definition_id: UUID
    agent_name: str
    agent_version: AgentVersionResponse
    deployments: list[AgentDeploymentResponse]

    @classmethod
    def from_domain(cls, value: AgentCandidate) -> "AgentCandidateResponse":
        return cls(
            agent_definition_id=value.definition.id,
            agent_name=value.definition.name,
            agent_version=AgentVersionResponse.from_domain(value.agent_version),
            deployments=[AgentDeploymentResponse.from_domain(item) for item in value.deployments],
        )


class AgentInstanceHeartbeatRequest(BaseModel):
    health: InstanceHealth
    capacity_slots: int = Field(ge=0)
    active_slots: int = Field(ge=0)
    protocol_endpoint: str | None = Field(default=None, max_length=512)
    lease_epoch: int = Field(ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentInstanceResponse(BaseModel):
    id: UUID
    deployment_id: UUID
    external_instance_id: str
    health: InstanceHealth
    last_heartbeat_at: datetime
    capacity_slots: int
    active_slots: int
    protocol_endpoint: str | None
    lease_epoch: int
    metadata: dict[str, Any]

    @classmethod
    def from_domain(cls, value: AgentInstance) -> "AgentInstanceResponse":
        return cls(
            id=value.id,
            deployment_id=value.deployment_id,
            external_instance_id=value.external_instance_id,
            health=value.health,
            last_heartbeat_at=value.last_heartbeat_at,
            capacity_slots=value.capacity_slots,
            active_slots=value.active_slots,
            protocol_endpoint=value.protocol_endpoint,
            lease_epoch=value.lease_epoch,
            metadata=dict(value.metadata),
        )


class AffectedRunResponse(BaseModel):
    id: UUID
    task_id: UUID
    status: RunStatus
    queued_at: datetime
    started_at: datetime | None

    @classmethod
    def from_domain(cls, value: TaskRun) -> "AffectedRunResponse":
        return cls(
            id=value.id,
            task_id=value.task_id,
            status=value.status,
            queued_at=value.queued_at,
            started_at=value.started_at,
        )
