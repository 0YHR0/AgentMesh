from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from agentmesh.domain.artifacts import Artifact, ArtifactVersion
from agentmesh.domain.messaging import IdempotencyRecord, InboxMessage, MessageEnvelope
from agentmesh.domain.registry import (
    AgentDefinition,
    AgentDeployment,
    AgentInstance,
    AgentVersion,
    Capability,
)
from agentmesh.domain.tasks import Task, TaskAttempt, TaskRun, TaskStatus


class TaskRepository(Protocol):
    def add(self, task: Task) -> None: ...

    def get(self, task_id: UUID, *, for_update: bool = False) -> Task | None: ...

    def save(self, task: Task) -> None: ...

    def list(
        self,
        *,
        limit: int,
        offset: int,
        tenant_id: str,
        status: TaskStatus | None = None,
    ) -> list[Task]: ...


class TaskRunRepository(Protocol):
    def add(self, run: TaskRun) -> None: ...

    def get(self, run_id: UUID, *, for_update: bool = False) -> TaskRun | None: ...

    def save(self, run: TaskRun) -> None: ...

    def list_for_task(self, task_id: UUID) -> list[TaskRun]: ...

    def list_active_for_agent_version(
        self, agent_version_id: UUID, *, tenant_id: str
    ) -> list[TaskRun]: ...


class TaskAttemptRepository(Protocol):
    def add(self, attempt: TaskAttempt) -> None: ...

    def get(self, attempt_id: UUID, *, for_update: bool = False) -> TaskAttempt | None: ...

    def save(self, attempt: TaskAttempt) -> None: ...

    def latest_for_run(self, run_id: UUID, *, for_update: bool = False) -> TaskAttempt | None: ...

    def list_for_task(self, task_id: UUID) -> list[TaskAttempt]: ...


class OutboxRepository(Protocol):
    def add(self, envelope: MessageEnvelope) -> None: ...


class InboxRepository(Protocol):
    def contains(self, consumer_name: str, message_id: UUID) -> bool: ...

    def add(self, message: InboxMessage) -> None: ...


class IdempotencyRepository(Protocol):
    def lock(self, scope: str, key: str) -> None: ...

    def get(self, scope: str, key: str) -> IdempotencyRecord | None: ...

    def add(self, record: IdempotencyRecord) -> None: ...


class AgentDefinitionRepository(Protocol):
    def add(self, definition: AgentDefinition) -> None: ...

    def get(self, definition_id: UUID, *, for_update: bool = False) -> AgentDefinition | None: ...

    def get_by_name(
        self, tenant_id: str, name: str, *, for_update: bool = False
    ) -> AgentDefinition | None: ...

    def list(self, *, tenant_id: str, limit: int, offset: int) -> list[AgentDefinition]: ...

    def save(self, definition: AgentDefinition) -> None: ...


class AgentVersionRepository(Protocol):
    def add(self, agent_version: AgentVersion) -> None: ...

    def get(self, agent_version_id: UUID, *, for_update: bool = False) -> AgentVersion | None: ...

    def get_by_semantic_version(
        self,
        definition_id: UUID,
        semantic_version: str,
        *,
        for_update: bool = False,
    ) -> AgentVersion | None: ...

    def list_for_definition(self, definition_id: UUID) -> list[AgentVersion]: ...

    def save(self, agent_version: AgentVersion) -> None: ...


class CapabilityRepository(Protocol):
    def add(self, capability: Capability) -> None: ...

    def get(self, capability_id: UUID) -> Capability | None: ...

    def get_by_key_version(self, tenant_id: str, key: str, version: str) -> Capability | None: ...

    def list(self, *, tenant_id: str, limit: int, offset: int) -> list[Capability]: ...


class AgentDeploymentRepository(Protocol):
    def add(self, deployment: AgentDeployment) -> None: ...

    def get(self, deployment_id: UUID, *, for_update: bool = False) -> AgentDeployment | None: ...

    def list_for_version(self, agent_version_id: UUID) -> list[AgentDeployment]: ...

    def save(self, deployment: AgentDeployment) -> None: ...


class AgentInstanceRepository(Protocol):
    def add(self, instance: AgentInstance) -> None: ...

    def get_by_external_id(
        self,
        deployment_id: UUID,
        external_instance_id: str,
        *,
        for_update: bool = False,
    ) -> AgentInstance | None: ...

    def list_for_deployment(self, deployment_id: UUID) -> list[AgentInstance]: ...

    def save(self, instance: AgentInstance) -> None: ...


class ArtifactRepository(Protocol):
    def add(self, artifact: Artifact) -> None: ...

    def get(self, artifact_id: UUID, *, for_update: bool = False) -> Artifact | None: ...

    def list(self, *, tenant_id: str, limit: int, offset: int) -> list[Artifact]: ...

    def save(self, artifact: Artifact) -> None: ...


class ArtifactVersionRepository(Protocol):
    def add(self, version: ArtifactVersion) -> None: ...

    def get(self, version_id: UUID) -> ArtifactVersion | None: ...

    def list_for_artifact(self, artifact_id: UUID) -> list[ArtifactVersion]: ...


class UnitOfWork(Protocol):
    tasks: TaskRepository
    runs: TaskRunRepository
    attempts: TaskAttemptRepository
    outbox: OutboxRepository
    inbox: InboxRepository
    idempotency: IdempotencyRepository
    agent_definitions: AgentDefinitionRepository
    agent_versions: AgentVersionRepository
    capabilities: CapabilityRepository
    agent_deployments: AgentDeploymentRepository
    agent_instances: AgentInstanceRepository
    artifacts: ArtifactRepository
    artifact_versions: ArtifactVersionRepository

    def __enter__(self) -> UnitOfWork: ...

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


UnitOfWorkFactory = Callable[[], UnitOfWork]


class WorkflowRunner(Protocol):
    def run(self, task: Task, run: TaskRun) -> dict[str, Any]: ...


@dataclass(frozen=True)
class AgentExecutionContext:
    task_id: UUID
    run_id: UUID
    thread_id: str
    agent_id: str
    agent_version_id: UUID | None
    agent_version_digest: str | None


class AgentExecutor(Protocol):
    def execute(
        self,
        *,
        objective: str,
        input: dict[str, Any],
        context: AgentExecutionContext,
    ) -> dict[str, Any]: ...


class ReadinessProbe(Protocol):
    def is_ready(self) -> bool: ...
