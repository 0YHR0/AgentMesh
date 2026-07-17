from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID

from agentmesh.domain.artifacts import Artifact, ArtifactVersion
from agentmesh.domain.coordination import Subtask, SubtaskDependency
from agentmesh.domain.errors import IdempotencyConflict
from agentmesh.domain.handoffs import Handoff, HandoffStatus
from agentmesh.domain.messaging import IdempotencyRecord, InboxMessage, MessageEnvelope
from agentmesh.domain.observability import UsageRecord
from agentmesh.domain.registry import (
    AgentDefinition,
    AgentDeployment,
    AgentInstance,
    AgentVersion,
    Capability,
)
from agentmesh.domain.tasks import RunStatus, Task, TaskAttempt, TaskRun, TaskStatus
from agentmesh.domain.tools import ToolInvocation


@dataclass
class InMemoryStore:
    tasks: dict[UUID, Task] = field(default_factory=dict)
    subtasks: dict[UUID, Subtask] = field(default_factory=dict)
    subtask_dependencies: dict[tuple[UUID, UUID, UUID], SubtaskDependency] = field(
        default_factory=dict
    )
    handoffs: dict[UUID, Handoff] = field(default_factory=dict)
    runs: dict[UUID, TaskRun] = field(default_factory=dict)
    attempts: dict[UUID, TaskAttempt] = field(default_factory=dict)
    outbox: list[MessageEnvelope] = field(default_factory=list)
    inbox: dict[tuple[str, str, UUID], InboxMessage] = field(default_factory=dict)
    idempotency: dict[tuple[str, str], IdempotencyRecord] = field(default_factory=dict)
    agent_definitions: dict[UUID, AgentDefinition] = field(default_factory=dict)
    agent_versions: dict[UUID, AgentVersion] = field(default_factory=dict)
    capabilities: dict[UUID, Capability] = field(default_factory=dict)
    agent_deployments: dict[UUID, AgentDeployment] = field(default_factory=dict)
    agent_instances: dict[UUID, AgentInstance] = field(default_factory=dict)
    artifacts: dict[UUID, Artifact] = field(default_factory=dict)
    artifact_versions: dict[UUID, ArtifactVersion] = field(default_factory=dict)
    tool_invocations: dict[UUID, ToolInvocation] = field(default_factory=dict)
    usage_records: dict[UUID, UsageRecord] = field(default_factory=dict)
    run_list_for_task_calls: int = 0
    run_list_for_tasks_calls: int = 0
    attempt_list_for_task_calls: int = 0
    attempt_list_for_tasks_calls: int = 0
    artifact_version_list_for_artifact_calls: int = 0
    artifact_version_list_for_artifacts_calls: int = 0


class InMemoryTaskRepository:
    def __init__(self, tasks: dict[UUID, Task]) -> None:
        self._tasks = tasks

    def add(self, task: Task) -> None:
        self._tasks[task.id] = deepcopy(task)

    def get(self, task_id: UUID, *, for_update: bool = False) -> Task | None:
        task = self._tasks.get(task_id)
        return deepcopy(task) if task is not None else None

    def save(self, task: Task) -> None:
        if task.id not in self._tasks:
            raise LookupError(task.id)
        self._tasks[task.id] = deepcopy(task)

    def list(
        self,
        *,
        limit: int,
        offset: int,
        tenant_id: str,
        status: TaskStatus | None = None,
    ) -> list[Task]:
        tasks = [task for task in self._tasks.values() if task.tenant_id == tenant_id]
        if status is not None:
            tasks = [task for task in tasks if task.status == status]
        tasks.sort(key=lambda task: task.created_at, reverse=True)
        return deepcopy(tasks[offset : offset + limit])


