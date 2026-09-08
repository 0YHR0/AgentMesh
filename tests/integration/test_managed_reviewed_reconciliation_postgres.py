"""PostgreSQL qualification for managed REVIEWED reconciliation."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select

from agentmesh.application.business_outcomes import BusinessOutcomeApplier
from agentmesh.domain.budgets import TaskBudget
from agentmesh.domain.errors import InvalidTaskTransition
from agentmesh.domain.messaging import MessageEnvelope
from agentmesh.domain.runtime_execution import (
    RuntimeLifecycleIntent,
    RuntimeLifecycleOperation,
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
    IdempotencyRecordModel,
    OutboxEventRecord,
    RuntimeAssignmentSnapshotRecord,
    RuntimeExecutionRecord,
    RuntimeHandleSnapshotRecord,
    RuntimeIntegrityIncidentActionRecord,
    RuntimeIntegrityIncidentRecord,
    RuntimeLifecycleOperationRecord,
    RuntimeObservationRecord,
    TaskRecord,
    TaskResolutionRecord,
    TaskRunRecord,
)
from agentmesh.infrastructure.postgres.repositories import SqlAlchemyOutboxRepository
from agentmesh.infrastructure.postgres.uow import SqlAlchemyUnitOfWork, SqlAlchemyUnitOfWorkFactory
from agentmesh.runtime_sdk import RuntimeObservation, RuntimePhase, canonical_digest
from tests.integration.test_managed_direct_worker_postgres import (
    _cleanup_task_outbox,
    _fixture,
    _operator,
    _reconciler,
    _request_reviewed,
)

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("AGENTMESH_RUN_POSTGRES_TESTS") != "1",
        reason="set AGENTMESH_RUN_POSTGRES_TESTS=1 to run service integration tests",
    ),
]


class _PoisonManagedExecution:
    def execute_authoritative(self, *args, **kwargs):
        raise AssertionError("parked reconciliation must not redispatch")


def _cleanup_runtime_markers(factory, task_id: UUID) -> None:
    """Remove only runtime writer rows owned by this qualification Task.

    Reconciliation deliberately exercises the durable runtime writer.  The
    shared PostgreSQL qualification database is later reused by migration
    tests, so leave no runtime marker rows behind while retaining the
    task-scoped cleanup boundary.
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
            select(func.count()).select_from(RuntimeLifecycleOperationRecord).where(
                RuntimeLifecycleOperationRecord.runtime_execution_id.in_(execution_ids)
            )
        ) == 0


def _park_reviewed(
    *, role: RunRole, budget=None, max_revisions: int = 1, review_deadline=None
):
    engine, factory, registry, tasks, worker, backend, consumer, settings = _fixture(
        lease_duration=timedelta(seconds=-1), reviewed_backend=True
    )
    task_id, run, envelope = _request_reviewed(
        tasks,
        settings.tenant_id,
        factory,
        budget=budget,
        max_revisions=max_revisions,
        review_deadline=review_deadline,
    )
    if role is RunRole.REVIEWER:
        # Cross the real executor first, then admit the reviewer continuation.
        worker._lease_duration = timedelta(minutes=5)
        assert worker.process(envelope) is True
        aggregate = tasks.get_task(task_id)
        run = next(item for item in aggregate.runs if item.role is RunRole.REVIEWER)
        envelope = MessageEnvelope.run_requested(
            tenant_id=settings.tenant_id, task_id=task_id, run_id=run.id
        )
        worker._lease_duration = timedelta(seconds=-1)

    task, leased_run, attempt = worker._acquire(
        envelope, task_id=task_id, run_id=run.id
    )
    adapter = worker._managed_execution_service._adapter
    assignment = adapter.assignment_for(task, leased_run, attempt)
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
        now=datetime.now(timezone.utc) - timedelta(seconds=2),
    )
    execution = registry.mark_execution_dispatching(
        execution_id=execution.id,
        attempt_id=attempt.id,
        fencing_token=attempt.fencing_token,
    )
    worker._managed_execution_service = _PoisonManagedExecution()
    assert worker.process(envelope) is True
    return (
        engine,
        factory,
        registry,
        tasks,
        worker,
        consumer,
        settings,
        task_id,
        run,
        attempt,
        execution,
    )


