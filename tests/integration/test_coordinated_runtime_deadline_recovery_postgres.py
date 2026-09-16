"""PostgreSQL qualification for coordinated lifecycle deadline recovery.

The provider probe deliberately uses a second SQLAlchemy connection.  It can
therefore prove that the consumer committed its claim and closed discovery
transactions before calling ``adapter.inspect``; a Task lock held by the
consumer would make the ``FOR UPDATE NOWAIT`` probe fail.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta, timezone
from threading import Barrier
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text

from agentmesh.application.coordinated_runtime_convergence import (
    CoordinatedRuntimeConvergenceService,
)
from agentmesh.application.coordinated_runtime_deadline_consumer import (
    CoordinatedRuntimeDeadlineConsumer,
)
from agentmesh.application.coordinated_runtime_deadline_recovery import (
    CoordinatedRuntimeDeadlineRecoveryService,
)
from agentmesh.application.coordinated_runtime_dispatch import (
    CoordinatedDispatchReceiptV1,
    CoordinatedRuntimeBindReceiptKind,
    CoordinatedRuntimePrepareKind,
)
from agentmesh.application.coordinated_runtime_unknown import (
    CoordinatedRuntimeUnknownOutcomeService,
    CoordinatedUnknownOutcomeKind,
)
from agentmesh.config import get_settings
from agentmesh.domain.errors import RuntimeExecutionConflict
from agentmesh.domain.runtime_execution import (
    RuntimeLifecycleIntent,
    RuntimeLifecycleOperation,
    RuntimeLifecycleStatus,
)
from agentmesh.features import FeatureGateSet
from agentmesh.runtime_sdk import RuntimeExecutionHandle, RuntimeObservation, RuntimePhase
from agentmesh.runtime_sdk.canonical import canonical_digest
from tests.integration.test_coordinated_runtime_dispatch_postgres import (
    _cleanup as _dispatch_cleanup,
)
from tests.integration.test_coordinated_runtime_dispatch_postgres import (
    _cross,
    _fixture,
    _lease_for,
    _prepare,
)
from tests.integration.test_coordinated_runtime_dispatch_postgres import (
    _service as _dispatch_service,
)

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("AGENTMESH_RUN_POSTGRES_TESTS") != "1",
        reason="set AGENTMESH_RUN_POSTGRES_TESTS=1 to run deadline recovery PostgreSQL tests",
    ),
]

UTC = timezone.utc
_GATES = FeatureGateSet.from_config("full", "managed_agent_runtime=true")


class _InspectProbe:
    def __init__(
        self,
        engine,
        *,
        task_id,
        execution,
        observation=None,
        error: Exception | None = None,
        entered: Barrier | None = None,
        release: Barrier | None = None,
    ) -> None:
        self.engine = engine
        self.task_id = task_id
        self.execution = execution
        self.observation = observation
        self.error = error
        self.entered = entered
        self.release = release
        self.calls = 0
        self.lock_probe_succeeded = False

    def inspect(self, handle):
        self.calls += 1
        with self.engine.begin() as connection:
            # A live aggregate-locking UoW would make this fail immediately.
            connection.execute(
                text("SELECT id FROM tasks WHERE id = :task_id FOR UPDATE NOWAIT"),
                {"task_id": self.task_id},
            ).one()
            self.lock_probe_succeeded = True
        if self.entered is not None:
            self.entered.wait(timeout=10)
        if self.release is not None:
            self.release.wait(timeout=10)
        if self.error is not None:
            raise self.error
        return self.observation


def _bind_handle(fixture, execution_id, *, at):
    lease = _lease_for(fixture)
    handle = RuntimeExecutionHandle(
        runtime_execution_id=str(execution_id),
        runtime_version_id=str(fixture.run.runtime_version_id),
        provider_execution_ref=f"provider://deadline/{uuid4().hex}",
        assignment_id=fixture.assignment.assignment_id,
        assignment_digest=fixture.assignment.assignment_digest or "",
        created_at=at,
    )
    receipt = CoordinatedDispatchReceiptV1(
        schema_version=1,
        dispatch_digest=canonical_digest(
            {
                "execution_id": str(execution_id),
                "dispatch_key": f"runtime-dispatch:{fixture.tenant_id}:{execution_id}",
                "assignment_digest": fixture.assignment.assignment_digest,
            }
        ),
        assignment_digest=fixture.assignment.assignment_digest or "",
        runtime_execution_id=execution_id,
        assignment_id=UUID(fixture.assignment.assignment_id),
        handle=handle,
    )
    result = _dispatch_service(fixture).bind_dispatch_receipt(
        lease=lease, receipt=receipt, now=at
    )
    assert result.kind is CoordinatedRuntimeBindReceiptKind.BOUND
    return handle


def _deadline_fixture(engine, *, bind_handle=True):
    fixture = _fixture(engine)
    prepared = _prepare(fixture)
    assert prepared.kind is CoordinatedRuntimePrepareKind.PREPARED
    crossed = _cross(fixture, prepared)
    at = fixture.now + timedelta(seconds=10)
    handle = _bind_handle(fixture, crossed.execution_id, at=at) if bind_handle else None
    intent = RuntimeLifecycleIntent(
        id=uuid4(),
        tenant_id=fixture.tenant_id,
        runtime_execution_id=crossed.execution_id,
        operation_id=f"runtime-cancel:{crossed.execution_id}:v1",
        operation=RuntimeLifecycleOperation.CANCEL,
        intent_digest="d" * 64,
        status=RuntimeLifecycleStatus.REQUESTED,
        deadline=at - timedelta(seconds=1),
        receipt_summary=None,
        version=1,
        created_at=at - timedelta(seconds=2),
        updated_at=at - timedelta(seconds=1),
        next_attempt_at=None,
    )
    with fixture.factory() as uow:
        uow.runtimes.add_lifecycle_operation(intent)
        uow.commit()
    return fixture, crossed.execution_id, at, handle


def _observation(execution, *, phase, observed_at):
    return RuntimeObservation(
        observation_id=str(uuid4()),
        runtime_execution_id=str(execution.id),
        assignment_id=str(execution.assignment_id),
        assignment_digest=execution.assignment_digest,
        phase=phase,
        observed_at=observed_at,
        provider_event_id=f"deadline-pg-{uuid4().hex}",
        provider_sequence=2,
        output={"ok": True} if phase is RuntimePhase.SUCCEEDED else None,
    )


def _services(fixture):
    convergence = CoordinatedRuntimeConvergenceService(
        uow_factory=fixture.factory,
        coordinated_scheduler=SimpleNamespace(schedule=lambda *_args, **_kwargs: []),
        cancel_deadline_window=timedelta(minutes=5),
    )
    unknown = CoordinatedRuntimeUnknownOutcomeService(
        uow_factory=fixture.factory,
        cancel_deadline_window=timedelta(minutes=5),
    )
    recovery = CoordinatedRuntimeDeadlineRecoveryService(
        uow_factory=fixture.factory,
        convergence_service=convergence,
        unknown_service=unknown,
    )
    return recovery


def _consumer(fixture, execution_id, recovery, adapter, *, lease=timedelta(seconds=30), clock=None):
    return CoordinatedRuntimeDeadlineConsumer(
        uow_factory=fixture.factory,
        tenant_id=fixture.tenant_id,
        feature_gates=_GATES,
        adapter=adapter,
        recovery_service=recovery,
        claim_lease=lease,
        clock=clock,
    )


def _cleanup(engine, fixture):
    with engine.begin() as connection:
        connection.execute(
            text(
                "DELETE FROM runtime_lifecycle_operations WHERE runtime_execution_id IN "
                "(SELECT id FROM runtime_executions WHERE run_id IN "
                "(SELECT id FROM task_runs WHERE task_id = :task_id))"
            ),
            {"task_id": fixture.task.id},
        )
    _dispatch_cleanup(engine, fixture)


def _lifecycle(engine, fixture, execution_id):
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT status, claim_token, claim_acquired_at, claim_expires_at, version, "
                "last_error_code FROM runtime_lifecycle_operations "
                "WHERE runtime_execution_id = :execution_id"
            ),
            {"execution_id": execution_id},
        ).one()
        return tuple(row)


def test_postgres_deadline_known_terminal_commits_claim_before_inspect_and_clears_claim():
    engine = create_engine(get_settings().database_url, pool_size=4, max_overflow=0)
    fixture, execution_id, at, _handle = _deadline_fixture(engine)
    try:
        with fixture.factory() as uow:
            execution = uow.runtimes.get_execution(execution_id, tenant_id=fixture.tenant_id)
        observation = _observation(execution, phase=RuntimePhase.SUCCEEDED, observed_at=at)
        adapter = _InspectProbe(
            engine,
            task_id=fixture.task.id,
            execution=execution,
            observation=observation,
        )
        result = _consumer(fixture, execution_id, _services(fixture), adapter).process_deadline(
            execution_id, operation_id=f"runtime-cancel:{execution_id}:v1", now=at
        )
        assert result.finalized is True
        assert result.inspection_attempted is True
        assert adapter.calls == 1
        assert adapter.lock_probe_succeeded is True
        status, claim, acquired, expires, version, error = _lifecycle(
            engine, fixture, execution_id
        )
        assert status == RuntimeLifecycleStatus.REQUESTED.value
        assert claim is None and acquired is None and expires is None
        assert error is None
        assert version == 3
    finally:
        _cleanup(engine, fixture)
        engine.dispose()


@pytest.mark.parametrize("mode", ["missing", "inspect_error"])
def test_postgres_deadline_missing_or_failed_inspection_parks_unknown(mode):
    engine = create_engine(get_settings().database_url, pool_size=4, max_overflow=0)
    fixture, execution_id, at, _handle = _deadline_fixture(engine, bind_handle=mode != "missing")
    try:
        with fixture.factory() as uow:
            execution = uow.runtimes.get_execution(execution_id, tenant_id=fixture.tenant_id)
        adapter = _InspectProbe(
            engine,
            task_id=fixture.task.id,
            execution=execution,
            error=RuntimeError("provider inspection failed") if mode == "inspect_error" else None,
        )
        result = _consumer(fixture, execution_id, _services(fixture), adapter).process_deadline(
            execution_id, operation_id=f"runtime-cancel:{execution_id}:v1", now=at
        )
        assert result.finalized is True
        assert result.convergence_result.kind is CoordinatedUnknownOutcomeKind.PARKED
        assert adapter.calls == (0 if mode == "missing" else 1)
        status, claim, acquired, expires, _version, error = _lifecycle(
            engine, fixture, execution_id
        )
        assert status == RuntimeLifecycleStatus.EXPIRED.value
        assert claim is None and acquired is None and expires is None
        assert error == "runtime.cancel_outcome_unknown"
    finally:
        _cleanup(engine, fixture)
        engine.dispose()


def test_postgres_duplicate_claims_do_not_inspect_or_converge_twice():
    engine = create_engine(get_settings().database_url, pool_size=5, max_overflow=0)
    fixture, execution_id, at, _handle = _deadline_fixture(engine)
    entered = Barrier(2)
    release = Barrier(2)
    try:
        with fixture.factory() as uow:
            execution = uow.runtimes.get_execution(execution_id, tenant_id=fixture.tenant_id)
        observation = _observation(execution, phase=RuntimePhase.SUCCEEDED, observed_at=at)
        adapters = [
            _InspectProbe(
                engine,
                task_id=fixture.task.id,
                execution=execution,
                observation=observation,
                entered=entered,
                release=release,
            ),
            _InspectProbe(engine, task_id=fixture.task.id, execution=execution),
        ]
        consumers = [
            _consumer(fixture, execution_id, _services(fixture), adapters[0]),
            _consumer(fixture, execution_id, _services(fixture), adapters[1]),
        ]
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(
                consumers[0].process_deadline,
                execution_id,
                operation_id=f"runtime-cancel:{execution_id}:v1",
                now=at,
            )
            entered.wait(timeout=10)
            second = pool.submit(
                consumers[1].process_deadline,
                execution_id,
                operation_id=f"runtime-cancel:{execution_id}:v1",
                now=at,
            )
            second_result = second.result(timeout=10)
            release.wait(timeout=10)
            first_result = first.result(timeout=10)
        assert first_result.finalized is True
        assert second_result.operation is None and second_result.finalized is False
        assert adapters[0].calls == 1
        assert adapters[1].calls == 0
    finally:
        _cleanup(engine, fixture)
        engine.dispose()


def test_postgres_deadline_stale_lease_fails_closed_without_second_convergence():
    engine = create_engine(get_settings().database_url, pool_size=4, max_overflow=0)
    fixture, execution_id, at, _handle = _deadline_fixture(engine)
    try:
        with fixture.factory() as uow:
            execution = uow.runtimes.get_execution(execution_id, tenant_id=fixture.tenant_id)
        observation = _observation(execution, phase=RuntimePhase.SUCCEEDED, observed_at=at)
        timestamps = iter((at, at + timedelta(seconds=2)))
        adapter = _InspectProbe(
            engine,
            task_id=fixture.task.id,
            execution=execution,
            observation=observation,
        )
        consumer = _consumer(
            fixture,
            execution_id,
            _services(fixture),
            adapter,
            lease=timedelta(seconds=1),
            clock=lambda: next(timestamps),
        )
        with pytest.raises(RuntimeExecutionConflict, match="stale"):
            consumer.process_deadline(
                execution_id, operation_id=f"runtime-cancel:{execution_id}:v1"
            )
        status, claim, acquired, expires, _version, _error = _lifecycle(
            engine, fixture, execution_id
        )
        assert status == RuntimeLifecycleStatus.REQUESTED.value
        assert claim is not None and acquired is not None and expires is not None
        assert adapter.calls == 1
    finally:
        _cleanup(engine, fixture)
        engine.dispose()
