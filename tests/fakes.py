from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID

from agentmesh.domain.a2a_delegation import RemoteTaskCorrelation
from agentmesh.domain.a2a_registry import A2APeer, AgentCardSnapshot
from agentmesh.domain.artifacts import Artifact, ArtifactVersion
from agentmesh.domain.coordination import Subtask, SubtaskDependency
from agentmesh.domain.errors import IdempotencyConflict
from agentmesh.domain.handoffs import Handoff, HandoffStatus
from agentmesh.domain.identity import ExternalIdentity, Principal, RoleBinding
from agentmesh.domain.mcp_registry import McpServer, McpServerVersion, McpToolCapability
from agentmesh.domain.messaging import IdempotencyRecord, InboxMessage, MessageEnvelope
from agentmesh.domain.observability import UsageRecord
from agentmesh.domain.policy import ApprovalDecision, ApprovalStatus, GovernedAction
from agentmesh.domain.registry import (
    AgentDefinition,
    AgentDeployment,
    AgentInstance,
    AgentVersion,
    Capability,
)
from agentmesh.domain.resolutions import TaskResolution
from agentmesh.domain.tasks import RunStatus, Task, TaskAttempt, TaskRun, TaskStatus
from agentmesh.domain.tools import ToolInvocation


@dataclass
class InMemoryStore:
    tasks: dict[UUID, Task] = field(default_factory=dict)
    task_resolutions: dict[UUID, TaskResolution] = field(default_factory=dict)
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
    governed_actions: dict[UUID, GovernedAction] = field(default_factory=dict)
    approval_decisions: dict[UUID, ApprovalDecision] = field(default_factory=dict)
    principals: dict[UUID, Principal] = field(default_factory=dict)
    external_identities: dict[UUID, ExternalIdentity] = field(default_factory=dict)
    role_bindings: dict[UUID, RoleBinding] = field(default_factory=dict)
    mcp_servers: dict[UUID, McpServer] = field(default_factory=dict)
    mcp_server_versions: dict[UUID, McpServerVersion] = field(default_factory=dict)
    mcp_tool_capabilities: dict[UUID, McpToolCapability] = field(default_factory=dict)
    a2a_peers: dict[UUID, A2APeer] = field(default_factory=dict)
    a2a_card_snapshots: dict[UUID, AgentCardSnapshot] = field(default_factory=dict)
    remote_correlations: dict[UUID, RemoteTaskCorrelation] = field(default_factory=dict)
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


class InMemoryTaskResolutionRepository:
    def __init__(self, resolutions: dict[UUID, TaskResolution]) -> None:
        self._resolutions = resolutions

    def add(self, resolution: TaskResolution) -> None:
        self._resolutions[resolution.id] = deepcopy(resolution)

    def get(self, resolution_id: UUID) -> TaskResolution | None:
        value = self._resolutions.get(resolution_id)
        return deepcopy(value) if value is not None else None

    def list_for_task(self, task_id: UUID) -> list[TaskResolution]:
        values = [value for value in self._resolutions.values() if value.task_id == task_id]
        values.sort(key=lambda value: (value.created_at, value.id))
        return deepcopy(values)


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
        values = [value for value in self._dependencies.values() if value.task_id == task_id]
        values.sort(key=lambda value: (value.successor_id, value.predecessor_id))
        return deepcopy(values)

    def list_for_tasks(self, task_ids: list[UUID]) -> list[SubtaskDependency]:
        task_id_set = set(task_ids)
        values = [value for value in self._dependencies.values() if value.task_id in task_id_set]
        values.sort(key=lambda value: (value.task_id, value.successor_id, value.predecessor_id))
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
        run_ids = {run.id for run in self._runs.values() if run.task_id in task_id_set}
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
        self._inbox[(message.tenant_id, message.consumer_name, message.message_id)] = deepcopy(
            message
        )


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


