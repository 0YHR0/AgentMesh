from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from agentmesh.domain.observability import TaskUsage, UsageSource
from agentmesh.domain.tasks import AttemptStatus, RunStatus, TaskAggregate, TaskStatus


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


class CreateTaskRequest(BaseModel):
    objective: str = Field(min_length=1, max_length=20_000)
    input: dict[str, Any] = Field(default_factory=dict)


class TaskRunResponse(BaseModel):
    id: UUID
    task_id: UUID
    thread_id: str
    agent_id: str
    agent_version_id: UUID | None
    agent_version_digest: str | None
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


class TaskResponse(BaseModel):
    id: UUID
    tenant_id: str
    objective: str
    input: dict[str, Any]
    status: TaskStatus
    current_run_id: UUID | None
    output: dict[str, Any] | None
    error: str | None
    version: int
    created_at: datetime
    updated_at: datetime
    runs: list[TaskRunResponse]
    attempts: list[TaskAttemptResponse]

    @classmethod
    def from_aggregate(cls, aggregate: TaskAggregate) -> "TaskResponse":
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
                )
                for attempt in aggregate.attempts
            ],
        )


class TaskListResponse(BaseModel):
    items: list[TaskResponse]
    limit: int
    offset: int


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
    def from_task_usage(cls, usage: TaskUsage) -> "TaskUsageResponse":
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
