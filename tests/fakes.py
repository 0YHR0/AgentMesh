from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID

from agentmesh.domain.a2a_delegation import RemoteCorrelationStatus, RemoteTaskCorrelation
from agentmesh.domain.a2a_registry import A2APeer, AgentCardSnapshot
from agentmesh.domain.activity import ReplayBookmark
from agentmesh.domain.artifacts import Artifact, ArtifactVersion
from agentmesh.domain.business_objects import (
    BusinessObject,
    BusinessObjectRevision,
    BusinessObjectType,
    BusinessObjectTypeStatus,
)
from agentmesh.domain.company import (
    Appointment,
    AppointmentStatus,
    Company,
    CompanyStatus,
    OrganizationRelationship,
    OrganizationUnit,
    Position,
    ResourceStatus,
)
from agentmesh.domain.company_goals import (
    CompanyObjective,
    Initiative,
    InitiativeTaskLink,
    KeyResult,
    OperatingCycle,
    OperatingCycleStatus,
)
from agentmesh.domain.company_operations import (
    CompanyOperation,
    OccurrenceStatus,
    OperationException,
    OperationOccurrence,
    OperationStatus,
    OperationTriggerState,
)
from agentmesh.domain.company_packs import CompanyPack, PackInstallation
from agentmesh.domain.coordination import Subtask, SubtaskDependency
from agentmesh.domain.credentials import (
    CredentialBinding,
    CredentialBindingStatus,
    CredentialLease,
    McpCredentialBinding,
    McpCredentialLease,
    SecretReference,
)
from agentmesh.domain.errors import IdempotencyConflict
from agentmesh.domain.financial_governance import (
    BudgetAllocation,
    BudgetLedgerEntry,
    EconomicEvidence,
    ExpenseRequest,
)
from agentmesh.domain.handoffs import Handoff, HandoffStatus
from agentmesh.domain.identity import ExternalIdentity, Principal, RoleBinding
from agentmesh.domain.mcp_registry import (
    McpDiscoverySnapshot,
    McpServer,
    McpServerVersion,
    McpToolCapability,
)
from agentmesh.domain.messaging import IdempotencyRecord, InboxMessage, MessageEnvelope
from agentmesh.domain.observability import UsageRecord
from agentmesh.domain.office import OfficePlacement, OfficeSpace
from agentmesh.domain.organizational_memory import (
    MemoryEvidence,
    MemoryPolicy,
    MemoryRecord,
    MemoryRetrieval,
    MemoryReview,
    MemoryStatus,
    MemoryType,
    namespace_key,
)
from agentmesh.domain.planning import GoalContract, PlanPatch
from agentmesh.domain.policy import ApprovalDecision, ApprovalStatus, GovernedAction
from agentmesh.domain.quotas import QuotaPolicy, QuotaReservation, QuotaScope
from agentmesh.domain.registry import (
    AgentDefinition,
    AgentDeployment,
    AgentInstance,
    AgentVersion,
    Capability,
)
from agentmesh.domain.resolutions import TaskResolution
from agentmesh.domain.tasks import RunStatus, Task, TaskAttempt, TaskRun, TaskStatus
from agentmesh.domain.tools import ToolExecutionAuthorization, ToolInvocation


class InMemoryOfficePlacementStore:
    def __init__(self) -> None:
        self.placements: dict[tuple[str, str], OfficePlacement] = {}
        self.spaces: dict[tuple[str, str], OfficeSpace] = {}

    def list(self, tenant_id: str) -> tuple[OfficePlacement, ...]:
        return tuple(
            deepcopy(value)
            for (stored_tenant_id, _), value in sorted(self.placements.items())
            if stored_tenant_id == tenant_id
        )

    def get_at_cell(
        self, tenant_id: str, grid_x: int, grid_z: int
    ) -> OfficePlacement | None:
        for (stored_tenant_id, _), value in self.placements.items():
            if (
                stored_tenant_id == tenant_id
                and value.grid_x == grid_x
                and value.grid_z == grid_z
            ):
                return deepcopy(value)
        return None

    def put(self, placement: OfficePlacement) -> None:
        self.placements[(placement.tenant_id, placement.agent_id)] = deepcopy(placement)

    def list_spaces(self, tenant_id: str) -> tuple[OfficeSpace, ...]:
        return tuple(
            deepcopy(value)
            for (stored_tenant_id, _), value in sorted(
                self.spaces.items(),
                key=lambda item: item[1].position,
            )
            if stored_tenant_id == tenant_id
        )

    def put_space(self, space: OfficeSpace) -> None:
        self.spaces[(space.tenant_id, space.key)] = deepcopy(space)

    def delete_spaces(self, tenant_id: str) -> int:
        keys = [key for key in self.spaces if key[0] == tenant_id]
        for key in keys:
            del self.spaces[key]
        return len(keys)


@dataclass
class InMemoryStore:
    companies: dict[UUID, Company] = field(default_factory=dict)
    organization_units: dict[UUID, OrganizationUnit] = field(default_factory=dict)
    company_positions: dict[UUID, Position] = field(default_factory=dict)
    company_appointments: dict[UUID, Appointment] = field(default_factory=dict)
    organization_relationships: dict[UUID, OrganizationRelationship] = field(
        default_factory=dict
    )
    operating_cycles: dict[UUID, OperatingCycle] = field(default_factory=dict)
    company_objectives: dict[UUID, CompanyObjective] = field(default_factory=dict)
    company_key_results: dict[UUID, KeyResult] = field(default_factory=dict)
    company_initiatives: dict[UUID, Initiative] = field(default_factory=dict)
    initiative_task_links: dict[tuple[UUID, UUID], InitiativeTaskLink] = field(
        default_factory=dict
    )
    company_operations: dict[UUID, CompanyOperation] = field(default_factory=dict)
    company_operation_trigger_states: dict[UUID, OperationTriggerState] = field(
        default_factory=dict
    )
    company_operation_occurrences: dict[UUID, OperationOccurrence] = field(
        default_factory=dict
    )
    company_operation_exceptions: dict[UUID, OperationException] = field(
        default_factory=dict
    )
    business_object_types: dict[UUID, BusinessObjectType] = field(default_factory=dict)
    business_objects: dict[UUID, BusinessObject] = field(default_factory=dict)
    business_object_revisions: dict[
        tuple[UUID, int], BusinessObjectRevision
    ] = field(default_factory=dict)
    memory_policies: dict[UUID, MemoryPolicy] = field(default_factory=dict)
    memory_records: dict[UUID, MemoryRecord] = field(default_factory=dict)
    memory_evidence: dict[tuple[UUID, str, str], MemoryEvidence] = field(
        default_factory=dict
    )
    memory_reviews: dict[UUID, MemoryReview] = field(default_factory=dict)
    memory_retrievals: dict[UUID, MemoryRetrieval] = field(default_factory=dict)
    budget_allocations: dict[UUID, BudgetAllocation] = field(default_factory=dict)
    budget_ledger_entries: dict[UUID, BudgetLedgerEntry] = field(default_factory=dict)
    economic_evidence: dict[UUID, EconomicEvidence] = field(default_factory=dict)
    expense_requests: dict[UUID, ExpenseRequest] = field(default_factory=dict)
    company_packs: dict[UUID, CompanyPack] = field(default_factory=dict)
    company_pack_installations: dict[UUID, PackInstallation] = field(
        default_factory=dict
    )
    tasks: dict[UUID, Task] = field(default_factory=dict)
    replay_bookmarks: dict[UUID, ReplayBookmark] = field(default_factory=dict)
    goal_contracts: dict[UUID, GoalContract] = field(default_factory=dict)
    plan_patches: dict[UUID, PlanPatch] = field(default_factory=dict)
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
    tool_execution_authorizations: dict[UUID, ToolExecutionAuthorization] = field(
        default_factory=dict
    )
    usage_records: dict[UUID, UsageRecord] = field(default_factory=dict)
    governed_actions: dict[UUID, GovernedAction] = field(default_factory=dict)
    approval_decisions: dict[UUID, ApprovalDecision] = field(default_factory=dict)
    principals: dict[UUID, Principal] = field(default_factory=dict)
    external_identities: dict[UUID, ExternalIdentity] = field(default_factory=dict)
    role_bindings: dict[UUID, RoleBinding] = field(default_factory=dict)
    mcp_servers: dict[UUID, McpServer] = field(default_factory=dict)
    mcp_server_versions: dict[UUID, McpServerVersion] = field(default_factory=dict)
    mcp_tool_capabilities: dict[UUID, McpToolCapability] = field(default_factory=dict)
    mcp_discovery_snapshots: dict[UUID, McpDiscoverySnapshot] = field(default_factory=dict)
    a2a_peers: dict[UUID, A2APeer] = field(default_factory=dict)
    a2a_card_snapshots: dict[UUID, AgentCardSnapshot] = field(default_factory=dict)
    remote_correlations: dict[UUID, RemoteTaskCorrelation] = field(default_factory=dict)
    secret_references: dict[UUID, SecretReference] = field(default_factory=dict)
    credential_bindings: dict[UUID, CredentialBinding] = field(default_factory=dict)
    credential_leases: dict[UUID, CredentialLease] = field(default_factory=dict)
    mcp_credential_bindings: dict[UUID, McpCredentialBinding] = field(default_factory=dict)
    mcp_credential_leases: dict[UUID, McpCredentialLease] = field(default_factory=dict)
    quota_policies: dict[UUID, QuotaPolicy] = field(default_factory=dict)
    quota_reservations: dict[UUID, QuotaReservation] = field(default_factory=dict)
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