class InMemoryMcpRegistryRepository:
    def __init__(
        self,
        servers: dict[UUID, McpServer],
        versions: dict[UUID, McpServerVersion],
        tools: dict[UUID, McpToolCapability],
    ) -> None:
        self._servers = servers
        self._versions = versions
        self._tools = tools

    def lock_catalog_key(self, *, tenant_id: str, logical_key: str) -> None:
        pass

    def add_server(self, server: McpServer) -> None:
        self._servers[server.id] = deepcopy(server)

    def get_server(self, server_id: UUID, *, for_update: bool = False) -> McpServer | None:
        value = self._servers.get(server_id)
        return deepcopy(value) if value is not None else None

    def get_server_by_name(self, *, tenant_id: str, name: str) -> McpServer | None:
        value = next(
            (
                item
                for item in self._servers.values()
                if item.tenant_id == tenant_id and item.name == name
            ),
            None,
        )
        return deepcopy(value) if value is not None else None

    def save_server(self, server: McpServer) -> None:
        if server.id not in self._servers:
            raise LookupError(server.id)
        self._servers[server.id] = deepcopy(server)

    def list_servers(self, *, tenant_id: str, limit: int, offset: int) -> list[McpServer]:
        values = [value for value in self._servers.values() if value.tenant_id == tenant_id]
        values.sort(key=lambda value: (value.created_at, str(value.id)))
        return deepcopy(values[offset : offset + limit])

    def add_version(self, version: McpServerVersion) -> None:
        self._versions[version.id] = deepcopy(version)

    def get_version(self, version_id: UUID, *, for_update: bool = False) -> McpServerVersion | None:
        value = self._versions.get(version_id)
        return deepcopy(value) if value is not None else None

    def get_version_by_semantic(
        self, server_id: UUID, semantic_version: str
    ) -> McpServerVersion | None:
        value = next(
            (
                item
                for item in self._versions.values()
                if item.server_id == server_id and item.semantic_version == semantic_version
            ),
            None,
        )
        return deepcopy(value) if value is not None else None

    def save_version(self, version: McpServerVersion) -> None:
        if version.id not in self._versions:
            raise LookupError(version.id)
        self._versions[version.id] = deepcopy(version)

    def list_versions(self, server_id: UUID) -> list[McpServerVersion]:
        values = [value for value in self._versions.values() if value.server_id == server_id]
        values.sort(key=lambda value: (value.created_at, str(value.id)))
        return deepcopy(values)

    def add_tool(self, tool: McpToolCapability) -> None:
        self._tools[tool.id] = deepcopy(tool)

    def list_tools(self, server_version_id: UUID) -> list[McpToolCapability]:
        values = [
            value for value in self._tools.values() if value.server_version_id == server_version_id
        ]
        values.sort(key=lambda value: value.logical_key)
        return deepcopy(values)

    def list_tools_by_key(self, *, tenant_id: str, logical_key: str) -> list[McpToolCapability]:
        values = [
            value
            for value in self._tools.values()
            if value.tenant_id == tenant_id and value.logical_key == logical_key
        ]
        values.sort(key=lambda value: value.created_at, reverse=True)
        return deepcopy(values)


class InMemoryA2ARegistryRepository:
    def __init__(
        self,
        peers: dict[UUID, A2APeer],
        snapshots: dict[UUID, AgentCardSnapshot],
    ) -> None:
        self._peers = peers
        self._snapshots = snapshots

    def add_peer(self, peer: A2APeer) -> None:
        self._peers[peer.id] = deepcopy(peer)

    def get_peer(self, peer_id: UUID, *, for_update: bool = False) -> A2APeer | None:
        return deepcopy(self._peers.get(peer_id))

    def get_peer_by_name(self, *, tenant_id: str, name: str) -> A2APeer | None:
        value = next(
            (
                peer
                for peer in self._peers.values()
                if peer.tenant_id == tenant_id and peer.name == name
            ),
            None,
        )
        return deepcopy(value)

    def save_peer(self, peer: A2APeer) -> None:
        if peer.id not in self._peers:
            raise LookupError(peer.id)
        self._peers[peer.id] = deepcopy(peer)

    def list_peers(self, *, tenant_id: str, limit: int, offset: int) -> list[A2APeer]:
        values = [peer for peer in self._peers.values() if peer.tenant_id == tenant_id]
        values.sort(key=lambda value: (value.created_at, str(value.id)))
        return deepcopy(values[offset : offset + limit])

    def add_snapshot(self, snapshot: AgentCardSnapshot) -> None:
        self._snapshots[snapshot.id] = deepcopy(snapshot)

    def get_snapshot(self, snapshot_id: UUID) -> AgentCardSnapshot | None:
        return deepcopy(self._snapshots.get(snapshot_id))

    def list_snapshots(self, peer_id: UUID) -> list[AgentCardSnapshot]:
        values = [value for value in self._snapshots.values() if value.peer_id == peer_id]
        values.sort(key=lambda value: (value.fetched_at, str(value.id)), reverse=True)
        return deepcopy(values[:20])


