from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import Select, delete, select
from sqlalchemy import func as sa_func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from agentmesh.domain.budgets import BudgetSettlementSource, TaskBudget
from agentmesh.domain.coordination import Subtask, SubtaskDependency, SubtaskStatus
from agentmesh.domain.errors import IdempotencyConflict
from agentmesh.domain.handoffs import Handoff, HandoffStatus
from agentmesh.domain.messaging import IdempotencyRecord, InboxMessage, MessageEnvelope
from agentmesh.domain.observability import UsageRecord, UsageSource
from agentmesh.domain.resolutions import TaskResolution, TaskResolutionAction
from agentmesh.domain.tasks import (
    AcceptanceCriterion,
    AttemptStatus,
    RunRole,
    RunStatus,
    Task,
    TaskAttempt,
    TaskExecutionMode,
    TaskRun,
    TaskStatus,
)
from agentmesh.infrastructure.postgres.models import (
    HandoffRecord,
    IdempotencyRecordModel,
    InboxMessageRecord,
    OutboxEventRecord,
    SubtaskDependencyRecord,
    SubtaskRecord,
    TaskAttemptRecord,
    TaskRecord,
    TaskResolutionRecord,
    TaskRunRecord,
    UsageRecordModel,
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
        record.execution_mode = task.execution_mode.value
        record.acceptance_criteria = [criterion.to_dict() for criterion in task.acceptance_criteria]
        record.max_revisions = task.max_revisions
        record.revision_count = task.revision_count
        record.review_deadline = task.review_deadline
        record.candidate_output = (
            dict(task.candidate_output) if task.candidate_output is not None else None
        )
        record.latest_review = (
            dict(task.latest_review) if task.latest_review is not None else None
        )
        record.plan_version = task.plan_version
        record.plan_digest = task.plan_digest
        record.max_concurrency = task.max_concurrency
        record.budget = task.budget.to_dict() if task.budget is not None else None
        record.settled_tokens = task.settled_tokens
        record.reserved_tokens = task.reserved_tokens
        record.settled_cost_micros = task.settled_cost_micros
        record.reserved_cost_micros = task.reserved_cost_micros
        record.budget_exhausted_reason = task.budget_exhausted_reason
        record.budget_revision = task.budget_revision
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
            execution_mode=task.execution_mode.value,
            acceptance_criteria=[
                criterion.to_dict() for criterion in task.acceptance_criteria
            ],
            max_revisions=task.max_revisions,
            revision_count=task.revision_count,
            review_deadline=task.review_deadline,
            candidate_output=(
                dict(task.candidate_output) if task.candidate_output is not None else None
            ),
            latest_review=(dict(task.latest_review) if task.latest_review is not None else None),
            plan_version=task.plan_version,
            plan_digest=task.plan_digest,
            max_concurrency=task.max_concurrency,
            budget=task.budget.to_dict() if task.budget is not None else None,
            settled_tokens=task.settled_tokens,
            reserved_tokens=task.reserved_tokens,
            settled_cost_micros=task.settled_cost_micros,
            reserved_cost_micros=task.reserved_cost_micros,
            budget_exhausted_reason=task.budget_exhausted_reason,
            budget_revision=task.budget_revision,
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
            execution_mode=TaskExecutionMode(record.execution_mode),
            acceptance_criteria=tuple(
                AcceptanceCriterion.from_dict(value) for value in record.acceptance_criteria
            ),
            max_revisions=record.max_revisions,
            revision_count=record.revision_count,
            review_deadline=record.review_deadline,
            candidate_output=(
                dict(record.candidate_output) if record.candidate_output is not None else None
            ),
            latest_review=(
                dict(record.latest_review) if record.latest_review is not None else None
            ),
            plan_version=record.plan_version,
            plan_digest=record.plan_digest,
            max_concurrency=record.max_concurrency,
            budget=(TaskBudget.from_dict(record.budget) if record.budget else None),
            settled_tokens=record.settled_tokens,
            reserved_tokens=record.reserved_tokens,
            settled_cost_micros=record.settled_cost_micros,
            reserved_cost_micros=record.reserved_cost_micros,
            budget_exhausted_reason=record.budget_exhausted_reason,
            budget_revision=record.budget_revision,
            version=record.version,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


class SqlAlchemyTaskResolutionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, resolution: TaskResolution) -> None:
        self._session.add(
            TaskResolutionRecord(
                id=resolution.id,
                task_id=resolution.task_id,
                action=resolution.action.value,
                actor=resolution.actor,
                reason=resolution.reason,
                previous_status=resolution.previous_status.value,
                resulting_status=resolution.resulting_status.value,
                previous_error=resolution.previous_error,
                details=dict(resolution.details),
                created_at=resolution.created_at,
            )
        )

    def get(self, resolution_id: UUID) -> TaskResolution | None:
        record = self._session.get(TaskResolutionRecord, resolution_id)
        return self._to_domain(record) if record is not None else None

    def list_for_task(self, task_id: UUID) -> list[TaskResolution]:
        statement = (
            select(TaskResolutionRecord)
            .where(TaskResolutionRecord.task_id == task_id)
            .order_by(TaskResolutionRecord.created_at.asc(), TaskResolutionRecord.id.asc())
        )
        return [self._to_domain(record) for record in self._session.scalars(statement)]

    @staticmethod
    def _to_domain(record: TaskResolutionRecord) -> TaskResolution:
        return TaskResolution(
            id=record.id,
            task_id=record.task_id,
            action=TaskResolutionAction(record.action),
            actor=record.actor,
            reason=record.reason,
            previous_status=TaskStatus(record.previous_status),
            resulting_status=TaskStatus(record.resulting_status),
            previous_error=record.previous_error,
            details=dict(record.details),
            created_at=record.created_at,
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
        record.role = run.role.value
        record.revision_number = run.revision_number
        record.subtask_id = run.subtask_id
        record.output = dict(run.output) if run.output is not None else None
        record.error = run.error
        record.queued_at = run.queued_at
        record.started_at = run.started_at
        record.completed_at = run.completed_at
        record.pause_requested_at = run.pause_requested_at
        record.paused_at = run.paused_at
        record.resumed_at = run.resumed_at
        record.paused_from_status = (
            run.paused_from_status.value if run.paused_from_status is not None else None
        )

    def list_for_task(self, task_id: UUID) -> list[TaskRun]:
        statement = (
            select(TaskRunRecord)
            .where(TaskRunRecord.task_id == task_id)
            .order_by(TaskRunRecord.queued_at.asc())
        )
        return [self._to_domain(record) for record in self._session.scalars(statement)]

    def list_for_tasks(self, task_ids: list[UUID]) -> list[TaskRun]:
        if not task_ids:
            return []
        statement = (
            select(TaskRunRecord)
            .where(TaskRunRecord.task_id.in_(task_ids))
            .order_by(TaskRunRecord.task_id.asc(), TaskRunRecord.queued_at.asc())
        )
        return [self._to_domain(record) for record in self._session.scalars(statement)]

    def list_active_for_agent_version(
        self, agent_version_id: UUID, *, tenant_id: str
    ) -> list[TaskRun]:
        statement = (
            select(TaskRunRecord)
            .join(TaskRecord, TaskRecord.id == TaskRunRecord.task_id)
            .where(
                TaskRunRecord.agent_version_id == agent_version_id,
                TaskRunRecord.status.in_(
                    [
                        RunStatus.QUEUED.value,
                        RunStatus.RUNNING.value,
                        RunStatus.PAUSE_REQUESTED.value,
                        RunStatus.PAUSED.value,
                    ]
                ),
                TaskRecord.tenant_id == tenant_id,
            )
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
            agent_version_id=run.agent_version_id,
            agent_version_digest=run.agent_version_digest,
            role=run.role.value,
            revision_number=run.revision_number,
            subtask_id=run.subtask_id,
            status=run.status.value,
            output=dict(run.output) if run.output is not None else None,
            error=run.error,
            queued_at=run.queued_at,
            started_at=run.started_at,
            completed_at=run.completed_at,
            pause_requested_at=run.pause_requested_at,
            paused_at=run.paused_at,
            resumed_at=run.resumed_at,
            paused_from_status=(
                run.paused_from_status.value if run.paused_from_status is not None else None
            ),
        )

    @staticmethod
    def _to_domain(record: TaskRunRecord) -> TaskRun:
        return TaskRun(
            id=record.id,
            task_id=record.task_id,
            thread_id=record.thread_id,
            agent_id=record.agent_id,
            agent_version_id=record.agent_version_id,
            agent_version_digest=record.agent_version_digest,
            role=RunRole(record.role),
            revision_number=record.revision_number,
            subtask_id=record.subtask_id,
            status=RunStatus(record.status),
            output=dict(record.output) if record.output is not None else None,
            error=record.error,
            queued_at=record.queued_at,
            started_at=record.started_at,
            completed_at=record.completed_at,
            pause_requested_at=record.pause_requested_at,
            paused_at=record.paused_at,
            resumed_at=record.resumed_at,
            paused_from_status=(
                RunStatus(record.paused_from_status) if record.paused_from_status else None
            ),
        )


class SqlAlchemySubtaskRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, subtask: Subtask) -> None:
        self._session.add(self._to_record(subtask))

    def get(self, subtask_id: UUID, *, for_update: bool = False) -> Subtask | None:
        record = self._session.get(SubtaskRecord, subtask_id, with_for_update=for_update)
        return self._to_domain(record) if record is not None else None

    def save(self, subtask: Subtask) -> None:
        record = self._session.get(SubtaskRecord, subtask.id)
        if record is None:
            raise LookupError(f"Subtask record {subtask.id} was not found")
        record.status = subtask.status.value
        record.current_run_id = subtask.current_run_id
        record.output = dict(subtask.output) if subtask.output is not None else None
        record.error = subtask.error
        record.version = subtask.version
        record.updated_at = subtask.updated_at

    def list_for_task(self, task_id: UUID, *, for_update: bool = False) -> list[Subtask]:
        statement = (
            select(SubtaskRecord)
            .where(SubtaskRecord.task_id == task_id)
            .order_by(SubtaskRecord.key.asc())
        )
        if for_update:
            statement = statement.with_for_update()
        return [self._to_domain(record) for record in self._session.scalars(statement)]

    def list_for_tasks(self, task_ids: list[UUID]) -> list[Subtask]:
        if not task_ids:
            return []
        statement = (
            select(SubtaskRecord)
            .where(SubtaskRecord.task_id.in_(task_ids))
            .order_by(SubtaskRecord.task_id.asc(), SubtaskRecord.key.asc())
        )
        return [self._to_domain(record) for record in self._session.scalars(statement)]

    @staticmethod
    def _to_record(subtask: Subtask) -> SubtaskRecord:
        return SubtaskRecord(
            id=subtask.id,
            task_id=subtask.task_id,
            key=subtask.key,
            objective=subtask.objective,
            input=dict(subtask.input),
            required_capabilities=list(subtask.required_capabilities),
            preferred_agent_id=subtask.preferred_agent_id,
            status=subtask.status.value,
            current_run_id=subtask.current_run_id,
            output=dict(subtask.output) if subtask.output is not None else None,
            error=subtask.error,
            version=subtask.version,
            created_at=subtask.created_at,
            updated_at=subtask.updated_at,
        )

    @staticmethod
    def _to_domain(record: SubtaskRecord) -> Subtask:
        return Subtask(
            id=record.id,
            task_id=record.task_id,
            key=record.key,
            objective=record.objective,
            input=dict(record.input),
            required_capabilities=tuple(record.required_capabilities),
            preferred_agent_id=record.preferred_agent_id,
            status=SubtaskStatus(record.status),
            current_run_id=record.current_run_id,
            output=dict(record.output) if record.output is not None else None,
            error=record.error,
            version=record.version,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


class SqlAlchemySubtaskDependencyRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, dependency: SubtaskDependency) -> None:
        self._session.add(
            SubtaskDependencyRecord(
                task_id=dependency.task_id,
                predecessor_id=dependency.predecessor_id,
                successor_id=dependency.successor_id,
            )
        )

    def list_for_task(self, task_id: UUID) -> list[SubtaskDependency]:
        statement = (
            select(SubtaskDependencyRecord)
            .where(SubtaskDependencyRecord.task_id == task_id)
            .order_by(
                SubtaskDependencyRecord.successor_id.asc(),
                SubtaskDependencyRecord.predecessor_id.asc(),
            )
        )
        return [self._to_domain(record) for record in self._session.scalars(statement)]

    def list_for_tasks(self, task_ids: list[UUID]) -> list[SubtaskDependency]:
        if not task_ids:
            return []
        statement = (
            select(SubtaskDependencyRecord)
            .where(SubtaskDependencyRecord.task_id.in_(task_ids))
            .order_by(
                SubtaskDependencyRecord.task_id.asc(),
                SubtaskDependencyRecord.successor_id.asc(),
            )
        )
        return [self._to_domain(record) for record in self._session.scalars(statement)]

    @staticmethod
    def _to_domain(record: SubtaskDependencyRecord) -> SubtaskDependency:
        return SubtaskDependency(
            task_id=record.task_id,
            predecessor_id=record.predecessor_id,
            successor_id=record.successor_id,
        )


class SqlAlchemyHandoffRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, handoff: Handoff) -> None:
        self._session.add(self._to_record(handoff))

    def get(self, handoff_id: UUID, *, for_update: bool = False) -> Handoff | None:
        record = self._session.get(HandoffRecord, handoff_id, with_for_update=for_update)
        return self._to_domain(record) if record is not None else None

    def save(self, handoff: Handoff) -> None:
        record = self._session.get(HandoffRecord, handoff.id)
        if record is None:
            raise LookupError(f"Handoff record {handoff.id} was not found")
        record.status = handoff.status.value
        record.decided_by = handoff.decided_by
        record.decision_reason = handoff.decision_reason
        record.decided_at = handoff.decided_at
        record.version = handoff.version

    def list_for_task(self, task_id: UUID) -> list[Handoff]:
        statement = (
            select(HandoffRecord)
            .where(HandoffRecord.task_id == task_id)
            .order_by(HandoffRecord.requested_at.asc(), HandoffRecord.id.asc())
        )
        return [self._to_domain(record) for record in self._session.scalars(statement)]

    def list_for_tasks(self, task_ids: list[UUID]) -> list[Handoff]:
        if not task_ids:
            return []
        statement = (
            select(HandoffRecord)
            .where(HandoffRecord.task_id.in_(task_ids))
            .order_by(
                HandoffRecord.task_id.asc(),
                HandoffRecord.requested_at.asc(),
                HandoffRecord.id.asc(),
            )
        )
        return [self._to_domain(record) for record in self._session.scalars(statement)]

    def list_for_target(
        self, target_subtask_id: UUID, *, status: HandoffStatus | None = None
    ) -> list[Handoff]:
        statement = select(HandoffRecord).where(
            HandoffRecord.target_subtask_id == target_subtask_id
        )
        if status is not None:
            statement = statement.where(HandoffRecord.status == status.value)
        statement = statement.order_by(HandoffRecord.requested_at.asc())
        return [self._to_domain(record) for record in self._session.scalars(statement)]

    @staticmethod
    def _to_record(handoff: Handoff) -> HandoffRecord:
        return HandoffRecord(
            id=handoff.id,
            task_id=handoff.task_id,
            source_subtask_id=handoff.source_subtask_id,
            source_run_id=handoff.source_run_id,
            source_trace_id=handoff.source_trace_id,
            causation_id=handoff.causation_id,
            source_agent_id=handoff.source_agent_id,
            target_subtask_id=handoff.target_subtask_id,
            target_agent_id=handoff.target_agent_id,
            objective=handoff.objective,
            reason=handoff.reason,
            completed_work_summary=handoff.completed_work_summary,
            unresolved_questions=list(handoff.unresolved_questions),
            constraints=dict(handoff.constraints),
            acceptance_criteria=[dict(value) for value in handoff.acceptance_criteria],
            status=handoff.status.value,
            requested_by=handoff.requested_by,
            requested_at=handoff.requested_at,
            decided_by=handoff.decided_by,
            decision_reason=handoff.decision_reason,
            decided_at=handoff.decided_at,
            version=handoff.version,
        )

    @staticmethod
    def _to_domain(record: HandoffRecord) -> Handoff:
        return Handoff(
            id=record.id,
            task_id=record.task_id,
            source_subtask_id=record.source_subtask_id,
            source_run_id=record.source_run_id,
            source_trace_id=record.source_trace_id,
            causation_id=record.causation_id,
            source_agent_id=record.source_agent_id,
            target_subtask_id=record.target_subtask_id,
            target_agent_id=record.target_agent_id,
            objective=record.objective,
            reason=record.reason,
            completed_work_summary=record.completed_work_summary,
            unresolved_questions=tuple(record.unresolved_questions),
            constraints=dict(record.constraints),
            acceptance_criteria=tuple(dict(value) for value in record.acceptance_criteria),
            status=HandoffStatus(record.status),
            requested_by=record.requested_by,
            requested_at=record.requested_at,
            decided_by=record.decided_by,
            decision_reason=record.decision_reason,
            decided_at=record.decided_at,
            version=record.version,
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
        record.settled_tokens = attempt.settled_tokens
        record.settled_cost_micros = attempt.settled_cost_micros
        record.budget_settlement_source = (
            attempt.budget_settlement_source.value
            if attempt.budget_settlement_source is not None
            else None
        )

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

    def list_for_tasks(self, task_ids: list[UUID]) -> list[TaskAttempt]:
        if not task_ids:
            return []
        statement = (
            select(TaskAttemptRecord)
            .join(TaskRunRecord, TaskRunRecord.id == TaskAttemptRecord.run_id)
            .where(TaskRunRecord.task_id.in_(task_ids))
            .order_by(TaskRunRecord.task_id.asc(), TaskAttemptRecord.started_at.asc())
        )
        return [self._to_domain(record) for record in self._session.scalars(statement)]

    @staticmethod
    def _to_record(attempt: TaskAttempt) -> TaskAttemptRecord:
        return TaskAttemptRecord(
            id=attempt.id,
            run_id=attempt.run_id,
            trace_id=attempt.trace_id,
            worker_id=attempt.worker_id,
            lease_token=attempt.lease_token,
            fencing_token=attempt.fencing_token,
            status=attempt.status.value,
            lease_expires_at=attempt.lease_expires_at,
            heartbeat_at=attempt.heartbeat_at,
            started_at=attempt.started_at,
            completed_at=attempt.completed_at,
            error=attempt.error,
            reserved_tokens=attempt.reserved_tokens,
            reserved_cost_micros=attempt.reserved_cost_micros,
            settled_tokens=attempt.settled_tokens,
            settled_cost_micros=attempt.settled_cost_micros,
            budget_settlement_source=(
                attempt.budget_settlement_source.value
                if attempt.budget_settlement_source is not None
                else None
            ),
        )

    @staticmethod
    def _to_domain(record: TaskAttemptRecord) -> TaskAttempt:
        return TaskAttempt(
            id=record.id,
            run_id=record.run_id,
            trace_id=record.trace_id,
            worker_id=record.worker_id,
            lease_token=record.lease_token,
            fencing_token=record.fencing_token,
            status=AttemptStatus(record.status),
            lease_expires_at=record.lease_expires_at,
            heartbeat_at=record.heartbeat_at,
            started_at=record.started_at,
            completed_at=record.completed_at,
            error=record.error,
            reserved_tokens=record.reserved_tokens,
            reserved_cost_micros=record.reserved_cost_micros,
            settled_tokens=record.settled_tokens,
            settled_cost_micros=record.settled_cost_micros,
            budget_settlement_source=(
                BudgetSettlementSource(record.budget_settlement_source)
                if record.budget_settlement_source
                else None
            ),
        )


class SqlAlchemyUsageRecordRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add_if_absent(self, record: UsageRecord) -> bool:
        statement = (
            insert(UsageRecordModel)
            .values(
                id=record.id,
                tenant_id=record.tenant_id,
                task_id=record.task_id,
                run_id=record.run_id,
                attempt_id=record.attempt_id,
                trace_id=record.trace_id,
                provider=record.provider,
                model=record.model,
                source=record.source.value,
                usage_details=record.usage_details,
                cost_details_micros=record.cost_details_micros,
                currency=record.currency,
                pricing_version=record.pricing_version,
                recorded_at=record.recorded_at,
            )
            .on_conflict_do_nothing(index_elements=[UsageRecordModel.id])
            .returning(UsageRecordModel.id)
        )
        if self._session.scalar(statement) is not None:
            return True
        existing = self._session.get(UsageRecordModel, record.id)
        if existing is None or self._to_domain(existing) != record:
            raise IdempotencyConflict(
                f"Usage record ID {record.id} was reused with different content"
            )
        return False

    def list_for_task(self, task_id: UUID) -> list[UsageRecord]:
        statement = (
            select(UsageRecordModel)
            .where(UsageRecordModel.task_id == task_id)
            .order_by(UsageRecordModel.recorded_at.asc(), UsageRecordModel.id.asc())
        )
        return [self._to_domain(record) for record in self._session.scalars(statement)]

    @staticmethod
    def _to_domain(record: UsageRecordModel) -> UsageRecord:
        return UsageRecord(
            id=record.id,
            tenant_id=record.tenant_id,
            task_id=record.task_id,
            run_id=record.run_id,
            attempt_id=record.attempt_id,
            trace_id=record.trace_id,
            provider=record.provider,
            model=record.model,
            source=UsageSource(record.source),
            usage_details=dict(record.usage_details),
            cost_details_micros=dict(record.cost_details_micros),
            currency=record.currency,
            pricing_version=record.pricing_version,
            recorded_at=record.recorded_at,
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
                quarantined_at=None,
                attempt_count=0,
                last_error=None,
            )
        )


class SqlAlchemyInboxRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def contains(self, tenant_id: str, consumer_name: str, message_id: UUID) -> bool:
        return (
            self._session.get(
                InboxMessageRecord,
                (tenant_id, consumer_name, message_id),
            )
            is not None
        )

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
