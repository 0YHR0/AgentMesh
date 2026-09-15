"""Real PostgreSQL qualification for managed Supervisor prepare and dispatch CAS."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from agentmesh.application.coordinated_runtime_dispatch import (
    CoordinatedRuntimeDispatchKind,
    CoordinatedRuntimeDispatchService,
    CoordinatedRuntimePrepareKind,
)
from agentmesh.domain.coordination import (
    CoordinationRuntimeDrain,
    CoordinationRuntimeDrainTarget,
)
from agentmesh.infrastructure.postgres.models import (
    RuntimeAssignmentSnapshotRecord,
    RuntimeExecutionRecord,
    TaskRunRecord,
)
from agentmesh.infrastructure.postgres.runtime_repositories import (
    SqlAlchemyRuntimeRepository,
)
from tests.integration.test_coordinated_runtime_convergence_postgres import _cleanup
from tests.integration.test_coordinated_runtime_unknown_postgres import _supervisor_fixture

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("AGENTMESH_RUN_POSTGRES_TESTS") != "1",
        reason="set AGENTMESH_RUN_POSTGRES_TESTS=1 to run Supervisor dispatch tests",
    ),
]


def _fixture(engine):
    fixture, execution, now = _supervisor_fixture(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "DELETE FROM runtime_assignment_snapshots "
                "WHERE runtime_execution_id = :execution_id"
            ),
            {"execution_id": execution.id},
        )
        connection.execute(
            text("UPDATE task_runs SET runtime_execution_id = NULL WHERE id = :run_id"),
            {"run_id": fixture.run.id},
        )
        connection.execute(
            text("DELETE FROM runtime_executions WHERE id = :execution_id"),
            {"execution_id": execution.id},
        )
    assignment = replace(
        fixture.assignment,
        assignment_id=str(execution.assignment_id),
        run_id=str(fixture.run.id),
        run_role="SUPERVISOR",
        structured_input={"source": "postgres", "role": "supervisor"},
        correlation_ids={
            "runtime_execution_id": str(fixture.run.runtime_execution_intent_id)
        },
        assignment_digest=None,
    )
    values = vars(fixture).copy()
    values.update(assignment=assignment, now=now + timedelta(seconds=1))
    return SimpleNamespace(**values)


def _service(fixture):
    return CoordinatedRuntimeDispatchService(uow_factory=fixture.factory)


def _prepare(fixture, *, now=None):
    return _service(fixture).prepare_runtime_assignment(
        tenant_id=fixture.tenant_id,
        task_id=fixture.task.id,
        run_id=fixture.run.id,
        attempt_id=fixture.attempt.id,
        fencing_token=fixture.attempt.fencing_token,
        assignment=fixture.assignment,
        now=now or fixture.now,
    )


def _cross(fixture, *, now=None):
    return _service(fixture).cross_runtime_dispatch_boundary(
        tenant_id=fixture.tenant_id,
        task_id=fixture.task.id,
        run_id=fixture.run.id,
        attempt_id=fixture.attempt.id,
        fencing_token=fixture.attempt.fencing_token,
        runtime_execution_id=fixture.run.runtime_execution_intent_id,
        assignment_digest=fixture.assignment.assignment_digest or "",
        now=now or fixture.now,
    )


def test_postgres_supervisor_prepare_and_replay_are_immutable():
    engine = create_engine(os.environ["AGENTMESH_DATABASE_URL"])
    fixture = _fixture(engine)
    try:
        prepared = _prepare(fixture)
        assert prepared.kind is CoordinatedRuntimePrepareKind.PREPARED
        with Session(engine) as session:
            execution = session.get(RuntimeExecutionRecord, prepared.execution_id)
            run = session.get(TaskRunRecord, fixture.run.id)
            snapshot = session.scalar(
                select(RuntimeAssignmentSnapshotRecord).where(
                    RuntimeAssignmentSnapshotRecord.runtime_execution_id
                    == prepared.execution_id
                )
            )
            assert execution is not None and run is not None and snapshot is not None
            before = (
                execution.version,
                execution.updated_at,
                execution.assignment_digest,
                run.runtime_execution_id,
                snapshot.canonical_payload,
            )
        replay = _prepare(fixture, now=fixture.now + timedelta(minutes=1))
        assert replay.kind is CoordinatedRuntimePrepareKind.REPLAY
        with Session(engine) as session:
            execution = session.get(RuntimeExecutionRecord, prepared.execution_id)
            run = session.get(TaskRunRecord, fixture.run.id)
            snapshot = session.scalar(
                select(RuntimeAssignmentSnapshotRecord).where(
                    RuntimeAssignmentSnapshotRecord.runtime_execution_id
                    == prepared.execution_id
                )
            )
            assert before == (
                execution.version,
                execution.updated_at,
                execution.assignment_digest,
                run.runtime_execution_id,
                snapshot.canonical_payload,
            )
    finally:
        _cleanup(engine, fixture)
        engine.dispose()


def test_postgres_supervisor_dispatch_authorizes_once_and_replays():
    engine = create_engine(os.environ["AGENTMESH_DATABASE_URL"])
    fixture = _fixture(engine)
    try:
        _prepare(fixture)
        first = _cross(fixture)
        second = _cross(fixture, now=fixture.now + timedelta(minutes=1))
        assert first.kind is CoordinatedRuntimeDispatchKind.DISPATCH_AUTHORIZED
        assert second.kind is CoordinatedRuntimeDispatchKind.ALREADY_CROSSED
        with Session(engine) as session:
            execution = session.get(RuntimeExecutionRecord, first.execution_id)
            assert execution is not None and execution.phase == "DISPATCHING"
    finally:
        _cleanup(engine, fixture)
        engine.dispose()


def test_postgres_concurrent_supervisor_dispatch_has_one_authorization():
    engine = create_engine(
        os.environ["AGENTMESH_DATABASE_URL"], pool_size=4, max_overflow=0
    )
    fixture = _fixture(engine)
    try:
        _prepare(fixture)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: _cross(fixture), range(2)))
        assert sorted(result.kind for result in results) == sorted(
            [
                CoordinatedRuntimeDispatchKind.DISPATCH_AUTHORIZED,
                CoordinatedRuntimeDispatchKind.ALREADY_CROSSED,
            ]
        )
        with Session(engine) as session:
            assert session.scalar(
                select(RuntimeExecutionRecord.phase).where(
                    RuntimeExecutionRecord.id
                    == fixture.run.runtime_execution_intent_id
                )
            ) == "DISPATCHING"
    finally:
        _cleanup(engine, fixture)
        engine.dispose()


def test_postgres_supervisor_prepare_rolls_back_every_projection(monkeypatch):
    engine = create_engine(os.environ["AGENTMESH_DATABASE_URL"])
    fixture = _fixture(engine)
    try:
        def fail_snapshot(self, value):
            raise RuntimeError("injected Supervisor snapshot failure")

        monkeypatch.setattr(
            SqlAlchemyRuntimeRepository, "add_assignment_snapshot", fail_snapshot
        )
        with pytest.raises(RuntimeError, match="injected Supervisor snapshot failure"):
            _prepare(fixture)
        with Session(engine) as session:
            assert (
                session.get(
                    RuntimeExecutionRecord, fixture.run.runtime_execution_intent_id
                )
                is None
            )
            run = session.get(TaskRunRecord, fixture.run.id)
            assert run is not None and run.runtime_execution_id is None
            assert session.scalar(
                select(RuntimeAssignmentSnapshotRecord).where(
                    RuntimeAssignmentSnapshotRecord.runtime_execution_id
                    == fixture.run.runtime_execution_intent_id
                )
            ) is None
    finally:
        _cleanup(engine, fixture)
        engine.dispose()


def test_postgres_supervisor_drain_wins_before_prepare():
    engine = create_engine(os.environ["AGENTMESH_DATABASE_URL"])
    fixture = _fixture(engine)
    try:
        drain = CoordinationRuntimeDrain.start(
            drain_id=uuid4(),
            tenant_id=fixture.tenant_id,
            task_id=fixture.task.id,
            triggering_run_id=fixture.run.id,
            target=CoordinationRuntimeDrainTarget.RUNNING,
            reason="Supervisor prepare qualification",
            at=fixture.now,
        )
        with fixture.factory() as uow:
            uow.coordination_runtime_drains.add(drain)
            uow.commit()
        blocked = _prepare(fixture)
        assert blocked.kind is CoordinatedRuntimePrepareKind.BLOCKED_BY_DRAIN
        with Session(engine) as session:
            assert (
                session.get(
                    RuntimeExecutionRecord, fixture.run.runtime_execution_intent_id
                )
                is None
            )
            run = session.get(TaskRunRecord, fixture.run.id)
            assert run is not None and run.runtime_execution_id is None
    finally:
        _cleanup(engine, fixture)
        engine.dispose()


def test_postgres_supervisor_drain_wins_before_cas():
    engine = create_engine(os.environ["AGENTMESH_DATABASE_URL"])
    fixture = _fixture(engine)
    try:
        prepared = _prepare(fixture)
        drain = CoordinationRuntimeDrain.start(
            drain_id=uuid4(),
            tenant_id=fixture.tenant_id,
            task_id=fixture.task.id,
            triggering_run_id=fixture.run.id,
            target=CoordinationRuntimeDrainTarget.RUNNING,
            reason="Supervisor dispatch qualification",
            at=fixture.now,
        )
        with fixture.factory() as uow:
            uow.coordination_runtime_drains.add(drain)
            uow.commit()
        blocked = _cross(fixture)
        assert blocked.kind is CoordinatedRuntimeDispatchKind.BLOCKED_BY_DRAIN
        with Session(engine) as session:
            execution = session.get(RuntimeExecutionRecord, prepared.execution_id)
            assert execution is not None and execution.phase == "PREPARED"
    finally:
        _cleanup(engine, fixture)
        engine.dispose()
