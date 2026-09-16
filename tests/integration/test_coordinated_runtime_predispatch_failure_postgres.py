"""Real PostgreSQL qualification for coordinated pre-dispatch failure."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from agentmesh.application.coordinated_runtime_barrier import (
    CoordinatedRuntimeBarrierApplier,
)
from agentmesh.application.coordinated_runtime_dispatch import (
    CoordinatedRuntimeDispatchService,
)
from agentmesh.application.coordinated_runtime_predispatch_failure import (
    CoordinatedPredispatchFailureKind,
    CoordinatedRuntimePredispatchFailureService,
)
from agentmesh.config import get_settings
from agentmesh.domain.errors import RuntimeExecutionConflict
from agentmesh.infrastructure.postgres.models import (
    CoordinationRuntimeDrainRecord,
    InboxMessageRecord,
    OutboxEventRecord,
    RuntimeExecutionRecord,
    RuntimeObservationRecord,
    SubtaskRecord,
    TaskAttemptRecord,
    TaskRecord,
    TaskRunRecord,
)
from tests.integration.test_coordinated_runtime_dispatch_postgres import (
    _cleanup as _dispatch_cleanup,
)
from tests.integration.test_coordinated_runtime_dispatch_postgres import (
    _fixture as _dispatch_fixture,
)
from tests.integration.test_coordinated_runtime_dispatch_postgres import _prepare

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("AGENTMESH_RUN_POSTGRES_TESTS") != "1",
        reason="set AGENTMESH_RUN_POSTGRES_TESTS=1 to run pre-dispatch failure tests",
    ),
]

CONSUMER = "predispatch-failure-pg"
REASON = "runtime.assignment_invalid"


def _engine():
    return create_engine(os.environ.get("AGENTMESH_DATABASE_URL", get_settings().database_url))


def _service(fixture, *, barrier_applier=None):
    return CoordinatedRuntimePredispatchFailureService(
        uow_factory=fixture.factory,
        barrier_applier=barrier_applier,
    )


def _envelope(fixture):
    from agentmesh.domain.messaging import MessageEnvelope

    return MessageEnvelope.run_requested(
        tenant_id=fixture.tenant_id,
        task_id=fixture.task.id,
        run_id=fixture.run.id,
        at=fixture.now,
    )


def _fail(service, fixture, envelope, *, at=None):
    return service.fail_delivery(
        fixture.tenant_id,
        fixture.task.id,
        fixture.run.id,
        fixture.attempt.id,
        fixture.attempt.fencing_token,
        CONSUMER,
        envelope,
        REASON,
        uuid4(),
        at or fixture.now + timedelta(seconds=2),
    )


def _cleanup(engine, fixture):
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM inbox_messages WHERE tenant_id = :tenant_id"),
            {"tenant_id": fixture.tenant_id},
        )
        connection.execute(
            text("DELETE FROM outbox_events WHERE tenant_id = :tenant_id"),
            {"tenant_id": fixture.tenant_id},
        )
    _dispatch_cleanup(engine, fixture)


def test_postgres_first_failure_and_concurrent_exact_replay_are_atomic():
    engine = _engine()
    fixture = _dispatch_fixture(engine)
    try:
        envelope = _envelope(fixture)
        service = _service(fixture)
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: _fail(service, fixture, envelope), (0, 1)))
        assert sorted(value.kind.value for value in results) == ["FAILED", "REPLAY"]

        with Session(engine) as session:
            task = session.get(TaskRecord, fixture.task.id)
            run = session.get(TaskRunRecord, fixture.run.id)
            subtask = session.get(SubtaskRecord, fixture.run.subtask_id)
            attempt = session.get(TaskAttemptRecord, fixture.attempt.id)
            assert task is not None and task.status == "FAILED" and task.error == REASON
            assert run is not None and run.status == "FAILED" and run.error == REASON
            assert subtask is not None and subtask.status == "FAILED" and subtask.error == REASON
            assert attempt is not None and attempt.status == "FAILED" and attempt.error == REASON
            assert session.scalar(
                select(func.count()).select_from(InboxMessageRecord).where(
                    InboxMessageRecord.tenant_id == fixture.tenant_id,
                    InboxMessageRecord.consumer_name == CONSUMER,
                    InboxMessageRecord.message_id == envelope.message_id,
                )
            ) == 1
            assert session.scalar(
                select(func.count()).select_from(OutboxEventRecord).where(
                    OutboxEventRecord.tenant_id == fixture.tenant_id,
                    OutboxEventRecord.topic == "agentmesh.runtime.predispatch-failed",
                )
            ) == 1
            drains = session.scalars(
                select(CoordinationRuntimeDrainRecord).where(
                    CoordinationRuntimeDrainRecord.task_id == fixture.task.id
                )
            ).all()
            assert len(drains) == 1
            assert (drains[0].target, drains[0].status) == ("FAILED", "COMPLETE")
    finally:
        _cleanup(engine, fixture)
        engine.dispose()


def test_postgres_crossed_target_conflicts_without_writes():
    engine = _engine()
    fixture = _dispatch_fixture(engine)
    try:
        prepared = _prepare(fixture)
        CoordinatedRuntimeDispatchService(
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
        envelope = _envelope(fixture)
        with pytest.raises(RuntimeExecutionConflict, match="crossed"):
            _fail(_service(fixture), fixture, envelope)
        with Session(engine) as session:
            assert session.scalar(
                select(func.count()).select_from(InboxMessageRecord).where(
                    InboxMessageRecord.tenant_id == fixture.tenant_id
                )
            ) == 0
            assert session.scalar(
                select(func.count()).select_from(OutboxEventRecord).where(
                    OutboxEventRecord.tenant_id == fixture.tenant_id,
                    OutboxEventRecord.topic == "agentmesh.runtime.predispatch-failed",
                )
            ) == 0
            assert session.get(TaskAttemptRecord, fixture.attempt.id).status == "RUNNING"
    finally:
        _cleanup(engine, fixture)
        engine.dispose()


def test_postgres_prepared_failure_aborts_without_provider_observation():
    engine = _engine()
    fixture = _dispatch_fixture(engine)
    try:
        prepared = _prepare(fixture)
        envelope = _envelope(fixture)
        result = _fail(_service(fixture), fixture, envelope)
        assert result.kind is CoordinatedPredispatchFailureKind.FAILED
        with Session(engine) as session:
            execution = session.get(RuntimeExecutionRecord, prepared.execution_id)
            assert execution is not None and execution.phase == "CANCELED"
            assert session.scalar(
                select(func.count()).select_from(RuntimeObservationRecord).where(
                    RuntimeObservationRecord.runtime_execution_id == prepared.execution_id
                )
            ) == 0
            topics = session.scalars(
                select(OutboxEventRecord.topic).where(
                    OutboxEventRecord.tenant_id == fixture.tenant_id
                )
            ).all()
            assert sorted(topics) == [
                "agentmesh.runtime.dispatch.aborted",
                "agentmesh.runtime.predispatch-failed",
            ]
    finally:
        _cleanup(engine, fixture)
        engine.dispose()


class _FailAfterBarrier(CoordinatedRuntimeBarrierApplier):
    def apply_in_uow(self, *args, **kwargs):
        super().apply_in_uow(*args, **kwargs)
        raise RuntimeError("injected failure after barrier")


def test_postgres_failure_after_barrier_rolls_back_every_writer():
    engine = _engine()
    fixture = _dispatch_fixture(engine)
    try:
        envelope = _envelope(fixture)
        with pytest.raises(RuntimeError, match="injected failure"):
            _fail(
                _service(fixture, barrier_applier=_FailAfterBarrier()),
                fixture,
                envelope,
            )
        with Session(engine) as session:
            assert session.get(TaskRecord, fixture.task.id).status == "RUNNING"
            assert session.get(TaskRunRecord, fixture.run.id).status == "RUNNING"
            assert session.get(SubtaskRecord, fixture.run.subtask_id).status == "RUNNING"
            assert session.get(TaskAttemptRecord, fixture.attempt.id).status == "RUNNING"
            assert session.scalar(
                select(func.count()).select_from(CoordinationRuntimeDrainRecord).where(
                    CoordinationRuntimeDrainRecord.task_id == fixture.task.id
                )
            ) == 0
            assert session.scalar(
                select(func.count()).select_from(InboxMessageRecord).where(
                    InboxMessageRecord.tenant_id == fixture.tenant_id
                )
            ) == 0
            assert session.scalar(
                select(func.count()).select_from(OutboxEventRecord).where(
                    OutboxEventRecord.tenant_id == fixture.tenant_id,
                    OutboxEventRecord.topic == "agentmesh.runtime.predispatch-failed",
                )
            ) == 0
    finally:
        _cleanup(engine, fixture)
        engine.dispose()