class InMemoryTaskRunRepository:
    def __init__(
        self,
        runs: dict[UUID, TaskRun],
        tasks: dict[UUID, Task],
        store: InMemoryStore,
    ) -> None:
        self._runs = runs
        self._tasks = tasks
        self._store = store

    def add(self, run: TaskRun) -> None:
        self._runs[run.id] = deepcopy(run)

    def get(self, run_id: UUID, *, for_update: bool = False) -> TaskRun | None:
        run = self._runs.get(run_id)
        return deepcopy(run) if run is not None else None

    def save(self, run: TaskRun) -> None:
        if run.id not in self._runs:
            raise LookupError(run.id)
        self._runs[run.id] = deepcopy(run)

    def list_for_task(self, task_id: UUID) -> list[TaskRun]:
        self._store.run_list_for_task_calls += 1
        runs = [run for run in self._runs.values() if run.task_id == task_id]
        runs.sort(key=lambda run: run.queued_at)
        return deepcopy(runs)

    def list_for_tasks(self, task_ids: list[UUID]) -> list[TaskRun]:
        self._store.run_list_for_tasks_calls += 1
        task_id_set = set(task_ids)
        runs = [run for run in self._runs.values() if run.task_id in task_id_set]
        runs.sort(key=lambda run: (run.task_id, run.queued_at))
        return deepcopy(runs)

    def list_active_for_agent_version(
        self, agent_version_id: UUID, *, tenant_id: str
    ) -> list[TaskRun]:
        active = {
            RunStatus.QUEUED,
            RunStatus.RUNNING,
            RunStatus.PAUSE_REQUESTED,
            RunStatus.PAUSED,
        }
        runs = [
            run
            for run in self._runs.values()
            if run.agent_version_id == agent_version_id
            and run.status in active
            and self._tasks[run.task_id].tenant_id == tenant_id
        ]
        runs.sort(key=lambda run: run.queued_at)
        return deepcopy(runs)


class InMemorySubtaskRepository:
    def __init__(self, subtasks: dict[UUID, Subtask]) -> None:
        self._subtasks = subtasks

    def add(self, subtask: Subtask) -> None:
        self._subtasks[subtask.id] = deepcopy(subtask)

    def get(self, subtask_id: UUID, *, for_update: bool = False) -> Subtask | None:
        value = self._subtasks.get(subtask_id)
        return deepcopy(value) if value is not None else None

    def save(self, subtask: Subtask) -> None:
        if subtask.id not in self._subtasks:
            raise LookupError(subtask.id)
        self._subtasks[subtask.id] = deepcopy(subtask)

    def list_for_task(self, task_id: UUID, *, for_update: bool = False) -> list[Subtask]:
        values = [value for value in self._subtasks.values() if value.task_id == task_id]
        values.sort(key=lambda value: value.key)
        return deepcopy(values)

    def list_for_tasks(self, task_ids: list[UUID]) -> list[Subtask]:
        task_id_set = set(task_ids)
        values = [value for value in self._subtasks.values() if value.task_id in task_id_set]
        values.sort(key=lambda value: (value.task_id, value.key))
        return deepcopy(values)


class InMemorySubtaskDependencyRepository:
    def __init__(
        self,
        dependencies: dict[tuple[UUID, UUID, UUID], SubtaskDependency],
    ) -> None:
        self._dependencies = dependencies

    def add(self, dependency: SubtaskDependency) -> None:
        key = (
            dependency.task_id,
            dependency.predecessor_id,
            dependency.successor_id,
        )
        self._dependencies[key] = deepcopy(dependency)

    def list_for_task(self, task_id: UUID) -> list[SubtaskDependency]:
        values = [
            value for value in self._dependencies.values() if value.task_id == task_id
        ]
        values.sort(key=lambda value: (value.successor_id, value.predecessor_id))
        return deepcopy(values)

    def list_for_tasks(self, task_ids: list[UUID]) -> list[SubtaskDependency]:
        task_id_set = set(task_ids)
        values = [
            value for value in self._dependencies.values() if value.task_id in task_id_set
        ]
        values.sort(
            key=lambda value: (value.task_id, value.successor_id, value.predecessor_id)
        )
        return deepcopy(values)


class InMemoryHandoffRepository:
    def __init__(self, handoffs: dict[UUID, Handoff]) -> None:
        self._handoffs = handoffs

    def add(self, handoff: Handoff) -> None:
        self._handoffs[handoff.id] = deepcopy(handoff)

    def get(self, handoff_id: UUID, *, for_update: bool = False) -> Handoff | None:
        value = self._handoffs.get(handoff_id)
        return deepcopy(value) if value is not None else None

    def save(self, handoff: Handoff) -> None:
        if handoff.id not in self._handoffs:
            raise LookupError(handoff.id)
        self._handoffs[handoff.id] = deepcopy(handoff)

    def list_for_task(self, task_id: UUID) -> list[Handoff]:
        values = [value for value in self._handoffs.values() if value.task_id == task_id]
        values.sort(key=lambda value: (value.requested_at, value.id))
        return deepcopy(values)

    def list_for_tasks(self, task_ids: list[UUID]) -> list[Handoff]:
        task_id_set = set(task_ids)
        values = [value for value in self._handoffs.values() if value.task_id in task_id_set]
        values.sort(key=lambda value: (value.task_id, value.requested_at, value.id))
        return deepcopy(values)

    def list_for_target(
        self, target_subtask_id: UUID, *, status: HandoffStatus | None = None
    ) -> list[Handoff]:
        values = [
            value
            for value in self._handoffs.values()
            if value.target_subtask_id == target_subtask_id
            and (status is None or value.status == status)
        ]
        values.sort(key=lambda value: value.requested_at)
        return deepcopy(values)


