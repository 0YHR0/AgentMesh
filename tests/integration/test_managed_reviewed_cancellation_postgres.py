"""PostgreSQL qualification for managed cancellation and late-result fencing.

These tests intentionally exercise the application command against the real
SQLAlchemy repositories.  Provider calls are not made by the cancel command;
the assertions therefore focus on the durable proof (Runtime, lifecycle,
outbox, and accounting rows) that a later worker can safely act on.
"""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timezone
from uuid import UUID

import pytest
from sqlalchemy import delete, func, select

from agentmesh.application.quota_services import QuotaPolicyService
from agentmesh.domain.budgets import TaskBudget
from agentmesh.domain.messaging import MessageEnvelope
from agentmesh.domain.quotas import QuotaScope
from agentmesh.domain.runtime_execution import (
    RuntimeExecutionPhase,
    RuntimeLifecycleStatus,
)
from agentmesh.domain.tasks import (
    AttemptStatus,
    RunRole,
    RunStatus,
    TaskExecutionMode,
    TaskStatus,
)
from agentmesh.infrastructure.postgres.models import (
    OutboxEventRecord,
    QuotaReservationRecord,
    RuntimeAssignmentSnapshotRecord,
    RuntimeExecutionRecord,
    RuntimeHandleSnapshotRecord,
    RuntimeIntegrityIncidentActionRecord,
    RuntimeIntegrityIncidentRecord,
    RuntimeLifecycleOperationRecord,
    RuntimeObservationRecord,
    TaskAttemptRecord,
    TaskRecord,
    TaskRunRecord,
)
from agentmesh.infrastructure.postgres.uow import SqlAlchemyUnitOfWorkFactory
from agentmesh.runtime_sdk import RuntimePhase
from tests.integration.test_managed_direct_worker_postgres import (
    _cleanup_task_outbox,
    _fixture,
    _request,
    _request_reviewed,
)

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("AGENTMESH_RUN_POSTGRES_TESTS") != "1",
        reason="set AGENTMESH_RUN_POSTGRES_TESTS=1 to run PostgreSQL cancellation tests",
    ),
]


def _install_quota_policy(factory, tenant_id: str) -> None:
    service = QuotaPolicyService(SqlAlchemyUnitOfWorkFactory(factory), tenant_id)
    service.put_policy(
        scope=QuotaScope.TENANT,
        project_id=None,
        max_concurrent_attempts=100,
        weight=1,
        created_by="postgres-cancellation-test",
    )
    service.put_policy(
        scope=QuotaScope.PROJECT,
        project_id="default",
        max_concurrent_attempts=100,
        weight=1,
        created_by="postgres-cancellation-test",
    )


