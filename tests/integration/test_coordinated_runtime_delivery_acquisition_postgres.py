"""Real PostgreSQL qualification for coordinated delivery acquisition (c2f3)."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from agentmesh.application.coordinated_runtime_delivery import (
    CoordinatedDeliveryResultKind,
)
from agentmesh.application.coordinated_runtime_delivery_acquisition import (
    CoordinatedRuntimeDeliveryAcquisitionService,
)
from agentmesh.application.runtime_work_items import CanonicalWorkItemBuilder
from agentmesh.config import get_settings
from agentmesh.domain.budgets import TaskBudget
from agentmesh.domain.coordination import CoordinationRuntimeDrain, CoordinationRuntimeDrainTarget
from agentmesh.features import FeatureGateSet
from agentmesh.infrastructure.postgres.models import (
    InboxMessageRecord,
    OutboxEventRecord,
    RuntimeAssignmentSnapshotRecord,
    RuntimeExecutionRecord,
    TaskAttemptRecord,
)
from agentmesh.runtime_sdk import RuntimeAssignment
from tests.integration.test_coordinated_runtime_convergence_postgres import (
    _real_scheduler,
)
from tests.integration.test_coordinated_runtime_dispatch_postgres import (
    _cleanup as _dispatch_cleanup,
)
from tests.integration.test_coordinated_runtime_dispatch_postgres import (
    _fixture as _dispatch_fixture,
)

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("AGENTMESH_RUN_POSTGRES_TESTS") != "1",
        reason="set AGENTMESH_RUN_POSTGRES_TESTS=1 to run coordinated acquisition PostgreSQL tests",
    ),
]


def _service(fixture, engine) -> CoordinatedRuntimeDeliveryAcquisitionService:
    return CoordinatedRuntimeDeliveryAcquisitionService(
        uow_factory=fixture.factory,
        worker_id="acquisition-pg-worker",
        consumer_name="acquisition-pg-consumer",
        lease_duration=timedelta(minutes=5),
        feature_gates=FeatureGateSet.from_config("full"),
        work_item_builder=CanonicalWorkItemBuilder(_real_scheduler(engine, fixture)),
    )


def _queued_fixture(engine):
    """Reuse the published AgentDefinition/Version dispatch fixture as queued input."""
    fixture = _dispatch_fixture(engine)
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM task_attempts WHERE run_id = :run_id"),
            {"run_id": fixture.run.id},
        )
        connection.execute(
            text(
                "UPDATE task_runs SET status = 'QUEUED', started_at = NULL, "
                "completed_at = NULL, pause_requested_at = NULL, paused_at = NULL, "
                "resumed_at = NULL, paused_from_status = NULL WHERE id = :run_id"
            ),
            {"run_id": fixture.run.id},
        )
        connection.execute(
            text(
                "UPDATE subtasks SET status = 'READY', version = version + 1 "
                "WHERE id = :subtask_id"
            ),
            {"subtask_id": fixture.run.subtask_id},
        )
    return fixture


def _envelope(fixture):
    from agentmesh.domain.messaging import MessageEnvelope

    return MessageEnvelope.run_requested(
        tenant_id=fixture.tenant_id,
        task_id=fixture.task.id,
        run_id=fixture.run.id,
        at=fixture.now,
    )


def _cleanup(engine, fixture) -> None:
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM outbox_events WHERE tenant_id = :tenant_id"),
            {"tenant_id": fixture.tenant_id},
        )
    _dispatch_cleanup(engine, fixture)


def _database_now(engine) -> datetime:
    with engine.connect() as connection:
        return connection.execute(text("SELECT CURRENT_TIMESTAMP")).scalar_one()


def _recoverable_assignment(fixture) -> RuntimeAssignment:
    payload = fixture.assignment.to_dict(include_digest=False)
    payload["objective"] = fixture.task.objective
    return RuntimeAssignment.from_dict(payload)


def test_postgres_queued_executor_first_acquire_and_concurrent_single_attempt():
    engine = create_engine(os.environ.get("AGENTMESH_DATABASE_URL", get_settings().database_url))
    fixture = _queued_fixture(engine)
    try:
        service = _service(fixture, engine)
        envelope = _envelope(fixture)
        assert fixture.run.started_at is not None
        acquisition_time = max(_database_now(engine), fixture.run.started_at) + timedelta(
            seconds=1
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    lambda _: service.classify_and_acquire(envelope, now=acquisition_time),
                    (0, 1),
                )
            )
        assert sorted(result.kind.value for result in results) == [
            CoordinatedDeliveryResultKind.ACQUIRED.value,
            CoordinatedDeliveryResultKind.IN_PROGRESS.value,
        ]
        with Session(engine) as session:
            assert session.scalar(
                select(TaskAttemptRecord).where(TaskAttemptRecord.run_id == fixture.run.id)
            ) is not None
            assert len(
                session.scalars(
                    select(TaskAttemptRecord).where(TaskAttemptRecord.run_id == fixture.run.id)
                ).all()
            ) == 1
            assert (
                session.scalar(
                    select(InboxMessageRecord).where(
                        InboxMessageRecord.tenant_id == fixture.tenant_id,
                        InboxMessageRecord.message_id == envelope.message_id,
                    )
                )
                is None
            )
    finally:
        _cleanup(engine, fixture)
        engine.dispose()


def test_postgres_expired_prepared_replacement_keeps_execution_and_snapshot_identity():
    engine = create_engine(os.environ.get("AGENTMESH_DATABASE_URL", get_settings().database_url))
    fixture = _dispatch_fixture(engine)
    try:
        from tests.integration.test_coordinated_runtime_dispatch_postgres import _prepare

        prepared = _prepare(fixture, assignment=_recoverable_assignment(fixture))
        assert prepared.execution_id is not None
        acquisition_time = _database_now(engine) + timedelta(seconds=1)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE task_attempts SET lease_expires_at = :expired "
                    "WHERE id = :attempt_id"
                ),
                {
                    "expired": acquisition_time - timedelta(minutes=1),
                    "attempt_id": fixture.attempt.id,
                },
            )
        with Session(engine) as session:
            execution = session.get(RuntimeExecutionRecord, prepared.execution_id)
            snapshot = session.scalar(
                select(RuntimeAssignmentSnapshotRecord).where(
                    RuntimeAssignmentSnapshotRecord.runtime_execution_id == prepared.execution_id
                )
            )
            assert execution is not None and snapshot is not None
            before = (
                execution.id,
                execution.assignment_id,
                execution.assignment_digest,
                execution.dispatch_key,
                execution.dispatch_digest,
                snapshot.id,
                snapshot.assignment_id,
                snapshot.assignment_digest,
            )

        result = _service(fixture, engine).classify_and_acquire(
            _envelope(fixture), now=acquisition_time
        )
        assert result.kind is CoordinatedDeliveryResultKind.RECOVERED_PRE_BOUNDARY
        assert result.lease is not None
        assert result.lease.fencing_token == fixture.attempt.fencing_token + 1

        with Session(engine) as session:
            execution = session.get(RuntimeExecutionRecord, prepared.execution_id)
            snapshot = session.scalar(
                select(RuntimeAssignmentSnapshotRecord).where(
                    RuntimeAssignmentSnapshotRecord.runtime_execution_id == prepared.execution_id
                )
            )
            attempts = session.scalars(
                select(TaskAttemptRecord)
                .where(TaskAttemptRecord.run_id == fixture.run.id)
                .order_by(TaskAttemptRecord.fencing_token)
            ).all()
            assert execution is not None and snapshot is not None
            assert (
                execution.id,
                execution.assignment_id,
                execution.assignment_digest,
                execution.dispatch_key,
                execution.dispatch_digest,
                snapshot.id,
                snapshot.assignment_id,
                snapshot.assignment_digest,
            ) == before
            assert len(attempts) == 2
            assert attempts[1].fencing_token == attempts[0].fencing_token + 1
    finally:
        _cleanup(engine, fixture)
        engine.dispose()


def test_postgres_expired_crossed_owner_returns_recovery_proof_without_writes():
    engine = create_engine(os.environ.get("AGENTMESH_DATABASE_URL", get_settings().database_url))
    fixture = _dispatch_fixture(engine)
    try:
        from agentmesh.application.coordinated_runtime_dispatch import (
            CoordinatedRuntimeDispatchService,
        )
        from tests.integration.test_coordinated_runtime_dispatch_postgres import _prepare

        prepared = _prepare(fixture)
        assert prepared.execution_id is not None
        crossed = CoordinatedRuntimeDispatchService(
            uow_factory=fixture.factory
        ).cross_runtime_dispatch_boundary(
            tenant_id=fixture.tenant_id,
            task_id=fixture.task.id,
            run_id=fixture.run.id,
            attempt_id=fixture.attempt.id,
            fencing_token=fixture.attempt.fencing_token,
            runtime_execution_id=prepared.execution_id,
            assignment_digest=fixture.assignment.assignment_digest,
            now=fixture.now + timedelta(seconds=1),
        )
        assert crossed.execution_id == prepared.execution_id
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE task_attempts SET lease_expires_at = :expired "
                    "WHERE id = :attempt_id"
                ),
                {
                    "expired": fixture.now - timedelta(minutes=1),
                    "attempt_id": fixture.attempt.id,
                },
            )
        with Session(engine) as session:
            before_attempt = session.get(TaskAttemptRecord, fixture.attempt.id)
            before_execution = session.get(RuntimeExecutionRecord, prepared.execution_id)
            before_inbox = session.scalar(
                select(InboxMessageRecord).where(
                    InboxMessageRecord.tenant_id == fixture.tenant_id,
                    InboxMessageRecord.message_id == _envelope(fixture).message_id,
                )
            )
            assert before_attempt is not None and before_execution is not None
            before = (
                before_attempt.status,
                before_attempt.fencing_token,
                before_execution.phase,
                before_execution.version,
                before_execution.current_owner_attempt_id,
                before_execution.current_fencing_token,
                before_inbox,
            )
        result = _service(fixture, engine).classify_and_acquire(
            _envelope(fixture), now=fixture.now + timedelta(minutes=2)
        )
        assert result.kind is CoordinatedDeliveryResultKind.RECOVER_CROSSED
        assert result.recovery_crossed_proof is not None
        assert result.recovery_crossed_proof.execution_id == prepared.execution_id
        with Session(engine) as session:
            after_attempt = session.get(TaskAttemptRecord, fixture.attempt.id)
            after_execution = session.get(RuntimeExecutionRecord, prepared.execution_id)
            after_inbox = session.scalar(
                select(InboxMessageRecord).where(
                    InboxMessageRecord.tenant_id == fixture.tenant_id,
                    InboxMessageRecord.message_id == _envelope(fixture).message_id,
                )
            )
            assert after_attempt is not None and after_execution is not None
            assert (
                after_attempt.status,
                after_attempt.fencing_token,
                after_execution.phase,
                after_execution.version,
                after_execution.current_owner_attempt_id,
                after_execution.current_fencing_token,
                after_inbox,
            ) == before
    finally:
        _cleanup(engine, fixture)
        engine.dispose()


def test_postgres_active_drain_aborts_prepared_once_and_consumes_inbox_atomically():
    engine = create_engine(os.environ.get("AGENTMESH_DATABASE_URL", get_settings().database_url))
    fixture = _dispatch_fixture(engine)
    try:
        from tests.integration.test_coordinated_runtime_dispatch_postgres import _prepare

        prepared = _prepare(fixture, assignment=_recoverable_assignment(fixture))
        assert prepared.execution_id is not None
        budget = TaskBudget.create(
            max_attempts=10,
            max_tokens=100,
            token_reservation_per_attempt=10,
            max_cost_micros=1000,
            cost_reservation_micros_per_attempt=100,
        )
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE tasks SET budget = CAST(:budget AS jsonb), reserved_tokens = 10, "
                    "reserved_cost_micros = 100 WHERE id = :task_id"
                ),
                {"budget": json.dumps(budget.to_dict()), "task_id": fixture.task.id},
            )
            connection.execute(
                text(
                    "UPDATE task_attempts SET reserved_tokens = 10, "
                    "reserved_cost_micros = 100 WHERE id = :attempt_id"
                ),
                {"attempt_id": fixture.attempt.id},
            )
        drain = CoordinationRuntimeDrain.start(
            drain_id=uuid4(),
            tenant_id=fixture.tenant_id,
            task_id=fixture.task.id,
            triggering_run_id=fixture.run.id,
            target=CoordinationRuntimeDrainTarget.RUNNING,
            reason="postgres acquisition abort",
            at=fixture.now + timedelta(seconds=1),
        )
        with fixture.factory() as uow:
            uow.coordination_runtime_drains.add(drain)
            uow.commit()
        envelope = _envelope(fixture)
        result = _service(fixture, engine).classify_and_acquire(
            envelope, now=fixture.now + timedelta(minutes=1)
        )
        assert result.kind is CoordinatedDeliveryResultKind.BLOCKED_BY_DRAIN
        replay = _service(fixture, engine).classify_and_acquire(
            envelope, now=fixture.now + timedelta(minutes=2)
        )
        assert replay.kind is CoordinatedDeliveryResultKind.REPLAY_PROCESSED
        with Session(engine) as session:
            execution = session.get(RuntimeExecutionRecord, prepared.execution_id)
            attempt = session.get(TaskAttemptRecord, fixture.attempt.id)
            outbox = session.scalars(
                select(OutboxEventRecord).where(
                    OutboxEventRecord.tenant_id == fixture.tenant_id,
                    OutboxEventRecord.topic == "agentmesh.runtime.dispatch.aborted",
                )
            ).all()
            inbox = session.get(
                InboxMessageRecord,
                (fixture.tenant_id, "acquisition-pg-consumer", envelope.message_id),
            )
            task = session.execute(
                text(
                    "SELECT reserved_tokens, reserved_cost_micros FROM tasks WHERE id = :id"
                ),
                {"id": fixture.task.id},
            ).one()
            run = session.execute(
                text("SELECT status, runtime_execution_id FROM task_runs WHERE id = :id"),
                {"id": fixture.run.id},
            ).one()
            subtask = session.execute(
                text("SELECT status, current_run_id FROM subtasks WHERE id = :id"),
                {"id": fixture.run.subtask_id},
            ).one()
            assert execution is not None and execution.phase == "CANCELED"
            assert attempt is not None and attempt.status == "CANCELED"
            assert len(outbox) == 1
            assert inbox is not None
            assert tuple(task) == (0, 0)
            assert tuple(run) == ("CANCELED", prepared.execution_id)
            assert tuple(subtask) == ("READY", None)
    finally:
        _cleanup(engine, fixture)
        engine.dispose()