class InMemoryTaskAttemptRepository:
    def __init__(
        self,
        attempts: dict[UUID, TaskAttempt],
        runs: dict[UUID, TaskRun],
        store: InMemoryStore,
    ) -> None:
        self._attempts = attempts
        self._runs = runs
        self._store = store

    def add(self, attempt: TaskAttempt) -> None:
        self._attempts[attempt.id] = deepcopy(attempt)

    def get(self, attempt_id: UUID, *, for_update: bool = False) -> TaskAttempt | None:
        attempt = self._attempts.get(attempt_id)
        return deepcopy(attempt) if attempt is not None else None

    def save(self, attempt: TaskAttempt) -> None:
        if attempt.id not in self._attempts:
            raise LookupError(attempt.id)
        self._attempts[attempt.id] = deepcopy(attempt)

    def latest_for_run(self, run_id: UUID, *, for_update: bool = False) -> TaskAttempt | None:
        attempts = [attempt for attempt in self._attempts.values() if attempt.run_id == run_id]
        if not attempts:
            return None
        return deepcopy(max(attempts, key=lambda attempt: attempt.fencing_token))

    def list_for_task(self, task_id: UUID) -> list[TaskAttempt]:
        self._store.attempt_list_for_task_calls += 1
        run_ids = {run.id for run in self._runs.values() if run.task_id == task_id}
        attempts = [attempt for attempt in self._attempts.values() if attempt.run_id in run_ids]
        attempts.sort(key=lambda attempt: attempt.started_at)
        return deepcopy(attempts)

    def list_for_tasks(self, task_ids: list[UUID]) -> list[TaskAttempt]:
        self._store.attempt_list_for_tasks_calls += 1
        task_id_set = set(task_ids)
        run_ids = {
            run.id for run in self._runs.values() if run.task_id in task_id_set
        }
        attempts = [attempt for attempt in self._attempts.values() if attempt.run_id in run_ids]
        attempts.sort(key=lambda attempt: (self._runs[attempt.run_id].task_id, attempt.started_at))
        return deepcopy(attempts)


class InMemoryOutboxRepository:
    def __init__(self, outbox: list[MessageEnvelope]) -> None:
        self._outbox = outbox

    def add(self, envelope: MessageEnvelope) -> None:
        self._outbox.append(deepcopy(envelope))


class InMemoryInboxRepository:
    def __init__(self, inbox: dict[tuple[str, str, UUID], InboxMessage]) -> None:
        self._inbox = inbox

    def contains(self, tenant_id: str, consumer_name: str, message_id: UUID) -> bool:
        return (tenant_id, consumer_name, message_id) in self._inbox

    def add(self, message: InboxMessage) -> None:
        self._inbox[
            (message.tenant_id, message.consumer_name, message.message_id)
        ] = deepcopy(message)


class InMemoryIdempotencyRepository:
    def __init__(self, records: dict[tuple[str, str], IdempotencyRecord]) -> None:
        self._records = records

    def lock(self, scope: str, key: str) -> None:
        pass

    def get(self, scope: str, key: str) -> IdempotencyRecord | None:
        record = self._records.get((scope, key))
        if record is not None and record.expires_at <= datetime.now(timezone.utc):
            del self._records[(scope, key)]
            return None
        return deepcopy(record) if record is not None else None

    def add(self, record: IdempotencyRecord) -> None:
        self._records[(record.scope, record.key)] = deepcopy(record)