def _active_chain(
    *,
    mode: TaskExecutionMode = TaskExecutionMode.DIRECT,
    role: RunRole = RunRole.EXECUTOR,
    quota: bool = False,
    budget: TaskBudget | None = None,
):
    """Create and persist an owned managed Runtime chain without dispatching."""
    engine, factory, registry, tasks, worker, backend, consumer, settings = _fixture(
        reviewed_backend=mode is TaskExecutionMode.REVIEWED,
        quota_admission=quota,
    )
    if quota:
        _install_quota_policy(factory, settings.tenant_id)

    if mode is TaskExecutionMode.REVIEWED:
        task_id, executor_run, envelope = _request_reviewed(
            tasks, settings.tenant_id, factory
        )
        if role is RunRole.REVIEWER:
            assert worker.process(envelope) is True
            aggregate = tasks.get_task(task_id)
            run = next(item for item in aggregate.runs if item.role is RunRole.REVIEWER)
            envelope = MessageEnvelope.run_requested(
                tenant_id=settings.tenant_id,
                task_id=task_id,
                run_id=run.id,
            )
        else:
            run = executor_run
    else:
        assert role is RunRole.EXECUTOR
        task_id, run, envelope = _request(
            tasks, settings.tenant_id, factory, budget=budget
        )

    task, leased_run, attempt = worker._acquire(
        envelope, task_id=task_id, run_id=run.id
    )
    assignment = worker._managed_execution_service._adapter.assignment_for(
        task, leased_run, attempt
    )
    execution = registry.prepare_execution(
        run_id=run.id,
        assignment_id=UUID(assignment.assignment_id),
        assignment_digest=assignment.assignment_digest,
        execution_id=run.runtime_execution_intent_id,
    )
    execution = registry.claim_execution_owner(
        execution_id=execution.id,
        attempt_id=attempt.id,
        fencing_token=attempt.fencing_token,
        expected_owner_attempt_id=None,
        expected_fencing_token=None,
        expected_version=execution.version,
        now=datetime.now(timezone.utc),
    )
    # The command requires the Run's durable execution binding, not merely an
    # intent row discovered by list_executions_for_run.
    with SqlAlchemyUnitOfWorkFactory(factory)() as uow:
        bound_run = uow.runs.get(run.id, for_update=True)
        assert bound_run is not None
        bound_run.bind_runtime_execution(execution.id)
        uow.runs.save(bound_run)
        uow.commit()
    return {
        "engine": engine,
        "factory": factory,
        "registry": registry,
        "tasks": tasks,
        "worker": worker,
        "backend": backend,
        "consumer": consumer,
        "settings": settings,
        "task_id": task_id,
        "run": run,
        "attempt": attempt,
        "execution": execution,
        "envelope": envelope,
    }


def _dispatching_chain(**kwargs):
    case = _active_chain(**kwargs)
    execution = case["registry"].mark_execution_dispatching(
        execution_id=case["execution"].id,
        attempt_id=case["attempt"].id,
        fencing_token=case["attempt"].fencing_token,
    )
    case["execution"] = execution
    return case


def _active_without_execution(*, budget: TaskBudget | None = None):
    engine, factory, registry, tasks, worker, backend, consumer, settings = _fixture()
    task_id, run, envelope = _request(
        tasks, settings.tenant_id, factory, budget=budget
    )
    task, leased_run, attempt = worker._acquire(
        envelope, task_id=task_id, run_id=run.id
    )
    return {
        "engine": engine,
        "factory": factory,
        "registry": registry,
        "tasks": tasks,
        "worker": worker,
        "backend": backend,
        "consumer": consumer,
        "settings": settings,
        "task_id": task_id,
        "run": run,
        "attempt": attempt,
        "execution": None,
        "envelope": envelope,
    }


def _close(case) -> None:
    _cleanup_task_outbox(case["factory"], case["task_id"])
    _cleanup_runtime_markers(case["factory"], case["task_id"])
    case["engine"].dispose()


