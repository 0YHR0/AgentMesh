from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from agentmesh.domain.a2a_delegation import RemoteTaskCorrelation
from agentmesh.domain.a2a_registry import A2APeer, AgentCardSnapshot
from agentmesh.domain.activity import ReplayBookmark
from agentmesh.domain.artifacts import Artifact, ArtifactVersion
from agentmesh.domain.business_objects import (
    BusinessObject,
    BusinessObjectRevision,
    BusinessObjectType,
)
from agentmesh.domain.company import (
    Appointment,
    Company,
    OrganizationRelationship,
    OrganizationUnit,
    Position,
)
from agentmesh.domain.company_goals import (
    CompanyObjective,
    Initiative,
    InitiativeTaskLink,
    KeyResult,
    OperatingCycle,
)
from agentmesh.domain.company_operations import (
    CompanyOperation,
    OccurrenceStatus,
    OperationException,
    OperationOccurrence,
    OperationTriggerState,
)
from agentmesh.domain.coordination import Subtask, SubtaskDependency
from agentmesh.domain.credentials import (
    CredentialBinding,
    CredentialLease,
    CredentialMaterial,
    McpCredentialBinding,
    McpCredentialLease,
    SecretReference,
)
from agentmesh.domain.financial_governance import (
    BudgetAllocation,
    BudgetLedgerEntry,
    EconomicEvidence,
    ExpenseRequest,
)
from agentmesh.domain.handoffs import Handoff, HandoffStatus
from agentmesh.domain.identity import ExternalIdentity, Principal, RoleBinding
from agentmesh.domain.mcp_registry import (
    McpCapabilityDiscovery,
    McpDiscoverySnapshot,
    McpServer,
    McpServerVersion,
    McpToolCapability,
)
from agentmesh.domain.messaging import IdempotencyRecord, InboxMessage, MessageEnvelope
from agentmesh.domain.observability import UsageRecord, UsageSource
from agentmesh.domain.organizational_memory import (
    MemoryEvidence,
    MemoryPolicy,
    MemoryRecord,
    MemoryRetrieval,
    MemoryReview,
    MemoryStatus,
    MemoryType,
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
from agentmesh.domain.tasks import Task, TaskAttempt, TaskRun, TaskStatus
from agentmesh.domain.tools import (
    ToolBinding,
    ToolCallResult,
    ToolExecutionAuthorization,
    ToolInvocation,
)


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


class CompanyModelRepository(Protocol):
    def add_company(self, company: Company) -> None: ...

    def get_company(self, company_id: UUID, *, for_update: bool = False) -> Company | None: ...

    def get_active_company(self, tenant_id: str) -> Company | None: ...

    def list_companies(self, tenant_id: str) -> list[Company]: ...

    def save_company(self, company: Company) -> None: ...

    def add_unit(self, unit: OrganizationUnit) -> None: ...

    def get_unit(self, unit_id: UUID) -> OrganizationUnit | None: ...

    def get_unit_by_key(self, company_id: UUID, key: str) -> OrganizationUnit | None: ...

    def list_units(self, company_id: UUID) -> list[OrganizationUnit]: ...

    def add_position(self, position: Position) -> None: ...

    def get_position(self, position_id: UUID) -> Position | None: ...

    def get_position_by_key(self, company_id: UUID, key: str) -> Position | None: ...

    def list_positions(self, company_id: UUID) -> list[Position]: ...

    def add_appointment(self, appointment: Appointment) -> None: ...

    def get_appointment(
        self, appointment_id: UUID, *, for_update: bool = False
    ) -> Appointment | None: ...

    def get_active_appointment(self, position_id: UUID) -> Appointment | None: ...

    def list_appointments(self, company_id: UUID) -> list[Appointment]: ...

    def save_appointment(self, appointment: Appointment) -> None: ...

    def add_relationship(self, relationship: OrganizationRelationship) -> None: ...

    def find_active_relationship(
        self,
        *,
        company_id: UUID,
        relationship_type: str,
        source_id: UUID,
        target_id: UUID,
    ) -> OrganizationRelationship | None: ...

    def list_relationships(self, company_id: UUID) -> list[OrganizationRelationship]: ...


class CompanyGoalRepository(Protocol):
    def add_cycle(self, value: OperatingCycle) -> None: ...

    def get_cycle(
        self, cycle_id: UUID, *, for_update: bool = False
    ) -> OperatingCycle | None: ...

    def get_active_cycle(self, company_id: UUID) -> OperatingCycle | None: ...

    def list_cycles(self, company_id: UUID) -> list[OperatingCycle]: ...

    def save_cycle(self, value: OperatingCycle) -> None: ...

    def add_objective(self, value: CompanyObjective) -> None: ...

    def get_objective(
        self, objective_id: UUID, *, for_update: bool = False
    ) -> CompanyObjective | None: ...

    def list_objectives(self, cycle_id: UUID) -> list[CompanyObjective]: ...

    def save_objective(self, value: CompanyObjective) -> None: ...

    def add_key_result(self, value: KeyResult) -> None: ...

    def get_key_result(
        self, key_result_id: UUID, *, for_update: bool = False
    ) -> KeyResult | None: ...

    def list_key_results(self, objective_id: UUID) -> list[KeyResult]: ...

    def save_key_result(self, value: KeyResult) -> None: ...

    def add_initiative(self, value: Initiative) -> None: ...

    def get_initiative(
        self, initiative_id: UUID, *, for_update: bool = False
    ) -> Initiative | None: ...

    def list_initiatives(self, objective_id: UUID) -> list[Initiative]: ...

    def save_initiative(self, value: Initiative) -> None: ...

    def add_task_link(self, value: InitiativeTaskLink) -> None: ...

    def list_task_links(self, initiative_id: UUID) -> list[InitiativeTaskLink]: ...


class CompanyOperationRepository(Protocol):
    def add_operation(self, value: CompanyOperation) -> None: ...

    def get_operation(
        self, operation_id: UUID, *, for_update: bool = False
    ) -> CompanyOperation | None: ...

    def get_operation_by_key(
        self, company_id: UUID, key: str
    ) -> CompanyOperation | None: ...

    def list_operations(self, company_id: UUID) -> list[CompanyOperation]: ...

    def save_operation(self, value: CompanyOperation) -> None: ...

    def add_trigger_state(self, value: OperationTriggerState) -> None: ...

    def get_trigger_state(
        self, operation_id: UUID, *, for_update: bool = False
    ) -> OperationTriggerState | None: ...

    def list_due(
        self, now: datetime, *, tenant_id: str, limit: int
    ) -> list[tuple[CompanyOperation, OperationTriggerState]]: ...

    def save_trigger_state(self, value: OperationTriggerState) -> None: ...

    def add_occurrence(self, value: OperationOccurrence) -> None: ...

    def get_occurrence_by_key(
        self, operation_id: UUID, occurrence_key: str
    ) -> OperationOccurrence | None: ...

    def list_occurrences(
        self, operation_id: UUID, *, limit: int = 100
    ) -> list[OperationOccurrence]: ...

    def count_occurrences(
        self,
        operation_id: UUID,
        *,
        since: datetime,
        statuses: set[OccurrenceStatus],
    ) -> int: ...

    def save_occurrence(self, value: OperationOccurrence) -> None: ...

    def add_exception(self, value: OperationException) -> None: ...

    def list_exceptions(
        self, operation_id: UUID, *, unresolved_only: bool = False
    ) -> list[OperationException]: ...

    def list_retryable(
        self, now: datetime, *, tenant_id: str, limit: int
    ) -> list[tuple[CompanyOperation, OperationOccurrence, OperationException]]: ...

    def save_exception(self, value: OperationException) -> None: ...


class BusinessObjectRepository(Protocol):
    def add_type(self, value: BusinessObjectType) -> None: ...

    def get_type(
        self, type_id: UUID, *, for_update: bool = False
    ) -> BusinessObjectType | None: ...

    def get_type_by_key(
        self,
        company_id: UUID,
        key: str,
        *,
        schema_version: int | None = None,
        published_only: bool = False,
    ) -> BusinessObjectType | None: ...

    def list_types(self, company_id: UUID) -> list[BusinessObjectType]: ...

    def save_type(self, value: BusinessObjectType) -> None: ...

    def add_object(self, value: BusinessObject) -> None: ...

    def get_object(
        self, object_id: UUID, *, for_update: bool = False
    ) -> BusinessObject | None: ...

    def get_object_by_external_ref(
        self, type_id: UUID, external_ref: str
    ) -> BusinessObject | None: ...

    def list_objects(
        self, company_id: UUID, *, type_id: UUID | None, limit: int, offset: int
    ) -> list[BusinessObject]: ...

    def save_object(self, value: BusinessObject) -> None: ...

    def add_revision(self, value: BusinessObjectRevision) -> None: ...

    def get_revision(
        self, object_id: UUID, revision: int
    ) -> BusinessObjectRevision | None: ...

    def list_revisions(self, object_id: UUID) -> list[BusinessObjectRevision]: ...


class OrganizationalMemoryRepository(Protocol):
    def add_policy(self, value: MemoryPolicy) -> None: ...

    def get_policy(self, policy_id: UUID) -> MemoryPolicy | None: ...

    def get_policy_by_key(
        self, company_id: UUID, key: str, *, active_only: bool = False
    ) -> MemoryPolicy | None: ...

    def list_policies(self, company_id: UUID) -> list[MemoryPolicy]: ...

    def save_policy(self, value: MemoryPolicy) -> None: ...

    def add_record(self, value: MemoryRecord) -> None: ...

    def get_record(
        self, memory_id: UUID, *, for_update: bool = False
    ) -> MemoryRecord | None: ...

    def find_by_digest(
        self,
        *,
        company_id: UUID,
        namespace_type: str,
        namespace_id: str,
        memory_type: MemoryType,
        content_digest: str,
        statuses: set[MemoryStatus],
    ) -> MemoryRecord | None: ...

    def list_candidates(self, company_id: UUID) -> list[MemoryRecord]: ...

    def search_records(
        self,
        *,
        company_id: UUID,
        namespace_keys: list[str],
        memory_types: list[MemoryType],
    ) -> list[MemoryRecord]: ...

    def save_record(self, value: MemoryRecord) -> None: ...

    def add_evidence(self, value: MemoryEvidence) -> None: ...

    def list_evidence(self, memory_id: UUID) -> list[MemoryEvidence]: ...

    def add_review(self, value: MemoryReview) -> None: ...

    def list_reviews(self, memory_id: UUID) -> list[MemoryReview]: ...

    def add_retrieval(self, value: MemoryRetrieval) -> None: ...

    def list_retrievals(
        self, *, task_id: UUID | None = None, run_id: UUID | None = None
    ) -> list[MemoryRetrieval]: ...


class FinancialGovernanceRepository(Protocol):
    def add_allocation(self, value: BudgetAllocation) -> None: ...

    def get_allocation(
        self, allocation_id: UUID, *, for_update: bool = False
    ) -> BudgetAllocation | None: ...

    def list_allocations(self, company_id: UUID) -> list[BudgetAllocation]: ...

    def save_allocation(self, value: BudgetAllocation) -> None: ...

    def add_ledger_entry(self, value: BudgetLedgerEntry) -> None: ...

    def get_ledger_entry_by_key(
        self, allocation_id: UUID, operation_key: str
    ) -> BudgetLedgerEntry | None: ...

    def list_ledger_entries(self, allocation_id: UUID) -> list[BudgetLedgerEntry]: ...

    def add_economic_evidence(self, value: EconomicEvidence) -> None: ...

    def get_economic_evidence_by_external_ref(
        self, company_id: UUID, external_ref: str
    ) -> EconomicEvidence | None: ...

    def list_economic_evidence(self, company_id: UUID) -> list[EconomicEvidence]: ...

    def add_expense_request(self, value: ExpenseRequest) -> None: ...

    def get_expense_request(
        self, request_id: UUID, *, for_update: bool = False
    ) -> ExpenseRequest | None: ...

    def save_expense_request(self, value: ExpenseRequest) -> None: ...

    def list_expense_requests(self, company_id: UUID) -> list[ExpenseRequest]: ...

class ReplayBookmarkRepository(Protocol):
    def add(self, bookmark: ReplayBookmark) -> None: ...

    def get(self, bookmark_id: UUID) -> ReplayBookmark | None: ...

    def find_for_event(
        self, *, tenant_id: str, task_id: UUID, event_id: str
    ) -> ReplayBookmark | None: ...

    def list_for_task(self, *, tenant_id: str, task_id: UUID) -> list[ReplayBookmark]: ...

    def delete(self, bookmark_id: UUID) -> None: ...


class ArtifactBlobStore(Protocol):
    def put(self, *, digest: str, content: bytes) -> str: ...

    def get(self, storage_key: str) -> bytes: ...


class GoalContractRepository(Protocol):
    def add(self, goal: GoalContract) -> None: ...

    def get(self, task_id: UUID, *, for_update: bool = False) -> GoalContract | None: ...


class PlanPatchRepository(Protocol):
    def add(self, patch: PlanPatch) -> None: ...

    def get(self, patch_id: UUID, *, for_update: bool = False) -> PlanPatch | None: ...

    def save(self, patch: PlanPatch) -> None: ...

    def list_for_task(self, task_id: UUID) -> list[PlanPatch]: ...


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

    def delete_for_task(self, task_id: UUID) -> None: ...

    def delete_ids(self, task_id: UUID, subtask_ids: list[UUID]) -> None: ...


class SubtaskDependencyRepository(Protocol):
    def add(self, dependency: SubtaskDependency) -> None: ...

    def list_for_task(self, task_id: UUID) -> list[SubtaskDependency]: ...

    def list_for_tasks(self, task_ids: list[UUID]) -> list[SubtaskDependency]: ...

    def delete_for_task(self, task_id: UUID) -> None: ...


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


class QuotaRepository(Protocol):
    def add_policy(self, policy: QuotaPolicy) -> None: ...
    def replace_active(self, policy: QuotaPolicy) -> None: ...
    def get_active(
        self,
        tenant_id: str,
        scope: QuotaScope,
        project_id: str | None,
        *,
        for_update: bool = False,
    ) -> QuotaPolicy | None: ...
    def list_active_for_task(
        self, tenant_id: str, project_id: str, *, for_update: bool = False
    ) -> list[QuotaPolicy]: ...
    def list_active(self, tenant_id: str) -> list[QuotaPolicy]: ...
    def next_version(self, tenant_id: str, scope: QuotaScope, project_id: str | None) -> int: ...
    def count_active(self, policy_id: UUID) -> int: ...

    def count_active_for_scope(
        self, tenant_id: str, scope: QuotaScope, project_id: str | None
    ) -> int: ...
    def add_reservation(self, reservation: QuotaReservation) -> None: ...
    def list_reservations_for_attempt(
        self, attempt_id: UUID, *, for_update: bool = False
    ) -> list[QuotaReservation]: ...
    def save_reservation(self, reservation: QuotaReservation) -> None: ...


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

    def list_for_producer_runs(self, run_ids: list[UUID]) -> list[ArtifactVersion]: ...


class ToolInvocationRepository(Protocol):
    def add(self, invocation: ToolInvocation) -> None: ...

    def get(self, invocation_id: UUID, *, for_update: bool = False) -> ToolInvocation | None: ...

    def save(self, invocation: ToolInvocation) -> None: ...

    def list_for_task(self, task_id: UUID) -> list[ToolInvocation]: ...


class ToolExecutionAuthorizationRepository(Protocol):
    def add(self, authorization: ToolExecutionAuthorization) -> None: ...

    def get_for_task(
        self, task_id: UUID, *, for_update: bool = False
    ) -> ToolExecutionAuthorization | None: ...

    def save(self, authorization: ToolExecutionAuthorization) -> None: ...


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

    def add_discovery_snapshot(self, snapshot: McpDiscoverySnapshot) -> None: ...

    def get_discovery_snapshot(self, snapshot_id: UUID) -> McpDiscoverySnapshot | None: ...

    def latest_discovery_snapshot(self, server_version_id: UUID) -> McpDiscoverySnapshot | None: ...

    def list_discovery_snapshots(
        self, server_version_id: UUID, *, limit: int, offset: int
    ) -> list[McpDiscoverySnapshot]: ...


class McpDiscoveryGateway(Protocol):
    def discover(
        self,
        *,
        endpoint_reference: str,
        expected_server_name: str,
        expected_protocol_version: str,
    ) -> McpCapabilityDiscovery: ...


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

    def claim_due(
        self,
        *,
        tenant_id: str,
        now: datetime,
        owner: str,
        lease_expires_at: datetime,
        limit: int,
    ) -> list[RemoteTaskCorrelation]: ...


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

    def list_actions_for_resource(
        self, *, tenant_id: str, resource_type: str, resource_id: UUID
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


class CredentialRepository(Protocol):
    def add_secret_reference(self, reference: SecretReference) -> None: ...

    def get_secret_reference(
        self, reference_id: UUID, *, for_update: bool = False
    ) -> SecretReference | None: ...

    def save_secret_reference(self, reference: SecretReference) -> None: ...

    def list_secret_references(
        self, *, tenant_id: str, limit: int, offset: int
    ) -> list[SecretReference]: ...

    def add_binding(self, binding: CredentialBinding) -> None: ...

    def get_binding(
        self, binding_id: UUID, *, for_update: bool = False
    ) -> CredentialBinding | None: ...

    def save_binding(self, binding: CredentialBinding) -> None: ...

    def list_bindings(
        self, *, tenant_id: str, limit: int, offset: int
    ) -> list[CredentialBinding]: ...

    def add_lease(self, lease: CredentialLease) -> None: ...

    def get_lease(self, lease_id: UUID, *, for_update: bool = False) -> CredentialLease | None: ...

    def save_lease(self, lease: CredentialLease) -> None: ...

    def list_leases(self, *, tenant_id: str, limit: int, offset: int) -> list[CredentialLease]: ...

    def add_mcp_binding(self, binding: McpCredentialBinding) -> None: ...

    def get_mcp_binding(
        self, binding_id: UUID, *, for_update: bool = False
    ) -> McpCredentialBinding | None: ...

    def find_mcp_binding(
        self,
        *,
        tenant_id: str,
        workload_principal_id: UUID,
        server_version_id: UUID,
        environment: str,
        for_update: bool = False,
    ) -> McpCredentialBinding | None: ...

    def save_mcp_binding(self, binding: McpCredentialBinding) -> None: ...

    def list_mcp_bindings(
        self, *, tenant_id: str, limit: int, offset: int
    ) -> list[McpCredentialBinding]: ...

    def add_mcp_lease(self, lease: McpCredentialLease) -> None: ...

    def get_mcp_lease(
        self, lease_id: UUID, *, for_update: bool = False
    ) -> McpCredentialLease | None: ...

    def save_mcp_lease(self, lease: McpCredentialLease) -> None: ...

    def list_mcp_leases(
        self, *, tenant_id: str, limit: int, offset: int
    ) -> list[McpCredentialLease]: ...


class SecretValueProvider(Protocol):
    def resolve(self, reference: SecretReference) -> str: ...


class UnitOfWork(Protocol):
    company_model: CompanyModelRepository
    company_goals: CompanyGoalRepository
    company_operations: CompanyOperationRepository
    business_objects: BusinessObjectRepository
    organizational_memory: OrganizationalMemoryRepository
    financial_governance: FinancialGovernanceRepository
    tasks: TaskRepository
    replay_bookmarks: ReplayBookmarkRepository
    goal_contracts: GoalContractRepository
    plan_patches: PlanPatchRepository
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
    tool_execution_authorizations: ToolExecutionAuthorizationRepository
    mcp_registry: McpRegistryRepository
    a2a_registry: A2ARegistryRepository
    remote_correlations: RemoteTaskCorrelationRepository
    usage_records: UsageRecordRepository
    policy: PolicyRepository
    identity: IdentityRepository
    credentials: CredentialRepository

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
        task_id: UUID,
        run_id: UUID,
        binding: ToolBinding,
        arguments: dict[str, Any],
    ) -> ToolCallResult: ...


class ToolCatalog(Protocol):
    def resolve(self, logical_key: str) -> ToolBinding: ...


@dataclass(frozen=True)
class AgentCardFetchResult:
    card: dict[str, Any] | None
    source_etag: str | None
    cache_max_age_seconds: int | None
    not_modified: bool = False


class AgentCardDiscoveryClient(Protocol):
    def fetch_agent_card(
        self, *, discovery_url: str, source_etag: str | None = None
    ) -> AgentCardFetchResult: ...


class A2AProtocolClient(Protocol):
    def send_message(
        self,
        *,
        endpoint_url: str,
        protocol_version: str,
        endpoint_tenant: str | None,
        message: dict[str, Any],
        accepted_output_modes: tuple[str, ...],
        credential: CredentialMaterial | None = None,
    ) -> dict[str, Any]: ...

    def get_task(
        self,
        *,
        endpoint_url: str,
        protocol_version: str,
        endpoint_tenant: str | None,
        remote_task_id: str,
        credential: CredentialMaterial | None = None,
    ) -> dict[str, Any]: ...

    def cancel_task(
        self,
        *,
        endpoint_url: str,
        protocol_version: str,
        endpoint_tenant: str | None,
        remote_task_id: str,
        metadata: dict[str, Any],
        credential: CredentialMaterial | None = None,
    ) -> dict[str, Any]: ...


class ReadinessProbe(Protocol):
    def is_ready(self) -> bool: ...
