"""Real PostgreSQL evidence for A4.1b.1 managed DIRECT authority."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from agentmesh.application.managed_runtime_execution import ManagedRuntimeExecutionService
from agentmesh.application.quota_services import QuotaPolicyService
from agentmesh.application.runtime_reconciliation import (
    RuntimeOutcomeReconciliationService,
)
from agentmesh.application.runtime_services import RuntimeRegistryService
from agentmesh.application.services import RunExecutionService, TaskApplicationService
from agentmesh.bootstrap import seed_builtin_registry
from agentmesh.config import get_settings
from agentmesh.domain.budgets import BudgetSettlementSource, TaskBudget
from agentmesh.domain.errors import (
    IdempotencyConflict,
    InvalidTaskTransition,
    RunLeaseUnavailable,
)
from agentmesh.domain.identity import PrincipalContext, PrincipalType, Role
from agentmesh.domain.messaging import MessageEnvelope
from agentmesh.domain.quotas import QuotaScope
from agentmesh.domain.runtime_execution import (
    RuntimeExecutionPhase,
    RuntimeLifecycleIntent,
    RuntimeLifecycleOperation,
    RuntimeLifecycleStatus,
    RuntimeObservationEvidence,
    RuntimeObservationOutcome,
)
from agentmesh.domain.tasks import AttemptStatus, RunStatus, TaskStatus
from agentmesh.features import FeatureGateSet
from agentmesh.infrastructure.postgres.models import (
    IdempotencyRecordModel,
    InboxMessageRecord,
    OutboxEventRecord,
    QuotaReservationRecord,
    RuntimeExecutionRecord,
    RuntimeObservationRecord,
    TaskResolutionRecord,
    TaskRunRecord,
)
from agentmesh.infrastructure.postgres.uow import SqlAlchemyUnitOfWorkFactory
from agentmesh.infrastructure.runtime.langgraph_adapter import (
    EphemeralRuntimeLifecycleController,
    EphemeralRuntimeStateStore,
    LangGraphManagedAgentRuntime,
)
from agentmesh.runtime_sdk import RuntimeObservation, RuntimePhase, canonical_digest

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


def _gates(*, quota_admission: bool = False) -> FeatureGateSet:
    return FeatureGateSet.from_config(
        "full",
        "managed_agent_runtime=true,managed_runtime_worker=true,"
        "managed_runtime_direct_cutover=true,outcome_reconciliation=true,"
        "identity_rbac=true,"
        f"quota_admission={'true' if quota_admission else 'false'}",
    )


def _fixture(
    *,
    lease_duration=timedelta(minutes=5),
    registry_type=RuntimeRegistryService,
    quota_admission: bool = False,
):
    settings = get_settings()
    engine = create_engine(settings.database_url)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as session:
        outbox_ids_before_seed = set(session.scalars(select(OutboxEventRecord.id)))
    seed_builtin_registry(settings)
    # Registry seeding emits durable domain events. Delete only the rows added
    # by this fixture so shared or parallel-test events are never consumed.
    with factory() as session:
        outbox_ids_after_seed = set(session.scalars(select(OutboxEventRecord.id)))
        seeded_outbox_ids = outbox_ids_after_seed - outbox_ids_before_seed
        if seeded_outbox_ids:
            session.execute(
                delete(OutboxEventRecord).where(
                    OutboxEventRecord.id.in_(seeded_outbox_ids),
                    OutboxEventRecord.tenant_id == settings.tenant_id,
                )
            )
            session.commit()
    uow_factory = SqlAlchemyUnitOfWorkFactory(factory)
    gates = _gates(quota_admission=quota_admission)
    registry = registry_type(
        uow_factory=uow_factory,
        tenant_id=settings.tenant_id,
        feature_gates=gates,
    )
    tasks = TaskApplicationService(
        uow_factory=uow_factory,
        agent_id=settings.agent_id,
        tenant_id=settings.tenant_id,
        feature_gates=gates,
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
        feature_gates=gates,
    )
    return engine, factory, registry, tasks, worker, backend, consumer, settings


def _request(tasks, tenant_id: str, factory, *, budget=None):
    task_id = tasks.create_task(
        f"postgres managed {uuid4().hex}", budget=budget
    ).task.id
    run = tasks.request_run(task_id).runs[0]
    # These tests invoke the application worker directly. Remove the durable
    # RunRequested row immediately so the later shared Redis relay vertical
    # cannot publish a wakeup that this test already consumed out of band.
    with factory() as session:
        for record in session.scalars(select(OutboxEventRecord)):
            payload = record.envelope.get("payload", {})
            if (
                record.envelope.get("schema_name") == "agentmesh.run.requested"
                and str(payload.get("run_id", "")) == str(run.id)
            ):
                session.delete(record)
        session.commit()
    envelope = MessageEnvelope.run_requested(
        tenant_id=tenant_id, task_id=task_id, run_id=run.id
    )
    return task_id, run, envelope


def _cleanup_task_outbox(factory, task_id) -> None:
    if task_id is None:
        return
    expected = str(task_id)
    with factory() as session:
        # Writer tests intentionally persist 0048-only enum values.  Remove
        # only those rows belonging to this test Task after assertions so the
        # shared suite can still exercise the pre-write 0048 -> 0047 downgrade.
        execution_ids = select(RuntimeExecutionRecord.id).join(
            TaskRunRecord, TaskRunRecord.id == RuntimeExecutionRecord.run_id
        ).where(TaskRunRecord.task_id == task_id)
        session.execute(
            delete(RuntimeObservationRecord).where(
                RuntimeObservationRecord.runtime_execution_id.in_(execution_ids),
                RuntimeObservationRecord.processing_outcome == "RECONCILED",
            )
        )
        session.execute(
            delete(TaskResolutionRecord).where(
                TaskResolutionRecord.task_id == task_id,
                TaskResolutionRecord.action.in_(
                    [
                        "RECONCILE_RUNTIME_SUCCEEDED",
                        "RECONCILE_RUNTIME_FAILED",
                        "RECONCILE_RUNTIME_CANCELED",
                        "RECONCILE_RUNTIME_TIMED_OUT",
                    ]
                ),
            )
        )
        for record in session.scalars(select(OutboxEventRecord)):
            envelope = record.envelope
            payload = envelope.get("payload", {})
            if (
                str(payload.get("task_id", "")) == expected
                or str(envelope.get("correlation_id", "")) == expected
            ):
                session.delete(record)
        session.commit()


def _operator(tenant_id: str) -> PrincipalContext:
    return PrincipalContext(
        principal_id=f"operator-{uuid4().hex}",
        tenant_id=tenant_id,
        principal_type=PrincipalType.USER,
        roles=frozenset({Role.OPERATOR}),
        authenticated=True,
        authentication_method="postgres-test",
    )


def _park_for_reconciliation(*, budget=None, quota: bool = False):
    fixture = _fixture(
        lease_duration=timedelta(seconds=-1), quota_admission=quota
    )
    engine, factory, registry, tasks, worker, _backend, _consumer, settings = fixture
    if quota:
        QuotaPolicyService(
            SqlAlchemyUnitOfWorkFactory(factory), settings.tenant_id
        ).put_policy(
            scope=QuotaScope.TENANT,
            project_id=None,
            max_concurrent_attempts=1,
            weight=1,
            created_by="postgres-reconciliation-test",
        )
    task_id, run, envelope = _request(
        tasks, settings.tenant_id, factory, budget=budget
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
    execution = registry.mark_execution_dispatching(
        execution_id=execution.id,
        attempt_id=attempt.id,
        fencing_token=attempt.fencing_token,
    )
    poison = _PoisonManagedExecution()
    worker._managed_execution_service = poison
    assert worker.process(envelope) is True
    assert poison.calls == 0
    return (*fixture, task_id, run, attempt, execution, poison)


def _confirmed_observation(execution, phase=RuntimePhase.SUCCEEDED, *, observed_at=None):
    return RuntimeObservation(
        observation_id=str(uuid4()),
        runtime_execution_id=str(execution.id),
        assignment_id=str(execution.assignment_id),
        assignment_digest=execution.assignment_digest,
        phase=phase,
        observed_at=observed_at or datetime.now(timezone.utc),
        provider_event_id=f"postgres-reconcile-{uuid4().hex}",
        output={"managed": "reconciled"} if phase is RuntimePhase.SUCCEEDED else None,
    )


def _reconciler(factory, settings, **kwargs):
    return RuntimeOutcomeReconciliationService(
        uow_factory=SqlAlchemyUnitOfWorkFactory(factory),
        tenant_id=settings.tenant_id,
        feature_gates=_gates(),
        **kwargs,
    )


class _MemoryProbe:
    def __init__(self) -> None:
        self.calls = 0

    def capture_completed_task_in_unit_of_work(self, uow, task):
        self.calls += 1


class _ResearchProbe:
    def __init__(self, *, fail=False) -> None:
        self.calls = 0
        self.fail = fail

    def materialize_if_ready(self, task_id, *, actor):
        self.calls += 1
        if self.fail:
            raise RuntimeError("best-effort research failure")


def test_postgres_managed_authoritative_success_is_atomic_and_replay_safe() -> None:
    engine, factory, _registry, tasks, worker, backend, consumer, settings = _fixture()
    task_id = None
    try:
        task_id, run, envelope = _request(tasks, settings.tenant_id, factory)
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
        _cleanup_task_outbox(factory, task_id)
        engine.dispose()


def test_postgres_managed_finalization_fault_rolls_back_all_authority() -> None:
    engine, factory, registry, tasks, worker, backend, _consumer, settings = _fixture()
    task_id = None
    try:
        task_id, run, envelope = _request(tasks, settings.tenant_id, factory)
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
        _cleanup_task_outbox(factory, task_id)
        engine.dispose()


def test_postgres_expired_dispatching_owner_parks_atomically_once() -> None:
    engine, factory, registry, tasks, worker, _backend, consumer, settings = _fixture(
        lease_duration=timedelta(seconds=-1), quota_admission=True
    )
    task_id = None
    try:
        budget = TaskBudget.create(max_tokens=100, token_reservation_per_attempt=25)
        task_id, run, envelope = _request(
            tasks, settings.tenant_id, factory, budget=budget
        )
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
        _cleanup_task_outbox(factory, task_id)
        engine.dispose()


def test_postgres_stale_parking_evidence_rolls_back_domain_state() -> None:
    engine, factory, registry, tasks, worker, _backend, _consumer, settings = _fixture(
        lease_duration=timedelta(seconds=-1)
    )
    task_id = None
    try:
        task_id, run, envelope = _request(tasks, settings.tenant_id, factory)
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
        with factory() as session:
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
            assert session.scalar(
                select(func.count()).select_from(OutboxEventRecord).where(
                    OutboxEventRecord.envelope["schema_name"].astext
                    == "agentmesh.runtime.reconciliation.required",
                    OutboxEventRecord.envelope["payload"]["run_id"].astext
                    == str(run.id),
                )
            ) == 0
    finally:
        _cleanup_task_outbox(factory, task_id)
        engine.dispose()


def test_postgres_runtime_outcome_reconciliation_is_atomic_and_replay_safe() -> None:
    (
        engine,
        factory,
        _registry,
        tasks,
        _worker,
        _backend,
        _consumer,
        settings,
        task_id,
        run,
        attempt,
        execution,
        poison,
    ) = _park_for_reconciliation()
    try:
        observation = _confirmed_observation(execution)
        digest = canonical_digest(observation.to_dict())
        memory = _MemoryProbe()
        research = _ResearchProbe(fail=True)
        service = _reconciler(
            factory,
            settings,
            runtime_memory_service=memory,
            research_materialization_service=research,
        )
        principal = _operator(settings.tenant_id)

        first = service.reconcile_outcome(
            execution.id,
            principal=principal,
            observation=observation,
            evidence_digest=digest,
            evidence_reference="case://postgres/runtime-success",
            reason="Provider support confirmed success",
            idempotency_key="pg-runtime-reconcile-success",
        )
        replay = service.reconcile_outcome(
            execution.id,
            principal=principal,
            observation=observation,
            evidence_digest=digest,
            evidence_reference="case://postgres/runtime-success",
            reason="Provider support confirmed success",
            idempotency_key="pg-runtime-reconcile-success",
        )

        aggregate = tasks.get_task(task_id)
        assert first.resolution.id == replay.resolution.id
        assert aggregate.task.status is TaskStatus.COMPLETED
        assert aggregate.task.output == {"managed": "reconciled"}
        assert aggregate.runs[0].status is RunStatus.SUCCEEDED
        assert aggregate.attempts[0].status is AttemptStatus.SUCCEEDED
        assert poison.calls == 0
        assert memory.calls == 1
        assert research.calls == 1
        with factory() as session:
            execution_row = session.get(RuntimeExecutionRecord, execution.id)
            assert execution_row is not None and execution_row.phase == "SUCCEEDED"
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
                    OutboxEventRecord.envelope["payload"]["run_id"].astext
                    == str(run.id),
                )
            ) == 1
            assert session.scalar(
                select(func.count()).select_from(IdempotencyRecordModel).where(
                    IdempotencyRecordModel.key == "pg-runtime-reconcile-success"
                )
            ) == 1
        conflicting = _confirmed_observation(execution, phase=RuntimePhase.FAILED)
        with pytest.raises(IdempotencyConflict):
            service.reconcile_outcome(
                execution.id,
                principal=principal,
                observation=conflicting,
                evidence_digest=canonical_digest(conflicting.to_dict()),
                evidence_reference="case://postgres/runtime-failure",
                reason="Conflicting conclusion",
                idempotency_key="pg-runtime-reconcile-success",
            )
    finally:
        _cleanup_task_outbox(factory, task_id)
        engine.dispose()


class _FailingMemoryCapture:
    def capture_completed_task_in_unit_of_work(self, uow, task):
        raise RuntimeError("memory capture fault")


def test_postgres_reconciliation_memory_failure_rolls_back_everything() -> None:
    (
        engine,
        factory,
        _registry,
        tasks,
        _worker,
        _backend,
        _consumer,
        settings,
        task_id,
        run,
        _attempt,
        execution,
        poison,
    ) = _park_for_reconciliation()
    try:
        observation = _confirmed_observation(execution)
        with pytest.raises(RuntimeError, match="memory capture fault"):
            _reconciler(
                factory, settings, runtime_memory_service=_FailingMemoryCapture()
            ).reconcile_outcome(
                execution.id,
                principal=_operator(settings.tenant_id),
                observation=observation,
                evidence_digest=canonical_digest(observation.to_dict()),
                evidence_reference="case://postgres/rollback",
                reason="Confirmed outcome",
                idempotency_key="pg-runtime-reconcile-rollback",
            )
        aggregate = tasks.get_task(task_id)
        assert aggregate.task.status is TaskStatus.RECONCILIATION_REQUIRED
        assert aggregate.runs[0].status is RunStatus.RECONCILIATION_REQUIRED
        assert aggregate.attempts[0].status is AttemptStatus.OUTCOME_UNKNOWN
        assert poison.calls == 0
        with factory() as session:
            execution_row = session.get(RuntimeExecutionRecord, execution.id)
            assert execution_row is not None and execution_row.phase == "OUTCOME_UNKNOWN"
            assert session.scalar(
                select(func.count()).select_from(RuntimeObservationRecord).where(
                    RuntimeObservationRecord.runtime_execution_id == execution.id,
                    RuntimeObservationRecord.processing_outcome == "RECONCILED",
                )
            ) == 0
            assert session.scalar(
                select(func.count()).select_from(TaskResolutionRecord).where(
                    TaskResolutionRecord.task_id == task_id
                )
            ) == 0
            assert session.scalar(
                select(func.count()).select_from(IdempotencyRecordModel).where(
                    IdempotencyRecordModel.key == "pg-runtime-reconcile-rollback"
                )
            ) == 0
            assert session.scalar(
                select(func.count()).select_from(OutboxEventRecord).where(
                    OutboxEventRecord.envelope["schema_name"].astext
                    == "agentmesh.runtime.outcome-reconciled",
                    OutboxEventRecord.envelope["payload"]["run_id"].astext
                    == str(run.id),
                )
            ) == 0
    finally:
        _cleanup_task_outbox(factory, task_id)
        engine.dispose()


def test_postgres_competing_reconciliation_conclusions_have_one_winner() -> None:
    (
        engine,
        factory,
        _registry,
        tasks,
        _worker,
        _backend,
        _consumer,
        settings,
        task_id,
        run,
        _attempt,
        execution,
        poison,
    ) = _park_for_reconciliation()
    try:
        success = _confirmed_observation(execution)
        failure = _confirmed_observation(execution, phase=RuntimePhase.FAILED)

        def reconcile(observation, key):
            return _reconciler(factory, settings).reconcile_outcome(
                execution.id,
                principal=_operator(settings.tenant_id),
                observation=observation,
                evidence_digest=canonical_digest(observation.to_dict()),
                evidence_reference=f"case://postgres/{key}",
                reason="Independent operator conclusion",
                idempotency_key=key,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(reconcile, success, "concurrent-success"),
                pool.submit(reconcile, failure, "concurrent-failure"),
            ]
            results = []
            errors = []
            for future in futures:
                try:
                    results.append(future.result())
                except InvalidTaskTransition as exc:
                    errors.append(exc)
        assert len(results) == len(errors) == 1
        assert poison.calls == 0
        aggregate = tasks.get_task(task_id)
        assert aggregate.task.status in {TaskStatus.COMPLETED, TaskStatus.FAILED}
        with factory() as session:
            assert session.scalar(
                select(func.count()).select_from(TaskResolutionRecord).where(
                    TaskResolutionRecord.task_id == task_id
                )
            ) == 1
            assert session.scalar(
                select(func.count()).select_from(RuntimeObservationRecord).where(
                    RuntimeObservationRecord.runtime_execution_id == execution.id,
                    RuntimeObservationRecord.processing_outcome == "RECONCILED",
                )
            ) == 1
    finally:
        _cleanup_task_outbox(factory, task_id)
        engine.dispose()


@pytest.mark.parametrize(
    ("phase", "expected_task", "expected_run", "expected_attempt", "reason"),
    [
        (
            RuntimePhase.FAILED,
            TaskStatus.FAILED,
            RunStatus.FAILED,
            AttemptStatus.FAILED,
            "runtime.reconciled_failed",
        ),
        (
            RuntimePhase.TIMED_OUT,
            TaskStatus.FAILED,
            RunStatus.FAILED,
            AttemptStatus.FAILED,
            "runtime.reconciled_timed_out",
        ),
        (
            RuntimePhase.CANCELED,
            TaskStatus.FAILED,
            RunStatus.FAILED,
            AttemptStatus.FAILED,
            "runtime.unrequested_cancellation",
        ),
    ],
)
def test_postgres_reconciliation_known_non_success_mapping(
    phase, expected_task, expected_run, expected_attempt, reason
) -> None:
    (
        engine,
        factory,
        _registry,
        tasks,
        _worker,
        _backend,
        _consumer,
        settings,
        task_id,
        _run,
        _attempt,
        execution,
        poison,
    ) = _park_for_reconciliation()
    try:
        observation = _confirmed_observation(execution, phase=phase)
        _reconciler(factory, settings).reconcile_outcome(
            execution.id,
            principal=_operator(settings.tenant_id),
            observation=observation,
            evidence_digest=canonical_digest(observation.to_dict()),
            evidence_reference=f"case://postgres/{phase.value.lower()}",
            reason="Confirmed terminal outcome",
            idempotency_key=f"known-{phase.value.lower()}-{uuid4().hex}",
        )
        aggregate = tasks.get_task(task_id)
        assert aggregate.task.status is expected_task
        assert aggregate.task.error == reason
        assert aggregate.runs[0].status is expected_run
        assert aggregate.attempts[0].status is expected_attempt
        assert poison.calls == 0
    finally:
        _cleanup_task_outbox(factory, task_id)
        engine.dispose()


def test_postgres_requested_cancellation_maps_all_business_state_to_canceled() -> None:
    (
        engine,
        factory,
        _registry,
        tasks,
        _worker,
        _backend,
        _consumer,
        settings,
        task_id,
        _run,
        _attempt,
        execution,
        poison,
    ) = _park_for_reconciliation()
    try:
        now = datetime.now(timezone.utc)
        with SqlAlchemyUnitOfWorkFactory(factory)() as uow:
            uow.runtimes.add_lifecycle_operation(
                RuntimeLifecycleIntent(
                    id=uuid4(),
                    tenant_id=settings.tenant_id,
                    runtime_execution_id=execution.id,
                    operation_id=f"operator-cancel-{uuid4().hex}",
                    operation=RuntimeLifecycleOperation.CANCEL,
                    intent_digest="f" * 64,
                    status=RuntimeLifecycleStatus.REQUESTED,
                    deadline=now + timedelta(minutes=10),
                    receipt_summary=None,
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            uow.commit()
        observation = _confirmed_observation(execution, phase=RuntimePhase.CANCELED)
        result = _reconciler(factory, settings).reconcile_outcome(
            execution.id,
            principal=_operator(settings.tenant_id),
            observation=observation,
            evidence_digest=canonical_digest(observation.to_dict()),
            evidence_reference="case://postgres/requested-cancel",
            reason="Provider confirmed requested cancellation",
            idempotency_key=f"requested-cancel-{uuid4().hex}",
        )
        aggregate = tasks.get_task(task_id)
        assert aggregate.task.status is TaskStatus.CANCELED
        assert aggregate.runs[0].status is RunStatus.CANCELED
        assert aggregate.attempts[0].status is AttemptStatus.CANCELED
        assert result.resolution.details["business_mapping_reason"] == (
            "runtime.reconciled_canceled"
        )
        assert poison.calls == 0
    finally:
        _cleanup_task_outbox(factory, task_id)
        engine.dispose()


def test_postgres_success_at_budget_deadline_waits_for_approval_without_resettling() -> None:
    deadline = datetime.now(timezone.utc) + timedelta(minutes=10)
    budget = TaskBudget.create(deadline=deadline)
    (
        engine,
        factory,
        _registry,
        tasks,
        _worker,
        _backend,
        _consumer,
        settings,
        task_id,
        _run,
        attempt,
        execution,
        poison,
    ) = _park_for_reconciliation(budget=budget, quota=True)
    try:
        parked = tasks.get_task(task_id)
        settlement_source = parked.attempts[0].budget_settlement_source
        settled_tokens = parked.task.settled_tokens
        with factory() as session:
            reservation_before = session.scalar(
                select(QuotaReservationRecord).where(
                    QuotaReservationRecord.attempt_id == attempt.id
                )
            )
            assert reservation_before is not None
            released_at = reservation_before.released_at
            assert released_at is not None
        observation = _confirmed_observation(execution, observed_at=deadline)
        result = _reconciler(factory, settings).reconcile_outcome(
            execution.id,
            principal=_operator(settings.tenant_id),
            observation=observation,
            evidence_digest=canonical_digest(observation.to_dict()),
            evidence_reference="case://postgres/deadline",
            reason="Success confirmed at the pinned deadline",
            idempotency_key=f"deadline-{uuid4().hex}",
        )
        aggregate = tasks.get_task(task_id)
        assert aggregate.task.status is TaskStatus.WAITING_APPROVAL
        assert aggregate.task.current_run_id is None
        assert aggregate.task.candidate_output == {"managed": "reconciled"}
        assert aggregate.task.budget_exhausted_reason == "budget_deadline_exceeded"
        assert aggregate.runs[0].status is RunStatus.SUCCEEDED
        assert aggregate.attempts[0].status is AttemptStatus.SUCCEEDED
        assert aggregate.attempts[0].budget_settlement_source is settlement_source
        assert aggregate.task.settled_tokens == settled_tokens
        with factory() as session:
            reservations = list(
                session.scalars(
                    select(QuotaReservationRecord).where(
                        QuotaReservationRecord.attempt_id == attempt.id
                    )
                )
            )
            assert len(reservations) == 1
            assert reservations[0].released_at == released_at
        assert result.resolution.resulting_status is TaskStatus.WAITING_APPROVAL
        assert result.resolution.details["business_mapping_reason"] == (
            "budget_deadline_exceeded"
        )
        assert aggregate.attempts[0].id == attempt.id
        assert poison.calls == 0
    finally:
        _cleanup_task_outbox(factory, task_id)
        engine.dispose()


def test_postgres_reuses_exact_late_evidence_without_duplicate_record() -> None:
    (
        engine,
        factory,
        _registry,
        tasks,
        _worker,
        _backend,
        _consumer,
        settings,
        task_id,
        _run,
        _attempt,
        execution,
        poison,
    ) = _park_for_reconciliation()
    try:
        observation = _confirmed_observation(execution)
        digest = canonical_digest(observation.to_dict())
        evidence_id = uuid4()
        with SqlAlchemyUnitOfWorkFactory(factory)() as uow:
            uow.runtimes.add_observation(
                RuntimeObservationEvidence(
                    id=evidence_id,
                    tenant_id=settings.tenant_id,
                    runtime_execution_id=execution.id,
                    observation_id=observation.observation_id,
                    observation_digest=digest,
                    assignment_id=execution.assignment_id,
                    assignment_digest=execution.assignment_digest,
                    provider_sequence=observation.provider_sequence,
                    phase=RuntimeExecutionPhase.SUCCEEDED,
                    observed_at=observation.observed_at,
                    received_at=datetime.now(timezone.utc),
                    safe_summary="Late terminal evidence",
                    processing_outcome=RuntimeObservationOutcome.CONFLICT,
                    provider_event_present=True,
                    evidence={
                        "provider_event_id": observation.provider_event_id,
                        "snapshot_digest": observation.snapshot_digest,
                    },
                )
            )
            uow.commit()
        _reconciler(factory, settings).reconcile_outcome(
            execution.id,
            principal=_operator(settings.tenant_id),
            observation=observation,
            evidence_digest=digest,
            evidence_reference="case://postgres/existing-evidence",
            reason="Existing evidence independently verified",
            idempotency_key=f"existing-evidence-{uuid4().hex}",
        )
        with factory() as session:
            records = list(
                session.scalars(
                    select(RuntimeObservationRecord).where(
                        RuntimeObservationRecord.runtime_execution_id == execution.id,
                        RuntimeObservationRecord.observation_id
                        == observation.observation_id,
                    )
                )
            )
            assert len(records) == 1
            assert records[0].id == evidence_id
            assert records[0].processing_outcome == "RECONCILED"
        assert tasks.get_task(task_id).task.status is TaskStatus.COMPLETED
        assert poison.calls == 0
    finally:
        _cleanup_task_outbox(factory, task_id)
        engine.dispose()


def test_postgres_stale_reconciliation_fence_has_zero_side_effects() -> None:
    (
        engine,
        factory,
        _registry,
        tasks,
        _worker,
        _backend,
        _consumer,
        settings,
        task_id,
        run,
        _attempt,
        execution,
        poison,
    ) = _park_for_reconciliation()
    try:
        with factory() as session:
            row = session.get(RuntimeExecutionRecord, execution.id)
            assert row is not None and row.current_fencing_token is not None
            row.current_fencing_token += 1
            session.commit()
        observation = _confirmed_observation(execution)
        with pytest.raises(InvalidTaskTransition, match="strictly consistent"):
            _reconciler(factory, settings).reconcile_outcome(
                execution.id,
                principal=_operator(settings.tenant_id),
                observation=observation,
                evidence_digest=canonical_digest(observation.to_dict()),
                evidence_reference="case://postgres/stale-fence",
                reason="Stale evidence must fail",
                idempotency_key="stale-fence-reconciliation",
            )
        aggregate = tasks.get_task(task_id)
        assert aggregate.task.status is TaskStatus.RECONCILIATION_REQUIRED
        assert aggregate.runs[0].status is RunStatus.RECONCILIATION_REQUIRED
        assert aggregate.attempts[0].status is AttemptStatus.OUTCOME_UNKNOWN
        assert poison.calls == 0
        with factory() as session:
            assert session.scalar(
                select(func.count()).select_from(RuntimeObservationRecord).where(
                    RuntimeObservationRecord.runtime_execution_id == execution.id,
                    RuntimeObservationRecord.processing_outcome == "RECONCILED",
                )
            ) == 0
            assert session.scalar(
                select(func.count()).select_from(TaskResolutionRecord).where(
                    TaskResolutionRecord.task_id == task_id
                )
            ) == 0
            assert session.scalar(
                select(func.count()).select_from(IdempotencyRecordModel).where(
                    IdempotencyRecordModel.key == "stale-fence-reconciliation"
                )
            ) == 0
            assert session.scalar(
                select(func.count()).select_from(OutboxEventRecord).where(
                    OutboxEventRecord.envelope["schema_name"].astext
                    == "agentmesh.runtime.outcome-reconciled",
                    OutboxEventRecord.envelope["payload"]["run_id"].astext
                    == str(run.id),
                )
            ) == 0
    finally:
        _cleanup_task_outbox(factory, task_id)
        engine.dispose()