class InMemoryReplayBookmarkRepository:
    def __init__(self, bookmarks: dict[UUID, ReplayBookmark]) -> None:
        self._bookmarks = bookmarks

    def add(self, bookmark: ReplayBookmark) -> None:
        self._bookmarks[bookmark.id] = deepcopy(bookmark)

    def get(self, bookmark_id: UUID) -> ReplayBookmark | None:
        bookmark = self._bookmarks.get(bookmark_id)
        return deepcopy(bookmark) if bookmark is not None else None

    def find_for_event(
        self, *, tenant_id: str, task_id: UUID, event_id: str
    ) -> ReplayBookmark | None:
        bookmark = next(
            (
                item
                for item in self._bookmarks.values()
                if item.tenant_id == tenant_id
                and item.task_id == task_id
                and item.event_id == event_id
            ),
            None,
        )
        return deepcopy(bookmark) if bookmark is not None else None

    def list_for_task(self, *, tenant_id: str, task_id: UUID) -> list[ReplayBookmark]:
        values = [
            item
            for item in self._bookmarks.values()
            if item.tenant_id == tenant_id and item.task_id == task_id
        ]
        values.sort(key=lambda item: (item.created_at, str(item.id)), reverse=True)
        return deepcopy(values)

    def delete(self, bookmark_id: UUID) -> None:
        self._bookmarks.pop(bookmark_id, None)


class InMemoryGoalContractRepository:
    def __init__(self, goals: dict[UUID, GoalContract]) -> None:
        self._goals = goals

    def add(self, goal: GoalContract) -> None:
        self._goals[goal.task_id] = deepcopy(goal)

    def get(self, task_id: UUID, *, for_update: bool = False) -> GoalContract | None:
        value = self._goals.get(task_id)
        return deepcopy(value) if value is not None else None


class InMemoryPlanPatchRepository:
    def __init__(self, patches: dict[UUID, PlanPatch]) -> None:
        self._patches = patches

    def add(self, patch: PlanPatch) -> None:
        self._patches[patch.id] = deepcopy(patch)

    def get(self, patch_id: UUID, *, for_update: bool = False) -> PlanPatch | None:
        value = self._patches.get(patch_id)
        return deepcopy(value) if value is not None else None

    def save(self, patch: PlanPatch) -> None:
        if patch.id not in self._patches:
            raise LookupError(patch.id)
        self._patches[patch.id] = deepcopy(patch)

    def list_for_task(self, task_id: UUID) -> list[PlanPatch]:
        values = [patch for patch in self._patches.values() if patch.task_id == task_id]
        values.sort(key=lambda patch: (patch.created_at, patch.id))
        return deepcopy(values)


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

    def delete_for_task(self, task_id: UUID) -> None:
        for key in [key for key, value in self._subtasks.items() if value.task_id == task_id]:
            del self._subtasks[key]

    def delete_ids(self, task_id: UUID, subtask_ids: list[UUID]) -> None:
        for subtask_id in subtask_ids:
            value = self._subtasks.get(subtask_id)
            if value is not None and value.task_id == task_id:
                del self._subtasks[subtask_id]


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

    def delete_for_task(self, task_id: UUID) -> None:
        for key in [key for key, value in self._dependencies.items() if value.task_id == task_id]:
            del self._dependencies[key]


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

    def list_for_producer_runs(self, run_ids: list[UUID]) -> list[ArtifactVersion]:
        run_id_set = set(run_ids)
        values = [
            value for value in self._versions.values() if value.producer_run_id in run_id_set
        ]
        values.sort(key=lambda value: value.created_at, reverse=True)
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


class InMemoryToolExecutionAuthorizationRepository:
    def __init__(self, values: dict[UUID, ToolExecutionAuthorization]) -> None:
        self._values = values

    def add(self, value: ToolExecutionAuthorization) -> None:
        self._values[value.id] = deepcopy(value)

    def get_for_task(
        self, task_id: UUID, *, for_update: bool = False
    ) -> ToolExecutionAuthorization | None:
        value = next((item for item in self._values.values() if item.task_id == task_id), None)
        return deepcopy(value) if value is not None else None

    def save(self, value: ToolExecutionAuthorization) -> None:
        if value.id not in self._values:
            raise LookupError(value.id)
        self._values[value.id] = deepcopy(value)


class InMemoryMcpRegistryRepository:
    def __init__(
        self,
        servers: dict[UUID, McpServer],
        versions: dict[UUID, McpServerVersion],
        tools: dict[UUID, McpToolCapability],
        snapshots: dict[UUID, McpDiscoverySnapshot],
    ) -> None:
        self._servers = servers
        self._versions = versions
        self._tools = tools
        self._snapshots = snapshots

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

    def add_discovery_snapshot(self, snapshot: McpDiscoverySnapshot) -> None:
        self._snapshots[snapshot.id] = deepcopy(snapshot)

    def get_discovery_snapshot(self, snapshot_id: UUID) -> McpDiscoverySnapshot | None:
        return deepcopy(self._snapshots.get(snapshot_id))

    def latest_discovery_snapshot(
        self, server_version_id: UUID
    ) -> McpDiscoverySnapshot | None:
        values = [
            value
            for value in self._snapshots.values()
            if value.server_version_id == server_version_id
        ]
        values.sort(key=lambda value: (value.fetched_at, str(value.id)), reverse=True)
        return deepcopy(values[0]) if values else None

    def list_discovery_snapshots(
        self, server_version_id: UUID, *, limit: int, offset: int
    ) -> list[McpDiscoverySnapshot]:
        values = [
            value
            for value in self._snapshots.values()
            if value.server_version_id == server_version_id
        ]
        values.sort(key=lambda value: (value.fetched_at, str(value.id)), reverse=True)
        return deepcopy(values[offset : offset + limit])


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

    def claim_due(
        self,
        *,
        tenant_id: str,
        now: datetime,
        owner: str,
        lease_expires_at: datetime,
        limit: int,
    ) -> list[RemoteTaskCorrelation]:
        eligible = [
            value
            for value in self._correlations.values()
            if value.tenant_id == tenant_id
            and value.status
            in {
                RemoteCorrelationStatus.WAITING_REMOTE,
                RemoteCorrelationStatus.CANCELING,
                RemoteCorrelationStatus.CANCEL_PENDING,
                RemoteCorrelationStatus.CANCEL_OUTCOME_UNKNOWN,
            }
            and value.remote_task_id is not None
            and value.next_poll_at is not None
            and value.next_poll_at <= now
            and (value.poll_lease_expires_at is None or value.poll_lease_expires_at <= now)
        ]
        eligible.sort(key=lambda value: (value.next_poll_at, str(value.id)))
        claimed = [
            value.claim_poll(owner=owner, lease_expires_at=lease_expires_at, now=now)
            for value in eligible[:limit]
        ]
        for value in claimed:
            self._correlations[value.id] = deepcopy(value)
        return deepcopy(claimed)


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


