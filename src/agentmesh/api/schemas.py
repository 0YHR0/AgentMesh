from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from agentmesh.domain.tasks import RunStatus, TaskAggregate, TaskStatus


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
    started_at: datetime
    completed_at: datetime | None


class TaskResponse(BaseModel):
    id: UUID
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

    @classmethod
    def from_aggregate(cls, aggregate: TaskAggregate) -> "TaskResponse":
        task = aggregate.task
        return cls(
            id=task.id,
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
                    started_at=run.started_at,
                    completed_at=run.completed_at,
                )
                for run in aggregate.runs
            ],
        )


class TaskListResponse(BaseModel):
    items: list[TaskResponse]
    limit: int
    offset: int


class ErrorResponse(BaseModel):
    code: str
    message: str