def _cleanup_runtime_markers(factory, task_id: UUID) -> None:
    """Remove only runtime writer rows owned by this test Task.

    Cancellation tests intentionally exercise the 0049 lifecycle writer.  A
    shared qualification database must not retain those rows for later
    downgrade or migration tests, so cleanup is keyed through this Task's
    Run/Runtime execution graph rather than using a global table delete.
    """
    with factory() as session:
        execution_ids = select(RuntimeExecutionRecord.id).join(
            TaskRunRecord, TaskRunRecord.id == RuntimeExecutionRecord.run_id
        ).where(TaskRunRecord.task_id == task_id)
        incident_ids = select(RuntimeIntegrityIncidentRecord.id).where(
            RuntimeIntegrityIncidentRecord.runtime_execution_id.in_(execution_ids)
        )
        session.execute(
            delete(RuntimeIntegrityIncidentActionRecord).where(
                RuntimeIntegrityIncidentActionRecord.incident_id.in_(incident_ids)
            )
        )
        session.execute(
            delete(RuntimeIntegrityIncidentRecord).where(
                RuntimeIntegrityIncidentRecord.runtime_execution_id.in_(execution_ids)
            )
        )
        session.execute(
            delete(RuntimeLifecycleOperationRecord).where(
                RuntimeLifecycleOperationRecord.runtime_execution_id.in_(execution_ids)
            )
        )
        session.execute(
            delete(RuntimeAssignmentSnapshotRecord).where(
                RuntimeAssignmentSnapshotRecord.runtime_execution_id.in_(execution_ids)
            )
        )
        session.execute(
            delete(RuntimeHandleSnapshotRecord).where(
                RuntimeHandleSnapshotRecord.runtime_execution_id.in_(execution_ids)
            )
        )
        session.execute(
            delete(RuntimeObservationRecord).where(
                RuntimeObservationRecord.runtime_execution_id.in_(execution_ids)
            )
        )
        session.commit()
    with factory() as session:
        execution_ids = select(RuntimeExecutionRecord.id).join(
            TaskRunRecord, TaskRunRecord.id == RuntimeExecutionRecord.run_id
        ).where(TaskRunRecord.task_id == task_id)
        assert session.scalar(
            select(func.count()).select_from(RuntimeLifecycleOperationRecord).where(
                RuntimeLifecycleOperationRecord.runtime_execution_id.in_(execution_ids)
            )
        ) == 0
        assert session.scalar(
            select(func.count()).select_from(RuntimeAssignmentSnapshotRecord).where(
                RuntimeAssignmentSnapshotRecord.runtime_execution_id.in_(execution_ids)
            )
        ) == 0
        assert session.scalar(
            select(func.count()).select_from(RuntimeHandleSnapshotRecord).where(
                RuntimeHandleSnapshotRecord.runtime_execution_id.in_(execution_ids)
            )
        ) == 0
        assert session.scalar(
            select(func.count()).select_from(RuntimeIntegrityIncidentRecord).where(
                RuntimeIntegrityIncidentRecord.runtime_execution_id.in_(execution_ids)
            )
        ) == 0
        assert session.scalar(
            select(func.count())
            .select_from(RuntimeIntegrityIncidentActionRecord)
            .join(
                RuntimeIntegrityIncidentRecord,
                RuntimeIntegrityIncidentActionRecord.incident_id
                == RuntimeIntegrityIncidentRecord.id,
            )
            .where(RuntimeIntegrityIncidentRecord.runtime_execution_id.in_(execution_ids))
        ) == 0


def _task_outbox(factory, task_id: UUID) -> list[OutboxEventRecord]:
    with factory() as session:
        run_ids = {
            run.id
            for run in session.scalars(
                select(TaskRunRecord).where(TaskRunRecord.task_id == task_id)
            )
        }
        execution_ids = {
            execution.id
            for execution in session.scalars(
                select(RuntimeExecutionRecord).where(RuntimeExecutionRecord.run_id.in_(run_ids))
            )
        } if run_ids else set()
        return [
            record
            for record in session.scalars(select(OutboxEventRecord))
            if (
                str(record.envelope.get("payload", {}).get("task_id", ""))
                == str(task_id)
                or str(record.envelope.get("payload", {}).get("run_id", ""))
                in {str(run_id) for run_id in run_ids}
                or str(record.envelope.get("payload", {}).get("runtime_execution_id", ""))
                in {str(execution_id) for execution_id in execution_ids}
            )
        ]


def _lifecycle(factory, execution_id: UUID) -> list[RuntimeLifecycleOperationRecord]:
    with factory() as session:
        return list(
            session.scalars(
                select(RuntimeLifecycleOperationRecord).where(
                    RuntimeLifecycleOperationRecord.runtime_execution_id == execution_id
                )
            )
        )


def _runtime_row(factory, execution_id: UUID) -> RuntimeExecutionRecord:
    with factory() as session:
        row = session.get(RuntimeExecutionRecord, execution_id)
        assert row is not None
        return row


def _outbox_signature(records: list[OutboxEventRecord]) -> tuple[tuple[UUID, str, dict], ...]:
    return tuple(
        (record.id, record.status, record.envelope)
        for record in records
    )