class InMemoryQuotaRepository:
    def __init__(
        self,
        policies: dict[UUID, QuotaPolicy],
        reservations: dict[UUID, QuotaReservation],
    ) -> None:
        self._policies = policies
        self._reservations = reservations

    def add_policy(self, policy: QuotaPolicy) -> None:
        self._policies[policy.id] = deepcopy(policy)

    def replace_active(self, policy: QuotaPolicy) -> None:
        for policy_id, current in tuple(self._policies.items()):
            if (
                current.tenant_id == policy.tenant_id
                and current.scope is policy.scope
                and current.project_id == policy.project_id
                and current.active
            ):
                self._policies[policy_id] = QuotaPolicy(**{**current.__dict__, "active": False})
        self.add_policy(policy)

    def get_active(
        self,
        tenant_id: str,
        scope: QuotaScope,
        project_id: str | None,
        *,
        for_update: bool = False,
    ) -> QuotaPolicy | None:
        value = next(
            (
                item
                for item in self._policies.values()
                if item.tenant_id == tenant_id
                and item.scope is scope
                and item.project_id == project_id
                and item.active
            ),
            None,
        )
        return deepcopy(value)

    def list_active_for_task(
        self, tenant_id: str, project_id: str, *, for_update: bool = False
    ) -> list[QuotaPolicy]:
        values = [
            item
            for item in self._policies.values()
            if item.tenant_id == tenant_id
            and item.active
            and (item.scope is QuotaScope.TENANT or item.project_id == project_id)
        ]
        return deepcopy(sorted(values, key=lambda value: (value.scope.value, str(value.id))))

    def list_active(self, tenant_id: str) -> list[QuotaPolicy]:
        values = [
            item for item in self._policies.values() if item.tenant_id == tenant_id and item.active
        ]
        return deepcopy(sorted(values, key=lambda value: (value.scope.value, value.scope_key)))

    def next_version(self, tenant_id: str, scope: QuotaScope, project_id: str | None) -> int:
        versions = [
            item.version
            for item in self._policies.values()
            if item.tenant_id == tenant_id
            and item.scope is scope
            and item.project_id == project_id
        ]
        return max(versions, default=0) + 1

    def count_active(self, policy_id: UUID) -> int:
        return sum(
            item.policy_id == policy_id and item.released_at is None
            for item in self._reservations.values()
        )

    def count_active_for_scope(
        self, tenant_id: str, scope: QuotaScope, project_id: str | None
    ) -> int:
        policy_ids = {
            item.id
            for item in self._policies.values()
            if item.tenant_id == tenant_id
            and item.scope is scope
            and item.project_id == project_id
        }
        return sum(
            item.policy_id in policy_ids and item.released_at is None
            for item in self._reservations.values()
        )

    def add_reservation(self, reservation: QuotaReservation) -> None:
        self._reservations[reservation.id] = deepcopy(reservation)

    def list_reservations_for_attempt(
        self, attempt_id: UUID, *, for_update: bool = False
    ) -> list[QuotaReservation]:
        return deepcopy(
            [
                item
                for item in self._reservations.values()
                if item.attempt_id == attempt_id and item.released_at is None
            ]
        )

    def save_reservation(self, reservation: QuotaReservation) -> None:
        self._reservations[reservation.id] = deepcopy(reservation)


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

    def list_actions_for_resource(
        self, *, tenant_id: str, resource_type: str, resource_id: UUID
    ) -> list[GovernedAction]:
        values = [
            value
            for value in self._actions.values()
            if value.tenant_id == tenant_id
            and value.resource_type == resource_type
            and value.resource_id == resource_id
        ]
        values.sort(key=lambda value: value.created_at)
        return deepcopy(values)

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


class InMemoryCredentialRepository:
    def __init__(
        self,
        secret_references: dict[UUID, SecretReference],
        bindings: dict[UUID, CredentialBinding],
        leases: dict[UUID, CredentialLease],
        mcp_bindings: dict[UUID, McpCredentialBinding],
        mcp_leases: dict[UUID, McpCredentialLease],
    ) -> None:
        self._secret_references = secret_references
        self._bindings = bindings
        self._leases = leases
        self._mcp_bindings = mcp_bindings
        self._mcp_leases = mcp_leases

    def add_secret_reference(self, reference: SecretReference) -> None:
        self._secret_references[reference.id] = deepcopy(reference)

    def get_secret_reference(
        self, reference_id: UUID, *, for_update: bool = False
    ) -> SecretReference | None:
        value = self._secret_references.get(reference_id)
        return deepcopy(value) if value is not None else None

    def save_secret_reference(self, reference: SecretReference) -> None:
        if reference.id not in self._secret_references:
            raise LookupError(reference.id)
        self._secret_references[reference.id] = deepcopy(reference)

    def list_secret_references(
        self, *, tenant_id: str, limit: int, offset: int
    ) -> list[SecretReference]:
        values = [
            value for value in self._secret_references.values() if value.tenant_id == tenant_id
        ]
        values.sort(key=lambda value: (value.created_at, str(value.id)), reverse=True)
        return deepcopy(values[offset : offset + limit])

    def add_binding(self, binding: CredentialBinding) -> None:
        self._bindings[binding.id] = deepcopy(binding)

    def get_binding(
        self, binding_id: UUID, *, for_update: bool = False
    ) -> CredentialBinding | None:
        value = self._bindings.get(binding_id)
        return deepcopy(value) if value is not None else None

    def save_binding(self, binding: CredentialBinding) -> None:
        if binding.id not in self._bindings:
            raise LookupError(binding.id)
        self._bindings[binding.id] = deepcopy(binding)

    def list_bindings(self, *, tenant_id: str, limit: int, offset: int) -> list[CredentialBinding]:
        values = [value for value in self._bindings.values() if value.tenant_id == tenant_id]
        values.sort(key=lambda value: (value.created_at, str(value.id)), reverse=True)
        return deepcopy(values[offset : offset + limit])

    def add_lease(self, lease: CredentialLease) -> None:
        self._leases[lease.id] = deepcopy(lease)

    def get_lease(self, lease_id: UUID, *, for_update: bool = False) -> CredentialLease | None:
        value = self._leases.get(lease_id)
        return deepcopy(value) if value is not None else None

    def save_lease(self, lease: CredentialLease) -> None:
        if lease.id not in self._leases:
            raise LookupError(lease.id)
        self._leases[lease.id] = deepcopy(lease)

    def list_leases(self, *, tenant_id: str, limit: int, offset: int) -> list[CredentialLease]:
        values = [value for value in self._leases.values() if value.tenant_id == tenant_id]
        values.sort(key=lambda value: (value.created_at, str(value.id)), reverse=True)
        return deepcopy(values[offset : offset + limit])

    def add_mcp_binding(self, binding: McpCredentialBinding) -> None:
        self._mcp_bindings[binding.id] = deepcopy(binding)

    def get_mcp_binding(
        self, binding_id: UUID, *, for_update: bool = False
    ) -> McpCredentialBinding | None:
        value = self._mcp_bindings.get(binding_id)
        return deepcopy(value) if value is not None else None

    def find_mcp_binding(
        self,
        *,
        tenant_id: str,
        workload_principal_id: UUID,
        server_version_id: UUID,
        environment: str,
        for_update: bool = False,
    ) -> McpCredentialBinding | None:
        matches = [
            value
            for value in self._mcp_bindings.values()
            if value.tenant_id == tenant_id
            and value.workload_principal_id == workload_principal_id
            and value.server_version_id == server_version_id
            and value.environment == environment
            and value.status is CredentialBindingStatus.ACTIVE
        ]
        if len(matches) > 1:
            raise LookupError("ambiguous active MCP credential bindings")
        return deepcopy(matches[0]) if matches else None

    def save_mcp_binding(self, binding: McpCredentialBinding) -> None:
        if binding.id not in self._mcp_bindings:
            raise LookupError(binding.id)
        self._mcp_bindings[binding.id] = deepcopy(binding)

    def list_mcp_bindings(
        self, *, tenant_id: str, limit: int, offset: int
    ) -> list[McpCredentialBinding]:
        values = [value for value in self._mcp_bindings.values() if value.tenant_id == tenant_id]
        values.sort(key=lambda value: (value.created_at, str(value.id)), reverse=True)
        return deepcopy(values[offset : offset + limit])

    def add_mcp_lease(self, lease: McpCredentialLease) -> None:
        self._mcp_leases[lease.id] = deepcopy(lease)

    def get_mcp_lease(
        self, lease_id: UUID, *, for_update: bool = False
    ) -> McpCredentialLease | None:
        value = self._mcp_leases.get(lease_id)
        return deepcopy(value) if value is not None else None

    def save_mcp_lease(self, lease: McpCredentialLease) -> None:
        if lease.id not in self._mcp_leases:
            raise LookupError(lease.id)
        self._mcp_leases[lease.id] = deepcopy(lease)

    def list_mcp_leases(
        self, *, tenant_id: str, limit: int, offset: int
    ) -> list[McpCredentialLease]:
        values = [value for value in self._mcp_leases.values() if value.tenant_id == tenant_id]
        values.sort(key=lambda value: (value.created_at, str(value.id)), reverse=True)
        return deepcopy(values[offset : offset + limit])


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


