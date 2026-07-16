from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from agentmesh.domain.errors import InvalidTaskInput, InvalidTaskTransition


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TaskStatus(str, Enum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


class RunStatus(str, Enum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


TERMINAL_TASK_STATUSES = {
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.CANCELED,
}


@dataclass
class Task:
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

    @classmethod
    def create(cls, objective: str, input: dict[str, Any] | None = None) -> Task:
        normalized_objective = objective.strip()
        if not normalized_objective:
            raise InvalidTaskInput("Task objective must not be empty")

        now = utc_now()
        return cls(
            id=uuid4(),
            objective=normalized_objective,
            input=dict(input or {}),
            status=TaskStatus.CREATED,
            current_run_id=None,
            output=None,
            error=None,
            version=1,
            created_at=now,
            updated_at=now,
        )

    def start(self, run_id: UUID) -> None:
        self._require_status(TaskStatus.CREATED, "start")
        self.status = TaskStatus.RUNNING
        self.current_run_id = run_id
        self.output = None
        self.error = None
        self._touch()

    def complete(self, run_id: UUID, output: dict[str, Any]) -> None:
        self._require_active_run(run_id, "complete")
        self.status = TaskStatus.COMPLETED
        self.output = dict(output)
        self.error = None
        self._touch()

    def fail(self, run_id: UUID, error: str) -> None:
        self._require_active_run(run_id, "fail")
        normalized_error = error.strip()
        if not normalized_error:
            raise InvalidTaskInput("Task failure must include an error summary")
        self.status = TaskStatus.FAILED
        self.output = None
        self.error = normalized_error
        self._touch()

    def cancel(self) -> None:
        if self.status in TERMINAL_TASK_STATUSES:
            raise InvalidTaskTransition(
                f"Cannot cancel task {self.id} from terminal status {self.status.value}"
            )
        self.status = TaskStatus.CANCELED
        self._touch()

    def _require_status(self, expected: TaskStatus, action: str) -> None:
        if self.status != expected:
            raise InvalidTaskTransition(
                f"Cannot {action} task {self.id} from status {self.status.value}"
            )

    def _require_active_run(self, run_id: UUID, action: str) -> None:
        self._require_status(TaskStatus.RUNNING, action)
        if self.current_run_id != run_id:
            raise InvalidTaskTransition(
                f"Run {run_id} is not the active run for task {self.id}"
            )

    def _touch(self) -> None:
        self.version += 1
        self.updated_at = utc_now()


@dataclass
class TaskRun:
    id: UUID
    task_id: UUID
    thread_id: str
    agent_id: str
    status: RunStatus
    output: dict[str, Any] | None
    error: str | None
    started_at: datetime
    completed_at: datetime | None

    @classmethod
    def start(cls, task_id: UUID, agent_id: str) -> TaskRun:
        normalized_agent_id = agent_id.strip()
        if not normalized_agent_id:
            raise InvalidTaskInput("Agent ID must not be empty")
        run_id = uuid4()
        return cls(
            id=run_id,
            task_id=task_id,
            thread_id=str(run_id),
            agent_id=normalized_agent_id,
            status=RunStatus.RUNNING,
            output=None,
            error=None,
            started_at=utc_now(),
            completed_at=None,
        )

    def complete(self, output: dict[str, Any]) -> None:
        self._require_running("complete")
        self.status = RunStatus.COMPLETED
        self.output = dict(output)
        self.error = None
        self.completed_at = utc_now()

    def fail(self, error: str) -> None:
        self._require_running("fail")
        normalized_error = error.strip()
        if not normalized_error:
            raise InvalidTaskInput("Run failure must include an error summary")
        self.status = RunStatus.FAILED
        self.output = None
        self.error = normalized_error
        self.completed_at = utc_now()

    def cancel(self) -> None:
        self._require_running("cancel")
        self.status = RunStatus.CANCELED
        self.completed_at = utc_now()

    def _require_running(self, action: str) -> None:
        if self.status != RunStatus.RUNNING:
            raise InvalidTaskTransition(
                f"Cannot {action} run {self.id} from status {self.status.value}"
            )


@dataclass(frozen=True)
class TaskAggregate:
    task: Task
    runs: list[TaskRun] = field(default_factory=list)
