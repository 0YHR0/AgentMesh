from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from agentmesh.domain.a2a_delegation import RemoteTaskCorrelation
from agentmesh.domain.a2a_registry import A2APeer, AgentCardSnapshot
from agentmesh.domain.artifacts import Artifact, ArtifactVersion
from agentmesh.domain.coordination import Subtask, SubtaskDependency
from agentmesh.domain.handoffs import Handoff, HandoffStatus
from agentmesh.domain.identity import ExternalIdentity, Principal, RoleBinding
from agentmesh.domain.mcp_registry import McpServer, McpServerVersion, McpToolCapability
from agentmesh.domain.messaging import IdempotencyRecord, InboxMessage, MessageEnvelope
from agentmesh.domain.observability import UsageRecord, UsageSource
from agentmesh.domain.policy import ApprovalDecision, ApprovalStatus, GovernedAction
from agentmesh.domain.registry import (
    AgentDefinition,
    AgentDeployment,
    AgentInstance,
    AgentVersion,
    Capability,
)
from agentmesh.domain.resolutions import TaskResolution
from agentmesh.domain.tasks import Task, TaskAttempt, TaskRun, TaskStatus
from agentmesh.domain.tools import ToolBinding, ToolCallResult, ToolInvocation


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

    def list_for_tasks(self, task_ids: list[UUID]) -> list[TaskRun]: ...

    def list_active_for_agent_version(
        self, agent_version_id: UUID, *, tenant_id: str
    ) -> list[TaskRun]: ...


class TaskResolutionRepository(Protocol):
    def add(self, resolution: TaskResolution) -> None: ...

    def get(self, resolution_id: UUID) -> TaskResolution | None: ...

    def list_for_task(self, task_id: UUID) -> list[TaskResolution]: ...


class SubtaskRepository(Protocol):
    def add(self, subtask: Subtask) -> None: ...

    def get(self, subtask_id: UUID, *, for_update: bool = False) -> Subtask | None: ...

    def save(self, subtask: Subtask) -> None: ...

    def list_for_task(self, task_id: UUID, *, for_update: bool = False) -> list[Subtask]: ...

    def list_for_tasks(self, task_ids: list[UUID]) -> list[Subtask]: ...


class SubtaskDependencyRepository(Protocol):
    def add(self, dependency: SubtaskDependency) -> None: ...

    def list_for_task(self, task_id: UUID) -> list[SubtaskDependency]: ...

    def list_for_tasks(self, task_ids: list[UUID]) -> list[SubtaskDependency]: ...


class HandoffRepository(Protocol):
    def add(self, handoff: Handoff) -> None: ...

    def get(self, handoff_id: UUID, *, for_update: bool = False) -> Handoff | None: ...

    def save(self, handoff: Handoff) -> None: ...

    def list_for_task(self, task_id: UUID) -> list[Handoff]: ...

    def list_for_tasks(self, task_ids: list[UUID]) -> list[Handoff]: ...

    def list_for_target(
        self, target_subtask_id: UUID, *, status: HandoffStatus | None = None
    ) -> list[Handoff]: ...


class TaskAttemptRepository(Protocol):
    def add(self, attempt: TaskAttempt) -> None: ...

    def get(self, attempt_id: UUID, *, for_update: bool = False) -> TaskAttempt | None: ...

    def save(self, attempt: TaskAttempt) -> None: ...

    def latest_for_run(self, run_id: UUID, *, for_update: bool = False) -> TaskAttempt | None: ...

    def list_for_task(self, task_id: UUID) -> list[TaskAttempt]: ...

    def list_for_tasks(self, task_ids: list[UUID]) -> list[TaskAttempt]: ...


class UsageRecordRepository(Protocol):
    def add_if_absent(self, record: UsageRecord) -> bool: ...

    def list_for_task(self, task_id: UUID) -> list[UsageRecord]: ...


class OutboxRepository(Protocol):
    def add(self, envelope: MessageEnvelope) -> None: ...


class InboxRepository(Protocol):
    def contains(self, tenant_id: str, consumer_name: str, message_id: UUID) -> bool: ...

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

    def list_for_artifacts(self, artifact_ids: list[UUID]) -> list[ArtifactVersion]: ...


