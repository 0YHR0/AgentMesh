from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID

from agentmesh.domain.messaging import IdempotencyRecord, InboxMessage, MessageEnvelope
from agentmesh.domain.tasks import Task, TaskAttempt, TaskRun, TaskStatus


@dataclass
class InMemoryStore:
    tasks: dict[UUID, Task] = field(default_factory=dict)
    runs: dict[UUID, TaskRun] = field(default_factory=dict)
    attempts: dict[UUID, TaskAttempt] = field(default_factory=dict)
    outbox: list[MessageEnvelope] = field(default_factory=list)
    inbox: dict[tuple[str, UUID], InboxMessage] = field(default_factory=dict)
    idempotency: dict[tuple[str, str], IdempotencyRecord] = field(default_factory=dict)


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


class InMemoryTaskRunRepository:
    def __init__(self, runs: dict[UUID, TaskRun]) -> None:
        self._runs = runs

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
        runs = [run for run in self._runs.values() if run.task_id == task_id]
        runs.sort(key=lambda run: run.queued_at)
        return deepcopy(runs)


class InMemoryTaskAttemptRepository:
    def __init__(
        self,
        attempts: dict[UUID, TaskAttempt],
        runs: dict[UUID, TaskRun],
    ) -> None:
        self._attempts = attempts
        self._runs = runs

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
        run_ids = {run.id for run in self._runs.values() if run.task_id == task_id}
        attempts = [attempt for attempt in self._attempts.values() if attempt.run_id in run_ids]
        attempts.sort(key=lambda attempt: attempt.started_at)
        return deepcopy(attempts)


class InMemoryOutboxRepository:
    def __init__(self, outbox: list[MessageEnvelope]) -> None:
        self._outbox = outbox

    def add(self, envelope: MessageEnvelope) -> None:
        self._outbox.append(deepcopy(envelope))


class InMemoryInboxRepository:
    def __init__(self, inbox: dict[tuple[str, UUID], InboxMessage]) -> None:
        self._inbox = inbox

    def contains(self, consumer_name: str, message_id: UUID) -> bool:
        return (consumer_name, message_id) in self._inbox

    def add(self, message: InboxMessage) -> None:
        self._inbox[(message.consumer_name, message.message_id)] = deepcopy(message)


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


class InMemoryUnitOfWork:
    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    def __enter__(self) -> InMemoryUnitOfWork:
        self._tasks = deepcopy(self._store.tasks)
        self._runs = deepcopy(self._store.runs)
        self._attempts = deepcopy(self._store.attempts)
        self._outbox = deepcopy(self._store.outbox)
        self._inbox = deepcopy(self._store.inbox)
        self._idempotency = deepcopy(self._store.idempotency)
        self.tasks = InMemoryTaskRepository(self._tasks)
        self.runs = InMemoryTaskRunRepository(self._runs)
        self.attempts = InMemoryTaskAttemptRepository(self._attempts, self._runs)
        self.outbox = InMemoryOutboxRepository(self._outbox)
        self.inbox = InMemoryInboxRepository(self._inbox)
        self.idempotency = InMemoryIdempotencyRepository(self._idempotency)
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        if exc_type is not None:
            self.rollback()

    def commit(self) -> None:
        self._store.tasks = deepcopy(self._tasks)
        self._store.runs = deepcopy(self._runs)
        self._store.attempts = deepcopy(self._attempts)
        self._store.outbox = deepcopy(self._outbox)
        self._store.inbox = deepcopy(self._inbox)
        self._store.idempotency = deepcopy(self._idempotency)

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
