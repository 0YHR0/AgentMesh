from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, model_validator
from typing_extensions import Self

from agentmesh.domain.budgets import TaskBudget, TaskBudgetStatus
from agentmesh.domain.coordination import SubtaskSpec, SubtaskStatus
from agentmesh.domain.handoffs import Handoff, HandoffStatus
from agentmesh.domain.observability import TaskUsage, UsageSource
from agentmesh.domain.resolutions import TaskResolution, TaskResolutionAction
from agentmesh.domain.tasks import (
    AcceptanceCriterion,
    AcceptanceCriterionKind,
    AttemptStatus,
    RunRole,
    RunStatus,
    TaskAggregate,
    TaskExecutionMode,
    TaskStatus,
)


class TaskAttemptResponse(BaseModel):
    id: UUID
    run_id: UUID
    trace_id: str
    worker_id: str
    fencing_token: int
    status: AttemptStatus
    lease_expires_at: datetime
    heartbeat_at: datetime
    started_at: datetime
    completed_at: datetime | None
    error: str | None
    reserved_tokens: int
    reserved_cost_micros: int
    settled_tokens: int | None
    settled_cost_micros: int | None
    budget_settlement_source: str | None


class TaskBudgetRequest(BaseModel):
    max_runs: int | None = Field(default=None, ge=1)
    max_attempts: int | None = Field(default=None, ge=1)
    max_tokens: int | None = Field(default=None, ge=1)
    token_reservation_per_attempt: int = Field(default=0, ge=0)
    max_cost_micros: int | None = Field(default=None, ge=1)
    cost_reservation_micros_per_attempt: int = Field(default=0, ge=0)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    deadline: datetime | None = None

    def to_domain(self) -> TaskBudget:
        return TaskBudget.create(**self.model_dump())


class ResolveTaskRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=2_000)


class IncreaseBudgetAndResumeRequest(ResolveTaskRequest):
    budget: TaskBudgetRequest


class AcceptanceCriterionRequest(BaseModel):
    key: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=2_000)
    kind: AcceptanceCriterionKind
    path: list[str] = Field(min_length=1, max_length=16)
    expected: Any = None
    required: bool = True

    def to_domain(self) -> AcceptanceCriterion:
        return AcceptanceCriterion.create(
            key=self.key,
            description=self.description,
            kind=self.kind,
            path=self.path,
            expected=self.expected,
            required=self.required,
        )


class SubtaskSpecRequest(BaseModel):
    key: str = Field(min_length=1, max_length=128)
    objective: str = Field(min_length=1, max_length=20_000)
    input: dict[str, Any] = Field(default_factory=dict)
    required_capabilities: list[str] = Field(
        default_factory=lambda: ["general.task"], min_length=1, max_length=20
    )
    depends_on: list[str] = Field(default_factory=list, max_length=20)
    preferred_agent_id: str | None = Field(default=None, min_length=1, max_length=63)

    def to_domain(self) -> SubtaskSpec:
        return SubtaskSpec.create(
            key=self.key,
            objective=self.objective,
            input=self.input,
            required_capabilities=self.required_capabilities,
            depends_on=self.depends_on,
            preferred_agent_id=self.preferred_agent_id,
        )


class CreateTaskRequest(BaseModel):
    objective: str = Field(min_length=1, max_length=20_000)
    input: dict[str, Any] = Field(default_factory=dict)
    execution_mode: TaskExecutionMode = TaskExecutionMode.DIRECT
    acceptance_criteria: list[AcceptanceCriterionRequest] = Field(
        default_factory=list, max_length=20
    )
    max_revisions: int = Field(default=0, ge=0, le=10)
    review_deadline: datetime | None = None
    subtasks: list[SubtaskSpecRequest] = Field(default_factory=list, max_length=20)
    max_concurrency: int = Field(default=1, ge=1, le=10)
    budget: TaskBudgetRequest | None = None

    @model_validator(mode="after")
    def validate_execution_shape(self) -> Self:
        if self.execution_mode == TaskExecutionMode.COORDINATED:
            if not self.subtasks:
                raise ValueError("Coordinated tasks require Subtasks")
        elif self.subtasks or self.max_concurrency != 1:
            raise ValueError("Subtasks and max_concurrency require COORDINATED mode")
        return self


