from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from agentmesh.application.ports import UnitOfWorkFactory
from agentmesh.domain.errors import (
    AgentDefinitionNotFound,
    AgentDeploymentNotFound,
    AgentRegistryConflict,
    AgentUnavailable,
    AgentVersionNotFound,
    CapabilityNotFound,
)
from agentmesh.domain.messaging import MessageEnvelope
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
    normalize_agent_name,
    validate_capability_key,
)
from agentmesh.domain.tasks import TaskRun


@dataclass(frozen=True)
class AgentDefinitionAggregate:
    definition: AgentDefinition
    versions: list[AgentVersion]


@dataclass(frozen=True)
class AgentCandidate:
    definition: AgentDefinition
    agent_version: AgentVersion
    deployments: list[AgentDeployment]


class AgentRegistryService:
    def __init__(self, *, uow_factory: UnitOfWorkFactory, tenant_id: str) -> None:
        self._uow_factory = uow_factory
        self._tenant_id = tenant_id

    def ensure_builtin_agent(
        self,
        name: str,
        *,
        reviewer: bool = False,
        supervisor: bool = False,
        role: str | None = None,
        instructions: str | None = None,
        description: str | None = None,
        extra_tags: tuple[str, ...] = (),
    ) -> AgentDefinitionAggregate:
        if reviewer and supervisor:
            raise ValueError("A built-in Agent cannot be both reviewer and supervisor")
        normalized_name = normalize_agent_name(name)
        with self._uow_factory() as uow:
            uow.idempotency.lock(f"builtin-agent:{self._tenant_id}", normalized_name)
            existing = uow.agent_definitions.get_by_name(
                self._tenant_id, normalized_name, for_update=True
            )
            if existing is not None:
                return AgentDefinitionAggregate(
                    definition=existing,
                    versions=uow.agent_versions.list_for_definition(existing.id),
                )

            capability_key = (
                "general.review"
                if reviewer
                else "general.supervise"
                if supervisor
                else "general.task"
            )
            default_description = (
                "Built-in deterministic AgentMesh acceptance reviewer"
                if reviewer
                else (
                    "Built-in AgentMesh coordination supervisor"
                    if supervisor
                    else "Built-in AgentMesh executor"
                )
            )
            default_role = (
                "Acceptance criteria reviewer"
                if reviewer
                else "Coordination supervisor"
                if supervisor
                else "General task executor"
            )
            default_instructions = (
                "Evaluate the candidate independently against every acceptance criterion."
                if reviewer
                else (
                    "Synthesize completed Subtask outputs into the final Task result."
                    if supervisor
                    else "Complete the assigned task and return a structured result."
                )
            )
            definition = AgentDefinition.create(
                tenant_id=self._tenant_id,
                owner_id="system",
                name=normalized_name,
                description=description or default_description,
                visibility=AgentVisibility.TENANT,
                tags=(
                    "builtin",
                    "configurable-runtime" if not reviewer else "deterministic",
                    "reviewer" if reviewer else "supervisor" if supervisor else "executor",
                    *extra_tags,
                ),
            )
            agent_version = AgentVersion.create_draft(
                definition_id=definition.id,
                semantic_version="0.1.0",
                role=role or default_role,
                instructions=instructions or default_instructions,
                declared_capabilities=(capability_key,),
                input_schema={"type": "object"},
                output_schema={"type": "object"},
                runtime_adapter="deterministic-local",
                execution_modes=("async",),
            )
            agent_version.submit_for_review()
            agent_version.publish((capability_key,))
            definition.set_default(agent_version)
            capability = Capability.create(
                tenant_id=self._tenant_id,
                key=capability_key,
                version="1.0.0",
                description=(
                    "Review a structured candidate against acceptance criteria"
                    if reviewer
                    else (
                        "Synthesize the outputs of a coordinated Subtask plan"
                        if supervisor
                        else "Execute a general structured task"
                    )
                ),
                input_schema={"type": "object"},
                output_schema={"type": "object"},
                evidence_requirements=("contract-test",),
            )
            uow.agent_definitions.add(definition)
            uow.agent_versions.add(agent_version)
            if (
                uow.capabilities.get_by_key_version(
                    self._tenant_id, capability.key, capability.version
                )
                is None
            ):
                uow.capabilities.add(capability)
            uow.outbox.add(self._definition_event("created", definition))
            uow.outbox.add(self._version_event("published", definition, agent_version))
            uow.commit()
            return AgentDefinitionAggregate(definition=definition, versions=[agent_version])

    def create_definition(
        self,
        *,
        owner_id: str,
        name: str,
        description: str,
        visibility: AgentVisibility,
        tags: list[str],
    ) -> AgentDefinitionAggregate:
        definition = AgentDefinition.create(
            tenant_id=self._tenant_id,
            owner_id=owner_id,
            name=name,
            description=description,
            visibility=visibility,
            tags=tags,
        )
        with self._uow_factory() as uow:
            uow.idempotency.lock(f"agent-name:{self._tenant_id}", definition.name)
            if uow.agent_definitions.get_by_name(self._tenant_id, definition.name) is not None:
                raise AgentRegistryConflict(f"Agent name {definition.name} already exists")
            uow.agent_definitions.add(definition)
            uow.outbox.add(self._definition_event("created", definition))
            uow.commit()
        return AgentDefinitionAggregate(definition=definition, versions=[])

    def get_definition(self, definition_id: UUID) -> AgentDefinitionAggregate:
        with self._uow_factory() as uow:
            definition = self._definition_or_raise(uow, definition_id)
            return AgentDefinitionAggregate(
                definition=definition,
                versions=uow.agent_versions.list_for_definition(definition.id),
            )

    def list_definitions(
        self, *, limit: int = 50, offset: int = 0
    ) -> list[AgentDefinitionAggregate]:
        with self._uow_factory() as uow:
            definitions = uow.agent_definitions.list(
                tenant_id=self._tenant_id, limit=limit, offset=offset
            )
            return [
                AgentDefinitionAggregate(
                    definition=definition,
                    versions=uow.agent_versions.list_for_definition(definition.id),
                )
                for definition in definitions
            ]

    def archive_definition(self, definition_id: UUID) -> AgentDefinitionAggregate:
        with self._uow_factory() as uow:
            definition = self._definition_or_raise(uow, definition_id, for_update=True)
            definition.archive()
            uow.agent_definitions.save(definition)
            uow.outbox.add(self._definition_event("archived", definition))
            uow.commit()
            return AgentDefinitionAggregate(
                definition=definition,
                versions=uow.agent_versions.list_for_definition(definition.id),
            )

    def create_version(self, definition_id: UUID, **values: Any) -> AgentVersion:
        with self._uow_factory() as uow:
            definition = self._definition_or_raise(uow, definition_id, for_update=True)
            if len(uow.agent_versions.list_for_definition(definition.id)) >= 100:
                raise AgentRegistryConflict(
                    "Agent Definition cannot contain more than 100 Versions"
                )
            agent_version = AgentVersion.create_draft(definition_id=definition.id, **values)
            if (
                uow.agent_versions.get_by_semantic_version(
                    definition.id, agent_version.semantic_version
                )
                is not None
            ):
                raise AgentRegistryConflict(
                    f"Agent Version {agent_version.semantic_version} already exists"
                )
            uow.agent_versions.add(agent_version)
            uow.commit()
            return agent_version

    def submit_version(self, agent_version_id: UUID) -> AgentVersion:
        return self._transition_version(agent_version_id, "submitted-for-review")

    def reject_version(self, agent_version_id: UUID) -> AgentVersion:
        return self._transition_version(agent_version_id, "rejected")

    def publish_version(
        self,
        agent_version_id: UUID,
        *,
        verified_capabilities: list[str],
        make_default: bool,
    ) -> AgentVersion:
        with self._uow_factory() as uow:
            agent_version, definition = self._version_and_definition_or_raise(
                uow, agent_version_id, for_update=True
            )
            for key in verified_capabilities:
                normalized = validate_capability_key(key)
                available = any(
                    capability.key == normalized
                    for capability in uow.capabilities.list(
                        tenant_id=self._tenant_id, limit=1_000, offset=0
                    )
                )
                if not available:
                    raise CapabilityNotFound(f"Capability {normalized} is not registered")
            agent_version.publish(verified_capabilities)
            uow.agent_versions.save(agent_version)
            if make_default or definition.default_version_id is None:
                definition.set_default(agent_version)
                uow.agent_definitions.save(definition)
            uow.outbox.add(self._version_event("published", definition, agent_version))
            uow.commit()
            return agent_version

    def deprecate_version(self, agent_version_id: UUID) -> AgentVersion:
        return self._transition_version(agent_version_id, "deprecated")

    def retire_version(self, agent_version_id: UUID) -> AgentVersion:
        return self._transition_version(agent_version_id, "retired")

    def revoke_version(self, agent_version_id: UUID, *, reason: str) -> AgentVersion:
        with self._uow_factory() as uow:
            agent_version, definition = self._version_and_definition_or_raise(
                uow, agent_version_id, for_update=True
            )
            agent_version.revoke(reason)
            definition.clear_default(agent_version.id)
            uow.agent_versions.save(agent_version)
            uow.agent_definitions.save(definition)
            uow.outbox.add(self._version_event("revoked", definition, agent_version))
            uow.commit()
            return agent_version

    def list_affected_active_runs(self, agent_version_id: UUID) -> list[TaskRun]:
        with self._uow_factory() as uow:
            self._version_and_definition_or_raise(uow, agent_version_id)
            return uow.runs.list_active_for_agent_version(
                agent_version_id, tenant_id=self._tenant_id
            )

    def set_default_version(
        self, definition_id: UUID, agent_version_id: UUID
    ) -> AgentDefinitionAggregate:
        with self._uow_factory() as uow:
            definition = self._definition_or_raise(uow, definition_id, for_update=True)
            agent_version = uow.agent_versions.get(agent_version_id, for_update=True)
            if agent_version is None or agent_version.definition_id != definition.id:
                raise AgentVersionNotFound(str(agent_version_id))
            definition.set_default(agent_version)
            uow.agent_definitions.save(definition)
            uow.outbox.add(self._definition_event("default-changed", definition))
            uow.commit()
            return AgentDefinitionAggregate(
                definition=definition,
                versions=uow.agent_versions.list_for_definition(definition.id),
            )

    def create_capability(self, **values: Any) -> Capability:
        capability = Capability.create(tenant_id=self._tenant_id, **values)
        with self._uow_factory() as uow:
            lock_key = f"{capability.key}@{capability.version}"
            uow.idempotency.lock(f"capability:{self._tenant_id}", lock_key)
            if (
                uow.capabilities.get_by_key_version(
                    self._tenant_id, capability.key, capability.version
                )
                is not None
            ):
                raise AgentRegistryConflict(f"Capability {lock_key} already exists")
            uow.capabilities.add(capability)
            uow.commit()
        return capability

    def list_capabilities(self, *, limit: int, offset: int) -> list[Capability]:
        with self._uow_factory() as uow:
            return uow.capabilities.list(tenant_id=self._tenant_id, limit=limit, offset=offset)

    def find_candidates(
        self,
        *,
        required_capabilities: list[str],
        execution_mode: str | None = None,
    ) -> list[AgentCandidate]:
        required = {validate_capability_key(value) for value in required_capabilities}
        mode = execution_mode.strip().lower() if execution_mode else None
        candidates: list[AgentCandidate] = []
        with self._uow_factory() as uow:
            definitions = uow.agent_definitions.list(
                tenant_id=self._tenant_id, limit=1_000, offset=0
            )
            for definition in definitions:
                if definition.lifecycle != AgentDefinitionLifecycle.ACTIVE:
                    continue
                for version in uow.agent_versions.list_for_definition(definition.id):
                    if version.status != AgentVersionStatus.PUBLISHED:
                        continue
                    if not required.issubset(version.verified_capabilities):
                        continue
                    if mode and mode not in version.execution_modes:
                        continue
                    deployments = [
                        deployment
                        for deployment in uow.agent_deployments.list_for_version(version.id)
                        if deployment.current_status == DeploymentStatus.ACTIVE
                    ]
                    candidates.append(
                        AgentCandidate(
                            definition=definition,
                            agent_version=version,
                            deployments=deployments,
                        )
                    )
        return candidates

    def create_deployment(self, agent_version_id: UUID, **values: Any) -> AgentDeployment:
        with self._uow_factory() as uow:
            agent_version, _definition = self._version_and_definition_or_raise(
                uow, agent_version_id, for_update=True
            )
            if agent_version.status != AgentVersionStatus.PUBLISHED:
                raise AgentUnavailable("Only published Agent Versions can be deployed")
            deployment = AgentDeployment.create(agent_version_id=agent_version.id, **values)
            uow.agent_deployments.add(deployment)
            uow.outbox.add(
                MessageEnvelope.domain_event(
                    schema_name="agentmesh.agent-deployment.changed",
                    tenant_id=self._tenant_id,
                    aggregate_id=deployment.id,
                    payload={
                        "deployment_id": str(deployment.id),
                        "agent_version_id": str(agent_version.id),
                        "status": deployment.current_status.value,
                    },
                )
            )
            uow.commit()
            return deployment

    def update_deployment_status(
        self,
        deployment_id: UUID,
        *,
        desired_status: DeploymentStatus | None,
        current_status: DeploymentStatus | None,
    ) -> AgentDeployment:
        with self._uow_factory() as uow:
            deployment = self._deployment_or_raise(uow, deployment_id, for_update=True)
            self._assert_deployment_tenant(uow, deployment)
            deployment.set_status(desired_status=desired_status, current_status=current_status)
            uow.agent_deployments.save(deployment)
            uow.outbox.add(
                MessageEnvelope.domain_event(
                    schema_name="agentmesh.agent-deployment.changed",
                    tenant_id=self._tenant_id,
                    aggregate_id=deployment.id,
                    payload={
                        "deployment_id": str(deployment.id),
                        "desired_status": deployment.desired_status.value,
                        "current_status": deployment.current_status.value,
                    },
                )
            )
            uow.commit()
            return deployment

    def list_deployments(self, agent_version_id: UUID) -> list[AgentDeployment]:
        with self._uow_factory() as uow:
            self._version_and_definition_or_raise(uow, agent_version_id)
            return uow.agent_deployments.list_for_version(agent_version_id)

    def heartbeat_instance(
        self,
        deployment_id: UUID,
        *,
        external_instance_id: str,
        health: InstanceHealth,
        capacity_slots: int,
        active_slots: int,
        protocol_endpoint: str | None,
        lease_epoch: int,
        metadata: dict[str, Any],
    ) -> AgentInstance:
        with self._uow_factory() as uow:
            deployment = self._deployment_or_raise(uow, deployment_id)
            self._assert_deployment_tenant(uow, deployment)
            instance = uow.agent_instances.get_by_external_id(
                deployment.id, external_instance_id, for_update=True
            )
            previous_health = instance.health if instance is not None else None
            if instance is None:
                instance = AgentInstance.register(
                    deployment_id=deployment.id,
                    external_instance_id=external_instance_id,
                    health=health,
                    capacity_slots=capacity_slots,
                    active_slots=active_slots,
                    protocol_endpoint=protocol_endpoint,
                    lease_epoch=lease_epoch,
                    metadata=metadata,
                )
                uow.agent_instances.add(instance)
            else:
                instance.heartbeat(
                    health=health,
                    capacity_slots=capacity_slots,
                    active_slots=active_slots,
                    protocol_endpoint=protocol_endpoint,
                    lease_epoch=lease_epoch,
                    metadata=metadata,
                )
                uow.agent_instances.save(instance)
            if previous_health != instance.health:
                uow.outbox.add(
                    MessageEnvelope.domain_event(
                        schema_name="agentmesh.agent-instance.health-changed",
                        tenant_id=self._tenant_id,
                        aggregate_id=instance.id,
                        payload={
                            "instance_id": str(instance.id),
                            "deployment_id": str(deployment.id),
                            "previous_health": (previous_health.value if previous_health else None),
                            "health": instance.health.value,
                        },
                    )
                )
            uow.commit()
            return instance

    def list_instances(self, deployment_id: UUID) -> list[AgentInstance]:
        with self._uow_factory() as uow:
            deployment = self._deployment_or_raise(uow, deployment_id)
            self._assert_deployment_tenant(uow, deployment)
            return uow.agent_instances.list_for_deployment(deployment.id)

    def _transition_version(self, agent_version_id: UUID, action: str) -> AgentVersion:
        with self._uow_factory() as uow:
            agent_version, definition = self._version_and_definition_or_raise(
                uow, agent_version_id, for_update=True
            )
            if action == "submitted-for-review":
                agent_version.submit_for_review()
            elif action == "rejected":
                agent_version.reject()
            elif action == "deprecated":
                agent_version.deprecate()
                definition.clear_default(agent_version.id)
                uow.agent_definitions.save(definition)
            elif action == "retired":
                agent_version.retire()
                definition.clear_default(agent_version.id)
                uow.agent_definitions.save(definition)
            else:
                raise ValueError(action)
            uow.agent_versions.save(agent_version)
            uow.outbox.add(self._version_event(action, definition, agent_version))
            uow.commit()
            return agent_version

    def _definition_or_raise(
        self, uow: Any, definition_id: UUID, *, for_update: bool = False
    ) -> AgentDefinition:
        definition = uow.agent_definitions.get(definition_id, for_update=for_update)
        if definition is None or definition.tenant_id != self._tenant_id:
            raise AgentDefinitionNotFound(str(definition_id))
        return definition

    def _version_and_definition_or_raise(
        self, uow: Any, agent_version_id: UUID, *, for_update: bool = False
    ) -> tuple[AgentVersion, AgentDefinition]:
        agent_version = uow.agent_versions.get(agent_version_id, for_update=for_update)
        if agent_version is None:
            raise AgentVersionNotFound(str(agent_version_id))
        definition = self._definition_or_raise(
            uow, agent_version.definition_id, for_update=for_update
        )
        return agent_version, definition

    @staticmethod
    def _deployment_or_raise(
        uow: Any, deployment_id: UUID, *, for_update: bool = False
    ) -> AgentDeployment:
        deployment = uow.agent_deployments.get(deployment_id, for_update=for_update)
        if deployment is None:
            raise AgentDeploymentNotFound(str(deployment_id))
        return deployment

    def _assert_deployment_tenant(self, uow: Any, deployment: AgentDeployment) -> None:
        self._version_and_definition_or_raise(uow, deployment.agent_version_id)

    def _definition_event(self, action: str, definition: AgentDefinition) -> MessageEnvelope:
        return MessageEnvelope.domain_event(
            schema_name=f"agentmesh.agent-definition.{action}",
            tenant_id=definition.tenant_id,
            aggregate_id=definition.id,
            payload={
                "agent_definition_id": str(definition.id),
                "name": definition.name,
                "default_version_id": (
                    str(definition.default_version_id) if definition.default_version_id else None
                ),
            },
        )

    def _version_event(
        self,
        action: str,
        definition: AgentDefinition,
        agent_version: AgentVersion,
    ) -> MessageEnvelope:
        return MessageEnvelope.domain_event(
            schema_name=f"agentmesh.agent-version.{action}",
            tenant_id=definition.tenant_id,
            aggregate_id=agent_version.id,
            payload={
                "agent_definition_id": str(definition.id),
                "agent_version_id": str(agent_version.id),
                "semantic_version": agent_version.semantic_version,
                "status": agent_version.status.value,
                "content_digest": agent_version.content_digest,
            },
        )