class InMemoryArtifactRepository:
    def __init__(self, artifacts: dict[UUID, Artifact]) -> None:
        self._artifacts = artifacts

    def add(self, artifact: Artifact) -> None:
        self._artifacts[artifact.id] = deepcopy(artifact)

    def get(self, artifact_id: UUID, *, for_update: bool = False) -> Artifact | None:
        return deepcopy(self._artifacts.get(artifact_id))

    def list(self, *, tenant_id: str, limit: int, offset: int) -> list[Artifact]:
        values = [value for value in self._artifacts.values() if value.tenant_id == tenant_id]
        values.sort(key=lambda value: value.created_at, reverse=True)
        return deepcopy(values[offset : offset + limit])

    def save(self, artifact: Artifact) -> None:
        if artifact.id not in self._artifacts:
            raise LookupError(artifact.id)
        self._artifacts[artifact.id] = deepcopy(artifact)


class InMemoryArtifactVersionRepository:
    def __init__(self, versions: dict[UUID, ArtifactVersion], store: InMemoryStore) -> None:
        self._versions = versions
        self._store = store

    def add(self, version: ArtifactVersion) -> None:
        self._versions[version.id] = deepcopy(version)

    def get(self, version_id: UUID) -> ArtifactVersion | None:
        return deepcopy(self._versions.get(version_id))

    def list_for_artifact(self, artifact_id: UUID) -> list[ArtifactVersion]:
        self._store.artifact_version_list_for_artifact_calls += 1
        values = [value for value in self._versions.values() if value.artifact_id == artifact_id]
        values.sort(key=lambda value: value.version_number)
        return deepcopy(values)

    def list_for_artifacts(self, artifact_ids: list[UUID]) -> list[ArtifactVersion]:
        self._store.artifact_version_list_for_artifacts_calls += 1
        artifact_id_set = set(artifact_ids)
        values = [
            value for value in self._versions.values() if value.artifact_id in artifact_id_set
        ]
        values.sort(key=lambda value: (value.artifact_id, value.version_number))
        return deepcopy(values)


class InMemoryToolInvocationRepository:
    def __init__(self, invocations: dict[UUID, ToolInvocation]) -> None:
        self._invocations = invocations

    def add(self, invocation: ToolInvocation) -> None:
        self._invocations[invocation.id] = deepcopy(invocation)

    def get(
        self,
        invocation_id: UUID,
        *,
        for_update: bool = False,
    ) -> ToolInvocation | None:
        return deepcopy(self._invocations.get(invocation_id))

    def save(self, invocation: ToolInvocation) -> None:
        if invocation.id not in self._invocations:
            raise LookupError(invocation.id)
        self._invocations[invocation.id] = deepcopy(invocation)

    def list_for_task(self, task_id: UUID) -> list[ToolInvocation]:
        values = [value for value in self._invocations.values() if value.task_id == task_id]
        values.sort(key=lambda value: value.started_at)
        return deepcopy(values)


class InMemoryUsageRecordRepository:
    def __init__(self, records: dict[UUID, UsageRecord]) -> None:
        self._records = records

    def add_if_absent(self, record: UsageRecord) -> bool:
        existing = self._records.get(record.id)
        if existing is not None:
            if existing != record:
                raise IdempotencyConflict(
                    f"Usage record ID {record.id} was reused with different content"
                )
            return False
        self._records[record.id] = deepcopy(record)
        return True

    def list_for_task(self, task_id: UUID) -> list[UsageRecord]:
        values = [value for value in self._records.values() if value.task_id == task_id]
        values.sort(key=lambda value: (value.recorded_at, value.id))
        return deepcopy(values)


class InMemoryAgentDefinitionRepository:
    def __init__(self, definitions: dict[UUID, AgentDefinition]) -> None:
        self._definitions = definitions

    def add(self, definition: AgentDefinition) -> None:
        self._definitions[definition.id] = deepcopy(definition)

    def get(self, definition_id: UUID, *, for_update: bool = False) -> AgentDefinition | None:
        value = self._definitions.get(definition_id)
        return deepcopy(value) if value is not None else None

    def get_by_name(
        self, tenant_id: str, name: str, *, for_update: bool = False
    ) -> AgentDefinition | None:
        value = next(
            (
                definition
                for definition in self._definitions.values()
                if definition.tenant_id == tenant_id and definition.name == name
            ),
            None,
        )
        return deepcopy(value) if value is not None else None

    def list(self, *, tenant_id: str, limit: int, offset: int) -> list[AgentDefinition]:
        values = [
            definition
            for definition in self._definitions.values()
            if definition.tenant_id == tenant_id
        ]
        values.sort(key=lambda value: value.created_at, reverse=True)
        return deepcopy(values[offset : offset + limit])

    def save(self, definition: AgentDefinition) -> None:
        if definition.id not in self._definitions:
            raise LookupError(definition.id)
        self._definitions[definition.id] = deepcopy(definition)