class ToolInvocationRepository(Protocol):
    def add(self, invocation: ToolInvocation) -> None: ...

    def get(self, invocation_id: UUID, *, for_update: bool = False) -> ToolInvocation | None: ...

    def save(self, invocation: ToolInvocation) -> None: ...

    def list_for_task(self, task_id: UUID) -> list[ToolInvocation]: ...


class McpRegistryRepository(Protocol):
    def lock_catalog_key(self, *, tenant_id: str, logical_key: str) -> None: ...

    def add_server(self, server: McpServer) -> None: ...

    def get_server(self, server_id: UUID, *, for_update: bool = False) -> McpServer | None: ...

    def get_server_by_name(self, *, tenant_id: str, name: str) -> McpServer | None: ...

    def save_server(self, server: McpServer) -> None: ...

    def list_servers(self, *, tenant_id: str, limit: int, offset: int) -> list[McpServer]: ...

    def add_version(self, version: McpServerVersion) -> None: ...

    def get_version(
        self, version_id: UUID, *, for_update: bool = False
    ) -> McpServerVersion | None: ...

    def get_version_by_semantic(
        self, server_id: UUID, semantic_version: str
    ) -> McpServerVersion | None: ...

    def save_version(self, version: McpServerVersion) -> None: ...

    def list_versions(self, server_id: UUID) -> list[McpServerVersion]: ...

    def add_tool(self, tool: McpToolCapability) -> None: ...

    def list_tools(self, server_version_id: UUID) -> list[McpToolCapability]: ...

    def list_tools_by_key(self, *, tenant_id: str, logical_key: str) -> list[McpToolCapability]: ...


class A2ARegistryRepository(Protocol):
    def add_peer(self, peer: A2APeer) -> None: ...

    def get_peer(self, peer_id: UUID, *, for_update: bool = False) -> A2APeer | None: ...

    def get_peer_by_name(self, *, tenant_id: str, name: str) -> A2APeer | None: ...

    def save_peer(self, peer: A2APeer) -> None: ...

    def list_peers(self, *, tenant_id: str, limit: int, offset: int) -> list[A2APeer]: ...

    def add_snapshot(self, snapshot: AgentCardSnapshot) -> None: ...

    def get_snapshot(self, snapshot_id: UUID) -> AgentCardSnapshot | None: ...

    def list_snapshots(self, peer_id: UUID) -> list[AgentCardSnapshot]: ...


class RemoteTaskCorrelationRepository(Protocol):
    def add(self, correlation: RemoteTaskCorrelation) -> None: ...

    def get(
        self, correlation_id: UUID, *, for_update: bool = False
    ) -> RemoteTaskCorrelation | None: ...

    def get_for_task(self, task_id: UUID) -> RemoteTaskCorrelation | None: ...

    def save(self, correlation: RemoteTaskCorrelation) -> None: ...

    def list(self, *, tenant_id: str, limit: int, offset: int) -> list[RemoteTaskCorrelation]: ...


class PolicyRepository(Protocol):
    def add_action(self, action: GovernedAction) -> None: ...

    def get_action(self, action_id: UUID, *, for_update: bool = False) -> GovernedAction | None: ...

    def get_by_approval(
        self, approval_id: UUID, *, for_update: bool = False
    ) -> GovernedAction | None: ...

    def get_by_permit(
        self, permit_id: UUID, *, for_update: bool = False
    ) -> GovernedAction | None: ...

    def save_action(self, action: GovernedAction) -> None: ...

    def list_actions(
        self, *, tenant_id: str, approval_status: ApprovalStatus | None, limit: int, offset: int
    ) -> list[GovernedAction]: ...

    def add_decision(self, decision: ApprovalDecision) -> None: ...

    def list_decisions(self, governed_action_id: UUID) -> list[ApprovalDecision]: ...


class IdentityRepository(Protocol):
    def add_principal(self, principal: Principal) -> None: ...

    def get_principal(
        self, principal_id: UUID, *, for_update: bool = False
    ) -> Principal | None: ...

    def save_principal(self, principal: Principal) -> None: ...

    def list_principals(self, *, tenant_id: str, limit: int, offset: int) -> list[Principal]: ...

    def add_external_identity(self, identity: ExternalIdentity) -> None: ...

    def get_external_identity(
        self, *, tenant_id: str, issuer: str, subject: str
    ) -> ExternalIdentity | None: ...

    def list_external_identities(self, principal_id: UUID) -> list[ExternalIdentity]: ...

    def add_role_binding(self, binding: RoleBinding) -> None: ...

    def get_role_binding(
        self, binding_id: UUID, *, for_update: bool = False
    ) -> RoleBinding | None: ...

    def save_role_binding(self, binding: RoleBinding) -> None: ...

    def list_role_bindings(self, principal_id: UUID) -> list[RoleBinding]: ...