def _task_chain_rows(factory, task_id: UUID) -> tuple[object, ...]:
    with factory() as session:
        task = session.get(TaskRecord, task_id)
        runs = list(
            session.scalars(select(TaskRunRecord).where(TaskRunRecord.task_id == task_id))
        )
        run_ids = [run.id for run in runs]
        attempts = list(
            session.scalars(
                select(TaskAttemptRecord).where(TaskAttemptRecord.run_id.in_(run_ids))
            )
        ) if run_ids else []
        executions = list(
            session.scalars(
                select(RuntimeExecutionRecord).where(RuntimeExecutionRecord.run_id.in_(run_ids))
            )
        ) if run_ids else []
        lifecycle = [
            item
            for item in session.scalars(select(RuntimeLifecycleOperationRecord))
            if item.runtime_execution_id in {row.id for row in executions}
        ]
        outbox = _task_outbox(factory, task_id)
        assert task is not None
        return (
            task.status,
            task.current_run_id,
            task.reserved_tokens,
            task.settled_tokens,
            tuple((run.id, run.status, run.completed_at) for run in runs),
            tuple((attempt.id, attempt.status, attempt.completed_at) for attempt in attempts),
            tuple((row.id, row.phase, row.version) for row in executions),
            tuple((item.id, item.status, item.version) for item in lifecycle),
            tuple((item.id, item.status) for item in outbox),
        )


def test_postgres_queued_and_active_without_execution_cancel_and_replay_are_exact():
    engine, factory, _registry, tasks, worker, _backend, _consumer, settings = _fixture(
        reviewed_backend=True
    )
    task_id = None
    try:
        task_id, run, envelope = _request_reviewed(tasks, settings.tenant_id, factory)
        before_outbox = _task_outbox(factory, task_id)
        first = tasks.cancel_task(task_id)
        assert first.task.status is TaskStatus.CANCELED
        assert tasks.get_task(task_id).runs[0].status is RunStatus.CANCELED
        assert _lifecycle(factory, run.runtime_execution_intent_id) == []
        assert _task_outbox(factory, task_id) == before_outbox
        assert tasks.cancel_task(task_id).task.status is TaskStatus.CANCELED

        # A separate active/no-execution chain proves that cancellation also
        # releases a running Attempt without manufacturing Runtime state.
        case = _active_without_execution(
            budget=TaskBudget.create(max_tokens=100, token_reservation_per_attempt=10),
        )
        try:
            active = case["tasks"].cancel_task(case["task_id"])
            assert active.task.status is TaskStatus.CANCELED
            aggregate = case["tasks"].get_task(case["task_id"])
            assert aggregate.attempts[0].status is AttemptStatus.CANCELED
            assert aggregate.task.reserved_tokens == 0
            assert _lifecycle(case["factory"], case["run"].runtime_execution_intent_id) == []
            assert not _task_outbox(case["factory"], case["task_id"])
        finally:
            _close(case)
    finally:
        _cleanup_task_outbox(factory, task_id)
        engine.dispose()


@pytest.mark.parametrize("role", [RunRole.EXECUTOR, RunRole.REVIEWER])
def test_postgres_reviewed_prepared_cancel_is_provider_free_abort_with_audit(role):
    case = _active_chain(mode=TaskExecutionMode.REVIEWED, role=role)
    try:
        before = _task_chain_rows(case["factory"], case["task_id"])
        canceled = case["tasks"].cancel_task(case["task_id"])
        assert canceled.task.status is TaskStatus.CANCELED
        runtime = _runtime_row(case["factory"], case["execution"].id)
        assert runtime.phase == RuntimeExecutionPhase.CANCELED.value
        assert _lifecycle(case["factory"], case["execution"].id) == []
        events = _task_outbox(case["factory"], case["task_id"])
        audit = [
            item for item in events
            if item.envelope.get("schema_name") == "agentmesh.runtime.dispatch.aborted"
        ]
        assert len(audit) == 1
        assert audit[0].envelope["payload"]["runtime_execution_id"] == str(case["execution"].id)
        after = _task_chain_rows(case["factory"], case["task_id"])
        assert after[0] == TaskStatus.CANCELED.value
        assert case["execution"].id in {item[0] for item in before[6]}
        assert case["tasks"].cancel_task(case["task_id"]).task.status is TaskStatus.CANCELED
        assert len(
            [
                item
                for item in _task_outbox(case["factory"], case["task_id"])
                if item.envelope.get("schema_name")
                == "agentmesh.runtime.dispatch.aborted"
            ]
        ) == 1
    finally:
        _close(case)