class InMemoryCompanyGoalRepository:
    def __init__(
        self,
        cycles: dict[UUID, OperatingCycle],
        objectives: dict[UUID, CompanyObjective],
        key_results: dict[UUID, KeyResult],
        initiatives: dict[UUID, Initiative],
        links: dict[tuple[UUID, UUID], InitiativeTaskLink],
    ) -> None:
        self._cycles = cycles
        self._objectives = objectives
        self._key_results = key_results
        self._initiatives = initiatives
        self._links = links

    def add_cycle(self, value: OperatingCycle) -> None:
        self._cycles[value.id] = deepcopy(value)

    def get_cycle(
        self, cycle_id: UUID, *, for_update: bool = False
    ) -> OperatingCycle | None:
        return deepcopy(self._cycles.get(cycle_id))

    def get_active_cycle(self, company_id: UUID) -> OperatingCycle | None:
        return deepcopy(
            next(
                (
                    value
                    for value in self._cycles.values()
                    if value.company_id == company_id
                    and value.status is OperatingCycleStatus.ACTIVE
                ),
                None,
            )
        )

    def list_cycles(self, company_id: UUID) -> list[OperatingCycle]:
        return deepcopy(
            sorted(
                (value for value in self._cycles.values() if value.company_id == company_id),
                key=lambda value: (value.created_at, str(value.id)),
            )
        )

    def save_cycle(self, value: OperatingCycle) -> None:
        self._cycles[value.id] = deepcopy(value)

    def add_objective(self, value: CompanyObjective) -> None:
        self._objectives[value.id] = deepcopy(value)

    def get_objective(
        self, objective_id: UUID, *, for_update: bool = False
    ) -> CompanyObjective | None:
        return deepcopy(self._objectives.get(objective_id))

    def list_objectives(self, cycle_id: UUID) -> list[CompanyObjective]:
        return deepcopy(
            sorted(
                (value for value in self._objectives.values() if value.cycle_id == cycle_id),
                key=lambda value: (value.priority, value.created_at, str(value.id)),
            )
        )

    def save_objective(self, value: CompanyObjective) -> None:
        self._objectives[value.id] = deepcopy(value)

    def add_key_result(self, value: KeyResult) -> None:
        self._key_results[value.id] = deepcopy(value)

    def get_key_result(
        self, key_result_id: UUID, *, for_update: bool = False
    ) -> KeyResult | None:
        return deepcopy(self._key_results.get(key_result_id))

    def list_key_results(self, objective_id: UUID) -> list[KeyResult]:
        return deepcopy(
            sorted(
                (
                    value
                    for value in self._key_results.values()
                    if value.objective_id == objective_id
                ),
                key=lambda value: (value.created_at, str(value.id)),
            )
        )

    def save_key_result(self, value: KeyResult) -> None:
        self._key_results[value.id] = deepcopy(value)

    def add_initiative(self, value: Initiative) -> None:
        self._initiatives[value.id] = deepcopy(value)

    def get_initiative(
        self, initiative_id: UUID, *, for_update: bool = False
    ) -> Initiative | None:
        return deepcopy(self._initiatives.get(initiative_id))

    def list_initiatives(self, objective_id: UUID) -> list[Initiative]:
        return deepcopy(
            sorted(
                (
                    value
                    for value in self._initiatives.values()
                    if value.objective_id == objective_id
                ),
                key=lambda value: (value.created_at, str(value.id)),
            )
        )

    def save_initiative(self, value: Initiative) -> None:
        self._initiatives[value.id] = deepcopy(value)

    def add_task_link(self, value: InitiativeTaskLink) -> None:
        self._links[(value.initiative_id, value.task_id)] = deepcopy(value)

    def list_task_links(self, initiative_id: UUID) -> list[InitiativeTaskLink]:
        return deepcopy(
            sorted(
                (
                    value
                    for value in self._links.values()
                    if value.initiative_id == initiative_id
                ),
                key=lambda value: (value.created_at, str(value.task_id)),
            )
        )