class TaskRunResponse(BaseModel):
    id: UUID
    task_id: UUID
    thread_id: str
    agent_id: str
    agent_version_id: UUID | None
    agent_version_digest: str | None
    role: RunRole
    revision_number: int
    subtask_id: UUID | None
    status: RunStatus
    output: dict[str, Any] | None
    error: str | None
    queued_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    pause_requested_at: datetime | None
    paused_at: datetime | None
    resumed_at: datetime | None
    paused_from_status: RunStatus | None


class SubtaskResponse(BaseModel):
    id: UUID
    task_id: UUID
    key: str
    objective: str
    input: dict[str, Any]
    required_capabilities: list[str]
    preferred_agent_id: str | None
    depends_on: list[str]
    status: SubtaskStatus
    current_run_id: UUID | None
    output: dict[str, Any] | None
    error: str | None
    version: int
    created_at: datetime
    updated_at: datetime


class RequestHandoffRequest(BaseModel):
    source_subtask_id: UUID
    target_subtask_id: UUID
    target_agent_id: str = Field(min_length=3, max_length=63)
    objective: str = Field(min_length=1, max_length=20_000)
    reason: str = Field(min_length=1, max_length=2_000)
    completed_work_summary: str = Field(min_length=1, max_length=20_000)
    requested_by: str = Field(min_length=1, max_length=128)
    unresolved_questions: list[str] = Field(default_factory=list, max_length=20)
    constraints: dict[str, Any] = Field(default_factory=dict)
    acceptance_criteria: list[dict[str, Any]] = Field(default_factory=list, max_length=20)


class DecideHandoffRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=128)
    reason: str | None = Field(default=None, max_length=2_000)


class HandoffResponse(BaseModel):
    id: UUID
    task_id: UUID
    source_subtask_id: UUID
    source_run_id: UUID
    source_trace_id: str
    causation_id: UUID
    source_agent_id: str
    target_subtask_id: UUID
    target_agent_id: str
    objective: str
    reason: str
    completed_work_summary: str
    unresolved_questions: list[str]
    constraints: dict[str, Any]
    acceptance_criteria: list[dict[str, Any]]
    status: HandoffStatus
    requested_by: str
    requested_at: datetime
    decided_by: str | None
    decision_reason: str | None
    decided_at: datetime | None
    version: int

    @classmethod
    def from_domain(cls, handoff: Handoff) -> HandoffResponse:
        return cls(
            id=handoff.id,
            task_id=handoff.task_id,
            source_subtask_id=handoff.source_subtask_id,
            source_run_id=handoff.source_run_id,
            source_trace_id=handoff.source_trace_id,
            causation_id=handoff.causation_id,
            source_agent_id=handoff.source_agent_id,
            target_subtask_id=handoff.target_subtask_id,
            target_agent_id=handoff.target_agent_id,
            objective=handoff.objective,
            reason=handoff.reason,
            completed_work_summary=handoff.completed_work_summary,
            unresolved_questions=list(handoff.unresolved_questions),
            constraints=dict(handoff.constraints),
            acceptance_criteria=[dict(value) for value in handoff.acceptance_criteria],
            status=handoff.status,
            requested_by=handoff.requested_by,
            requested_at=handoff.requested_at,
            decided_by=handoff.decided_by,
            decision_reason=handoff.decision_reason,
            decided_at=handoff.decided_at,
            version=handoff.version,
        )


