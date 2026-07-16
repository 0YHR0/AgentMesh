from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentmesh.domain.registry import (
    AgentDefinition,
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
from agentmesh.infrastructure.postgres.models import (
    AgentDefinitionRecord,
    AgentDeploymentRecord,
    AgentInstanceRecord,
    AgentVersionRecord,
    CapabilityRecord,
)


class SqlAlchemyAgentDefinitionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, definition: AgentDefinition) -> None:
        self._session.add(
            AgentDefinitionRecord(
                id=definition.id,
                tenant_id=definition.tenant_id,
                owner_id=definition.owner_id,
                name=definition.name,
                description=definition.description,
                visibility=definition.visibility.value,
                lifecycle=definition.lifecycle.value,
                default_version_id=definition.default_version_id,
                tags=list(definition.tags),
                version=definition.version,
                created_at=definition.created_at,
                updated_at=definition.updated_at,
            )
        )

    def get(self, definition_id: UUID, *, for_update: bool = False) -> AgentDefinition | None:
        record = self._session.get(AgentDefinitionRecord, definition_id, with_for_update=for_update)
        return self._to_domain(record) if record is not None else None

    def get_by_name(
        self, tenant_id: str, name: str, *, for_update: bool = False
    ) -> AgentDefinition | None:
        statement = select(AgentDefinitionRecord).where(
            AgentDefinitionRecord.tenant_id == tenant_id,
            AgentDefinitionRecord.name == name,
        )
        if for_update:
            statement = statement.with_for_update()
        record = self._session.scalar(statement)
        return self._to_domain(record) if record is not None else None

    def list(self, *, tenant_id: str, limit: int, offset: int) -> list[AgentDefinition]:
        statement = (
            select(AgentDefinitionRecord)
            .where(AgentDefinitionRecord.tenant_id == tenant_id)
            .order_by(AgentDefinitionRecord.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return [self._to_domain(record) for record in self._session.scalars(statement)]

    def save(self, definition: AgentDefinition) -> None:
        record = self._session.get(AgentDefinitionRecord, definition.id)
        if record is None:
            raise LookupError(definition.id)
        record.description = definition.description
        record.visibility = definition.visibility.value
        record.lifecycle = definition.lifecycle.value
        record.default_version_id = definition.default_version_id
        record.tags = list(definition.tags)
        record.version = definition.version
        record.updated_at = definition.updated_at

    @staticmethod
    def _to_domain(record: AgentDefinitionRecord) -> AgentDefinition:
        return AgentDefinition(
            id=record.id,
            tenant_id=record.tenant_id,
            owner_id=record.owner_id,
            name=record.name,
            description=record.description,
            visibility=AgentVisibility(record.visibility),
            lifecycle=AgentDefinitionLifecycle(record.lifecycle),
            default_version_id=record.default_version_id,
            tags=tuple(record.tags),
            version=record.version,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


class SqlAlchemyAgentVersionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, agent_version: AgentVersion) -> None:
        self._session.add(self._to_record(agent_version))

    def get(self, agent_version_id: UUID, *, for_update: bool = False) -> AgentVersion | None:
        record = self._session.get(AgentVersionRecord, agent_version_id, with_for_update=for_update)
        return self._to_domain(record) if record is not None else None

    def get_by_semantic_version(
        self,
        definition_id: UUID,
        semantic_version: str,
        *,
        for_update: bool = False,
    ) -> AgentVersion | None:
        statement = select(AgentVersionRecord).where(
            AgentVersionRecord.definition_id == definition_id,
            AgentVersionRecord.semantic_version == semantic_version,
        )
        if for_update:
            statement = statement.with_for_update()
        record = self._session.scalar(statement)
        return self._to_domain(record) if record is not None else None

    def list_for_definition(self, definition_id: UUID) -> list[AgentVersion]:
        statement = (
            select(AgentVersionRecord)
            .where(AgentVersionRecord.definition_id == definition_id)
            .order_by(AgentVersionRecord.created_at.asc())
        )
        return [self._to_domain(record) for record in self._session.scalars(statement)]

    def save(self, agent_version: AgentVersion) -> None:
        record = self._session.get(AgentVersionRecord, agent_version.id)
        if record is None:
            raise LookupError(agent_version.id)
        record.status = agent_version.status.value
        record.content_digest = agent_version.content_digest
        record.verified_capabilities = list(agent_version.verified_capabilities)
        record.updated_at = agent_version.updated_at
        record.published_at = agent_version.published_at
        record.revoked_at = agent_version.revoked_at
        record.revoke_reason = agent_version.revoke_reason

    @staticmethod
    def _to_record(value: AgentVersion) -> AgentVersionRecord:
        return AgentVersionRecord(
            id=value.id,
            definition_id=value.definition_id,
            semantic_version=value.semantic_version,
            status=value.status.value,
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

    @staticmethod
    def _to_domain(record: AgentVersionRecord) -> AgentVersion:
        return AgentVersion(
            id=record.id,
            definition_id=record.definition_id,
            semantic_version=record.semantic_version,
            status=AgentVersionStatus(record.status),
            content_digest=record.content_digest,
            role=record.role,
            instructions=record.instructions,
            declared_capabilities=tuple(record.declared_capabilities),
            verified_capabilities=tuple(record.verified_capabilities),
            input_schema=dict(record.input_schema),
            output_schema=dict(record.output_schema),
            model_policy=dict(record.model_policy),
            tool_profile=dict(record.tool_profile),
            knowledge_profile=dict(record.knowledge_profile),
            policy_profile=dict(record.policy_profile),
            risk_class=record.risk_class,
            data_classification_ceiling=record.data_classification_ceiling,
            resource_defaults=dict(record.resource_defaults),
            runtime_adapter=record.runtime_adapter,
            artifact_digest=record.artifact_digest,
            execution_modes=tuple(record.execution_modes),
            compatibility=dict(record.compatibility),
            created_at=record.created_at,
            updated_at=record.updated_at,
            published_at=record.published_at,
            revoked_at=record.revoked_at,
            revoke_reason=record.revoke_reason,
        )


class SqlAlchemyCapabilityRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, capability: Capability) -> None:
        self._session.add(
            CapabilityRecord(
                id=capability.id,
                tenant_id=capability.tenant_id,
                key=capability.key,
                version=capability.version,
                description=capability.description,
                input_schema=dict(capability.input_schema),
                output_schema=dict(capability.output_schema),
                evidence_requirements=list(capability.evidence_requirements),
                created_at=capability.created_at,
            )
        )

    def get(self, capability_id: UUID) -> Capability | None:
        record = self._session.get(CapabilityRecord, capability_id)
        return self._to_domain(record) if record is not None else None

    def get_by_key_version(self, tenant_id: str, key: str, version: str) -> Capability | None:
        record = self._session.scalar(
            select(CapabilityRecord).where(
                CapabilityRecord.tenant_id == tenant_id,
                CapabilityRecord.key == key,
                CapabilityRecord.version == version,
            )
        )
        return self._to_domain(record) if record is not None else None

    def list(self, *, tenant_id: str, limit: int, offset: int) -> list[Capability]:
        statement = (
            select(CapabilityRecord)
            .where(CapabilityRecord.tenant_id == tenant_id)
            .order_by(CapabilityRecord.key, CapabilityRecord.version)
            .limit(limit)
            .offset(offset)
        )
        return [self._to_domain(record) for record in self._session.scalars(statement)]

    @staticmethod
    def _to_domain(record: CapabilityRecord) -> Capability:
        return Capability(
            id=record.id,
            tenant_id=record.tenant_id,
            key=record.key,
            version=record.version,
            description=record.description,
            input_schema=dict(record.input_schema),
            output_schema=dict(record.output_schema),
            evidence_requirements=tuple(record.evidence_requirements),
            created_at=record.created_at,
        )


class SqlAlchemyAgentDeploymentRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, deployment: AgentDeployment) -> None:
        self._session.add(self._to_record(deployment))

    def get(self, deployment_id: UUID, *, for_update: bool = False) -> AgentDeployment | None:
        record = self._session.get(AgentDeploymentRecord, deployment_id, with_for_update=for_update)
        return self._to_domain(record) if record is not None else None

    def list_for_version(self, agent_version_id: UUID) -> list[AgentDeployment]:
        statement = (
            select(AgentDeploymentRecord)
            .where(AgentDeploymentRecord.agent_version_id == agent_version_id)
            .order_by(AgentDeploymentRecord.created_at.asc())
        )
        return [self._to_domain(record) for record in self._session.scalars(statement)]

    def save(self, deployment: AgentDeployment) -> None:
        record = self._session.get(AgentDeploymentRecord, deployment.id)
        if record is None:
            raise LookupError(deployment.id)
        record.desired_status = deployment.desired_status.value
        record.current_status = deployment.current_status.value
        record.traffic_weight = deployment.traffic_weight
        record.updated_at = deployment.updated_at

    @staticmethod
    def _to_record(value: AgentDeployment) -> AgentDeploymentRecord:
        return AgentDeploymentRecord(
            id=value.id,
            agent_version_id=value.agent_version_id,
            environment=value.environment,
            runtime_kind=value.runtime_kind,
            remote_peer_id=value.remote_peer_id,
            endpoint_reference=value.endpoint_reference,
            desired_status=value.desired_status.value,
            current_status=value.current_status.value,
            traffic_weight=value.traffic_weight,
            region=value.region,
            rollout_policy=dict(value.rollout_policy),
            created_at=value.created_at,
            updated_at=value.updated_at,
        )

    @staticmethod
    def _to_domain(record: AgentDeploymentRecord) -> AgentDeployment:
        return AgentDeployment(
            id=record.id,
            agent_version_id=record.agent_version_id,
            environment=record.environment,
            runtime_kind=record.runtime_kind,
            remote_peer_id=record.remote_peer_id,
            endpoint_reference=record.endpoint_reference,
            desired_status=DeploymentStatus(record.desired_status),
            current_status=DeploymentStatus(record.current_status),
            traffic_weight=record.traffic_weight,
            region=record.region,
            rollout_policy=dict(record.rollout_policy),
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


class SqlAlchemyAgentInstanceRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, instance: AgentInstance) -> None:
        self._session.add(self._to_record(instance))

    def get_by_external_id(
        self,
        deployment_id: UUID,
        external_instance_id: str,
        *,
        for_update: bool = False,
    ) -> AgentInstance | None:
        statement = select(AgentInstanceRecord).where(
            AgentInstanceRecord.deployment_id == deployment_id,
            AgentInstanceRecord.external_instance_id == external_instance_id,
        )
        if for_update:
            statement = statement.with_for_update()
        record = self._session.scalar(statement)
        return self._to_domain(record) if record is not None else None

    def list_for_deployment(self, deployment_id: UUID) -> list[AgentInstance]:
        statement = (
            select(AgentInstanceRecord)
            .where(AgentInstanceRecord.deployment_id == deployment_id)
            .order_by(AgentInstanceRecord.external_instance_id)
        )
        return [self._to_domain(record) for record in self._session.scalars(statement)]

    def save(self, instance: AgentInstance) -> None:
        record = self._session.get(AgentInstanceRecord, instance.id)
        if record is None:
            raise LookupError(instance.id)
        record.health = instance.health.value
        record.last_heartbeat_at = instance.last_heartbeat_at
        record.capacity_slots = instance.capacity_slots
        record.active_slots = instance.active_slots
        record.protocol_endpoint = instance.protocol_endpoint
        record.lease_epoch = instance.lease_epoch
        record.metadata_json = dict(instance.metadata)

    @staticmethod
    def _to_record(value: AgentInstance) -> AgentInstanceRecord:
        return AgentInstanceRecord(
            id=value.id,
            deployment_id=value.deployment_id,
            external_instance_id=value.external_instance_id,
            health=value.health.value,
            last_heartbeat_at=value.last_heartbeat_at,
            capacity_slots=value.capacity_slots,
            active_slots=value.active_slots,
            protocol_endpoint=value.protocol_endpoint,
            lease_epoch=value.lease_epoch,
            metadata_json=dict(value.metadata),
        )

    @staticmethod
    def _to_domain(record: AgentInstanceRecord) -> AgentInstance:
        return AgentInstance(
            id=record.id,
            deployment_id=record.deployment_id,
            external_instance_id=record.external_instance_id,
            health=InstanceHealth(record.health),
            last_heartbeat_at=record.last_heartbeat_at,
            capacity_slots=record.capacity_slots,
            active_slots=record.active_slots,
            protocol_endpoint=record.protocol_endpoint,
            lease_epoch=record.lease_epoch,
            metadata=dict(record.metadata_json),
        )