class InMemoryCompanyOperationRepository:
    def __init__(
        self,
        companies: dict[UUID, Company],
        operations: dict[UUID, CompanyOperation],
        states: dict[UUID, OperationTriggerState],
        occurrences: dict[UUID, OperationOccurrence],
        exceptions: dict[UUID, OperationException],
    ) -> None:
        self._companies = companies
        self._operations = operations
        self._states = states
        self._occurrences = occurrences
        self._exceptions = exceptions

    def add_operation(self, value: CompanyOperation) -> None:
        self._operations[value.id] = deepcopy(value)

    def get_operation(
        self, operation_id: UUID, *, for_update: bool = False
    ) -> CompanyOperation | None:
        return deepcopy(self._operations.get(operation_id))

    def get_operation_by_key(
        self, company_id: UUID, key: str
    ) -> CompanyOperation | None:
        return deepcopy(
            next(
                (
                    value
                    for value in self._operations.values()
                    if value.company_id == company_id and value.key == key
                ),
                None,
            )
        )

    def list_operations(self, company_id: UUID) -> list[CompanyOperation]:
        return deepcopy(
            sorted(
                (
                    value
                    for value in self._operations.values()
                    if value.company_id == company_id
                ),
                key=lambda value: (value.created_at, str(value.id)),
            )
        )

    def save_operation(self, value: CompanyOperation) -> None:
        self._operations[value.id] = deepcopy(value)

    def add_trigger_state(self, value: OperationTriggerState) -> None:
        self._states[value.operation_id] = deepcopy(value)

    def get_trigger_state(
        self, operation_id: UUID, *, for_update: bool = False
    ) -> OperationTriggerState | None:
        return deepcopy(self._states.get(operation_id))

    def list_due(
        self, now: datetime, *, tenant_id: str, limit: int
    ) -> list[tuple[CompanyOperation, OperationTriggerState]]:
        values = [
            (operation, state)
            for operation in self._operations.values()
            if operation.status is OperationStatus.ACTIVE
            and self._companies[operation.company_id].tenant_id == tenant_id
            and (state := self._states.get(operation.id)) is not None
            and state.next_due_at is not None
            and state.next_due_at <= now
        ]
        values.sort(key=lambda pair: pair[1].next_due_at or now)
        return deepcopy(values[:limit])

    def save_trigger_state(self, value: OperationTriggerState) -> None:
        self._states[value.operation_id] = deepcopy(value)

    def add_occurrence(self, value: OperationOccurrence) -> None:
        self._occurrences[value.id] = deepcopy(value)

    def get_occurrence_by_key(
        self, operation_id: UUID, occurrence_key: str
    ) -> OperationOccurrence | None:
        return deepcopy(
            next(
                (
                    value
                    for value in self._occurrences.values()
                    if value.operation_id == operation_id
                    and value.occurrence_key == occurrence_key
                ),
                None,
            )
        )

    def list_occurrences(
        self, operation_id: UUID, *, limit: int = 100
    ) -> list[OperationOccurrence]:
        values = [
            value
            for value in self._occurrences.values()
            if value.operation_id == operation_id
        ]
        values.sort(key=lambda value: (value.scheduled_at, str(value.id)), reverse=True)
        return deepcopy(values[:limit])

    def count_occurrences(
        self,
        operation_id: UUID,
        *,
        since: datetime,
        statuses: set[OccurrenceStatus],
    ) -> int:
        return sum(
            value.operation_id == operation_id
            and value.created_at >= since
            and value.status in statuses
            for value in self._occurrences.values()
        )

    def save_occurrence(self, value: OperationOccurrence) -> None:
        self._occurrences[value.id] = deepcopy(value)

    def add_exception(self, value: OperationException) -> None:
        self._exceptions[value.id] = deepcopy(value)

    def list_exceptions(
        self, operation_id: UUID, *, unresolved_only: bool = False
    ) -> list[OperationException]:
        values = [
            value
            for value in self._exceptions.values()
            if value.operation_id == operation_id
            and (not unresolved_only or value.resolved_at is None)
        ]
        values.sort(key=lambda value: (value.created_at, str(value.id)), reverse=True)
        return deepcopy(values)

    def list_retryable(
        self, now: datetime, *, tenant_id: str, limit: int
    ) -> list[tuple[CompanyOperation, OperationOccurrence, OperationException]]:
        values = []
        for exception in self._exceptions.values():
            occurrence = (
                self._occurrences.get(exception.occurrence_id)
                if exception.occurrence_id
                else None
            )
            operation = (
                self._operations.get(exception.operation_id)
                if occurrence is not None
                else None
            )
            if (
                operation is not None
                and self._companies[operation.company_id].tenant_id == tenant_id
                and operation.status is OperationStatus.ACTIVE
                and occurrence is not None
                and occurrence.status is OccurrenceStatus.PENDING
                and exception.retryable
                and exception.resolved_at is None
                and exception.next_retry_at is not None
                and exception.next_retry_at <= now
            ):
                values.append((operation, occurrence, exception))
        values.sort(key=lambda value: value[2].next_retry_at or now)
        return deepcopy(values[:limit])

    def save_exception(self, value: OperationException) -> None:
        self._exceptions[value.id] = deepcopy(value)


class InMemoryBusinessObjectRepository:
    def __init__(
        self,
        types: dict[UUID, BusinessObjectType],
        objects: dict[UUID, BusinessObject],
        revisions: dict[tuple[UUID, int], BusinessObjectRevision],
    ) -> None:
        self._types = types
        self._objects = objects
        self._revisions = revisions

    def add_type(self, value: BusinessObjectType) -> None:
        self._types[value.id] = deepcopy(value)

    def get_type(
        self, type_id: UUID, *, for_update: bool = False
    ) -> BusinessObjectType | None:
        return deepcopy(self._types.get(type_id))

    def get_type_by_key(
        self,
        company_id: UUID,
        key: str,
        *,
        schema_version: int | None = None,
        published_only: bool = False,
    ) -> BusinessObjectType | None:
        values = [
            value
            for value in self._types.values()
            if value.company_id == company_id
            and value.key == key
            and (schema_version is None or value.schema_version == schema_version)
            and (
                not published_only
                or value.status is BusinessObjectTypeStatus.PUBLISHED
            )
        ]
        values.sort(key=lambda value: value.schema_version, reverse=True)
        return deepcopy(values[0]) if values else None

    def list_types(self, company_id: UUID) -> list[BusinessObjectType]:
        values = [
            value for value in self._types.values() if value.company_id == company_id
        ]
        values.sort(key=lambda value: (value.key, -value.schema_version))
        return deepcopy(values)

    def save_type(self, value: BusinessObjectType) -> None:
        self._types[value.id] = deepcopy(value)

    def add_object(self, value: BusinessObject) -> None:
        self._objects[value.id] = deepcopy(value)

    def get_object(
        self, object_id: UUID, *, for_update: bool = False
    ) -> BusinessObject | None:
        return deepcopy(self._objects.get(object_id))

    def get_object_by_external_ref(
        self, type_id: UUID, external_ref: str
    ) -> BusinessObject | None:
        return deepcopy(
            next(
                (
                    value
                    for value in self._objects.values()
                    if value.type_id == type_id and value.external_ref == external_ref
                ),
                None,
            )
        )

    def list_objects(
        self,
        company_id: UUID,
        *,
        type_id: UUID | None,
        limit: int,
        offset: int,
    ) -> list[BusinessObject]:
        values = [
            value
            for value in self._objects.values()
            if value.company_id == company_id
            and (type_id is None or value.type_id == type_id)
        ]
        values.sort(key=lambda value: (value.updated_at, str(value.id)), reverse=True)
        return deepcopy(values[offset : offset + limit])

    def save_object(self, value: BusinessObject) -> None:
        self._objects[value.id] = deepcopy(value)

    def add_revision(self, value: BusinessObjectRevision) -> None:
        self._revisions[(value.object_id, value.revision)] = deepcopy(value)

    def get_revision(
        self, object_id: UUID, revision: int
    ) -> BusinessObjectRevision | None:
        return deepcopy(self._revisions.get((object_id, revision)))

    def list_revisions(self, object_id: UUID) -> list[BusinessObjectRevision]:
        values = [
            value
            for (candidate_id, _), value in self._revisions.items()
            if candidate_id == object_id
        ]
        values.sort(key=lambda value: value.revision)
        return deepcopy(values)