class TaskResponse(BaseModel):
    id: UUID
    tenant_id: str
    objective: str
    input: dict[str, Any]
    status: TaskStatus
    current_run_id: UUID | None
    output: dict[str, Any] | None
    error: str | None
    execution_mode: TaskExecutionMode
    acceptance_criteria: list[dict[str, Any]]
    max_revisions: int
    revision_count: int
    review_deadline: datetime | None
    candidate_output: dict[str, Any] | None
    latest_review: dict[str, Any] | None
    plan_version: int | None
    plan_digest: str | None
    max_concurrency: int
    budget: dict[str, Any] | None
    settled_tokens: int
    reserved_tokens: int
    settled_cost_micros: int
    reserved_cost_micros: int
    budget_exhausted_reason: str | None
    budget_revision: int
    version: int
    created_at: datetime
    updated_at: datetime
    runs: list[TaskRunResponse]
    attempts: list[TaskAttemptResponse]
    subtasks: list[SubtaskResponse]
    handoffs: list[HandoffResponse]

    @classmethod
    def from_aggregate(cls, aggregate: TaskAggregate) -> TaskResponse:
        task = aggregate.task
        return cls(
            id=task.id,
            tenant_id=task.tenant_id,
            objective=task.objective,
            input=dict(task.input),
            status=task.status,
            current_run_id=task.current_run_id,
            output=dict(task.output) if task.output is not None else None,
            error=task.error,
            execution_mode=task.execution_mode,
            acceptance_criteria=[criterion.to_dict() for criterion in task.acceptance_criteria],
            max_revisions=task.max_revisions,
            revision_count=task.revision_count,
            review_deadline=task.review_deadline,
            candidate_output=(
                dict(task.candidate_output) if task.candidate_output is not None else None
            ),
            latest_review=(dict(task.latest_review) if task.latest_review is not None else None),
            plan_version=task.plan_version,
            plan_digest=task.plan_digest,
            max_concurrency=task.max_concurrency,
            budget=task.budget.to_dict() if task.budget is not None else None,
            settled_tokens=task.settled_tokens,
            reserved_tokens=task.reserved_tokens,
            settled_cost_micros=task.settled_cost_micros,
            reserved_cost_micros=task.reserved_cost_micros,
            budget_exhausted_reason=task.budget_exhausted_reason,
            budget_revision=task.budget_revision,
            version=task.version,
            created_at=task.created_at,
            updated_at=task.updated_at,
            runs=[
                TaskRunResponse(
                    id=run.id,
                    task_id=run.task_id,
                    thread_id=run.thread_id,
                    agent_id=run.agent_id,
                    agent_version_id=run.agent_version_id,
                    agent_version_digest=run.agent_version_digest,
                    role=run.role,
                    revision_number=run.revision_number,
                    subtask_id=run.subtask_id,
                    status=run.status,
                    output=dict(run.output) if run.output is not None else None,
                    error=run.error,
                    queued_at=run.queued_at,
                    started_at=run.started_at,
                    completed_at=run.completed_at,
                    pause_requested_at=run.pause_requested_at,
                    paused_at=run.paused_at,
                    resumed_at=run.resumed_at,
                    paused_from_status=run.paused_from_status,
                )
                for run in aggregate.runs
            ],
            attempts=[
                TaskAttemptResponse(
                    id=attempt.id,
                    run_id=attempt.run_id,
                    trace_id=attempt.trace_id,
                    worker_id=attempt.worker_id,
                    fencing_token=attempt.fencing_token,
                    status=attempt.status,
                    lease_expires_at=attempt.lease_expires_at,
                    heartbeat_at=attempt.heartbeat_at,
                    started_at=attempt.started_at,
                    completed_at=attempt.completed_at,
                    error=attempt.error,
                    reserved_tokens=attempt.reserved_tokens,
                    reserved_cost_micros=attempt.reserved_cost_micros,
                    settled_tokens=attempt.settled_tokens,
                    settled_cost_micros=attempt.settled_cost_micros,
                    budget_settlement_source=(
                        attempt.budget_settlement_source.value
                        if attempt.budget_settlement_source is not None
                        else None
                    ),
                )
                for attempt in aggregate.attempts
            ],
            subtasks=cls._subtask_responses(aggregate),
            handoffs=[HandoffResponse.from_domain(value) for value in aggregate.handoffs],
        )

    @staticmethod
    def _subtask_responses(aggregate: TaskAggregate) -> list[SubtaskResponse]:
        key_by_id = {subtask.id: subtask.key for subtask in aggregate.subtasks}
        predecessors: dict[UUID, list[str]] = {subtask.id: [] for subtask in aggregate.subtasks}
        for dependency in aggregate.dependencies:
            predecessors[dependency.successor_id].append(key_by_id[dependency.predecessor_id])
        return [
            SubtaskResponse(
                id=subtask.id,
                task_id=subtask.task_id,
                key=subtask.key,
                objective=subtask.objective,
                input=dict(subtask.input),
                required_capabilities=list(subtask.required_capabilities),
                preferred_agent_id=subtask.preferred_agent_id,
                depends_on=sorted(predecessors[subtask.id]),
                status=subtask.status,
                current_run_id=subtask.current_run_id,
                output=dict(subtask.output) if subtask.output is not None else None,
                error=subtask.error,
                version=subtask.version,
                created_at=subtask.created_at,
                updated_at=subtask.updated_at,
            )
            for subtask in aggregate.subtasks
        ]


