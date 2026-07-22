from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from agentmesh.domain.errors import (
    InvalidAgentDefinition,
    InvalidAgentTransition,
    InvalidAgentVersion,
)
from agentmesh.domain.model_runtime import AgentToolPolicy, ModelRuntimePolicy
from agentmesh.domain.tasks import utc_now

AGENT_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]{2,62}$")
SEMVER_PATTERN = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
CAPABILITY_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*(?:\.[a-z0-9][a-z0-9_-]*)+$")


class AgentVisibility(str, Enum):
    PRIVATE = "PRIVATE"
    TENANT = "TENANT"
    PUBLIC = "PUBLIC"


class AgentDefinitionLifecycle(str, Enum):
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class AgentVersionStatus(str, Enum):
    DRAFT = "DRAFT"
    IN_REVIEW = "IN_REVIEW"
    PUBLISHED = "PUBLISHED"
    DEPRECATED = "DEPRECATED"
    RETIRED = "RETIRED"
    REJECTED = "REJECTED"
    REVOKED = "REVOKED"


class DeploymentStatus(str, Enum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    DRAINING = "DRAINING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


class InstanceHealth(str, Enum):
    UNKNOWN = "UNKNOWN"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNHEALTHY = "UNHEALTHY"
    DRAINING = "DRAINING"


def normalize_agent_name(value: str) -> str:
    normalized = value.strip().lower()
    if not AGENT_NAME_PATTERN.fullmatch(normalized):
        raise InvalidAgentDefinition(
            "Agent name must be 3-63 lowercase letters, numbers, or hyphens"
        )
    return normalized


def validate_capability_key(value: str) -> str:
    normalized = value.strip().lower()
    if not CAPABILITY_PATTERN.fullmatch(normalized):
        raise InvalidAgentVersion(
            "Capability key must be a namespaced value such as code.review.python"
        )
    return normalized


@dataclass
class AgentDefinition:
    id: UUID
    tenant_id: str
    owner_id: str
    name: str
    description: str
    visibility: AgentVisibility
    lifecycle: AgentDefinitionLifecycle
    default_version_id: UUID | None
    tags: tuple[str, ...]
    version: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def create(
        cls,
        *,
        tenant_id: str,
        owner_id: str,
        name: str,
        description: str,
        visibility: AgentVisibility = AgentVisibility.PRIVATE,
        tags: list[str] | tuple[str, ...] = (),
    ) -> AgentDefinition:
        normalized_tenant = tenant_id.strip()
        normalized_owner = owner_id.strip()
        if not normalized_tenant or not normalized_owner:
            raise InvalidAgentDefinition("Agent tenant and owner must not be empty")
        normalized_tags = tuple(sorted({tag.strip().lower() for tag in tags if tag.strip()}))
        if len(normalized_tags) > 100:
            raise InvalidAgentDefinition("Agent Definition cannot have more than 100 tags")
        now = utc_now()
        return cls(
            id=uuid4(),
            tenant_id=normalized_tenant,
            owner_id=normalized_owner,
            name=normalize_agent_name(name),
            description=description.strip(),
            visibility=visibility,
            lifecycle=AgentDefinitionLifecycle.ACTIVE,
            default_version_id=None,
            tags=normalized_tags,
            version=1,
            created_at=now,
            updated_at=now,
        )

    def set_default(self, agent_version: AgentVersion) -> None:
        if self.lifecycle != AgentDefinitionLifecycle.ACTIVE:
            raise InvalidAgentTransition("Archived Agent Definition cannot select a default")
        if agent_version.definition_id != self.id:
            raise InvalidAgentVersion("Default Agent Version belongs to another definition")
        if agent_version.status != AgentVersionStatus.PUBLISHED:
            raise InvalidAgentTransition("Only a published Agent Version can be the default")
        self.default_version_id = agent_version.id
        self._touch()

    def archive(self) -> None:
        if self.lifecycle == AgentDefinitionLifecycle.ARCHIVED:
            raise InvalidAgentTransition("Agent Definition is already archived")
        self.lifecycle = AgentDefinitionLifecycle.ARCHIVED
        self.default_version_id = None
        self._touch()

    def clear_default(self, agent_version_id: UUID) -> None:
        if self.default_version_id == agent_version_id:
            self.default_version_id = None
            self._touch()

    def _touch(self) -> None:
        self.version += 1
        self.updated_at = utc_now()


@dataclass
class AgentVersion:
    id: UUID
    definition_id: UUID
    semantic_version: str
    status: AgentVersionStatus
    content_digest: str | None
    role: str
    instructions: str
    declared_capabilities: tuple[str, ...]
    verified_capabilities: tuple[str, ...]
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
    execution_modes: tuple[str, ...]
    compatibility: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    published_at: datetime | None
    revoked_at: datetime | None
    revoke_reason: str | None

    @classmethod
    def create_draft(
        cls,
        *,
        definition_id: UUID,
        semantic_version: str,
        role: str,
        instructions: str,
        declared_capabilities: list[str] | tuple[str, ...],
        input_schema: dict[str, Any],
        output_schema: dict[str, Any],
        model_policy: dict[str, Any] | None = None,
        tool_profile: dict[str, Any] | None = None,
        knowledge_profile: dict[str, Any] | None = None,
        policy_profile: dict[str, Any] | None = None,
        risk_class: str = "LOW",
        data_classification_ceiling: str = "INTERNAL",
        resource_defaults: dict[str, Any] | None = None,
        runtime_adapter: str = "local",
        artifact_digest: str | None = None,
        execution_modes: list[str] | tuple[str, ...] = ("sync",),
        compatibility: dict[str, Any] | None = None,
    ) -> AgentVersion:
        semantic_version = semantic_version.strip()
        if not SEMVER_PATTERN.fullmatch(semantic_version):
            raise InvalidAgentVersion("Agent semantic version must follow SemVer")
        normalized_role = role.strip()
        normalized_instructions = instructions.strip()
        normalized_adapter = runtime_adapter.strip()
        if not normalized_role or not normalized_instructions or not normalized_adapter:
            raise InvalidAgentVersion("Role, instructions, and runtime adapter are required")
        capabilities = tuple(
            sorted({validate_capability_key(value) for value in declared_capabilities})
        )
        if not capabilities:
            raise InvalidAgentVersion("Agent Version must declare at least one capability")
        if len(capabilities) > 200:
            raise InvalidAgentVersion("Agent Version cannot declare more than 200 capabilities")
        normalized_model_policy = ModelRuntimePolicy.from_dict(dict(model_policy or {}))
        normalized_tool_profile = AgentToolPolicy.from_dict(dict(tool_profile or {}))
        modes = tuple(sorted({mode.strip().lower() for mode in execution_modes if mode.strip()}))
        if not modes:
            raise InvalidAgentVersion("Agent Version must support an execution mode")
        now = utc_now()
        return cls(
            id=uuid4(),
            definition_id=definition_id,
            semantic_version=semantic_version,
            status=AgentVersionStatus.DRAFT,
            content_digest=None,
            role=normalized_role,
            instructions=normalized_instructions,
            declared_capabilities=capabilities,
            verified_capabilities=(),
            input_schema=dict(input_schema),
            output_schema=dict(output_schema),
            model_policy=normalized_model_policy.to_dict(),
            tool_profile=normalized_tool_profile.to_dict(),
            knowledge_profile=dict(knowledge_profile or {}),
            policy_profile=dict(policy_profile or {}),
            risk_class=risk_class.strip().upper(),
            data_classification_ceiling=data_classification_ceiling.strip().upper(),
            resource_defaults=dict(resource_defaults or {}),
            runtime_adapter=normalized_adapter,
            artifact_digest=artifact_digest.strip() if artifact_digest else None,
            execution_modes=modes,
            compatibility=dict(compatibility or {}),
            created_at=now,
            updated_at=now,
            published_at=None,
            revoked_at=None,
            revoke_reason=None,
        )

    def submit_for_review(self) -> None:
        self._require_status(AgentVersionStatus.DRAFT, "submit for review")
        self.status = AgentVersionStatus.IN_REVIEW
        self.updated_at = utc_now()

    def reject(self) -> None:
        self._require_status(AgentVersionStatus.IN_REVIEW, "reject")
        self.status = AgentVersionStatus.REJECTED
        self.updated_at = utc_now()

    def publish(self, verified_capabilities: list[str] | tuple[str, ...]) -> None:
        self._require_status(AgentVersionStatus.IN_REVIEW, "publish")
        verified = tuple(
            sorted({validate_capability_key(value) for value in verified_capabilities})
        )
        if not set(verified).issubset(self.declared_capabilities):
            raise InvalidAgentVersion("Verified capabilities must have been declared")
        now = utc_now()
        self.verified_capabilities = verified
        self.content_digest = self._calculate_digest()
        self.status = AgentVersionStatus.PUBLISHED
        self.published_at = now
        self.updated_at = now

    def deprecate(self) -> None:
        self._require_status(AgentVersionStatus.PUBLISHED, "deprecate")
        self.status = AgentVersionStatus.DEPRECATED
        self.updated_at = utc_now()

    def retire(self) -> None:
        if self.status not in {AgentVersionStatus.PUBLISHED, AgentVersionStatus.DEPRECATED}:
            raise InvalidAgentTransition(f"Cannot retire Agent Version from {self.status.value}")
        self.status = AgentVersionStatus.RETIRED
        self.updated_at = utc_now()

    def revoke(self, reason: str) -> None:
        if self.status not in {AgentVersionStatus.PUBLISHED, AgentVersionStatus.DEPRECATED}:
            raise InvalidAgentTransition(f"Cannot revoke Agent Version from {self.status.value}")
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise InvalidAgentVersion("Revocation requires a reason")
        now = utc_now()
        self.status = AgentVersionStatus.REVOKED
        self.revoked_at = now
        self.revoke_reason = normalized_reason
        self.updated_at = now

    def _calculate_digest(self) -> str:
        content = {
            "semantic_version": self.semantic_version,
            "role": self.role,
            "instructions": self.instructions,
            "declared_capabilities": self.declared_capabilities,
            "verified_capabilities": self.verified_capabilities,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "model_policy": self.model_policy,
            "tool_profile": self.tool_profile,
            "knowledge_profile": self.knowledge_profile,
            "policy_profile": self.policy_profile,
            "risk_class": self.risk_class,
            "data_classification_ceiling": self.data_classification_ceiling,
            "resource_defaults": self.resource_defaults,
            "runtime_adapter": self.runtime_adapter,
            "artifact_digest": self.artifact_digest,
            "execution_modes": self.execution_modes,
            "compatibility": self.compatibility,
        }
        encoded = json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
        return f"sha256:{sha256(encoded).hexdigest()}"

    def _require_status(self, expected: AgentVersionStatus, action: str) -> None:
        if self.status != expected:
            raise InvalidAgentTransition(
                f"Cannot {action} Agent Version {self.id} from {self.status.value}"
            )


@dataclass(frozen=True)
class Capability:
    id: UUID
    tenant_id: str
    key: str
    version: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    evidence_requirements: tuple[str, ...]
    created_at: datetime

    @classmethod
    def create(
        cls,
        *,
        tenant_id: str,
        key: str,
        version: str,
        description: str,
        input_schema: dict[str, Any],
        output_schema: dict[str, Any],
        evidence_requirements: list[str] | tuple[str, ...] = (),
    ) -> Capability:
        normalized_tenant = tenant_id.strip()
        if not normalized_tenant:
            raise InvalidAgentVersion("Capability tenant must not be empty")
        if not SEMVER_PATTERN.fullmatch(version.strip()):
            raise InvalidAgentVersion("Capability version must follow SemVer")
        return cls(
            id=uuid4(),
            tenant_id=normalized_tenant,
            key=validate_capability_key(key),
            version=version.strip(),
            description=description.strip(),
            input_schema=dict(input_schema),
            output_schema=dict(output_schema),
            evidence_requirements=tuple(
                requirement.strip() for requirement in evidence_requirements if requirement.strip()
            ),
            created_at=utc_now(),
        )


@dataclass
class AgentDeployment:
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
    def create(
        cls,
        *,
        agent_version_id: UUID,
        environment: str,
        runtime_kind: str,
        endpoint_reference: str | None = None,
        remote_peer_id: str | None = None,
        traffic_weight: int = 100,
        region: str | None = None,
        rollout_policy: dict[str, Any] | None = None,
    ) -> AgentDeployment:
        if not 0 <= traffic_weight <= 100:
            raise InvalidAgentVersion("Deployment traffic weight must be between 0 and 100")
        if not environment.strip() or not runtime_kind.strip():
            raise InvalidAgentVersion("Deployment environment and runtime kind are required")
        now = utc_now()
        return cls(
            id=uuid4(),
            agent_version_id=agent_version_id,
            environment=environment.strip(),
            runtime_kind=runtime_kind.strip(),
            remote_peer_id=remote_peer_id.strip() if remote_peer_id else None,
            endpoint_reference=endpoint_reference.strip() if endpoint_reference else None,
            desired_status=DeploymentStatus.ACTIVE,
            current_status=DeploymentStatus.PENDING,
            traffic_weight=traffic_weight,
            region=region.strip() if region else None,
            rollout_policy=dict(rollout_policy or {}),
            created_at=now,
            updated_at=now,
        )

    def set_status(
        self,
        *,
        desired_status: DeploymentStatus | None = None,
        current_status: DeploymentStatus | None = None,
    ) -> None:
        if desired_status is not None:
            self.desired_status = desired_status
        if current_status is not None:
            self.current_status = current_status
        self.updated_at = utc_now()


@dataclass
class AgentInstance:
    id: UUID
    deployment_id: UUID
    external_instance_id: str
    health: InstanceHealth
    last_heartbeat_at: datetime
    capacity_slots: int
    active_slots: int
    protocol_endpoint: str | None
    lease_epoch: int
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def register(
        cls,
        *,
        deployment_id: UUID,
        external_instance_id: str,
        health: InstanceHealth,
        capacity_slots: int,
        active_slots: int,
        protocol_endpoint: str | None,
        lease_epoch: int,
        metadata: dict[str, Any] | None = None,
    ) -> AgentInstance:
        instance = cls(
            id=uuid4(),
            deployment_id=deployment_id,
            external_instance_id=external_instance_id.strip(),
            health=health,
            last_heartbeat_at=utc_now(),
            capacity_slots=capacity_slots,
            active_slots=active_slots,
            protocol_endpoint=protocol_endpoint.strip() if protocol_endpoint else None,
            lease_epoch=lease_epoch,
            metadata=dict(metadata or {}),
        )
        instance._validate_capacity()
        if not instance.external_instance_id or lease_epoch < 1:
            raise InvalidAgentVersion("Instance ID and positive lease epoch are required")
        return instance

    def heartbeat(
        self,
        *,
        health: InstanceHealth,
        capacity_slots: int,
        active_slots: int,
        protocol_endpoint: str | None,
        lease_epoch: int,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if lease_epoch < self.lease_epoch:
            raise InvalidAgentTransition("Stale instance lease epoch")
        self.health = health
        self.capacity_slots = capacity_slots
        self.active_slots = active_slots
        self.protocol_endpoint = protocol_endpoint.strip() if protocol_endpoint else None
        self.lease_epoch = lease_epoch
        self.metadata = dict(metadata or {})
        self.last_heartbeat_at = utc_now()
        self._validate_capacity()

    def _validate_capacity(self) -> None:
        if self.capacity_slots < 0 or not 0 <= self.active_slots <= self.capacity_slots:
            raise InvalidAgentVersion("Instance slot counts are inconsistent")
