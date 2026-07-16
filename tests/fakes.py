from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from uuid import UUID

from agentmesh.domain.tasks import Task, TaskRun, TaskStatus


@dataclass
class InMemoryStore:
    tasks: dict[UUID, Task] = field(default_factory=dict)
    runs: dict[UUID, TaskRun] = field(default_factory=dict)


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
        status: TaskStatus | None = None,
    ) -> list[Task]:
        tasks = list(self._tasks.values())
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
        runs.sort(key=lambda run: run.started_at)
        return deepcopy(runs)


class InMemoryUnitOfWork:
    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    def __enter__(self) -> InMemoryUnitOfWork:
        self._tasks = deepcopy(self._store.tasks)
        self._runs = deepcopy(self._store.runs)
        self.tasks = InMemoryTaskRepository(self._tasks)
        self.runs = InMemoryTaskRunRepository(self._runs)
        self._committed = False
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        if exc_type is not None:
            self.rollback()

    def commit(self) -> None:
        self._store.tasks = deepcopy(self._tasks)
        self._store.runs = deepcopy(self._runs)
        self._committed = True

    def rollback(self) -> None:
        self._committed = False


class InMemoryUnitOfWorkFactory:
    def __init__(self, store: InMemoryStore | None = None) -> None:
        self.store = store or InMemoryStore()

    def __call__(self) -> InMemoryUnitOfWork:
        return InMemoryUnitOfWork(self.store)


class AlwaysReady:
    def is_ready(self) -> bool:
        return True