class UnitOfWork(Protocol):
    tasks: TaskRepository
    task_resolutions: TaskResolutionRepository
    subtasks: SubtaskRepository
    subtask_dependencies: SubtaskDependencyRepository
    handoffs: HandoffRepository
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
    tool_invocations: ToolInvocationRepository
    mcp_registry: McpRegistryRepository
    a2a_registry: A2ARegistryRepository
    remote_correlations: RemoteTaskCorrelationRepository
    usage_records: UsageRecordRepository
    policy: PolicyRepository
    identity: IdentityRepository

    def __enter__(self) -> UnitOfWork: ...

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None: ...

    def commit(self) -> None: ...

    def flush(self) -> None: ...

    def rollback(self) -> None: ...


UnitOfWorkFactory = Callable[[], UnitOfWork]


@dataclass(frozen=True)
class WorkflowExecutionResult:
    output: dict[str, Any]
    usage_records: tuple[UsageRecord, ...] = ()


@dataclass(frozen=True)
class WorkflowWorkItem:
    objective: str
    input: dict[str, Any]


class WorkflowRunner(Protocol):
    def run(
        self,
        task: Task,
        run: TaskRun,
        attempt: TaskAttempt,
        work_item: WorkflowWorkItem | None = None,
    ) -> WorkflowExecutionResult: ...


@dataclass(frozen=True)
class AgentExecutionContext:
    task_id: UUID
    run_id: UUID
    thread_id: str
    agent_id: str
    agent_version_id: UUID | None
    agent_version_digest: str | None
    run_role: str = "EXECUTOR"
    revision_number: int = 0
    tenant_id: str = "default"
    attempt_id: UUID | None = None
    trace_id: str | None = None
    usage_reporter: Callable[[UsageRecord], None] | None = None

    def report_usage(
        self,
        *,
        provider: str,
        model: str,
        usage_details: dict[str, int],
        cost_details_micros: dict[str, int] | None = None,
        currency: str = "USD",
        source: UsageSource = UsageSource.PROVIDER,
        pricing_version: str | None = None,
    ) -> UUID:
        if self.attempt_id is None or self.trace_id is None or self.usage_reporter is None:
            raise RuntimeError("Usage reporting is unavailable outside an active Task Attempt")
        record = UsageRecord.create(
            tenant_id=self.tenant_id,
            task_id=self.task_id,
            run_id=self.run_id,
            attempt_id=self.attempt_id,
            trace_id=self.trace_id,
            provider=provider,
            model=model,
            usage_details=usage_details,
            cost_details_micros=cost_details_micros,
            currency=currency,
            source=source,
            pricing_version=pricing_version,
        )
        self.usage_reporter(record)
        return record.id


class AgentExecutor(Protocol):
    def execute(
        self,
        *,
        objective: str,
        input: dict[str, Any],
        context: AgentExecutionContext,
    ) -> dict[str, Any]: ...


class AttemptTelemetry(Protocol):
    def observe_attempt(
        self,
        task: Task,
        run: TaskRun,
        attempt: TaskAttempt,
    ) -> AbstractContextManager[None]: ...

    def record_usage(self, record: UsageRecord) -> None: ...

    def close(self) -> None: ...


class ReadOnlyToolGateway(Protocol):
    def invoke(
        self,
        *,
        invocation_id: UUID,
        binding: ToolBinding,
        arguments: dict[str, Any],
    ) -> ToolCallResult: ...


class ToolCatalog(Protocol):
    def resolve(self, logical_key: str) -> ToolBinding: ...


class A2AProtocolClient(Protocol):
    def send_message(
        self,
        *,
        endpoint_url: str,
        protocol_version: str,
        endpoint_tenant: str | None,
        message: dict[str, Any],
        accepted_output_modes: tuple[str, ...],
    ) -> dict[str, Any]: ...

    def get_task(
        self,
        *,
        endpoint_url: str,
        protocol_version: str,
        endpoint_tenant: str | None,
        remote_task_id: str,
    ) -> dict[str, Any]: ...


class ReadinessProbe(Protocol):
    def is_ready(self) -> bool: ...