def _observe(
    execution,
    phase=RuntimePhase.SUCCEEDED,
    *,
    reviewer=False,
    reviewer_accepted: bool = True,
):
    output = None
    if phase is RuntimePhase.SUCCEEDED:
        output = (
            {
                "criteria": [{"key": "summary", "passed": reviewer_accepted}],
                "feedback": [],
            }
            if reviewer
            else {"summary": "reconciled candidate"}
        )
    return RuntimeObservation(
        observation_id=str(uuid4()),
        runtime_execution_id=str(execution.id),
        assignment_id=str(execution.assignment_id),
        assignment_digest=execution.assignment_digest,
        phase=phase,
        observed_at=datetime.now(timezone.utc) - timedelta(minutes=2),
        provider_event_id=f"reviewed-pg-{uuid4().hex}",
        output=output,
    )


def _reconcile(factory, settings, execution, observation, key, *, principal=None):
    service = _reconciler(
        factory,
        settings,
        business_outcome_applier=BusinessOutcomeApplier(
            executor_agent_id=settings.agent_id,
            reviewer_agent_id=settings.reviewer_agent_id,
        ),
    )
    return service.reconcile_outcome(
        execution.id,
        principal=principal or _operator(settings.tenant_id),
        observation=observation,
        evidence_digest=canonical_digest(observation.to_dict()),
        evidence_reference="case://postgres/reviewed-reconciliation",
        reason="Reviewed provider evidence confirmed",
        idempotency_key=key,
    )