class InMemoryAgentVersionRepository:
    def __init__(self, versions: dict[UUID, AgentVersion]) -> None:
        self._versions = versions

    def add(self, agent_version: AgentVersion) -> None:
        self._versions[agent_version.id] = deepcopy(agent_version)

    def get(self, agent_version_id: UUID, *, for_update: bool = False) -> AgentVersion | None:
        value = self._versions.get(agent_version_id)
        return deepcopy(value) if value is not None else None

    def get_by_semantic_version(
        self,
        definition_id: UUID,
        semantic_version: str,
        *,
        for_update: bool = False,
    ) -> AgentVersion | None:
        value = next(
            (
                version
                for version in self._versions.values()
                if version.definition_id == definition_id
                and version.semantic_version == semantic_version
            ),
            None,
        )
        return deepcopy(value) if value is not None else None

    def list_for_definition(self, definition_id: UUID) -> list[AgentVersion]:
        values = [
            version for version in self._versions.values() if version.definition_id == definition_id
        ]
        values.sort(key=lambda value: value.created_at)
        return deepcopy(values)

    def save(self, agent_version: AgentVersion) -> None:
        if agent_version.id not in self._versions:
            raise LookupError(agent_version.id)
        self._versions[agent_version.id] = deepcopy(agent_version)


class InMemoryCapabilityRepository:
    def __init__(self, capabilities: dict[UUID, Capability]) -> None:
        self._capabilities = capabilities

    def add(self, capability: Capability) -> None:
        self._capabilities[capability.id] = deepcopy(capability)

    def get(self, capability_id: UUID) -> Capability | None:
        return deepcopy(self._capabilities.get(capability_id))

    def get_by_key_version(self, tenant_id: str, key: str, version: str) -> Capability | None:
        value = next(
            (
                capability
                for capability in self._capabilities.values()
                if capability.tenant_id == tenant_id
                and capability.key == key
                and capability.version == version
            ),
            None,
        )
        return deepcopy(value) if value is not None else None

    def list(self, *, tenant_id: str, limit: int, offset: int) -> list[Capability]:
        values = [
            capability
            for capability in self._capabilities.values()
            if capability.tenant_id == tenant_id
        ]
        values.sort(key=lambda value: (value.key, value.version))
        return deepcopy(values[offset : offset + limit])


class InMemoryAgentDeploymentRepository:
    def __init__(self, deployments: dict[UUID, AgentDeployment]) -> None:
        self._deployments = deployments

    def add(self, deployment: AgentDeployment) -> None:
        self._deployments[deployment.id] = deepcopy(deployment)

    def get(self, deployment_id: UUID, *, for_update: bool = False) -> AgentDeployment | None:
        return deepcopy(self._deployments.get(deployment_id))

    def list_for_version(self, agent_version_id: UUID) -> list[AgentDeployment]:
        values = [
            deployment
            for deployment in self._deployments.values()
            if deployment.agent_version_id == agent_version_id
        ]
        values.sort(key=lambda value: value.created_at)
        return deepcopy(values)

    def save(self, deployment: AgentDeployment) -> None:
        if deployment.id not in self._deployments:
            raise LookupError(deployment.id)
        self._deployments[deployment.id] = deepcopy(deployment)


class InMemoryAgentInstanceRepository:
    def __init__(self, instances: dict[UUID, AgentInstance]) -> None:
        self._instances = instances

    def add(self, instance: AgentInstance) -> None:
        self._instances[instance.id] = deepcopy(instance)

    def get_by_external_id(
        self,
        deployment_id: UUID,
        external_instance_id: str,
        *,
        for_update: bool = False,
    ) -> AgentInstance | None:
        value = next(
            (
                instance
                for instance in self._instances.values()
                if instance.deployment_id == deployment_id
                and instance.external_instance_id == external_instance_id
            ),
            None,
        )
        return deepcopy(value) if value is not None else None

    def list_for_deployment(self, deployment_id: UUID) -> list[AgentInstance]:
        values = [
            instance
            for instance in self._instances.values()
            if instance.deployment_id == deployment_id
        ]
        values.sort(key=lambda value: value.external_instance_id)
        return deepcopy(values)

    def save(self, instance: AgentInstance) -> None:
        if instance.id not in self._instances:
            raise LookupError(instance.id)
        self._instances[instance.id] = deepcopy(instance)