class InMemoryOrganizationalMemoryRepository:
    def __init__(
        self,
        policies: dict[UUID, MemoryPolicy],
        records: dict[UUID, MemoryRecord],
        evidence: dict[tuple[UUID, str, str], MemoryEvidence],
        reviews: dict[UUID, MemoryReview],
        retrievals: dict[UUID, MemoryRetrieval],
    ) -> None:
        self._policies = policies
        self._records = records
        self._evidence = evidence
        self._reviews = reviews
        self._retrievals = retrievals

    def add_policy(self, value: MemoryPolicy) -> None:
        self._policies[value.id] = deepcopy(value)

    def get_policy(self, policy_id: UUID) -> MemoryPolicy | None:
        return deepcopy(self._policies.get(policy_id))

    def get_policy_by_key(
        self, company_id: UUID, key: str, *, active_only: bool = False
    ) -> MemoryPolicy | None:
        values = [
            value
            for value in self._policies.values()
            if value.company_id == company_id
            and value.key == key
            and (not active_only or value.active)
        ]
        values.sort(key=lambda value: value.version, reverse=True)
        return deepcopy(values[0]) if values else None

    def list_policies(self, company_id: UUID) -> list[MemoryPolicy]:
        values = [
            value
            for value in self._policies.values()
            if value.company_id == company_id
        ]
        values.sort(key=lambda value: (value.key, -value.version))
        return deepcopy(values)

    def save_policy(self, value: MemoryPolicy) -> None:
        self._policies[value.id] = deepcopy(value)

    def add_record(self, value: MemoryRecord) -> None:
        self._records[value.id] = deepcopy(value)

    def get_record(
        self, memory_id: UUID, *, for_update: bool = False
    ) -> MemoryRecord | None:
        return deepcopy(self._records.get(memory_id))

    def find_by_digest(
        self,
        *,
        company_id: UUID,
        namespace_type: str,
        namespace_id: str,
        memory_type: MemoryType,
        content_digest: str,
        statuses: set[MemoryStatus],
    ) -> MemoryRecord | None:
        return deepcopy(
            next(
                (
                    value
                    for value in self._records.values()
                    if value.company_id == company_id
                    and value.namespace_type.value == namespace_type
                    and value.namespace_id == namespace_id
                    and value.memory_type is memory_type
                    and value.content_digest == content_digest
                    and value.status in statuses
                ),
                None,
            )
        )

    def list_candidates(self, company_id: UUID) -> list[MemoryRecord]:
        values = [
            value
            for value in self._records.values()
            if value.company_id == company_id
            and value.status is MemoryStatus.CANDIDATE
        ]
        values.sort(key=lambda value: (value.created_at, str(value.id)))
        return deepcopy(values)

    def search_records(
        self,
        *,
        company_id: UUID,
        namespace_keys: list[str],
        memory_types: list[MemoryType],
    ) -> list[MemoryRecord]:
        return deepcopy(
            [
                value
                for value in self._records.values()
                if value.company_id == company_id
                and namespace_key(value.namespace_type, value.namespace_id)
                in namespace_keys
                and value.memory_type in memory_types
            ]
        )

    def save_record(self, value: MemoryRecord) -> None:
        self._records[value.id] = deepcopy(value)

    def add_evidence(self, value: MemoryEvidence) -> None:
        self._evidence[
            (value.memory_id, value.evidence_type, value.evidence_id)
        ] = deepcopy(value)

    def list_evidence(self, memory_id: UUID) -> list[MemoryEvidence]:
        values = [
            value
            for value in self._evidence.values()
            if value.memory_id == memory_id
        ]
        values.sort(key=lambda value: value.created_at)
        return deepcopy(values)

    def add_review(self, value: MemoryReview) -> None:
        self._reviews[value.id] = deepcopy(value)

    def list_reviews(self, memory_id: UUID) -> list[MemoryReview]:
        values = [
            value
            for value in self._reviews.values()
            if value.memory_id == memory_id
        ]
        values.sort(key=lambda value: value.created_at)
        return deepcopy(values)

    def add_retrieval(self, value: MemoryRetrieval) -> None:
        self._retrievals[value.id] = deepcopy(value)

    def list_retrievals(
        self, *, task_id: UUID | None = None, run_id: UUID | None = None
    ) -> list[MemoryRetrieval]:
        values = [
            value
            for value in self._retrievals.values()
            if (task_id is None or value.task_id == task_id)
            and (run_id is None or value.run_id == run_id)
        ]
        values.sort(key=lambda value: value.created_at)
        return deepcopy(values)


class InMemoryFinancialGovernanceRepository:
    def __init__(
        self,
        allocations: dict[UUID, BudgetAllocation],
        entries: dict[UUID, BudgetLedgerEntry],
        evidence: dict[UUID, EconomicEvidence],
        expenses: dict[UUID, ExpenseRequest],
    ) -> None:
        self._allocations = allocations
        self._entries = entries
        self._evidence = evidence
        self._expenses = expenses

    def add_allocation(self, value: BudgetAllocation) -> None:
        self._allocations[value.id] = deepcopy(value)

    def get_allocation(
        self, allocation_id: UUID, *, for_update: bool = False
    ) -> BudgetAllocation | None:
        value = self._allocations.get(allocation_id)
        return deepcopy(value) if value else None

    def list_allocations(self, company_id: UUID) -> list[BudgetAllocation]:
        return deepcopy(
            sorted(
                (
                    value
                    for value in self._allocations.values()
                    if value.company_id == company_id
                ),
                key=lambda value: value.created_at,
            )
        )

    def save_allocation(self, value: BudgetAllocation) -> None:
        self._allocations[value.id] = deepcopy(value)

    def add_ledger_entry(self, value: BudgetLedgerEntry) -> None:
        self._entries[value.id] = deepcopy(value)

    def get_ledger_entry_by_key(
        self, allocation_id: UUID, operation_key: str
    ) -> BudgetLedgerEntry | None:
        value = next(
            (
                item
                for item in self._entries.values()
                if item.allocation_id == allocation_id
                and item.operation_key == operation_key
            ),
            None,
        )
        return deepcopy(value) if value else None

    def list_ledger_entries(self, allocation_id: UUID) -> list[BudgetLedgerEntry]:
        return deepcopy(
            sorted(
                (
                    value
                    for value in self._entries.values()
                    if value.allocation_id == allocation_id
                ),
                key=lambda value: value.created_at,
            )
        )

    def add_economic_evidence(self, value: EconomicEvidence) -> None:
        self._evidence[value.id] = deepcopy(value)

    def get_economic_evidence_by_external_ref(
        self, company_id: UUID, external_ref: str
    ) -> EconomicEvidence | None:
        value = next(
            (
                item
                for item in self._evidence.values()
                if item.company_id == company_id and item.external_ref == external_ref
            ),
            None,
        )
        return deepcopy(value) if value else None

    def list_economic_evidence(self, company_id: UUID) -> list[EconomicEvidence]:
        return deepcopy(
            sorted(
                (
                    value
                    for value in self._evidence.values()
                    if value.company_id == company_id
                ),
                key=lambda value: value.occurred_at,
            )
        )

    def add_expense_request(self, value: ExpenseRequest) -> None:
        self._expenses[value.id] = deepcopy(value)

    def get_expense_request(
        self, request_id: UUID, *, for_update: bool = False
    ) -> ExpenseRequest | None:
        value = self._expenses.get(request_id)
        return deepcopy(value) if value else None

    def save_expense_request(self, value: ExpenseRequest) -> None:
        self._expenses[value.id] = deepcopy(value)

    def list_expense_requests(self, company_id: UUID) -> list[ExpenseRequest]:
        return deepcopy(
            sorted(
                (
                    value
                    for value in self._expenses.values()
                    if value.company_id == company_id
                ),
                key=lambda value: value.created_at,
            )
        )


class InMemoryCompanyPackRepository:
    def __init__(
        self,
        packs: dict[UUID, CompanyPack],
        installations: dict[UUID, PackInstallation],
    ) -> None:
        self._packs = packs
        self._installations = installations

    def add_pack(self, value: CompanyPack) -> None:
        self._packs[value.id] = deepcopy(value)

    def get_pack(self, pack_id: UUID) -> CompanyPack | None:
        value = self._packs.get(pack_id)
        return deepcopy(value) if value else None

    def get_pack_by_key_version(self, key: str, version: str) -> CompanyPack | None:
        value = next(
            (
                item
                for item in self._packs.values()
                if item.key == key and item.version == version
            ),
            None,
        )
        return deepcopy(value) if value else None

    def list_packs(self) -> list[CompanyPack]:
        return deepcopy(
            sorted(self._packs.values(), key=lambda item: (item.key, item.version))
        )

    def save_pack(self, value: CompanyPack) -> None:
        self._packs[value.id] = deepcopy(value)

    def add_installation(self, value: PackInstallation) -> None:
        self._installations[value.id] = deepcopy(value)

    def get_installation(
        self, company_id: UUID, pack_key: str
    ) -> PackInstallation | None:
        value = next(
            (
                item
                for item in self._installations.values()
                if item.company_id == company_id and item.pack_key == pack_key
            ),
            None,
        )
        return deepcopy(value) if value else None

    def list_installations(self, company_id: UUID) -> list[PackInstallation]:
        return deepcopy(
            sorted(
                (
                    item
                    for item in self._installations.values()
                    if item.company_id == company_id
                ),
                key=lambda item: item.installed_at,
            )
        )


