"""PostgreSQL qualification for the c2f4 coordinated delivery call boundary."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Event
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from agentmesh.application.coordinated_runtime_convergence import (
    CoordinatedRuntimeConvergenceService,
)
from agentmesh.application.coordinated_runtime_delivery import (
    CoordinatedRuntimeDeliveryResultKind,
    DeliveryInProgress,
    stable_dispatch_identity,
)
from agentmesh.application.coordinated_runtime_delivery_acquisition import (
    CoordinatedRuntimeDeliveryAcquisitionService,
)
from agentmesh.application.coordinated_runtime_delivery_service import (
    CoordinatedRuntimeDeliveryService,
)
from agentmesh.application.coordinated_runtime_dispatch import (
    CoordinatedRuntimeDispatchKind,
    CoordinatedRuntimeDispatchService,
)
from agentmesh.application.coordinated_runtime_predispatch_failure import (
    CoordinatedRuntimePredispatchFailureService,
)
from agentmesh.application.coordinated_runtime_unknown import (
    CoordinatedRuntimeUnknownOutcomeService,
)
from agentmesh.application.runtime_work_items import CanonicalWorkItemBuilder
from agentmesh.bootstrap import seed_builtin_registry
from agentmesh.config import get_settings
from agentmesh.features import FeatureGateSet
from agentmesh.infrastructure.postgres.models import (
    InboxMessageRecord,
    RuntimeExecutionRecord,
    RuntimeHandleSnapshotRecord,
    RuntimeObservationRecord,
)
from agentmesh.runtime_sdk import (
    RuntimeExecutionHandle,
    RuntimeObservation,
    RuntimePhase,
    ValidationReport,
)
from agentmesh.runtime_sdk.canonical import thaw_json
from tests.integration.test_coordinated_runtime_convergence_postgres import (
    _real_scheduler,
)
from tests.integration.test_coordinated_runtime_delivery_acquisition_postgres import (
    _envelope,
    _queued_fixture,
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
        reason="set AGENTMESH_RUN_POSTGRES_TESTS=1 to run coordinated delivery PostgreSQL tests",
    ),
]


def _engine():
    settings = get_settings()
    seed_builtin_registry(settings)
    return create_engine(os.environ.get("AGENTMESH_DATABASE_URL", settings.database_url))


class _ManagedExecution:
    def __init__(self, fixture):
        self._fixture = fixture

    def assignment_for_delivery(self, lease, work_item):
        if work_item != lease.work_item:
            raise AssertionError("managed port received a different work item")
        return replace(
            self._fixture.assignment,
            objective=work_item.objective,
            structured_input=thaw_json(work_item.input),
            assignment_digest=None,
            extensions={
                "coordinated_delivery": {
                    "assignment_projection_digest": lease.assignment_projection_digest,
                }
            },
        )

    def bind_delivery_context(self, assignment, lease, work_item):
        if assignment.run_id != str(lease.run_id) or work_item != lease.work_item:
            raise AssertionError("managed delivery context identity changed")


class _ProviderProbe:
    def __init__(self, engine, *, include_handle=False):
        self.engine = engine
        self.include_handle = include_handle
        self.validation_phases = []
        self.dispatch_phases = []
        self.dispatch_calls = 0
        self.dispatch_keys = []
        self.expected_dispatch_keys = []

    def validate(self, assignment):
        with Session(self.engine) as session:
            phase = session.scalar(
                select(RuntimeExecutionRecord.phase).where(
                    RuntimeExecutionRecord.id
                    == assignment.correlation_ids["runtime_execution_id"]
                )
            )
        self.validation_phases.append(phase)
        return ValidationReport(valid=True)

    def dispatch(self, assignment, *, dispatch_key):
        with Session(self.engine) as session:
            phase = session.scalar(
                select(RuntimeExecutionRecord.phase).where(
                    RuntimeExecutionRecord.id
                    == assignment.correlation_ids["runtime_execution_id"]
                )
            )
        self.dispatch_phases.append(phase)
        self.dispatch_calls += 1
        self.dispatch_keys.append(dispatch_key)
        expected_key, _ = stable_dispatch_identity(
            assignment.tenant_id,
            UUID(assignment.correlation_ids["runtime_execution_id"]),
            assignment.assignment_digest,
        )
        assert dispatch_key == expected_key
        self.expected_dispatch_keys.append(expected_key)
        observation = RuntimeObservation(
            observation_id=str(uuid4()),
            runtime_execution_id=assignment.correlation_ids["runtime_execution_id"],
            assignment_id=assignment.assignment_id,
            assignment_digest=assignment.assignment_digest,
            phase=RuntimePhase.SUCCEEDED,
            observed_at=self.now,
            provider_event_id=f"postgres-event-{self.dispatch_calls}",
            output={"provider": "postgres"},
        )
        handle = None
        if self.include_handle:
            handle = RuntimeExecutionHandle(
                runtime_execution_id=assignment.correlation_ids["runtime_execution_id"],
                runtime_version_id=assignment.runtime_version_id,
                provider_execution_ref=f"provider://postgres/{self.dispatch_calls}",
                assignment_id=assignment.assignment_id,
                assignment_digest=assignment.assignment_digest,
                created_at=self.now,
                provider_generation=f"generation-{self.dispatch_calls}",
            )
        return SimpleNamespace(
            dispatch_key=dispatch_key,
            runtime_execution_id=assignment.correlation_ids["runtime_execution_id"],
            assignment_digest=assignment.assignment_digest,
            observation=observation,
            handle=handle,
        )

    def inspect(self, handle):
        raise AssertionError(f"provider inspect was not expected: {handle}")


class _BoundaryGate:
    def __init__(self, delegate, entered, release):
        self._delegate = delegate
        self._entered = entered
        self._release = release

    def prepare_runtime_assignment(self, **kwargs):
        return self._delegate.prepare_runtime_assignment(**kwargs)

    def cross_runtime_dispatch_boundary(self, **kwargs):
        self._entered.set()
        if not self._release.wait(timeout=10):
            raise AssertionError("boundary gate was not released")
        return self._delegate.cross_runtime_dispatch_boundary(**kwargs)

    def bind_dispatch_receipt(self, **kwargs):
        return self._delegate.bind_dispatch_receipt(**kwargs)


def _service(fixture, engine, provider, *, dispatch=None):
    scheduler = _real_scheduler(engine, fixture)
    return CoordinatedRuntimeDeliveryService(
        acquisition_service=CoordinatedRuntimeDeliveryAcquisitionService(
            uow_factory=fixture.factory,
            worker_id="delivery-service-pg-worker",
            consumer_name="delivery-service-pg-consumer",
            lease_duration=timedelta(minutes=5),
            feature_gates=FeatureGateSet.from_config("full"),
            work_item_builder=CanonicalWorkItemBuilder(scheduler),
        ),
        managed_execution_port=_ManagedExecution(fixture),
        adapter=provider,
        dispatch_service=dispatch
        or CoordinatedRuntimeDispatchService(uow_factory=fixture.factory),
        predispatch_failure_service=CoordinatedRuntimePredispatchFailureService(
            uow_factory=fixture.factory
        ),
        convergence_service=CoordinatedRuntimeConvergenceService(
            uow_factory=fixture.factory,
            coordinated_scheduler=scheduler,
            cancel_deadline_window=timedelta(minutes=5),
        ),
        unknown_service=CoordinatedRuntimeUnknownOutcomeService(
            uow_factory=fixture.factory,
            cancel_deadline_window=timedelta(minutes=5),
        ),
        handle_snapshot_reader=lambda _execution_id: None,
        consumer_name="delivery-service-pg-consumer",
        utc_clock=lambda: provider.now,
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


def test_postgres_delivery_service_calls_provider_only_after_authorized_boundary():
    engine = _engine()
    fixture = _queued_fixture(engine)
    provider = _ProviderProbe(engine)
    provider.now = fixture.now + timedelta(minutes=1)
    try:
        result = _service(fixture, engine, provider).process(_envelope(fixture))
        assert result.attempt_id is not None
        assert provider.validation_phases == [None]
        assert provider.dispatch_phases == ["DISPATCHING"]
        assert provider.dispatch_calls == 1
        assert result.kind is CoordinatedRuntimeDeliveryResultKind.PROCESSED
        assert provider.dispatch_keys == provider.expected_dispatch_keys
        assert result.execution_id is not None
        with Session(engine) as session:
            assert session.scalar(
                select(InboxMessageRecord).where(
                    InboxMessageRecord.tenant_id == fixture.tenant_id,
                    InboxMessageRecord.consumer_name == "delivery-service-pg-consumer",
                )
            ) is None
    finally:
        _cleanup(engine, fixture)
        engine.dispose()


def test_postgres_delivery_receipt_binds_handle_before_terminal_finalize():
    engine = _engine()
    fixture = _queued_fixture(engine)
    provider = _ProviderProbe(engine, include_handle=True)
    provider.now = fixture.now + timedelta(minutes=1)
    try:
        result = _service(fixture, engine, provider).process(_envelope(fixture))
        assert result.kind is CoordinatedRuntimeDeliveryResultKind.PROCESSED
        assert provider.dispatch_calls == 1
        with Session(engine) as session:
            execution = session.get(RuntimeExecutionRecord, result.execution_id)
            handles = session.scalars(
                select(RuntimeHandleSnapshotRecord).where(
                    RuntimeHandleSnapshotRecord.runtime_execution_id == result.execution_id
                )
            ).all()
            assert execution is not None and execution.phase == "SUCCEEDED"
            assert len(handles) == 1
            assert session.scalar(
                select(RuntimeObservationRecord).where(
                    RuntimeObservationRecord.runtime_execution_id == result.execution_id
                )
            ) is not None
    finally:
        _cleanup(engine, fixture)
        engine.dispose()


def test_postgres_delivery_service_already_crossed_does_not_redispatch():
    engine = _engine()
    fixture = _dispatch_fixture(engine)
    provider = _ProviderProbe(engine)
    provider.now = fixture.now + timedelta(minutes=1)
    try:
        from tests.integration.test_coordinated_runtime_dispatch_postgres import _prepare

        prepared = _prepare(fixture)
        dispatch = CoordinatedRuntimeDispatchService(uow_factory=fixture.factory)
        crossed = dispatch.cross_runtime_dispatch_boundary(
            tenant_id=fixture.tenant_id,
            task_id=fixture.task.id,
            run_id=fixture.run.id,
            attempt_id=fixture.attempt.id,
            fencing_token=fixture.attempt.fencing_token,
            runtime_execution_id=prepared.execution_id,
            assignment_digest=fixture.assignment.assignment_digest,
            now=fixture.now + timedelta(seconds=1),
        )
        assert crossed.kind is CoordinatedRuntimeDispatchKind.DISPATCH_AUTHORIZED
        replay = dispatch.cross_runtime_dispatch_boundary(
            tenant_id=fixture.tenant_id,
            task_id=fixture.task.id,
            run_id=fixture.run.id,
            attempt_id=fixture.attempt.id,
            fencing_token=fixture.attempt.fencing_token,
            runtime_execution_id=prepared.execution_id,
            assignment_digest=fixture.assignment.assignment_digest,
            now=fixture.now + timedelta(minutes=1),
        )
        assert replay.kind is CoordinatedRuntimeDispatchKind.ALREADY_CROSSED
        with pytest.raises(DeliveryInProgress):
            _service(fixture, engine, provider).process(_envelope(fixture))
        assert provider.dispatch_calls == 0
        assert provider.dispatch_keys == []
    finally:
        _cleanup(engine, fixture)
        engine.dispose()


def test_postgres_delivery_recover_crossed_missing_handle_parks_unknown_without_dispatch():
    engine = _engine()
    fixture = _dispatch_fixture(engine)
    provider = _ProviderProbe(engine)
    provider.now = fixture.now + timedelta(minutes=2)
    try:
        from tests.integration.test_coordinated_runtime_dispatch_postgres import _prepare

        prepared = _prepare(fixture)
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
        assert crossed.kind is CoordinatedRuntimeDispatchKind.DISPATCH_AUTHORIZED
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
        result = _service(fixture, engine, provider).process(_envelope(fixture))
        assert result.kind is CoordinatedRuntimeDeliveryResultKind.PARKED_UNKNOWN
        assert provider.dispatch_calls == 0
        with Session(engine) as session:
            execution = session.get(RuntimeExecutionRecord, prepared.execution_id)
            assert execution is not None and execution.phase == "OUTCOME_UNKNOWN"
            assert session.scalar(
                select(RuntimeObservationRecord).where(
                    RuntimeObservationRecord.runtime_execution_id == prepared.execution_id
                )
            ) is not None
    finally:
        _cleanup(engine, fixture)
        engine.dispose()


def test_postgres_delivery_boundary_drain_race_fails_closed_without_provider_call():
    engine = _engine()
    fixture = _queued_fixture(engine)
    provider = _ProviderProbe(engine)
    provider.now = fixture.now + timedelta(minutes=1)
    try:
        from agentmesh.domain.coordination import (
            CoordinationRuntimeDrain,
            CoordinationRuntimeDrainTarget,
        )
        entered = Event()
        release = Event()
        delegate = CoordinatedRuntimeDispatchService(uow_factory=fixture.factory)
        service = _service(
            fixture,
            engine,
            provider,
            dispatch=_BoundaryGate(delegate, entered, release),
        )
        envelope = _envelope(fixture)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(service.process, envelope)
            assert entered.wait(timeout=10)
            drain = CoordinationRuntimeDrain.start(
                drain_id=uuid4(),
                tenant_id=fixture.tenant_id,
                task_id=fixture.task.id,
                triggering_run_id=fixture.run.id,
                target=CoordinationRuntimeDrainTarget.RUNNING,
                reason="delivery boundary race",
                at=provider.now,
            )
            with fixture.factory() as uow:
                uow.coordination_runtime_drains.add(drain)
                uow.commit()
            release.set()
            result = future.result(timeout=20)
        assert result.kind.value == "BLOCKED_BY_DRAIN"
        assert provider.validation_phases == [None]
        assert provider.dispatch_calls == 0
        with Session(engine) as session:
            assert session.scalar(
                select(InboxMessageRecord).where(
                    InboxMessageRecord.tenant_id == fixture.tenant_id,
                    InboxMessageRecord.consumer_name == "delivery-service-pg-consumer",
                    InboxMessageRecord.message_id == envelope.message_id,
                )
            ) is not None
    finally:
        _cleanup(engine, fixture)
        engine.dispose()
