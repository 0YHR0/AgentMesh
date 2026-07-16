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
    READY = "READY"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


class RunStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


class AttemptStatus(str, Enum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"
    LEASE_EXPIRED = "LEASE_EXPIRED"


TERMINAL_TASK_STATUSES = {
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.CANCELED,
}
TERMINAL_RUN_STATUSES = {
    RunStatus.SUCCEEDED,
    RunStatus.FAILED,
    RunStatus.CANCELED,
}


@dataclass
class Task:
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

    @classmethod
    def create(
        cls,
        *,
        tenant_id: str,
        objective: str,
        input: dict[str, Any] | None = None,
    ) -> Task:
        normalized_tenant_id = tenant_id.strip()
        normalized_objective = objective.strip()
        if not normalized_tenant_id:
            raise InvalidTaskInput("Task tenant ID must not be empty")
        if not normalized_objective:
            raise InvalidTaskInput("Task objective must not be empty")

        now = utc_now()
        return cls(
            id=uuid4(),
            tenant_id=normalized_tenant_id,
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

    def queue(self, run_id: UUID) -> None:
        self._require_status(TaskStatus.CREATED, "queue")
        self.status = TaskStatus.READY
        self.current_run_id = run_id
        self.output = None
        self.error = None
        self._touch()

    def start(self, run_id: UUID) -> None:
        self._require_active_run(run_id, "start", expected=TaskStatus.READY)
        self.status = TaskStatus.RUNNING
        self._touch()

    def complete(self, run_id: UUID, output: dict[str, Any]) -> None:
        self._require_active_run(run_id, "complete", expected=TaskStatus.RUNNING)
        self.status = TaskStatus.COMPLETED
        self.output = dict(output)
        self.error = None
        self._touch()

    def fail(self, run_id: UUID, error: str) -> None:
        self._require_active_run(run_id, "fail", expected=TaskStatus.RUNNING)
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

    def _require_active_run(
        self,
        run_id: UUID,
        action: str,
        *,
        expected: TaskStatus,
    ) -> None:
        self._require_status(expected, action)
        if self.current_run_id != run_id:
            raise InvalidTaskTransition(f"Run {run_id} is not the active run for task {self.id}")

    def _touch(self) -> None:
        self.version += 1
        self.updated_at = utc_now()


@dataclass
class TaskRun:
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

    @classmethod
    def request(
        cls,
        task_id: UUID,
        agent_id: str,
        *,
        agent_version_id: UUID | None = None,
        agent_version_digest: str | None = None,
    ) -> TaskRun:
        normalized_agent_id = agent_id.strip()
        if not normalized_agent_id:
            raise InvalidTaskInput("Agent ID must not be empty")
        run_id = uuid4()
        return cls(
            id=run_id,
            task_id=task_id,
            thread_id=str(run_id),
            agent_id=normalized_agent_id,
            agent_version_id=agent_version_id,
            agent_version_digest=agent_version_digest,
            status=RunStatus.QUEUED,
            output=None,
            error=None,
            queued_at=utc_now(),
            started_at=None,
            completed_at=None,
        )

    def start(self) -> None:
        self._require_status(RunStatus.QUEUED, "start")
        self.status = RunStatus.RUNNING
        self.started_at = utc_now()

    def succeed(self, output: dict[str, Any]) -> None:
        self._require_status(RunStatus.RUNNING, "succeed")
        self.status = RunStatus.SUCCEEDED
        self.output = dict(output)
        self.error = None
        self.completed_at = utc_now()

    def fail(self, error: str) -> None:
        self._require_status(RunStatus.RUNNING, "fail")
        normalized_error = error.strip()
        if not normalized_error:
            raise InvalidTaskInput("Run failure must include an error summary")
        self.status = RunStatus.FAILED
        self.output = None
        self.error = normalized_error
        self.completed_at = utc_now()

    def cancel(self) -> None:
        if self.status not in {RunStatus.QUEUED, RunStatus.RUNNING}:
            raise InvalidTaskTransition(
                f"Cannot cancel run {self.id} from status {self.status.value}"
            )
        self.status = RunStatus.CANCELED
        self.completed_at = utc_now()

    def _require_status(self, expected: RunStatus, action: str) -> None:
        if self.status != expected:
            raise InvalidTaskTransition(
                f"Cannot {action} run {self.id} from status {self.status.value}"
            )


@dataclass
class TaskAttempt:
    id: UUID
    run_id: UUID
    worker_id: str
    lease_token: UUID
    fencing_token: int
    status: AttemptStatus
    lease_expires_at: datetime
    heartbeat_at: datetime
    started_at: datetime
    completed_at: datetime | None
    error: str | None

    @classmethod
    def lease(
        cls,
        *,
        run_id: UUID,
        worker_id: str,
        fencing_token: int,
        lease_expires_at: datetime,
    ) -> TaskAttempt:
        normalized_worker_id = worker_id.strip()
        if not normalized_worker_id:
            raise InvalidTaskInput("Worker ID must not be empty")
        now = utc_now()
        return cls(
            id=uuid4(),
            run_id=run_id,
            worker_id=normalized_worker_id,
            lease_token=uuid4(),
            fencing_token=fencing_token,
            status=AttemptStatus.RUNNING,
            lease_expires_at=lease_expires_at,
            heartbeat_at=now,
            started_at=now,
            completed_at=None,
            error=None,
        )

    def succeed(self) -> None:
        self._require_running("succeed")
        self.status = AttemptStatus.SUCCEEDED
        self.completed_at = utc_now()

    def fail(self, error: str) -> None:
        self._require_running("fail")
        normalized_error = error.strip()
        if not normalized_error:
            raise InvalidTaskInput("Attempt failure must include an error summary")
        self.status = AttemptStatus.FAILED
        self.error = normalized_error
        self.completed_at = utc_now()

    def cancel(self) -> None:
        self._require_running("cancel")
        self.status = AttemptStatus.CANCELED
        self.completed_at = utc_now()

    def expire(self) -> None:
        self._require_running("expire")
        self.status = AttemptStatus.LEASE_EXPIRED
        self.completed_at = utc_now()

    def _require_running(self, action: str) -> None:
        if self.status != AttemptStatus.RUNNING:
            raise InvalidTaskTransition(
                f"Cannot {action} attempt {self.id} from status {self.status.value}"
            )


@dataclass(frozen=True)
class TaskAggregate:
    task: Task
    runs: list[TaskRun] = field(default_factory=list)
    attempts: list[TaskAttempt] = field(default_factory=list)