class InMemoryUnitOfWork:
    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    def __enter__(self) -> InMemoryUnitOfWork:
        self._tasks = deepcopy(self._store.tasks)
        self._subtasks = deepcopy(self._store.subtasks)
        self._subtask_dependencies = deepcopy(self._store.subtask_dependencies)
        self._handoffs = deepcopy(self._store.handoffs)
        self._runs = deepcopy(self._store.runs)
        self._attempts = deepcopy(self._store.attempts)
        self._outbox = deepcopy(self._store.outbox)
        self._inbox = deepcopy(self._store.inbox)
        self._idempotency = deepcopy(self._store.idempotency)
        self._agent_definitions = deepcopy(self._store.agent_definitions)
        self._agent_versions = deepcopy(self._store.agent_versions)
        self._capabilities = deepcopy(self._store.capabilities)
        self._agent_deployments = deepcopy(self._store.agent_deployments)
        self._agent_instances = deepcopy(self._store.agent_instances)
        self._artifacts = deepcopy(self._store.artifacts)
        self._artifact_versions = deepcopy(self._store.artifact_versions)
        self._tool_invocations = deepcopy(self._store.tool_invocations)
        self._usage_records = deepcopy(self._store.usage_records)
        self.tasks = InMemoryTaskRepository(self._tasks)
        self.subtasks = InMemorySubtaskRepository(self._subtasks)
        self.subtask_dependencies = InMemorySubtaskDependencyRepository(
            self._subtask_dependencies
        )
        self.handoffs = InMemoryHandoffRepository(self._handoffs)
        self.runs = InMemoryTaskRunRepository(self._runs, self._tasks, self._store)
        self.attempts = InMemoryTaskAttemptRepository(self._attempts, self._runs, self._store)
        self.outbox = InMemoryOutboxRepository(self._outbox)
        self.inbox = InMemoryInboxRepository(self._inbox)
        self.idempotency = InMemoryIdempotencyRepository(self._idempotency)
        self.agent_definitions = InMemoryAgentDefinitionRepository(self._agent_definitions)
        self.agent_versions = InMemoryAgentVersionRepository(self._agent_versions)
        self.capabilities = InMemoryCapabilityRepository(self._capabilities)
        self.agent_deployments = InMemoryAgentDeploymentRepository(self._agent_deployments)
        self.agent_instances = InMemoryAgentInstanceRepository(self._agent_instances)
        self.artifacts = InMemoryArtifactRepository(self._artifacts)
        self.artifact_versions = InMemoryArtifactVersionRepository(
            self._artifact_versions,
            self._store,
        )
        self.tool_invocations = InMemoryToolInvocationRepository(self._tool_invocations)
        self.usage_records = InMemoryUsageRecordRepository(self._usage_records)
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        if exc_type is not None:
            self.rollback()

    def commit(self) -> None:
        self._store.tasks = deepcopy(self._tasks)
        self._store.subtasks = deepcopy(self._subtasks)
        self._store.subtask_dependencies = deepcopy(self._subtask_dependencies)
        self._store.handoffs = deepcopy(self._handoffs)
        self._store.runs = deepcopy(self._runs)
        self._store.attempts = deepcopy(self._attempts)
        self._store.outbox = deepcopy(self._outbox)
        self._store.inbox = deepcopy(self._inbox)
        self._store.idempotency = deepcopy(self._idempotency)
        self._store.agent_definitions = deepcopy(self._agent_definitions)
        self._store.agent_versions = deepcopy(self._agent_versions)
        self._store.capabilities = deepcopy(self._capabilities)
        self._store.agent_deployments = deepcopy(self._agent_deployments)
        self._store.agent_instances = deepcopy(self._agent_instances)
        self._store.artifacts = deepcopy(self._artifacts)
        self._store.artifact_versions = deepcopy(self._artifact_versions)
        self._store.tool_invocations = deepcopy(self._tool_invocations)
        self._store.usage_records = deepcopy(self._usage_records)

    def flush(self) -> None:
        pass

    def rollback(self) -> None:
        pass


class InMemoryUnitOfWorkFactory:
    def __init__(self, store: InMemoryStore | None = None) -> None:
        self.store = store or InMemoryStore()

    def __call__(self) -> InMemoryUnitOfWork:
        return InMemoryUnitOfWork(self.store)


class AlwaysReady:
    def is_ready(self) -> bool:
        return True
