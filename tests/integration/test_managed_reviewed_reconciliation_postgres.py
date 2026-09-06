"""PostgreSQL qualification for managed REVIEWED reconciliation."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from agentmesh.application.business_outcomes import BusinessOutcomeApplier
from agentmesh.domain.errors import InvalidTaskTransition
from agentmesh.domain.messaging import MessageEnvelope
from agentmesh.domain.tasks import RunRole, TaskExecutionMode, TaskStatus
from agentmesh.infrastructure.postgres.models import (
    OutboxEventRecord,
    RuntimeObservationRecord,
    TaskResolutionRecord,
    TaskRunRecord,
)
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


def _park_reviewed(*, role: RunRole):
    engine, factory, registry, tasks, worker, backend, consumer, settings = _fixture(
        lease_duration=timedelta(seconds=-1), reviewed_backend=True
    )
    task_id, run, envelope = _request_reviewed(tasks, settings.tenant_id, factory)
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


def _observe(execution, phase=RuntimePhase.SUCCEEDED, *, reviewer=False):
    output = None
    if phase is RuntimePhase.SUCCEEDED:
        output = (
            {"criteria": [{"key": "summary", "passed": True}], "feedback": []}
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
        engine.dispose()
