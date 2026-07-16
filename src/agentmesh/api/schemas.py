from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from agentmesh.domain.tasks import AttemptStatus, RunStatus, TaskAggregate, TaskStatus


class TaskAttemptResponse(BaseModel):
    id: UUID
    run_id: UUID
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
    status: RunStatus
    output: dict[str, Any] | None
    error: str | None
    queued_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


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
                    status=run.status,
                    output=dict(run.output) if run.output is not None else None,
                    error=run.error,
                    queued_at=run.queued_at,
                    started_at=run.started_at,
                    completed_at=run.completed_at,
                )
                for run in aggregate.runs
            ],
            attempts=[
                TaskAttemptResponse(
                    id=attempt.id,
                    run_id=attempt.run_id,
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


class ErrorResponse(BaseModel):
    code: str
    message: str