def _add_cancel_intent(factory, settings, execution) -> None:
    now = datetime.now(timezone.utc)
    with SqlAlchemyUnitOfWorkFactory(factory)() as uow:
        uow.runtimes.add_lifecycle_operation(
            RuntimeLifecycleIntent(
                id=uuid4(),
                tenant_id=settings.tenant_id,
                runtime_execution_id=execution.id,
                operation_id=f"reviewed-reconcile-cancel-{uuid4().hex}",
                operation=RuntimeLifecycleOperation.CANCEL,
                intent_digest="c" * 64,
                status=RuntimeLifecycleStatus.REQUESTED,
                deadline=now + timedelta(minutes=10),
                receipt_summary=None,
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
        uow.commit()


def _evidence_count(factory, execution_id):
    with factory() as session:
        return session.scalar(
            select(func.count()).select_from(RuntimeObservationRecord).where(
                RuntimeObservationRecord.runtime_execution_id == execution_id
            )
        )


def test_postgres_reviewed_executor_reconciliation_creates_one_reviewer_and_replay_is_noop():
    case = _park_reviewed(role=RunRole.EXECUTOR)
    (
        engine,
        factory,
        _registry,
        tasks,
        _worker,
        consumer,
        settings,
        task_id,
        _run,
        _attempt,
        execution,
    ) = case
    try:
        observation = _observe(execution)
        principal = _operator(settings.tenant_id)
        result = _reconcile(
            factory,
            settings,
            execution,
            observation,
            "reviewed-executor-once",
            principal=principal,
        )
        replay = _reconcile(
            factory,
            settings,
            execution,
            observation,
            "reviewed-executor-once",
            principal=principal,
        )
        aggregate = tasks.get_task(task_id)
        assert aggregate.task.status is TaskStatus.REVIEWING
        assert len(aggregate.runs) == 2
        reviewer = next(item for item in aggregate.runs if item.role is RunRole.REVIEWER)
        assert reviewer.runtime_version_id == aggregate.runs[0].runtime_version_id
        assert reviewer.runtime_execution_intent_id != aggregate.runs[0].runtime_execution_intent_id
        assert result.resolution.id == replay.resolution.id
        with factory() as session:
            assert session.scalar(
                select(func.count()).select_from(OutboxEventRecord).where(
                    OutboxEventRecord.envelope["schema_name"].as_string()
                    == "agentmesh.run.requested",
                    OutboxEventRecord.envelope["payload"]["run_id"].as_string()
                    == str(reviewer.id),
                )
            ) == 1
            assert session.scalar(
                select(func.count()).select_from(TaskResolutionRecord).where(
                    TaskResolutionRecord.id == result.resolution.id
                )
            ) == 1
    finally:
        _cleanup_task_outbox(factory, task_id)
        _cleanup_runtime_markers(factory, task_id)
        engine.dispose()


def test_postgres_reviewed_reviewer_reconciliation_accepts_candidate_once():
    case = _park_reviewed(role=RunRole.REVIEWER)
    (
        engine,
        factory,
        _registry,
        tasks,
        _worker,
        _consumer,
        settings,
        task_id,
        _run,
        _attempt,
        execution,
    ) = case
    try:
        result = _reconcile(
            factory,
            settings,
            execution,
            _observe(execution, reviewer=True),
            "reviewed-reviewer-accept",
        )
        aggregate = tasks.get_task(task_id)
        assert aggregate.task.status is TaskStatus.COMPLETED
        assert aggregate.task.output == {"summary": "postgres-reviewed-candidate"}
        assert result.resolution.details["mode"] == TaskExecutionMode.REVIEWED.value
        assert result.resolution.details["role"] == RunRole.REVIEWER.value
        assert result.resolution.details["new_run_id"] is None
    finally:
        _cleanup_task_outbox(factory, task_id)
        _cleanup_runtime_markers(factory, task_id)
        engine.dispose()


def test_postgres_reviewed_parked_identity_conflict_has_zero_writes():
    case = _park_reviewed(role=RunRole.REVIEWER)
    (
        engine,
        factory,
        _registry,
        tasks,
        _worker,
        _consumer,
        settings,
        task_id,
        run,
        _attempt,
        execution,
    ) = case
    try:
        with factory() as session:
            baseline_evidence = session.scalar(
                select(func.count()).select_from(RuntimeObservationRecord).where(
                    RuntimeObservationRecord.runtime_execution_id == execution.id
                )
            )
            record = session.get(TaskRunRecord, run.id)
            assert record is not None
            # Keep all database constraints valid while making the persisted
            # Run metadata disagree with the parked proof.  The application
            # strictness check must reject this before writing evidence.
            record.runtime_execution_intent_id = None
            session.commit()
        observation = _observe(execution, reviewer=True)
        with pytest.raises(InvalidTaskTransition):
            _reconcile(factory, settings, execution, observation, "reviewed-conflict")
        aggregate = tasks.get_task(task_id)
        assert aggregate.task.status is TaskStatus.RECONCILIATION_REQUIRED
        with factory() as session:
            assert session.scalar(
                select(func.count()).select_from(RuntimeObservationRecord).where(
                    RuntimeObservationRecord.runtime_execution_id == execution.id
                )
            ) == baseline_evidence
    finally:
        _cleanup_task_outbox(factory, task_id)
        _cleanup_runtime_markers(factory, task_id)
        engine.dispose()


@pytest.mark.parametrize("role", [RunRole.EXECUTOR, RunRole.REVIEWER])
@pytest.mark.parametrize(
    "phase",
    [
        RuntimePhase.SUCCEEDED,
        RuntimePhase.FAILED,
        RuntimePhase.CANCELED,
        RuntimePhase.TIMED_OUT,
    ],
)
def test_postgres_reviewed_reconciliation_maps_every_known_terminal_for_each_role(
    role, phase
):
    case = _park_reviewed(role=role)
    (
        engine,
        factory,
        _registry,
        tasks,
        _worker,
        _consumer,
        settings,
        task_id,
        run,
        attempt,
        execution,
    ) = case
    try:
        key = f"reviewed-matrix-{role.value.lower()}-{phase.value.lower()}-{uuid4().hex}"
        result = _reconcile(
            factory,
            settings,
            execution,
            _observe(execution, phase, reviewer=role is RunRole.REVIEWER),
            key,
        )
        aggregate = tasks.get_task(task_id)
        expected_task = (
            TaskStatus.REVIEWING
            if role is RunRole.EXECUTOR and phase is RuntimePhase.SUCCEEDED
            else TaskStatus.COMPLETED
            if role is RunRole.REVIEWER and phase is RuntimePhase.SUCCEEDED
            else TaskStatus.FAILED
        )
        expected_run = (
            RunStatus.SUCCEEDED
            if phase is RuntimePhase.SUCCEEDED
            else RunStatus.FAILED
        )
        persisted_run = next(item for item in aggregate.runs if item.id == run.id)
        persisted_attempt = next(item for item in aggregate.attempts if item.id == attempt.id)
        assert aggregate.task.status is expected_task
        assert persisted_run.status is expected_run
        assert persisted_attempt.status is (
            AttemptStatus.SUCCEEDED
            if phase is RuntimePhase.SUCCEEDED
            else AttemptStatus.FAILED
        )
        assert result.resolution.resulting_status is expected_task
        with factory() as session:
            assert session.scalar(
                select(func.count()).select_from(RuntimeObservationRecord).where(
                    RuntimeObservationRecord.runtime_execution_id == execution.id,
                    RuntimeObservationRecord.processing_outcome == "RECONCILED",
                )
            ) == 1
            assert session.scalar(
                select(func.count()).select_from(TaskResolutionRecord).where(
                    TaskResolutionRecord.task_id == task_id
                )
            ) == 1
            assert session.scalar(
                select(func.count()).select_from(OutboxEventRecord).where(
                    OutboxEventRecord.envelope["schema_name"].astext
                    == "agentmesh.runtime.outcome-reconciled",
                    OutboxEventRecord.envelope["payload"]["task_id"].astext
                    == str(task_id),
                )
            ) == 1
            assert session.scalar(
                select(func.count()).select_from(IdempotencyRecordModel).where(
                    IdempotencyRecordModel.key == key
                )
            ) == 1
    finally:
        _cleanup_task_outbox(factory, task_id)
        _cleanup_runtime_markers(factory, task_id)
        engine.dispose()


@pytest.mark.parametrize("role", [RunRole.EXECUTOR, RunRole.REVIEWER])
def test_postgres_reviewed_reconciliation_requested_cancel_is_canceled(role):
    case = _park_reviewed(role=role)
    (
        engine,
        factory,
        _registry,
        tasks,
        _worker,
        _consumer,
        settings,
        task_id,
        _run,
        _attempt,
        execution,
    ) = case
    try:
        key = f"reviewed-requested-cancel-{role.value.lower()}-{uuid4().hex}"
        _add_cancel_intent(factory, settings, execution)
        result = _reconcile(
            factory,
            settings,
            execution,
            _observe(execution, RuntimePhase.CANCELED, reviewer=role is RunRole.REVIEWER),
            key,
        )
        aggregate = tasks.get_task(task_id)
        assert aggregate.task.status is TaskStatus.CANCELED
        assert (
            next(item for item in aggregate.runs if item.id == _run.id).status
            is RunStatus.CANCELED
        )
        assert (
            next(item for item in aggregate.attempts if item.id == _attempt.id).status
            is AttemptStatus.CANCELED
        )
        assert result.resolution.details["business_mapping_reason"] == (
            "runtime.reconciled_canceled"
        )
    finally:
        _cleanup_task_outbox(factory, task_id)
        _cleanup_runtime_markers(factory, task_id)
        engine.dispose()


def test_postgres_reviewed_executor_deadline_waits_without_reviewer():
    budget = TaskBudget.create(
        max_tokens=100,
        token_reservation_per_attempt=10,
        deadline=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    case = _park_reviewed(role=RunRole.EXECUTOR, budget=budget)
    (
        engine,
        factory,
        _registry,
        tasks,
        _worker,
        _consumer,
        settings,
        task_id,
        _run,
        _attempt,
        execution,
    ) = case
    try:
        key = f"reviewed-executor-budget-deadline-{uuid4().hex}"
        expired = datetime.now(timezone.utc) - timedelta(seconds=1)
        with factory() as session:
            task_record = session.get(TaskRecord, task_id)
            assert task_record is not None and task_record.budget is not None
            task_record.budget = {**task_record.budget, "deadline": expired.isoformat()}
            session.commit()
        result = _reconcile(
            factory,
            settings,
            execution,
            _observe(execution),
            key,
        )
        aggregate = tasks.get_task(task_id)
        assert aggregate.task.status is TaskStatus.WAITING_APPROVAL
        assert aggregate.task.current_run_id is None
        assert aggregate.task.candidate_output == {"summary": "reconciled candidate"}
        assert not any(item.role is RunRole.REVIEWER for item in aggregate.runs)
        assert result.resolution.details["business_mapping_reason"] == (
            "budget_deadline_exceeded"
        )
    finally:
        _cleanup_task_outbox(factory, task_id)
        _cleanup_runtime_markers(factory, task_id)
        engine.dispose()


@pytest.mark.parametrize("limit_kind", ["revision", "deadline"])
def test_postgres_reviewed_reviewer_rejection_waits_at_limit_or_deadline(limit_kind):
    case = _park_reviewed(
        role=RunRole.REVIEWER,
        max_revisions=0 if limit_kind == "revision" else 1,
    )
    (
        engine,
        factory,
        _registry,
        tasks,
        _worker,
        _consumer,
        settings,
        task_id,
        _run,
        _attempt,
        execution,
    ) = case
    try:
        key = f"reviewed-review-limit-{limit_kind}-{uuid4().hex}"
        if limit_kind == "deadline":
            with factory() as session:
                task_record = session.get(TaskRecord, task_id)
                assert task_record is not None
                task_record.review_deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
                session.commit()
        result = _reconcile(
            factory,
            settings,
            execution,
            _observe(execution, reviewer=True, reviewer_accepted=False),
            key,
        )
        aggregate = tasks.get_task(task_id)
        assert aggregate.task.status is TaskStatus.WAITING_APPROVAL
        assert aggregate.task.latest_review is not None
        assert aggregate.task.latest_review["accepted"] is False
        assert len(aggregate.runs) == 2
        assert result.resolution.details["new_run_id"] is None
    finally:
        _cleanup_task_outbox(factory, task_id)
        _cleanup_runtime_markers(factory, task_id)
        engine.dispose()


def test_postgres_reviewed_reviewer_invalid_decision_is_consumed_as_failure():
    case = _park_reviewed(role=RunRole.REVIEWER)
    (
        engine,
        factory,
        _registry,
        tasks,
        _worker,
        _consumer,
        settings,
        task_id,
        _run,
        _attempt,
        execution,
    ) = case
    try:
        key = f"reviewed-invalid-decision-{uuid4().hex}"
        observation = _observe(execution, reviewer=True)
        observation = replace(observation, output={"criteria": [], "feedback": []})
        result = _reconcile(
            factory,
            settings,
            execution,
            observation,
            key,
        )
        aggregate = tasks.get_task(task_id)
        assert aggregate.task.status is TaskStatus.FAILED
        assert aggregate.task.error == "review.invalid_decision"
        assert (
            next(item for item in aggregate.runs if item.id == _run.id).status
            is RunStatus.FAILED
        )
        assert (
            next(item for item in aggregate.attempts if item.id == _attempt.id).status
            is AttemptStatus.FAILED
        )
        assert result.resolution.details["business_mapping_reason"] == (
            "review.invalid_decision"
        )
        assert result.resolution.details["confirmed_phase"] == RuntimePhase.SUCCEEDED.value
    finally:
        _cleanup_task_outbox(factory, task_id)
        _cleanup_runtime_markers(factory, task_id)
        engine.dispose()


def test_postgres_reviewed_reconciliation_continuation_outbox_failure_rolls_back(monkeypatch):
    case = _park_reviewed(role=RunRole.EXECUTOR)
    (
        engine,
        factory,
        _registry,
        tasks,
        _worker,
        _consumer,
        settings,
        task_id,
        _run,
        _attempt,
        execution,
    ) = case
    try:
        key = f"reviewed-continuation-rollback-{uuid4().hex}"
        observation = _observe(execution)
        principal = _operator(settings.tenant_id)
        baseline = _evidence_count(factory, execution.id)
        original_add = SqlAlchemyOutboxRepository.add

        def fail_continuation(self, envelope):
            if envelope.schema_name == "agentmesh.run.requested":
                raise RuntimeError("continuation outbox unavailable")
            return original_add(self, envelope)

        monkeypatch.setattr(SqlAlchemyOutboxRepository, "add", fail_continuation)
        with pytest.raises(RuntimeError, match="continuation outbox unavailable"):
            _reconcile(
                factory,
                settings,
                execution,
                observation,
                key,
                principal=principal,
            )
        aggregate = tasks.get_task(task_id)
        assert aggregate.task.status is TaskStatus.RECONCILIATION_REQUIRED
        assert len(aggregate.runs) == 1
        assert _evidence_count(factory, execution.id) == baseline
        with factory() as session:
            assert session.scalar(
                select(func.count()).select_from(TaskResolutionRecord).where(
                    TaskResolutionRecord.task_id == task_id
                )
            ) == 0
            assert session.scalar(
                    select(func.count()).select_from(IdempotencyRecordModel).where(
                    IdempotencyRecordModel.key == key
                )
            ) == 0
        monkeypatch.setattr(SqlAlchemyOutboxRepository, "add", original_add)
        result = _reconcile(
            factory,
            settings,
            execution,
            observation,
            key,
            principal=principal,
        )
        assert tasks.get_task(task_id).task.status is TaskStatus.REVIEWING
        assert result.resolution.id is not None
    finally:
        _cleanup_task_outbox(factory, task_id)
        _cleanup_runtime_markers(factory, task_id)
        engine.dispose()


def test_postgres_reviewed_reconciliation_commit_failure_rolls_back_and_replays(monkeypatch):
    case = _park_reviewed(role=RunRole.EXECUTOR)
    (
        engine,
        factory,
        _registry,
        tasks,
        _worker,
        _consumer,
        settings,
        task_id,
        _run,
        _attempt,
        execution,
    ) = case
    try:
        key = f"reviewed-commit-rollback-{uuid4().hex}"
        observation = _observe(execution)
        principal = _operator(settings.tenant_id)
        baseline = _evidence_count(factory, execution.id)
        original_commit = SqlAlchemyUnitOfWork.commit
        calls = {"count": 0}

        def fail_once(self):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("reconciliation commit unavailable")
            return original_commit(self)

        monkeypatch.setattr(SqlAlchemyUnitOfWork, "commit", fail_once)
        with pytest.raises(RuntimeError, match="reconciliation commit unavailable"):
            _reconcile(
                factory,
                settings,
                execution,
                observation,
                key,
                principal=principal,
            )
        assert tasks.get_task(task_id).task.status is TaskStatus.RECONCILIATION_REQUIRED
        assert _evidence_count(factory, execution.id) == baseline
        monkeypatch.setattr(SqlAlchemyUnitOfWork, "commit", original_commit)
        result = _reconcile(
            factory,
            settings,
            execution,
            observation,
            key,
            principal=principal,
        )
        assert tasks.get_task(task_id).task.status is TaskStatus.REVIEWING
        assert result.resolution.id is not None
    finally:
        _cleanup_task_outbox(factory, task_id)
        _cleanup_runtime_markers(factory, task_id)
        engine.dispose()


def test_postgres_reviewed_reconciliation_same_observation_has_one_concurrent_winner():
    case = _park_reviewed(role=RunRole.EXECUTOR)
    (
        engine,
        factory,
        _registry,
        tasks,
        _worker,
        _consumer,
        settings,
        task_id,
        _run,
        _attempt,
        execution,
    ) = case
    try:
        key = f"reviewed-concurrent-reconciliation-{uuid4().hex}"
        observation = _observe(execution)
        principal = _operator(settings.tenant_id)
        barrier = Barrier(2)

        def reconcile_once():
            barrier.wait(timeout=30)
            return _reconcile(
                factory,
                settings,
                execution,
                observation,
                key,
                principal=principal,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = tuple(pool.map(lambda _item: reconcile_once(), (1, 2)))
        assert first.resolution.id == second.resolution.id
        aggregate = tasks.get_task(task_id)
        assert aggregate.task.status is TaskStatus.REVIEWING
        assert len(aggregate.runs) == 2
        reviewer = [item for item in aggregate.runs if item.role is RunRole.REVIEWER]
        assert len(reviewer) == 1
        with factory() as session:
            assert session.scalar(
                select(func.count()).select_from(TaskResolutionRecord).where(
                    TaskResolutionRecord.task_id == task_id
                )
            ) == 1
            assert session.scalar(
                select(func.count()).select_from(OutboxEventRecord).where(
                    OutboxEventRecord.envelope["schema_name"].astext
                    == "agentmesh.run.requested",
                    OutboxEventRecord.envelope["payload"]["run_id"].astext
                    == str(reviewer[0].id),
                )
            ) == 1
    finally:
        _cleanup_task_outbox(factory, task_id)
        _cleanup_runtime_markers(factory, task_id)
        engine.dispose()