class InMemoryRemoteTaskCorrelationRepository:
    def __init__(self, correlations: dict[UUID, RemoteTaskCorrelation]) -> None:
        self._correlations = correlations

    def add(self, correlation: RemoteTaskCorrelation) -> None:
        self._correlations[correlation.id] = deepcopy(correlation)

    def get(
        self, correlation_id: UUID, *, for_update: bool = False
    ) -> RemoteTaskCorrelation | None:
        return deepcopy(self._correlations.get(correlation_id))

    def get_for_task(self, task_id: UUID) -> RemoteTaskCorrelation | None:
        value = next(
            (item for item in self._correlations.values() if item.task_id == task_id), None
        )
        return deepcopy(value)

    def save(self, correlation: RemoteTaskCorrelation) -> None:
        if correlation.id not in self._correlations:
            raise LookupError(correlation.id)
        self._correlations[correlation.id] = deepcopy(correlation)

    def list(self, *, tenant_id: str, limit: int, offset: int) -> list[RemoteTaskCorrelation]:
        values = [item for item in self._correlations.values() if item.tenant_id == tenant_id]
        values.sort(key=lambda item: (item.created_at, str(item.id)), reverse=True)
        return deepcopy(values[offset : offset + limit])


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


class InMemoryPolicyRepository:
    def __init__(
        self,
        actions: dict[UUID, GovernedAction],
        decisions: dict[UUID, ApprovalDecision],
    ) -> None:
        self._actions = actions
        self._decisions = decisions

    def add_action(self, action: GovernedAction) -> None:
        self._actions[action.id] = deepcopy(action)

    def get_action(self, action_id: UUID, *, for_update: bool = False) -> GovernedAction | None:
        value = self._actions.get(action_id)
        return deepcopy(value) if value is not None else None

    def get_by_approval(
        self, approval_id: UUID, *, for_update: bool = False
    ) -> GovernedAction | None:
        value = next(
            (item for item in self._actions.values() if item.approval_id == approval_id), None
        )
        return deepcopy(value) if value is not None else None

    def get_by_permit(self, permit_id: UUID, *, for_update: bool = False) -> GovernedAction | None:
        value = next((item for item in self._actions.values() if item.permit_id == permit_id), None)
        return deepcopy(value) if value is not None else None

    def save_action(self, action: GovernedAction) -> None:
        if action.id not in self._actions:
            raise LookupError(action.id)
        self._actions[action.id] = deepcopy(action)

    def list_actions(
        self,
        *,
        tenant_id: str,
        approval_status: ApprovalStatus | None,
        limit: int,
        offset: int,
    ) -> list[GovernedAction]:
        values = [
            value
            for value in self._actions.values()
            if value.tenant_id == tenant_id and value.approval_id is not None
        ]
        if approval_status is not None:
            values = [value for value in values if value.approval_status is approval_status]
        values.sort(key=lambda value: value.created_at, reverse=True)
        return deepcopy(values[offset : offset + limit])

    def add_decision(self, decision: ApprovalDecision) -> None:
        self._decisions[decision.id] = deepcopy(decision)

    def list_decisions(self, governed_action_id: UUID) -> list[ApprovalDecision]:
        values = [
            value
            for value in self._decisions.values()
            if value.governed_action_id == governed_action_id
        ]
        values.sort(key=lambda value: value.created_at)
        return deepcopy(values)


class InMemoryIdentityRepository:
    def __init__(
        self,
        principals: dict[UUID, Principal],
        external_identities: dict[UUID, ExternalIdentity],
        role_bindings: dict[UUID, RoleBinding],
    ) -> None:
        self._principals = principals
        self._external_identities = external_identities
        self._role_bindings = role_bindings

    def add_principal(self, principal: Principal) -> None:
        self._principals[principal.id] = deepcopy(principal)

    def get_principal(self, principal_id: UUID, *, for_update: bool = False) -> Principal | None:
        value = self._principals.get(principal_id)
        return deepcopy(value) if value is not None else None

    def save_principal(self, principal: Principal) -> None:
        if principal.id not in self._principals:
            raise LookupError(principal.id)
        self._principals[principal.id] = deepcopy(principal)

    def list_principals(self, *, tenant_id: str, limit: int, offset: int) -> list[Principal]:
        values = [value for value in self._principals.values() if value.tenant_id == tenant_id]
        values.sort(key=lambda value: (value.created_at, str(value.id)))
        return deepcopy(values[offset : offset + limit])

    def add_external_identity(self, identity: ExternalIdentity) -> None:
        self._external_identities[identity.id] = deepcopy(identity)

    def get_external_identity(
        self, *, tenant_id: str, issuer: str, subject: str
    ) -> ExternalIdentity | None:
        value = next(
            (
                item
                for item in self._external_identities.values()
                if item.tenant_id == tenant_id and item.issuer == issuer and item.subject == subject
            ),
            None,
        )
        return deepcopy(value) if value is not None else None

    def list_external_identities(self, principal_id: UUID) -> list[ExternalIdentity]:
        return deepcopy(
            [
                value
                for value in self._external_identities.values()
                if value.principal_id == principal_id
            ]
        )

    def add_role_binding(self, binding: RoleBinding) -> None:
        self._role_bindings[binding.id] = deepcopy(binding)

    def get_role_binding(self, binding_id: UUID, *, for_update: bool = False) -> RoleBinding | None:
        value = self._role_bindings.get(binding_id)
        return deepcopy(value) if value is not None else None

    def save_role_binding(self, binding: RoleBinding) -> None:
        if binding.id not in self._role_bindings:
            raise LookupError(binding.id)
        self._role_bindings[binding.id] = deepcopy(binding)

    def list_role_bindings(self, principal_id: UUID) -> list[RoleBinding]:
        values = [
            value for value in self._role_bindings.values() if value.principal_id == principal_id
        ]
        values.sort(key=lambda value: (value.created_at, str(value.id)))
        return deepcopy(values)