@pytest.mark.parametrize("role", [RunRole.EXECUTOR, RunRole.REVIEWER])
def test_postgres_reviewed_dispatching_cancel_has_one_stable_lifecycle_and_outbox(role):
    case = _dispatching_chain(mode=TaskExecutionMode.REVIEWED, role=role)
    try:
        canceled = case["tasks"].cancel_task(case["task_id"])
        assert canceled.task.status is TaskStatus.CANCELED
        runtime = _runtime_row(case["factory"], case["execution"].id)
        assert runtime.phase == RuntimeExecutionPhase.CANCEL_REQUESTED.value
        intents = _lifecycle(case["factory"], case["execution"].id)
        assert len(intents) == 1
        assert intents[0].operation_id == f"runtime-cancel:{case['execution'].id}:v1"
        assert intents[0].status == RuntimeLifecycleStatus.REQUESTED.value
        lifecycle_events = [
            item
            for item in _task_outbox(case["factory"], case["task_id"])
            if item.envelope.get("schema_name") == "agentmesh.runtime.lifecycle.requested"
        ]
        assert len(lifecycle_events) == 1
        snapshot = _task_chain_rows(case["factory"], case["task_id"])
        outbox_snapshot = _outbox_signature(
            _task_outbox(case["factory"], case["task_id"])
        )
        assert case["tasks"].cancel_task(case["task_id"]).task.status is TaskStatus.CANCELED
        assert _task_chain_rows(case["factory"], case["task_id"]) == snapshot
        assert _outbox_signature(
            _task_outbox(case["factory"], case["task_id"])
        ) == outbox_snapshot
    finally:
        _close(case)


@pytest.mark.parametrize("role", [RunRole.EXECUTOR, RunRole.REVIEWER])
def test_postgres_reviewed_terminal_pending_cancel_rejects_lifecycle_without_provider(role):
    case = _dispatching_chain(mode=TaskExecutionMode.REVIEWED, role=role)
    try:
        terminal_at = datetime.now(timezone.utc)
        with case["factory"]() as session:
            row = session.get(RuntimeExecutionRecord, case["execution"].id)
            assert row is not None
            row.phase = RuntimeExecutionPhase.SUCCEEDED.value
            row.terminal_at = terminal_at
            row.updated_at = terminal_at
            row.version += 1
            session.commit()
        canceled = case["tasks"].cancel_task(case["task_id"])
        assert canceled.task.status is TaskStatus.CANCELED
        runtime = _runtime_row(case["factory"], case["execution"].id)
        assert runtime.phase == RuntimeExecutionPhase.SUCCEEDED.value
        intents = _lifecycle(case["factory"], case["execution"].id)
        assert len(intents) == 1
        assert intents[0].status == RuntimeLifecycleStatus.REJECTED.value
        assert not [
            item
            for item in _task_outbox(case["factory"], case["task_id"])
            if item.envelope.get("schema_name") == "agentmesh.runtime.lifecycle.requested"
        ]
    finally:
        _close(case)