class TaskListResponse(BaseModel):
    items: list[TaskResponse]
    limit: int
    offset: int


class TaskBudgetStatusResponse(BaseModel):
    task_id: UUID
    policy: dict[str, Any]
    run_count: int
    attempt_count: int
    settled_tokens: int
    reserved_tokens: int
    settled_cost_micros: int
    reserved_cost_micros: int
    exhausted_reason: str | None

    @classmethod
    def from_domain(cls, status: TaskBudgetStatus) -> TaskBudgetStatusResponse:
        return cls(
            task_id=status.task_id,
            policy=status.policy.to_dict(),
            run_count=status.run_count,
            attempt_count=status.attempt_count,
            settled_tokens=status.settled_tokens,
            reserved_tokens=status.reserved_tokens,
            settled_cost_micros=status.settled_cost_micros,
            reserved_cost_micros=status.reserved_cost_micros,
            exhausted_reason=status.exhausted_reason,
        )


class TaskResolutionResponse(BaseModel):
    id: UUID
    task_id: UUID
    action: TaskResolutionAction
    actor: str
    reason: str
    previous_status: TaskStatus
    resulting_status: TaskStatus
    previous_error: str | None
    details: dict[str, Any]
    created_at: datetime

    @classmethod
    def from_domain(cls, value: TaskResolution) -> TaskResolutionResponse:
        return cls(
            id=value.id,
            task_id=value.task_id,
            action=value.action,
            actor=value.actor,
            reason=value.reason,
            previous_status=value.previous_status,
            resulting_status=value.resulting_status,
            previous_error=value.previous_error,
            details=dict(value.details),
            created_at=value.created_at,
        )


class TaskResolutionResultResponse(BaseModel):
    resolution: TaskResolutionResponse
    task: TaskResponse


class TaskResolutionListResponse(BaseModel):
    items: list[TaskResolutionResponse]


class UsageRecordResponse(BaseModel):
    id: UUID
    task_id: UUID
    run_id: UUID
    attempt_id: UUID
    trace_id: str
    provider: str
    model: str
    source: UsageSource
    usage_details: dict[str, int]
    cost_details_micros: dict[str, int]
    currency: str
    pricing_version: str | None
    recorded_at: datetime


class TaskUsageResponse(BaseModel):
    task_id: UUID
    usage_details: dict[str, int]
    cost_details_micros_by_currency: dict[str, dict[str, int]]
    records: list[UsageRecordResponse]

    @classmethod
    def from_task_usage(cls, usage: TaskUsage) -> TaskUsageResponse:
        return cls(
            task_id=usage.task_id,
            usage_details=dict(usage.usage_details),
            cost_details_micros_by_currency={
                currency: dict(details)
                for currency, details in usage.cost_details_micros_by_currency.items()
            },
            records=[
                UsageRecordResponse(
                    id=record.id,
                    task_id=record.task_id,
                    run_id=record.run_id,
                    attempt_id=record.attempt_id,
                    trace_id=record.trace_id,
                    provider=record.provider,
                    model=record.model,
                    source=record.source,
                    usage_details=dict(record.usage_details),
                    cost_details_micros=dict(record.cost_details_micros),
                    currency=record.currency,
                    pricing_version=record.pricing_version,
                    recorded_at=record.recorded_at,
                )
                for record in usage.records
            ],
        )


class ErrorResponse(BaseModel):
    code: str
    message: str
