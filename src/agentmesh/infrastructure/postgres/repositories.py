from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import Select, delete, select
from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session

from agentmesh.domain.messaging import IdempotencyRecord, InboxMessage, MessageEnvelope
from agentmesh.domain.tasks import AttemptStatus, RunStatus, Task, TaskAttempt, TaskRun, TaskStatus
from agentmesh.infrastructure.postgres.models import (
    IdempotencyRecordModel,
    InboxMessageRecord,
    OutboxEventRecord,
    TaskAttemptRecord,
    TaskRecord,
    TaskRunRecord,
)


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
        record.tenant_id = task.tenant_id
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
        tenant_id: str,
        status: TaskStatus | None = None,
    ) -> list[Task]:
        statement: Select[tuple[TaskRecord]] = select(TaskRecord).where(
            TaskRecord.tenant_id == tenant_id
        )
        if status is not None:
            statement = statement.where(TaskRecord.status == status.value)
        statement = statement.order_by(TaskRecord.created_at.desc()).limit(limit).offset(offset)
        return [self._to_domain(record) for record in self._session.scalars(statement)]

    @staticmethod
    def _to_record(task: Task) -> TaskRecord:
        return TaskRecord(
            id=task.id,
            tenant_id=task.tenant_id,
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
            tenant_id=record.tenant_id,
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
        record.queued_at = run.queued_at
        record.started_at = run.started_at
        record.completed_at = run.completed_at

    def list_for_task(self, task_id: UUID) -> list[TaskRun]:
        statement = (
            select(TaskRunRecord)
            .where(TaskRunRecord.task_id == task_id)
            .order_by(TaskRunRecord.queued_at.asc())
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
            queued_at=run.queued_at,
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
            queued_at=record.queued_at,
            started_at=record.started_at,
            completed_at=record.completed_at,
        )


class SqlAlchemyTaskAttemptRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, attempt: TaskAttempt) -> None:
        self._session.add(self._to_record(attempt))

    def get(self, attempt_id: UUID, *, for_update: bool = False) -> TaskAttempt | None:
        record = self._session.get(TaskAttemptRecord, attempt_id, with_for_update=for_update)
        return self._to_domain(record) if record is not None else None

    def save(self, attempt: TaskAttempt) -> None:
        record = self._session.get(TaskAttemptRecord, attempt.id)
        if record is None:
            raise LookupError(f"Task attempt record {attempt.id} was not found")
        record.status = attempt.status.value
        record.lease_expires_at = attempt.lease_expires_at
        record.heartbeat_at = attempt.heartbeat_at
        record.completed_at = attempt.completed_at
        record.error = attempt.error

    def latest_for_run(self, run_id: UUID, *, for_update: bool = False) -> TaskAttempt | None:
        statement = (
            select(TaskAttemptRecord)
            .where(TaskAttemptRecord.run_id == run_id)
            .order_by(TaskAttemptRecord.fencing_token.desc())
            .limit(1)
        )
        if for_update:
            statement = statement.with_for_update()
        record = self._session.scalar(statement)
        return self._to_domain(record) if record is not None else None

    def list_for_task(self, task_id: UUID) -> list[TaskAttempt]:
        statement = (
            select(TaskAttemptRecord)
            .join(TaskRunRecord, TaskRunRecord.id == TaskAttemptRecord.run_id)
            .where(TaskRunRecord.task_id == task_id)
            .order_by(TaskAttemptRecord.started_at.asc())
        )
        return [self._to_domain(record) for record in self._session.scalars(statement)]

    @staticmethod
    def _to_record(attempt: TaskAttempt) -> TaskAttemptRecord:
        return TaskAttemptRecord(
            id=attempt.id,
            run_id=attempt.run_id,
            worker_id=attempt.worker_id,
            lease_token=attempt.lease_token,
            fencing_token=attempt.fencing_token,
            status=attempt.status.value,
            lease_expires_at=attempt.lease_expires_at,
            heartbeat_at=attempt.heartbeat_at,
            started_at=attempt.started_at,
            completed_at=attempt.completed_at,
            error=attempt.error,
        )

    @staticmethod
    def _to_domain(record: TaskAttemptRecord) -> TaskAttempt:
        return TaskAttempt(
            id=record.id,
            run_id=record.run_id,
            worker_id=record.worker_id,
            lease_token=record.lease_token,
            fencing_token=record.fencing_token,
            status=AttemptStatus(record.status),
            lease_expires_at=record.lease_expires_at,
            heartbeat_at=record.heartbeat_at,
            started_at=record.started_at,
            completed_at=record.completed_at,
            error=record.error,
        )


class SqlAlchemyOutboxRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, envelope: MessageEnvelope) -> None:
        self._session.add(
            OutboxEventRecord(
                id=envelope.message_id,
                tenant_id=envelope.tenant_id,
                topic=envelope.schema_name,
                envelope=envelope.to_dict(),
                status="PENDING",
                available_at=envelope.occurred_at,
                created_at=envelope.occurred_at,
                claimed_by=None,
                claimed_until=None,
                published_at=None,
                attempt_count=0,
                last_error=None,
            )
        )


class SqlAlchemyInboxRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def contains(self, consumer_name: str, message_id: UUID) -> bool:
        return self._session.get(InboxMessageRecord, (consumer_name, message_id)) is not None

    def add(self, message: InboxMessage) -> None:
        self._session.add(
            InboxMessageRecord(
                consumer_name=message.consumer_name,
                message_id=message.message_id,
                tenant_id=message.tenant_id,
                schema_name=message.schema_name,
                schema_version=message.schema_version,
                processed_at=message.processed_at,
            )
        )


class SqlAlchemyIdempotencyRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def lock(self, scope: str, key: str) -> None:
        lock_name = f"{len(scope)}:{scope}:{key}"
        self._session.execute(
            select(sa_func.pg_advisory_xact_lock(sa_func.hashtextextended(lock_name, 0)))
        )

    def get(self, scope: str, key: str) -> IdempotencyRecord | None:
        record = self._session.get(IdempotencyRecordModel, (scope, key))
        if record is None:
            return None
        now = datetime.now(timezone.utc)
        if record.expires_at <= now:
            self._session.execute(
                delete(IdempotencyRecordModel).where(
                    IdempotencyRecordModel.scope == scope,
                    IdempotencyRecordModel.key == key,
                )
            )
            return None
        return IdempotencyRecord(
            scope=record.scope,
            key=record.key,
            request_hash=record.request_hash,
            result=dict(record.result),
            created_at=record.created_at,
            expires_at=record.expires_at,
        )

    def add(self, record: IdempotencyRecord) -> None:
        self._session.add(
            IdempotencyRecordModel(
                scope=record.scope,
                key=record.key,
                request_hash=record.request_hash,
                result=dict(record.result),
                created_at=record.created_at,
                expires_at=record.expires_at,
            )
        )