def test_postgres_reviewed_reviewer_waiting_approval_cancel_preserves_terminal_history():
    case = _active_chain(mode=TaskExecutionMode.REVIEWED, role=RunRole.REVIEWER)
    try:
        # Complete the reviewer business history first, then model the policy
        # decision that leaves it waiting for operator approval.
        with case["factory"]() as session:
            attempt = session.get(TaskAttemptRecord, case["attempt"].id)
            run = session.get(TaskRunRecord, case["run"].id)
            task = session.get(TaskRecord, case["task_id"])
            assert attempt is not None and run is not None and task is not None
            now = datetime.now(timezone.utc)
            attempt.status = AttemptStatus.SUCCEEDED.value
            attempt.completed_at = now
            run.status = RunStatus.SUCCEEDED.value
            run.completed_at = now
            task.status = TaskStatus.WAITING_APPROVAL.value
            task.current_run_id = run.id
            task.candidate_output = {"summary": "candidate"}
            task.latest_review = {"accepted": False, "reason": "needs work"}
            task.error = "review_revision_limit_reached"
            task.updated_at = now
            session.commit()
        with case["factory"]() as session:
            before_run = session.get(TaskRunRecord, case["run"].id)
            before_attempt = session.get(TaskAttemptRecord, case["attempt"].id)
            assert before_run is not None and before_attempt is not None
            history = (
                before_run.status,
                before_run.completed_at,
                before_attempt.status,
                before_attempt.completed_at,
            )
        outbox_before = _outbox_signature(
            _task_outbox(case["factory"], case["task_id"])
        )
        assert case["tasks"].cancel_task(case["task_id"]).task.status is TaskStatus.CANCELED
        with case["factory"]() as session:
            after_run = session.get(TaskRunRecord, case["run"].id)
            after_attempt = session.get(TaskAttemptRecord, case["attempt"].id)
            assert after_run is not None and after_attempt is not None
            assert (
                after_run.status,
                after_run.completed_at,
                after_attempt.status,
                after_attempt.completed_at,
            ) == history
        assert _outbox_signature(
            _task_outbox(case["factory"], case["task_id"])
        ) == outbox_before
        assert _lifecycle(case["factory"], case["execution"].id) == []
    finally:
        _close(case)


@pytest.mark.parametrize(
    "phase", [RuntimePhase.SUCCEEDED, RuntimePhase.LOST, RuntimePhase.OUTCOME_UNKNOWN]
)
def test_postgres_reviewed_late_result_after_cancel_is_runtime_only(phase):
    case = _active_chain(mode=TaskExecutionMode.REVIEWED, role=RunRole.EXECUTOR)
    try:
        task, run, attempt = case["tasks"].get_task(case["task_id"]), case["run"], case["attempt"]
        result = case["worker"]._managed_execution_service.execute_authoritative(
            task.task, run, attempt
        )
        # execute_authoritative may have persisted the same execution; cancel
        # first and then feed the saved provider evidence back to the worker.
        case["tasks"].cancel_task(case["task_id"])
        observation = result.observation
        if phase is not RuntimePhase.SUCCEEDED:
            observation = replace(observation, phase=phase, output=None)
        result = replace(result, observation=observation)
        assert case["worker"]._finalize_managed(
            case["envelope"],
            task_id=case["task_id"],
            run_id=case["run"].id,
            attempt_id=case["attempt"].id,
            result=result,
        ) is False
        aggregate = case["tasks"].get_task(case["task_id"])
        assert aggregate.task.status is TaskStatus.CANCELED
        assert aggregate.runs[0].status is RunStatus.CANCELED
        assert aggregate.attempts[0].status is AttemptStatus.CANCELED
        with case["factory"]() as session:
            evidence = list(
                session.scalars(
                    select(RuntimeObservationRecord).where(
                        RuntimeObservationRecord.runtime_execution_id == case["execution"].id
                    )
                )
            )
            assert len(evidence) == 1
            if phase is RuntimePhase.SUCCEEDED:
                assert evidence[0].evidence.get("quarantined_output") is not None
            else:
                assert evidence[0].phase == phase.value
                assert len(
                    [
                        item
                        for item in _task_outbox(case["factory"], case["task_id"])
                        if item.envelope.get("schema_name")
                        == "agentmesh.runtime.reconciliation.required"
                    ]
                ) == 1
    finally:
        _close(case)