class InMemoryCompanyModelRepository:
    def __init__(
        self,
        companies: dict[UUID, Company],
        units: dict[UUID, OrganizationUnit],
        positions: dict[UUID, Position],
        appointments: dict[UUID, Appointment],
        relationships: dict[UUID, OrganizationRelationship],
    ) -> None:
        self._companies = companies
        self._units = units
        self._positions = positions
        self._appointments = appointments
        self._relationships = relationships

    def add_company(self, value: Company) -> None:
        self._companies[value.id] = deepcopy(value)

    def get_company(self, company_id: UUID, *, for_update: bool = False) -> Company | None:
        value = self._companies.get(company_id)
        return deepcopy(value) if value else None

    def get_active_company(self, tenant_id: str) -> Company | None:
        value = next(
            (
                item
                for item in self._companies.values()
                if item.tenant_id == tenant_id and item.status is CompanyStatus.ACTIVE
            ),
            None,
        )
        return deepcopy(value) if value else None

    def list_companies(self, tenant_id: str) -> list[Company]:
        values = [item for item in self._companies.values() if item.tenant_id == tenant_id]
        values.sort(key=lambda item: (item.created_at, str(item.id)))
        return deepcopy(values)

    def save_company(self, value: Company) -> None:
        self._companies[value.id] = deepcopy(value)

    def add_unit(self, value: OrganizationUnit) -> None:
        self._units[value.id] = deepcopy(value)

    def get_unit(self, unit_id: UUID) -> OrganizationUnit | None:
        value = self._units.get(unit_id)
        return deepcopy(value) if value else None

    def get_unit_by_key(self, company_id: UUID, key: str) -> OrganizationUnit | None:
        value = next(
            (
                item
                for item in self._units.values()
                if item.company_id == company_id and item.key == key
            ),
            None,
        )
        return deepcopy(value) if value else None

    def list_units(self, company_id: UUID) -> list[OrganizationUnit]:
        return deepcopy(
            sorted(
                (item for item in self._units.values() if item.company_id == company_id),
                key=lambda item: (item.created_at, str(item.id)),
            )
        )

    def add_position(self, value: Position) -> None:
        self._positions[value.id] = deepcopy(value)

    def get_position(self, position_id: UUID) -> Position | None:
        value = self._positions.get(position_id)
        return deepcopy(value) if value else None

    def get_position_by_key(self, company_id: UUID, key: str) -> Position | None:
        value = next(
            (
                item
                for item in self._positions.values()
                if item.company_id == company_id and item.key == key
            ),
            None,
        )
        return deepcopy(value) if value else None

    def list_positions(self, company_id: UUID) -> list[Position]:
        return deepcopy(
            sorted(
                (item for item in self._positions.values() if item.company_id == company_id),
                key=lambda item: (item.created_at, str(item.id)),
            )
        )

    def add_appointment(self, value: Appointment) -> None:
        self._appointments[value.id] = deepcopy(value)

    def get_appointment(
        self, appointment_id: UUID, *, for_update: bool = False
    ) -> Appointment | None:
        value = self._appointments.get(appointment_id)
        return deepcopy(value) if value else None

    def get_active_appointment(self, position_id: UUID) -> Appointment | None:
        value = next(
            (
                item
                for item in self._appointments.values()
                if item.position_id == position_id and item.status is AppointmentStatus.ACTIVE
            ),
            None,
        )
        return deepcopy(value) if value else None

    def list_appointments(self, company_id: UUID) -> list[Appointment]:
        return deepcopy(
            sorted(
                (
                    item
                    for item in self._appointments.values()
                    if item.company_id == company_id
                ),
                key=lambda item: (item.created_at, str(item.id)),
            )
        )

    def save_appointment(self, value: Appointment) -> None:
        self._appointments[value.id] = deepcopy(value)

    def add_relationship(self, value: OrganizationRelationship) -> None:
        self._relationships[value.id] = deepcopy(value)

    def find_active_relationship(
        self,
        *,
        company_id: UUID,
        relationship_type: str,
        source_id: UUID,
        target_id: UUID,
    ) -> OrganizationRelationship | None:
        value = next(
            (
                item
                for item in self._relationships.values()
                if item.company_id == company_id
                and item.relationship_type == relationship_type
                and item.source_id == source_id
                and item.target_id == target_id
                and item.status is ResourceStatus.ACTIVE
            ),
            None,
        )
        return deepcopy(value) if value else None

    def list_relationships(self, company_id: UUID) -> list[OrganizationRelationship]:
        return deepcopy(
            sorted(
                (
                    item
                    for item in self._relationships.values()
                    if item.company_id == company_id
                ),
                key=lambda item: (item.created_at, str(item.id)),
            )
        )


