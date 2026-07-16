from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from agentmesh.domain.messaging import IdempotencyRecord, InboxMessage, MessageEnvelope
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


class UnitOfWork(Protocol):
    tasks: TaskRepository
    runs: TaskRunRepository
    attempts: TaskAttemptRepository
    outbox: OutboxRepository
    inbox: InboxRepository
    idempotency: IdempotencyRepository

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