class InMemoryUnitOfWork:
    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    def __enter__(self) -> InMemoryUnitOfWork:
        self._tasks = deepcopy(self._store.tasks)
        self._task_resolutions = deepcopy(self._store.task_resolutions)
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
        self._governed_actions = deepcopy(self._store.governed_actions)
        self._approval_decisions = deepcopy(self._store.approval_decisions)
        self._principals = deepcopy(self._store.principals)
        self._external_identities = deepcopy(self._store.external_identities)
        self._role_bindings = deepcopy(self._store.role_bindings)
        self._mcp_servers = deepcopy(self._store.mcp_servers)
        self._mcp_server_versions = deepcopy(self._store.mcp_server_versions)
        self._mcp_tool_capabilities = deepcopy(self._store.mcp_tool_capabilities)
        self._a2a_peers = deepcopy(self._store.a2a_peers)
        self._a2a_card_snapshots = deepcopy(self._store.a2a_card_snapshots)
        self._remote_correlations = deepcopy(self._store.remote_correlations)
        self.tasks = InMemoryTaskRepository(self._tasks)
        self.task_resolutions = InMemoryTaskResolutionRepository(self._task_resolutions)
        self.subtasks = InMemorySubtaskRepository(self._subtasks)
        self.subtask_dependencies = InMemorySubtaskDependencyRepository(self._subtask_dependencies)
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
        self.policy = InMemoryPolicyRepository(
            self._governed_actions,
            self._approval_decisions,
        )
        self.identity = InMemoryIdentityRepository(
            self._principals,
            self._external_identities,
            self._role_bindings,
        )
        self.mcp_registry = InMemoryMcpRegistryRepository(
            self._mcp_servers,
            self._mcp_server_versions,
            self._mcp_tool_capabilities,
        )
        self.a2a_registry = InMemoryA2ARegistryRepository(
            self._a2a_peers,
            self._a2a_card_snapshots,
        )
        self.remote_correlations = InMemoryRemoteTaskCorrelationRepository(
            self._remote_correlations
        )
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        if exc_type is not None:
            self.rollback()

    def commit(self) -> None:
        self._store.tasks = deepcopy(self._tasks)
        self._store.task_resolutions = deepcopy(self._task_resolutions)
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
        self._store.governed_actions = deepcopy(self._governed_actions)
        self._store.approval_decisions = deepcopy(self._approval_decisions)
        self._store.principals = deepcopy(self._principals)
        self._store.external_identities = deepcopy(self._external_identities)
        self._store.role_bindings = deepcopy(self._role_bindings)
        self._store.mcp_servers = deepcopy(self._mcp_servers)
        self._store.mcp_server_versions = deepcopy(self._mcp_server_versions)
        self._store.mcp_tool_capabilities = deepcopy(self._mcp_tool_capabilities)
        self._store.a2a_peers = deepcopy(self._a2a_peers)
        self._store.a2a_card_snapshots = deepcopy(self._a2a_card_snapshots)
        self._store.remote_correlations = deepcopy(self._remote_correlations)

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


class ScriptedA2AClient:
    def __init__(
        self,
        *,
        send_responses: list[object] | None = None,
        task_responses: list[object] | None = None,
    ) -> None:
        self.send_responses = list(send_responses or [])
        self.task_responses = list(task_responses or [])
        self.send_calls: list[dict[str, object]] = []
        self.task_calls: list[dict[str, object]] = []

    def send_message(self, **kwargs) -> dict[str, object]:
        self.send_calls.append(kwargs)
        return self._next(self.send_responses)

    def get_task(self, **kwargs) -> dict[str, object]:
        self.task_calls.append(kwargs)
        return self._next(self.task_responses)

    @staticmethod
    def _next(values: list[object]) -> dict[str, object]:
        if not values:
            raise AssertionError("No scripted A2A response remains")
        value = values.pop(0)
        if isinstance(value, Exception):
            raise value
        if not isinstance(value, dict):
            raise AssertionError("Scripted A2A response must be an object")
        return deepcopy(value)