class InMemoryUnitOfWork:
    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    def __enter__(self) -> InMemoryUnitOfWork:
        self._companies = deepcopy(self._store.companies)
        self._organization_units = deepcopy(self._store.organization_units)
        self._company_positions = deepcopy(self._store.company_positions)
        self._company_appointments = deepcopy(self._store.company_appointments)
        self._organization_relationships = deepcopy(
            self._store.organization_relationships
        )
        self._operating_cycles = deepcopy(self._store.operating_cycles)
        self._company_objectives = deepcopy(self._store.company_objectives)
        self._company_key_results = deepcopy(self._store.company_key_results)
        self._company_initiatives = deepcopy(self._store.company_initiatives)
        self._initiative_task_links = deepcopy(self._store.initiative_task_links)
        self._company_operations = deepcopy(self._store.company_operations)
        self._company_operation_trigger_states = deepcopy(
            self._store.company_operation_trigger_states
        )
        self._company_operation_occurrences = deepcopy(
            self._store.company_operation_occurrences
        )
        self._company_operation_exceptions = deepcopy(
            self._store.company_operation_exceptions
        )
        self._business_object_types = deepcopy(self._store.business_object_types)
        self._business_objects = deepcopy(self._store.business_objects)
        self._business_object_revisions = deepcopy(
            self._store.business_object_revisions
        )
        self._memory_policies = deepcopy(self._store.memory_policies)
        self._memory_records = deepcopy(self._store.memory_records)
        self._memory_evidence = deepcopy(self._store.memory_evidence)
        self._memory_reviews = deepcopy(self._store.memory_reviews)
        self._memory_retrievals = deepcopy(self._store.memory_retrievals)
        self._budget_allocations = deepcopy(self._store.budget_allocations)
        self._budget_ledger_entries = deepcopy(self._store.budget_ledger_entries)
        self._economic_evidence = deepcopy(self._store.economic_evidence)
        self._expense_requests = deepcopy(self._store.expense_requests)
        self._company_packs = deepcopy(self._store.company_packs)
        self._company_pack_installations = deepcopy(
            self._store.company_pack_installations
        )
        self._tasks = deepcopy(self._store.tasks)
        self._replay_bookmarks = deepcopy(self._store.replay_bookmarks)
        self._goal_contracts = deepcopy(self._store.goal_contracts)
        self._plan_patches = deepcopy(self._store.plan_patches)
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
        self._tool_execution_authorizations = deepcopy(
            self._store.tool_execution_authorizations
        )
        self._usage_records = deepcopy(self._store.usage_records)
        self._governed_actions = deepcopy(self._store.governed_actions)
        self._approval_decisions = deepcopy(self._store.approval_decisions)
        self._principals = deepcopy(self._store.principals)
        self._external_identities = deepcopy(self._store.external_identities)
        self._role_bindings = deepcopy(self._store.role_bindings)
        self._mcp_servers = deepcopy(self._store.mcp_servers)
        self._mcp_server_versions = deepcopy(self._store.mcp_server_versions)
        self._mcp_tool_capabilities = deepcopy(self._store.mcp_tool_capabilities)
        self._mcp_discovery_snapshots = deepcopy(self._store.mcp_discovery_snapshots)
        self._a2a_peers = deepcopy(self._store.a2a_peers)
        self._a2a_card_snapshots = deepcopy(self._store.a2a_card_snapshots)
        self._remote_correlations = deepcopy(self._store.remote_correlations)
        self._secret_references = deepcopy(self._store.secret_references)
        self._credential_bindings = deepcopy(self._store.credential_bindings)
        self._credential_leases = deepcopy(self._store.credential_leases)
        self._mcp_credential_bindings = deepcopy(self._store.mcp_credential_bindings)
        self._mcp_credential_leases = deepcopy(self._store.mcp_credential_leases)
        self._quota_policies = deepcopy(self._store.quota_policies)
        self._quota_reservations = deepcopy(self._store.quota_reservations)
        self.company_model = InMemoryCompanyModelRepository(
            self._companies,
            self._organization_units,
            self._company_positions,
            self._company_appointments,
            self._organization_relationships,
        )
        self.company_goals = InMemoryCompanyGoalRepository(
            self._operating_cycles,
            self._company_objectives,
            self._company_key_results,
            self._company_initiatives,
            self._initiative_task_links,
        )
        self.company_operations = InMemoryCompanyOperationRepository(
            self._companies,
            self._company_operations,
            self._company_operation_trigger_states,
            self._company_operation_occurrences,
            self._company_operation_exceptions,
        )
        self.business_objects = InMemoryBusinessObjectRepository(
            self._business_object_types,
            self._business_objects,
            self._business_object_revisions,
        )
        self.organizational_memory = InMemoryOrganizationalMemoryRepository(
            self._memory_policies,
            self._memory_records,
            self._memory_evidence,
            self._memory_reviews,
            self._memory_retrievals,
        )
        self.financial_governance = InMemoryFinancialGovernanceRepository(
            self._budget_allocations,
            self._budget_ledger_entries,
            self._economic_evidence,
            self._expense_requests,
        )
        self.company_packs = InMemoryCompanyPackRepository(
            self._company_packs, self._company_pack_installations
        )
        self.tasks = InMemoryTaskRepository(self._tasks)
        self.replay_bookmarks = InMemoryReplayBookmarkRepository(self._replay_bookmarks)
        self.goal_contracts = InMemoryGoalContractRepository(self._goal_contracts)
        self.plan_patches = InMemoryPlanPatchRepository(self._plan_patches)
        self.task_resolutions = InMemoryTaskResolutionRepository(self._task_resolutions)
        self.subtasks = InMemorySubtaskRepository(self._subtasks)
        self.subtask_dependencies = InMemorySubtaskDependencyRepository(self._subtask_dependencies)
        self.handoffs = InMemoryHandoffRepository(self._handoffs)
        self.runs = InMemoryTaskRunRepository(self._runs, self._tasks, self._store)
        self.attempts = InMemoryTaskAttemptRepository(self._attempts, self._runs, self._store)
        self.quotas = InMemoryQuotaRepository(
            self._quota_policies, self._quota_reservations
        )
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
        self.tool_execution_authorizations = InMemoryToolExecutionAuthorizationRepository(
            self._tool_execution_authorizations
        )
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
            self._mcp_discovery_snapshots,
        )
        self.a2a_registry = InMemoryA2ARegistryRepository(
            self._a2a_peers,
            self._a2a_card_snapshots,
        )
        self.remote_correlations = InMemoryRemoteTaskCorrelationRepository(
            self._remote_correlations
        )
        self.credentials = InMemoryCredentialRepository(
            self._secret_references,
            self._credential_bindings,
            self._credential_leases,
            self._mcp_credential_bindings,
            self._mcp_credential_leases,
        )
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        if exc_type is not None:
            self.rollback()

    def commit(self) -> None:
        self._store.companies = deepcopy(self._companies)
        self._store.organization_units = deepcopy(self._organization_units)
        self._store.company_positions = deepcopy(self._company_positions)
        self._store.company_appointments = deepcopy(self._company_appointments)
        self._store.organization_relationships = deepcopy(
            self._organization_relationships
        )
        self._store.operating_cycles = deepcopy(self._operating_cycles)
        self._store.company_objectives = deepcopy(self._company_objectives)
        self._store.company_key_results = deepcopy(self._company_key_results)
        self._store.company_initiatives = deepcopy(self._company_initiatives)
        self._store.initiative_task_links = deepcopy(self._initiative_task_links)
        self._store.company_operations = deepcopy(self._company_operations)
        self._store.company_operation_trigger_states = deepcopy(
            self._company_operation_trigger_states
        )
        self._store.company_operation_occurrences = deepcopy(
            self._company_operation_occurrences
        )
        self._store.company_operation_exceptions = deepcopy(
            self._company_operation_exceptions
        )
        self._store.business_object_types = deepcopy(self._business_object_types)
        self._store.business_objects = deepcopy(self._business_objects)
        self._store.business_object_revisions = deepcopy(
            self._business_object_revisions
        )
        self._store.memory_policies = deepcopy(self._memory_policies)
        self._store.memory_records = deepcopy(self._memory_records)
        self._store.memory_evidence = deepcopy(self._memory_evidence)
        self._store.memory_reviews = deepcopy(self._memory_reviews)
        self._store.memory_retrievals = deepcopy(self._memory_retrievals)
        self._store.budget_allocations = deepcopy(self._budget_allocations)
        self._store.budget_ledger_entries = deepcopy(self._budget_ledger_entries)
        self._store.economic_evidence = deepcopy(self._economic_evidence)
        self._store.expense_requests = deepcopy(self._expense_requests)
        self._store.company_packs = deepcopy(self._company_packs)
        self._store.company_pack_installations = deepcopy(
            self._company_pack_installations
        )
        self._store.tasks = deepcopy(self._tasks)
        self._store.replay_bookmarks = deepcopy(self._replay_bookmarks)
        self._store.goal_contracts = deepcopy(self._goal_contracts)
        self._store.plan_patches = deepcopy(self._plan_patches)
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
        self._store.tool_execution_authorizations = deepcopy(
            self._tool_execution_authorizations
        )
        self._store.usage_records = deepcopy(self._usage_records)
        self._store.governed_actions = deepcopy(self._governed_actions)
        self._store.approval_decisions = deepcopy(self._approval_decisions)
        self._store.principals = deepcopy(self._principals)
        self._store.external_identities = deepcopy(self._external_identities)
        self._store.role_bindings = deepcopy(self._role_bindings)
        self._store.mcp_servers = deepcopy(self._mcp_servers)
        self._store.mcp_server_versions = deepcopy(self._mcp_server_versions)
        self._store.mcp_tool_capabilities = deepcopy(self._mcp_tool_capabilities)
        self._store.mcp_discovery_snapshots = deepcopy(self._mcp_discovery_snapshots)
        self._store.a2a_peers = deepcopy(self._a2a_peers)
        self._store.a2a_card_snapshots = deepcopy(self._a2a_card_snapshots)
        self._store.remote_correlations = deepcopy(self._remote_correlations)
        self._store.secret_references = deepcopy(self._secret_references)
        self._store.credential_bindings = deepcopy(self._credential_bindings)
        self._store.credential_leases = deepcopy(self._credential_leases)
        self._store.mcp_credential_bindings = deepcopy(self._mcp_credential_bindings)
        self._store.mcp_credential_leases = deepcopy(self._mcp_credential_leases)
        self._store.quota_policies = deepcopy(self._quota_policies)
        self._store.quota_reservations = deepcopy(self._quota_reservations)

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
        cancel_responses: list[object] | None = None,
    ) -> None:
        self.send_responses = list(send_responses or [])
        self.task_responses = list(task_responses or [])
        self.cancel_responses = list(cancel_responses or [])
        self.send_calls: list[dict[str, object]] = []
        self.task_calls: list[dict[str, object]] = []
        self.cancel_calls: list[dict[str, object]] = []

    def send_message(self, **kwargs) -> dict[str, object]:
        self.send_calls.append(kwargs)
        return self._next(self.send_responses)

    def get_task(self, **kwargs) -> dict[str, object]:
        self.task_calls.append(kwargs)
        return self._next(self.task_responses)

    def cancel_task(self, **kwargs) -> dict[str, object]:
        self.cancel_calls.append(kwargs)
        return self._next(self.cancel_responses)

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
