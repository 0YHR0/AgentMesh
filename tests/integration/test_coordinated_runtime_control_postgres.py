"""Real PostgreSQL qualification for the c.2f6 coordinated cancel command."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from agentmesh.application.coordinated_runtime_cancel_applier import (
    CoordinatedRuntimeCancelApplier,
)
from agentmesh.application.coordinated_runtime_control import CoordinatedCancelKind
from agentmesh.application.coordinated_runtime_control_service import (
    CoordinatedRuntimeControlService,
)
from agentmesh.config import get_settings
from agentmesh.domain.identity import PrincipalContext, PrincipalType, Role
from agentmesh.domain.runtime_execution import RuntimeExecutionPhase
from agentmesh.domain.tasks import TaskStatus
from agentmesh.runtime_sdk import canonical_digest
from tests.integration.test_coordinated_runtime_convergence_postgres import (
    _add_sibling,
    _running_fixture,
)
from tests.integration.test_coordinated_runtime_convergence_postgres import (
    _cleanup as _convergence_cleanup,
)
from tests.integration.test_coordinated_runtime_dispatch_postgres import (
    _cleanup as _dispatch_cleanup,
)
from tests.integration.test_coordinated_runtime_dispatch_postgres import (
    _fixture,
    _prepare,
)

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("AGENTMESH_RUN_POSTGRES_TESTS") != "1",
        reason="set AGENTMESH_RUN_POSTGRES_TESTS=1 to run coordinated control PostgreSQL tests",
    ),
]


def _principal(tenant_id: str) -> PrincipalContext:
    return PrincipalContext(
        principal_id="coordinated-pg-operator",
        tenant_id=tenant_id,
        principal_type=PrincipalType.USER,
        roles=frozenset({Role.OPERATOR}),
        authenticated=True,
        authentication_method="postgres-test",
    )


def _request(fixture, *, key: str | None = None, at=None):
    return {
        "tenant_id": fixture.tenant_id,
        "task_id": fixture.task.id,
        "principal": _principal(fixture.tenant_id),
        "reason": "operator.postgres",
        "idempotency_key": key or f"cancel-pg-{uuid4().hex}",
        "causation_id": uuid4(),
        # The shared fixture deliberately advances several domain timestamps
        # beyond ``fixture.now``. Keep the command clock after every possible
        # setup transition instead of depending on wall-clock scheduling.
        "at": at or fixture.now + timedelta(seconds=30),
    }


def _cleanup(engine, fixture, *, convergence: bool = False) -> None:
    # Cancellation adds tenant-scoped audit and abort/lifecycle events.  Clear
    # those before the shared dispatch fixture removes the Task graph.
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM outbox_events WHERE tenant_id = :tenant_id"),
            {"tenant_id": fixture.tenant_id},
        )
        connection.execute(
            text(
                "DELETE FROM idempotency_records "
                "WHERE scope = :scope"
            ),
            {
                "scope": "coordinated-runtime-cancel:"
                + canonical_digest(
                    {
                        "tenant_id": fixture.tenant_id,
                        "task_id": str(fixture.task.id),
                    }
                )
            },
        )
        # Migration 0053 deliberately prevents a Drain from being deleted
        # while a Subtask still records it as cancellation provenance. The
        # shared fixture cleanup removes Drains before Tasks/Subtasks, so
        # release only this fixture's provenance first.
        connection.execute(
            text(
                "UPDATE subtasks SET cancellation_source = NULL, "
                "canceled_by_drain_id = NULL WHERE task_id = :task_id"
            ),
            {"task_id": fixture.task.id},
        )
    if convergence:
        _convergence_cleanup(engine, fixture)
    else:
        _dispatch_cleanup(engine, fixture)


def test_postgres_cancel_fresh_and_exact_replay_are_audited_once():
    engine = create_engine(get_settings().database_url)
    fixture = _fixture(engine)
    request = _request(fixture, key=f"cancel-pg-{uuid4().hex}")
    try:
        service = CoordinatedRuntimeControlService(uow_factory=fixture.factory)
        first = service.request_cancel(**request)
        replay = service.request_cancel(
            **{**request, "at": request["at"] + timedelta(minutes=1)}
        )

        assert first.kind is CoordinatedCancelKind.APPLIED
        assert first.task_status is TaskStatus.CANCELED
        assert replay.kind is CoordinatedCancelKind.REPLAY
        assert replay.audit_event_id == first.audit_event_id
        with engine.connect() as connection:
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM outbox_events "
                    "WHERE tenant_id = :tenant_id "
                    "AND envelope->>'schema_name' = "
                    "'agentmesh.runtime.coordinated-cancel-requested'"
                ),
                {"tenant_id": fixture.tenant_id},
            ) == 1
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM idempotency_records "
                    "WHERE scope LIKE 'coordinated-runtime-cancel:%' AND key = :key"
                ),
                {"key": request["idempotency_key"]},
            ) == 1
    finally:
        _cleanup(engine, fixture)


def test_postgres_cancel_crossed_active_creates_one_stable_cancel_intent():
    engine = create_engine(get_settings().database_url)
    fixture, execution, now = _running_fixture(engine)
    request = _request(fixture, at=now + timedelta(seconds=1))
    try:
        service = CoordinatedRuntimeControlService(uow_factory=fixture.factory)
        result = service.request_cancel(**request)

        assert result.kind is CoordinatedCancelKind.DRAINING_ACTIVE
        assert result.task_status is TaskStatus.RUNNING
        assert len(result.lifecycle_operation_ids) == 1
        with fixture.factory() as uow:
            persisted = uow.runtimes.get_execution(
                execution.id, tenant_id=fixture.tenant_id, for_update=False
            )
            assert persisted is not None
            assert persisted.phase is RuntimeExecutionPhase.CANCEL_REQUESTED
            operations = uow.runtimes.list_lifecycle_operations(
                execution.id, tenant_id=fixture.tenant_id
            )
            assert len(operations) == 1
    finally:
        _cleanup(engine, fixture, convergence=True)


def test_postgres_cancel_waits_for_reconciliation_evidence():
    engine = create_engine(get_settings().database_url)
    fixture = _fixture(engine)
    _add_sibling(engine, fixture, state="reconciliation")
    request = _request(fixture)
    try:
        result = CoordinatedRuntimeControlService(
            uow_factory=fixture.factory
        ).request_cancel(**request)
        assert result.kind is CoordinatedCancelKind.WAIT_RECONCILIATION
        assert result.task_status is TaskStatus.RUNNING
        assert len(result.lifecycle_operation_ids) == 1
    finally:
        _cleanup(engine, fixture, convergence=True)


def test_postgres_cancel_aborts_prepared_without_provider_lifecycle_call():
    engine = create_engine(get_settings().database_url)
    fixture = _fixture(engine)
    prepared = _prepare(fixture)
    request = _request(fixture)
    try:
        result = CoordinatedRuntimeControlService(
            uow_factory=fixture.factory
        ).request_cancel(**request)
        assert result.kind is CoordinatedCancelKind.APPLIED
        assert result.task_status is TaskStatus.CANCELED
        with fixture.factory() as uow:
            execution = uow.runtimes.get_execution(
                prepared.execution_id, tenant_id=fixture.tenant_id, for_update=False
            )
            assert execution is not None
            assert execution.phase is RuntimeExecutionPhase.CANCELED
            assert uow.runtimes.list_lifecycle_operations(
                prepared.execution_id, tenant_id=fixture.tenant_id
            ) == []
    finally:
        _cleanup(engine, fixture)


def test_postgres_cancel_queued_sibling_is_provider_free():
    engine = create_engine(get_settings().database_url)
    fixture = _fixture(engine)
    _add_sibling(engine, fixture, state="queued")
    request = _request(fixture)
    try:
        result = CoordinatedRuntimeControlService(
            uow_factory=fixture.factory
        ).request_cancel(**request)
        assert result.kind is CoordinatedCancelKind.APPLIED
        assert result.task_status is TaskStatus.CANCELED
        with engine.connect() as connection:
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM runtime_lifecycle_operations "
                    "WHERE runtime_execution_id IN "
                    "(SELECT id FROM runtime_executions WHERE run_id IN "
                    "(SELECT id FROM task_runs WHERE task_id = :task_id))"
                ),
                {"task_id": fixture.task.id},
            ) == 0
    finally:
        _cleanup(engine, fixture)


def test_postgres_cancel_rolls_back_and_concurrent_exact_request_has_one_winner():
    engine = create_engine(get_settings().database_url)
    fixture = _fixture(engine)
    request = _request(fixture)
    concurrent_fixture = _fixture(engine)
    concurrent_request = _request(concurrent_fixture)
    try:
        class _FailOnce:
            def __init__(self):
                self.delegate = None
                self.failed = False

            def apply_in_uow(self, *args, **kwargs):
                if not self.failed:
                    self.failed = True
                    raise RuntimeError("qualification rollback")
                return self.delegate.apply_in_uow(*args, **kwargs)

        failing = _FailOnce()
        service = CoordinatedRuntimeControlService(
            uow_factory=fixture.factory, cancel_applier=failing
        )
        failing.delegate = CoordinatedRuntimeCancelApplier()
        with pytest.raises(RuntimeError, match="qualification rollback"):
            service.request_cancel(**request)
        with engine.connect() as connection:
            assert connection.scalar(
                text("SELECT count(*) FROM coordination_runtime_drains WHERE task_id = :task_id"),
                {"task_id": fixture.task.id},
            ) == 0

        first = service.request_cancel(**request)
        assert first.kind is CoordinatedCancelKind.APPLIED
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda _: CoordinatedRuntimeControlService(
                        uow_factory=concurrent_fixture.factory
                    ).request_cancel(**concurrent_request),
                    range(2),
                )
            )
        assert sorted(result.kind.value for result in results) == sorted(
            [CoordinatedCancelKind.APPLIED.value, CoordinatedCancelKind.REPLAY.value]
        )
    finally:
        _cleanup(engine, fixture)
        _cleanup(engine, concurrent_fixture)
