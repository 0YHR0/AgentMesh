"""Real PostgreSQL qualification for the c2c3 dispatch-boundary CAS."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from agentmesh.application.coordinated_runtime_dispatch import (
    CoordinatedRuntimeDispatchKind,
    CoordinatedRuntimeDispatchService,
)
from agentmesh.config import get_settings
from agentmesh.domain.coordination import CoordinationRuntimeDrain, CoordinationRuntimeDrainTarget
from agentmesh.infrastructure.postgres.models import RuntimeExecutionRecord, TaskRunRecord
from tests.integration.test_coordinated_runtime_dispatch_postgres import (
    _cleanup,
    _fixture,
    _prepare,
)

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("AGENTMESH_RUN_POSTGRES_TESTS") != "1",
        reason="set AGENTMESH_RUN_POSTGRES_TESTS=1 to run dispatch-boundary PostgreSQL tests",
    ),
]


def _cross(fixture, *, now=None):
    return CoordinatedRuntimeDispatchService(
        uow_factory=fixture.factory
    ).cross_runtime_dispatch_boundary(
        tenant_id=fixture.tenant_id,
        task_id=fixture.task.id,
        run_id=fixture.run.id,
        attempt_id=fixture.attempt.id,
        fencing_token=fixture.attempt.fencing_token,
        runtime_execution_id=fixture.run.runtime_execution_intent_id,
        assignment_digest=fixture.assignment.assignment_digest or "",
        now=now or fixture.now,
    )


def _start_drain(fixture) -> None:
    drain = CoordinationRuntimeDrain.start(
        drain_id=uuid4(),
        tenant_id=fixture.tenant_id,
        task_id=fixture.task.id,
        triggering_run_id=fixture.run.id,
        target=CoordinationRuntimeDrainTarget.RUNNING,
        reason="boundary integration drain",
        at=fixture.now,
    )
    with fixture.factory() as uow:
        uow.coordination_runtime_drains.add(drain)
        uow.commit()


def test_postgres_drain_after_prepared_prevents_boundary_cas():
    engine = create_engine(get_settings().database_url)
    fixture = _fixture(engine)
    try:
        prepared = _prepare(fixture)
        _start_drain(fixture)
        result = _cross(fixture)
        assert result.kind is CoordinatedRuntimeDispatchKind.BLOCKED_BY_DRAIN
        with Session(engine) as session:
            execution = session.get(RuntimeExecutionRecord, prepared.execution_id)
            assert execution is not None and execution.phase == "PREPARED"
            assert (
                session.get(TaskRunRecord, fixture.run.id).runtime_execution_id
                == prepared.execution_id
            )
    finally:
        _cleanup(engine, fixture)
        engine.dispose()


def test_postgres_boundary_cas_before_drain_persists_dispatching():
    engine = create_engine(get_settings().database_url)
    fixture = _fixture(engine)
    try:
        _prepare(fixture)
        authorized = _cross(fixture)
        assert authorized.kind is CoordinatedRuntimeDispatchKind.DISPATCH_AUTHORIZED
        _start_drain(fixture)
        replay = _cross(fixture, now=fixture.now + timedelta(minutes=1))
        assert replay.kind is CoordinatedRuntimeDispatchKind.ALREADY_CROSSED
        with Session(engine) as session:
            execution = session.get(RuntimeExecutionRecord, authorized.execution_id)
            assert execution is not None and execution.phase == "DISPATCHING"
    finally:
        _cleanup(engine, fixture)
        engine.dispose()


def test_postgres_concurrent_boundary_cas_has_one_authorization():
    engine = create_engine(get_settings().database_url, pool_size=4, max_overflow=0)
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
                    RuntimeExecutionRecord.id == fixture.run.runtime_execution_intent_id
                )
            ) == "DISPATCHING"
    finally:
        _cleanup(engine, fixture)
        engine.dispose()


def test_postgres_response_loss_never_reauthorizes_boundary():
    engine = create_engine(get_settings().database_url)
    fixture = _fixture(engine)
    try:
        _prepare(fixture)
        first = _cross(fixture)
        second = _cross(fixture, now=fixture.now + timedelta(minutes=1))
        assert first.kind is CoordinatedRuntimeDispatchKind.DISPATCH_AUTHORIZED
        assert second.kind is CoordinatedRuntimeDispatchKind.ALREADY_CROSSED
    finally:
        _cleanup(engine, fixture)
        engine.dispose()