def test_postgres_reviewed_cancel_releases_budget_and_quota_once():
    budget = TaskBudget.create(
        max_tokens=100,
        token_reservation_per_attempt=10,
        max_cost_micros=100,
        cost_reservation_micros_per_attempt=10,
    )
    case = _dispatching_chain(mode=TaskExecutionMode.DIRECT, quota=True, budget=budget)
    try:
        # REVIEWED helper creates the task without a budget; install the same
        # persisted policy before admission only for this focused accounting
        # assertion, then assert release on the actual Attempt reservation.
        case["tasks"].cancel_task(case["task_id"])
        aggregate = case["tasks"].get_task(case["task_id"])
        assert aggregate.task.reserved_tokens == 0
        assert aggregate.task.reserved_cost_micros == 0
        with case["factory"]() as session:
            reservations = list(
                session.scalars(
                    select(QuotaReservationRecord).where(
                        QuotaReservationRecord.attempt_id == case["attempt"].id
                    )
                )
            )
            assert reservations and all(item.released_at is not None for item in reservations)
            released = [item.released_at for item in reservations]
        case["tasks"].cancel_task(case["task_id"])
        with case["factory"]() as session:
            reservations_after = list(
                session.scalars(
                    select(QuotaReservationRecord).where(
                        QuotaReservationRecord.attempt_id == case["attempt"].id
                    )
                )
            )
            assert [item.released_at for item in reservations_after] == released
    finally:
        _close(case)


class _FailingRuntimeSaveFactory:
    """Inject a post-runtime-write failure while preserving SQL rollback."""

    def __init__(self, base) -> None:
        self.base = base
        self.enabled = True

    def __call__(self):
        return _FailingRuntimeSaveUnitOfWork(self)


class _FailingRuntimeSaveUnitOfWork:
    def __init__(self, owner: _FailingRuntimeSaveFactory) -> None:
        self.owner = owner
        self.uow = None

    def __enter__(self):
        self.uow = self.owner.base().__enter__()
        original = self.uow.runtimes.save_execution

        def save(value, *, tenant_id):
            original(value, tenant_id=tenant_id)
            if self.owner.enabled:
                raise RuntimeError("injected cancellation runtime failure")

        self.uow.runtimes.save_execution = save
        return self.uow

    def __exit__(self, exc_type, exc_value, traceback):
        assert self.uow is not None
        return self.uow.__exit__(exc_type, exc_value, traceback)


def test_postgres_reviewed_cancel_runtime_failure_rolls_back_everything():
    case = _dispatching_chain(mode=TaskExecutionMode.REVIEWED, role=RunRole.EXECUTOR, quota=True)
    try:
        before = _task_chain_rows(case["factory"], case["task_id"])
        failing = _FailingRuntimeSaveFactory(
            SqlAlchemyUnitOfWorkFactory(case["factory"])
        )
        case["tasks"]._uow_factory = failing
        with pytest.raises(RuntimeError, match="injected cancellation runtime failure"):
            case["tasks"].cancel_task(case["task_id"])
        failing.enabled = False
        assert _task_chain_rows(case["factory"], case["task_id"]) == before
        assert _runtime_row(case["factory"], case["execution"].id).phase == (
            RuntimeExecutionPhase.DISPATCHING.value
        )
        assert _lifecycle(case["factory"], case["execution"].id) == []
        with case["factory"]() as session:
            assert session.scalar(
                select(func.count()).select_from(QuotaReservationRecord).where(
                    QuotaReservationRecord.attempt_id == case["attempt"].id,
                    QuotaReservationRecord.released_at.is_not(None),
                )
            ) == 0
    finally:
        _close(case)
