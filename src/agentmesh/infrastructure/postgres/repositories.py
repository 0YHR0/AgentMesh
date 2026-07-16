from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from agentmesh.domain.tasks import RunStatus, Task, TaskRun, TaskStatus
from agentmesh.infrastructure.postgres.models import TaskRecord, TaskRunRecord


class SqlAlchemyTaskRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, task: Task) -> None:
        self._session.add(self._to_record(task))

    def get(self, task_id: UUID, *, for_update: bool = False) -> Task | None:
        record = self._session.get(TaskRecord, task_id, with_for_update=for_update)
        return self._to_domain(record) if record is not None else None

    def save(self, task: Task) -> None:
        record = self._session.get(TaskRecord, task.id)
        if record is None:
            raise LookupError(f"Task record {task.id} was not found")
        record.objective = task.objective
        record.input = dict(task.input)
        record.status = task.status.value
        record.current_run_id = task.current_run_id
        record.output = dict(task.output) if task.output is not None else None
        record.error = task.error
        record.version = task.version
        record.updated_at = task.updated_at

    def list(
        self,
        *,
        limit: int,
        offset: int,
        status: TaskStatus | None = None,
    ) -> list[Task]:
        statement: Select[tuple[TaskRecord]] = select(TaskRecord)
        if status is not None:
            statement = statement.where(TaskRecord.status == status.value)
        statement = statement.order_by(TaskRecord.created_at.desc()).limit(limit).offset(offset)
        return [self._to_domain(record) for record in self._session.scalars(statement)]

    @staticmethod
    def _to_record(task: Task) -> TaskRecord:
        return TaskRecord(
            id=task.id,
            objective=task.objective,
            input=dict(task.input),
            status=task.status.value,
            current_run_id=task.current_run_id,
            output=dict(task.output) if task.output is not None else None,
            error=task.error,
            version=task.version,
            created_at=task.created_at,
            updated_at=task.updated_at,
        )

    @staticmethod
    def _to_domain(record: TaskRecord) -> Task:
        return Task(
            id=record.id,
            objective=record.objective,
            input=dict(record.input),
            status=TaskStatus(record.status),
            current_run_id=record.current_run_id,
            output=dict(record.output) if record.output is not None else None,
            error=record.error,
            version=record.version,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


class SqlAlchemyTaskRunRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, run: TaskRun) -> None:
        self._session.add(self._to_record(run))

    def get(self, run_id: UUID, *, for_update: bool = False) -> TaskRun | None:
        record = self._session.get(TaskRunRecord, run_id, with_for_update=for_update)
        return self._to_domain(record) if record is not None else None

    def save(self, run: TaskRun) -> None:
        record = self._session.get(TaskRunRecord, run.id)
        if record is None:
            raise LookupError(f"Task run record {run.id} was not found")
        record.status = run.status.value
        record.output = dict(run.output) if run.output is not None else None
        record.error = run.error
        record.completed_at = run.completed_at

    def list_for_task(self, task_id: UUID) -> list[TaskRun]:
        statement = (
            select(TaskRunRecord)
            .where(TaskRunRecord.task_id == task_id)
            .order_by(TaskRunRecord.started_at.asc())
        )
        return [self._to_domain(record) for record in self._session.scalars(statement)]

    @staticmethod
    def _to_record(run: TaskRun) -> TaskRunRecord:
        return TaskRunRecord(
            id=run.id,
            task_id=run.task_id,
            thread_id=run.thread_id,
            agent_id=run.agent_id,
            status=run.status.value,
            output=dict(run.output) if run.output is not None else None,
            error=run.error,
            started_at=run.started_at,
            completed_at=run.completed_at,
        )

    @staticmethod
    def _to_domain(record: TaskRunRecord) -> TaskRun:
        return TaskRun(
            id=record.id,
            task_id=record.task_id,
            thread_id=record.thread_id,
            agent_id=record.agent_id,
            status=RunStatus(record.status),
            output=dict(record.output) if record.output is not None else None,
            error=record.error,
            started_at=record.started_at,
            completed_at=record.completed_at,
        )
