from __future__ import annotations

from typing import Any
from uuid import UUID

from agentmesh.application.ports import UnitOfWorkFactory, WorkflowRunner
from agentmesh.domain.errors import TaskExecutionFailed, TaskNotFound
from agentmesh.domain.tasks import RunStatus, Task, TaskAggregate, TaskRun, TaskStatus


class TaskApplicationService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        workflow_runner: WorkflowRunner,
        agent_id: str,
    ) -> None:
        self._uow_factory = uow_factory
        self._workflow_runner = workflow_runner
        self._agent_id = agent_id

    def create_task(
        self,
        objective: str,
        input: dict[str, Any] | None = None,
    ) -> TaskAggregate:
        task = Task.create(objective=objective, input=input)
        with self._uow_factory() as uow:
            uow.tasks.add(task)
            uow.commit()
        return TaskAggregate(task=task)

    def get_task(self, task_id: UUID) -> TaskAggregate:
        with self._uow_factory() as uow:
            task = self._get_task_or_raise(uow, task_id)
            runs = uow.runs.list_for_task(task_id)
            return TaskAggregate(task=task, runs=runs)

    def list_tasks(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        status: TaskStatus | None = None,
    ) -> list[TaskAggregate]:
        with self._uow_factory() as uow:
            tasks = uow.tasks.list(limit=limit, offset=offset, status=status)
            return [
                TaskAggregate(task=task, runs=uow.runs.list_for_task(task.id))
                for task in tasks
            ]

    def run_task(self, task_id: UUID) -> TaskAggregate:
        with self._uow_factory() as uow:
            task = self._get_task_or_raise(uow, task_id, for_update=True)
            run = TaskRun.start(task_id=task.id, agent_id=self._agent_id)
            task.start(run.id)
            uow.runs.add(run)
            uow.tasks.save(task)
            uow.commit()

        try:
            output = self._workflow_runner.run(task, run)
        except Exception as exc:
            safe_error = f"Workflow execution failed: {type(exc).__name__}"
            self._mark_failed(task_id=task_id, run_id=run.id, error=safe_error)
            raise TaskExecutionFailed(task_id, safe_error) from exc

        with self._uow_factory() as uow:
            persisted_task = self._get_task_or_raise(uow, task_id, for_update=True)
            persisted_run = uow.runs.get(run.id, for_update=True)
            if persisted_run is None:
                raise TaskExecutionFailed(task_id, f"Run {run.id} was not found")
            persisted_task.complete(run.id, output)
            persisted_run.complete(output)
            uow.tasks.save(persisted_task)
            uow.runs.save(persisted_run)
            uow.commit()

        return self.get_task(task_id)

    def cancel_task(self, task_id: UUID) -> TaskAggregate:
        with self._uow_factory() as uow:
            task = self._get_task_or_raise(uow, task_id, for_update=True)
            task.cancel()
            if task.current_run_id is not None:
                run = uow.runs.get(task.current_run_id, for_update=True)
                if run is not None and run.status == RunStatus.RUNNING:
                    run.cancel()
                    uow.runs.save(run)
            uow.tasks.save(task)
            uow.commit()
        return self.get_task(task_id)

    def _mark_failed(self, *, task_id: UUID, run_id: UUID, error: str) -> None:
        with self._uow_factory() as uow:
            task = self._get_task_or_raise(uow, task_id, for_update=True)
            run = uow.runs.get(run_id, for_update=True)
            if run is None:
                raise TaskExecutionFailed(task_id, f"Run {run_id} was not found")
            task.fail(run_id, error)
            run.fail(error)
            uow.tasks.save(task)
            uow.runs.save(run)
            uow.commit()

    @staticmethod
    def _get_task_or_raise(uow: Any, task_id: UUID, *, for_update: bool = False) -> Task:
        task = uow.tasks.get(task_id, for_update=for_update)
        if task is None:
            raise TaskNotFound(task_id)
        return task
