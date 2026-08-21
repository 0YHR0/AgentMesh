"""Real PostgreSQL evidence for A4.1b.1 managed DIRECT authority."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from agentmesh.application.managed_runtime_execution import ManagedRuntimeExecutionService
from agentmesh.application.quota_services import QuotaPolicyService
from agentmesh.application.runtime_services import RuntimeRegistryService
from agentmesh.application.services import RunExecutionService, TaskApplicationService
from agentmesh.bootstrap import seed_builtin_registry
from agentmesh.config import get_settings
from agentmesh.domain.budgets import BudgetSettlementSource, TaskBudget
from agentmesh.domain.errors import RunLeaseUnavailable
from agentmesh.domain.messaging import MessageEnvelope
from agentmesh.domain.quotas import QuotaScope
from agentmesh.domain.runtime_execution import (
    RuntimeObservationOutcome,
)
from agentmesh.domain.tasks import AttemptStatus, RunStatus, TaskStatus
from agentmesh.features import FeatureGateSet
from agentmesh.infrastructure.postgres.models import (
    InboxMessageRecord,
    OutboxEventRecord,
    QuotaReservationRecord,
    RuntimeExecutionRecord,
    RuntimeObservationRecord,
)
from agentmesh.infrastructure.postgres.uow import SqlAlchemyUnitOfWorkFactory
from agentmesh.infrastructure.runtime.langgraph_adapter import (
    EphemeralRuntimeLifecycleController,
    EphemeralRuntimeStateStore,
    LangGraphManagedAgentRuntime,
)
from agentmesh.runtime_sdk import RuntimeObservation, RuntimePhase

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("AGENTMESH_RUN_POSTGRES_TESTS") != "1",
        reason="set AGENTMESH_RUN_POSTGRES_TESTS=1 to run service integration tests",
    ),
]


class _PoisonLegacyRunner:
    def run(self, *args, **kwargs):
        raise AssertionError("managed authority must never call the legacy runner")


class _DeterministicBackend:
    def __init__(self) -> None:
        self.calls = 0

    def bind(self, assignment, task, run, attempt, work_item) -> None:
        return None

    def execute(self, assignment):
        self.calls += 1
        return RuntimeObservation(
            observation_id=str(uuid4()),
            runtime_execution_id=assignment.correlation_ids["runtime_execution_id"],
            assignment_id=assignment.assignment_id,
            assignment_digest=assignment.assignment_digest,
            phase=RuntimePhase.SUCCEEDED,
            observed_at=datetime.now(timezone.utc),
            provider_event_id="postgres-managed-success",
            output={"managed": "postgres"},
        )


class _FaultAfterEvidenceRegistry(RuntimeRegistryService):
    def record_observation_in_uow(self, uow, **kwargs):
        outcome = super().record_observation_in_uow(uow, **kwargs)
        assert outcome is RuntimeObservationOutcome.APPLIED
        raise RuntimeError("fault after Runtime evidence")


class _StaleParkingRegistry(RuntimeRegistryService):
    def record_observation_in_uow(self, uow, **kwargs):
        kwargs["attempt_id"] = uuid4()
        return super().record_observation_in_uow(uow, **kwargs)


class _PoisonManagedExecution:
    def __init__(self) -> None:
        self.calls = 0

    def execute_authoritative(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("crossed execution must park without redispatch")


def _gates() -> FeatureGateSet:
    return FeatureGateSet.from_config(
        "full",
        "managed_agent_runtime=true,managed_runtime_worker=true,"
        "managed_runtime_direct_cutover=true",
    )


def _fixture(*, lease_duration=timedelta(minutes=5), registry_type=RuntimeRegistryService):
    settings = get_settings()
    seed_builtin_registry(settings)
    engine = create_engine(settings.database_url)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    uow_factory = SqlAlchemyUnitOfWorkFactory(factory)
    registry = registry_type(
        uow_factory=uow_factory,
        tenant_id=settings.tenant_id,
        feature_gates=_gates(),
    )
    tasks = TaskApplicationService(
        uow_factory=uow_factory,
        agent_id=settings.agent_id,
        tenant_id=settings.tenant_id,
        feature_gates=_gates(),
        runtime_registry_service=registry,
    )
    backend = _DeterministicBackend()
    adapter = LangGraphManagedAgentRuntime(
        backend=backend,
        state_store=EphemeralRuntimeStateStore(),
        lifecycle_controller=EphemeralRuntimeLifecycleController(),
    )
    managed = ManagedRuntimeExecutionService(
        registry=registry,
        adapter=adapter,
        assignment_builder=adapter,
    )
    consumer = f"pg-managed-{uuid4().hex}"
    worker = RunExecutionService(
        uow_factory=uow_factory,
        workflow_runner=_PoisonLegacyRunner(),
        managed_execution_service=managed,
        runtime_registry_service=registry,
        worker_id=consumer,
        consumer_name=consumer,
        lease_duration=lease_duration,
        feature_gates=_gates(),
    )
    return engine, factory, registry, tasks, worker, backend, consumer, settings


def _request(tasks, tenant_id: str, *, budget=None):
    task_id = tasks.create_task(
        f"postgres managed {uuid4().hex}", budget=budget
    ).task.id
    run = tasks.request_run(task_id).runs[0]
    envelope = MessageEnvelope.run_requested(
        tenant_id=tenant_id, task_id=task_id, run_id=run.id
    )
    return task_id, run, envelope


def test_postgres_managed_authoritative_success_is_atomic_and_replay_safe() -> None:
    engine, factory, _registry, tasks, worker, backend, consumer, settings = _fixture()
    try:
        task_id, run, envelope = _request(tasks, settings.tenant_id)
        assert worker.process(envelope) is True
        aggregate = tasks.get_task(task_id)
        assert aggregate.task.status is TaskStatus.COMPLETED
        assert aggregate.runs[0].status is RunStatus.SUCCEEDED
        assert aggregate.attempts[0].status is AttemptStatus.SUCCEEDED
        assert backend.calls == 1
        with factory() as session:
            execution = session.scalar(
                select(RuntimeExecutionRecord).where(RuntimeExecutionRecord.run_id == run.id)
            )
            assert execution is not None and execution.phase == "SUCCEEDED"
            assert session.scalar(
                select(func.count()).select_from(RuntimeObservationRecord).where(
                    RuntimeObservationRecord.runtime_execution_id == execution.id,
                    RuntimeObservationRecord.processing_outcome == "APPLIED",
                )
            ) == 1
            assert session.get(
                InboxMessageRecord,
                (settings.tenant_id, consumer, envelope.message_id),
            ) is not None
        assert worker.process(envelope) is False
        assert backend.calls == 1
        with factory() as session:
            assert session.scalar(
                select(func.count()).select_from(RuntimeObservationRecord).join(
                    RuntimeExecutionRecord,
                    RuntimeExecutionRecord.id
                    == RuntimeObservationRecord.runtime_execution_id,
                ).where(RuntimeExecutionRecord.run_id == run.id)
            ) == 1
    finally:
        engine.dispose()


def test_postgres_managed_finalization_fault_rolls_back_all_authority() -> None:
    engine, factory, registry, tasks, worker, backend, _consumer, settings = _fixture()
    try:
        task_id, run, envelope = _request(tasks, settings.tenant_id)
        fault = _FaultAfterEvidenceRegistry(
            uow_factory=SqlAlchemyUnitOfWorkFactory(factory),
            tenant_id=settings.tenant_id,
            feature_gates=_gates(),
        )
        worker._runtime_registry_service = fault
        with pytest.raises(RuntimeError, match="fault after Runtime evidence"):
            worker.process(envelope)
        aggregate = tasks.get_task(task_id)
        assert aggregate.task.status is TaskStatus.RUNNING
        assert aggregate.runs[0].status is RunStatus.RUNNING
        assert aggregate.attempts[0].status is AttemptStatus.RUNNING
        assert backend.calls == 1
        with factory() as session:
            execution = session.scalar(
                select(RuntimeExecutionRecord).where(RuntimeExecutionRecord.run_id == run.id)
            )
            assert execution is not None and execution.phase == "DISPATCHING"
            assert session.scalar(
                select(func.count()).select_from(RuntimeObservationRecord).where(
                    RuntimeObservationRecord.runtime_execution_id == execution.id
                )
            ) == 0
            assert session.scalar(
                select(func.count()).select_from(InboxMessageRecord).where(
                    InboxMessageRecord.message_id == envelope.message_id
                )
            ) == 0
    finally:
        engine.dispose()


def test_postgres_expired_dispatching_owner_parks_atomically_once() -> None:
    engine, factory, registry, tasks, worker, _backend, consumer, settings = _fixture(
        lease_duration=timedelta(seconds=-1)
    )
    try:
        budget = TaskBudget.create(max_tokens=100, token_reservation_per_attempt=25)
        task_id, run, envelope = _request(tasks, settings.tenant_id, budget=budget)
        QuotaPolicyService(
            SqlAlchemyUnitOfWorkFactory(factory), settings.tenant_id
        ).put_policy(
            scope=QuotaScope.TENANT,
            project_id=None,
            max_concurrent_attempts=1,
            weight=1,
            created_by="postgres-test",
        )
        task, leased_run, attempt = worker._acquire(
            envelope, task_id=task_id, run_id=run.id
        )
        adapter = LangGraphManagedAgentRuntime(
            backend=_DeterministicBackend(),
            state_store=EphemeralRuntimeStateStore(),
            lifecycle_controller=EphemeralRuntimeLifecycleController(),
        )
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
        registry.mark_execution_dispatching(
            execution_id=execution.id,
            attempt_id=attempt.id,
            fencing_token=attempt.fencing_token,
        )
        poison = _PoisonManagedExecution()
        worker._managed_execution_service = poison
        assert worker.process(envelope) is True
        parked = tasks.get_task(task_id)
        assert parked.task.status is TaskStatus.RECONCILIATION_REQUIRED
        assert parked.runs[0].status is RunStatus.RECONCILIATION_REQUIRED
        assert parked.attempts[0].status is AttemptStatus.OUTCOME_UNKNOWN
        assert (
            parked.attempts[0].budget_settlement_source
            is BudgetSettlementSource.CONSERVATIVE_ESTIMATE
        )
        assert poison.calls == 0
        with factory() as session:
            execution_row = session.get(RuntimeExecutionRecord, execution.id)
            assert execution_row is not None and execution_row.phase == "OUTCOME_UNKNOWN"
            assert session.scalar(
                select(func.count()).select_from(OutboxEventRecord).where(
                    OutboxEventRecord.envelope["schema_name"].astext
                    == "agentmesh.runtime.reconciliation.required",
                    OutboxEventRecord.envelope["payload"]["run_id"].astext == str(run.id),
                )
            ) == 1
            assert session.get(
                InboxMessageRecord,
                (settings.tenant_id, consumer, envelope.message_id),
            ) is not None
            reservation = session.scalar(
                select(QuotaReservationRecord).where(
                    QuotaReservationRecord.attempt_id == attempt.id
                )
            )
            assert reservation is not None and reservation.released_at is not None
        assert worker.process(envelope) is False
        assert poison.calls == 0
    finally:
        engine.dispose()


def test_postgres_stale_parking_evidence_rolls_back_domain_state() -> None:
    engine, _factory, registry, tasks, worker, _backend, _consumer, settings = _fixture(
        lease_duration=timedelta(seconds=-1)
    )
    try:
        task_id, run, envelope = _request(tasks, settings.tenant_id)
        task, leased_run, attempt = worker._acquire(
            envelope, task_id=task_id, run_id=run.id
        )
        adapter = LangGraphManagedAgentRuntime(
            backend=_DeterministicBackend(),
            state_store=EphemeralRuntimeStateStore(),
            lifecycle_controller=EphemeralRuntimeLifecycleController(),
        )
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
        registry.mark_execution_dispatching(
            execution_id=execution.id,
            attempt_id=attempt.id,
            fencing_token=attempt.fencing_token,
        )
        stale = _StaleParkingRegistry(
            uow_factory=SqlAlchemyUnitOfWorkFactory(
                sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
            ),
            tenant_id=settings.tenant_id,
            feature_gates=_gates(),
        )
        worker._runtime_registry_service = stale
        with pytest.raises(RunLeaseUnavailable, match="STALE_OWNER"):
            worker.process(envelope)
        unchanged = tasks.get_task(task_id)
        assert unchanged.task.status is TaskStatus.RUNNING
        assert unchanged.runs[0].status is RunStatus.RUNNING
        assert unchanged.attempts[0].status is AttemptStatus.RUNNING
    finally:
        engine.dispose()
