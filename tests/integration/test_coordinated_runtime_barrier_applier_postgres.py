"""Real PostgreSQL qualification for the transaction-local coordinated barrier applier."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from agentmesh.application.business_outcomes import KnownTerminalPhase
from agentmesh.application.coordinated_runtime import CoordinatedRuntimeAggregateLocker
from agentmesh.application.coordinated_runtime_barrier import (
    CoordinatedRuntimeBarrierApplier,
    plan_known_terminal,
)
from agentmesh.config import get_settings
from agentmesh.domain.budgets import TaskBudget
from agentmesh.domain.coordination import (
    CoordinationRuntimeBoundary,
    CoordinationRuntimeDrain,
    CoordinationRuntimeDrainTarget,
    Subtask,
)
from agentmesh.domain.quotas import QuotaPolicy, QuotaReservation, QuotaScope
from agentmesh.domain.runtime_execution import (
    RuntimeExecution,
    RuntimeExecutionPhase,
    RuntimeRegistration,
    RuntimeRegistrationStatus,
    RuntimeTrustProfile,
    RuntimeVersion,
    RuntimeVersionStatus,
    RuntimeVisibility,
)
from agentmesh.domain.tasks import (
    RunRole,
    Task,
    TaskAttempt,
    TaskExecutionMode,
    TaskRun,
)
from agentmesh.infrastructure.postgres.models import PrincipalRecord, RuntimeRegistrationRecord
from agentmesh.infrastructure.postgres.repositories import (
    SqlAlchemyCoordinationRuntimeDrainRepository,
    SqlAlchemySubtaskRepository,
    SqlAlchemyTaskAttemptRepository,
    SqlAlchemyTaskRepository,
    SqlAlchemyTaskRunRepository,
)
from agentmesh.infrastructure.postgres.runtime_repositories import SqlAlchemyRuntimeRepository
from agentmesh.infrastructure.postgres.uow import SqlAlchemyUnitOfWorkFactory
from agentmesh.runtime_sdk.canonical import canonical_digest

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("AGENTMESH_RUN_POSTGRES_TESTS") != "1",
        reason="set AGENTMESH_RUN_POSTGRES_TESTS=1 to run coordinated barrier PostgreSQL tests",
    ),
]

UTC = timezone.utc


@dataclass(frozen=True)
class _Fixture:
    task_id: UUID
    tenant_id: str
    target_run_id: UUID
    sibling_run_id: UUID
    sibling_attempt_id: UUID | None
    sibling_execution_id: UUID | None
    sibling_boundary: str
    quota_policy_id: UUID | None
    runtime_registration_id: UUID
    runtime_version_id: UUID
    principal_id: UUID


def _factory(engine):
    return SqlAlchemyUnitOfWorkFactory(
        sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    )


def _version(now: datetime, owner_principal_id: UUID) -> tuple[RuntimeRegistration, RuntimeVersion]:
    registration = RuntimeRegistration(
        id=uuid4(),
        tenant_id=None,
        name=f"coordination-pg-{uuid4().hex}",
        owner_principal_id=owner_principal_id,
        visibility=RuntimeVisibility.PLATFORM,
        status=RuntimeRegistrationStatus.ACTIVE,
        default_version_id=None,
        version=1,
        created_at=now,
        updated_at=now,
    )
    version = RuntimeVersion(
        id=uuid4(),
        runtime_id=registration.id,
        api_version=1,
        adapter_kind="python-in-process",
        artifact_digest=canonical_digest(
            {"package": "agentmesh", "runtime": "agentmesh.langgraph", "release": "v2"}
        ),
        configuration_digest=canonical_digest(
            {
                "runtime_key": "agentmesh.test.coordinated",
                "capabilities": {"execution_mode": ["managed_async"]},
                "limits": {"max_assignment_bytes": 262144},
            }
        ),
        descriptor={},
        trust_profile=RuntimeTrustProfile.BUILT_IN,
        compatibility={},
        status=RuntimeVersionStatus.PUBLISHED,
        created_at=now,
        published_at=now,
    )
    return registration, version


def _chain(task: Task, version_id: UUID, *, now: datetime, boundary: str):
    subtask = Subtask.create(
        subtask_id=uuid4(),
        task_id=task.id,
        key=f"worker-{uuid4().hex[:8]}",
        objective="barrier qualification",
        input={},
        required_capabilities=("general.task",),
        preferred_agent_id=None,
        initially_ready=True,
    )
    run = TaskRun.request(
        task.id,
        "worker",
        role=RunRole.EXECUTOR,
        subtask_id=subtask.id,
        runtime_version_id=version_id,
        runtime_authority="managed",
        at=now,
    )
    subtask.queue(run.id, at=now + timedelta(seconds=1))
    attempt = None
    execution = None
    if boundary != "queued":
        subtask.start(run.id, at=now + timedelta(seconds=2))
        run.start(at=now + timedelta(seconds=2))
        attempt = TaskAttempt.lease(
            run_id=run.id,
            worker_id="worker",
            fencing_token=1,
            lease_expires_at=now + timedelta(hours=1),
            reserved_tokens=10,
            reserved_cost_micros=100,
        )
        if boundary in {"prepared", "crossed"}:
            execution = RuntimeExecution.prepare(
                tenant_id=task.tenant_id,
                run_id=run.id,
                runtime_version_id=version_id,
                assignment_id=uuid4(),
                assignment_digest="a" * 64,
                dispatch_key=f"coordination-pg:{uuid4().hex}",
                dispatch_digest="b" * 64,
                execution_id=run.runtime_execution_intent_id,
                now=now + timedelta(seconds=2),
            )
            if boundary == "crossed":
                execution = execution.claim(
                    attempt_id=attempt.id,
                    fencing_token=attempt.fencing_token,
                    expected_owner_attempt_id=None,
                    expected_fencing_token=None,
                    expected_version=1,
                    now=now + timedelta(seconds=3),
                )
                execution = execution.apply_observation(
                    phase=RuntimeExecutionPhase.RUNNING,
                    provider_sequence=1,
                    now=now + timedelta(seconds=4),
                )
            else:
                execution = execution.claim(
                    attempt_id=attempt.id,
                    fencing_token=attempt.fencing_token,
                    expected_owner_attempt_id=None,
                    expected_fencing_token=None,
                    expected_version=1,
                    now=now + timedelta(seconds=3),
                )
            run.bind_runtime_execution(execution.id)
    return subtask, run, attempt, execution


def _seed(
    engine,
    *,
    sibling_boundary: str,
    existing_drain: bool = False,
    with_budget_quota: bool = False,
) -> _Fixture:
    now = datetime.now(UTC).replace(microsecond=0)
    tenant = f"barrier-pg-{uuid4().hex}"
    budget = (
        TaskBudget.create(
            max_attempts=10,
            max_tokens=100,
            token_reservation_per_attempt=10,
            max_cost_micros=1000,
            cost_reservation_micros_per_attempt=100,
        )
        if with_budget_quota
        else None
    )
    task = Task.create(
        tenant_id=tenant,
        objective="coordinated barrier qualification",
        execution_mode=TaskExecutionMode.COORDINATED,
        plan_version=1,
        plan_digest=f"sha256:{uuid4().hex}",
        max_concurrency=2,
        budget=budget,
    )
    task.start_coordination(at=now)
    principal_id = uuid4()
    registration, version = _version(now, principal_id)
    target = _chain(task, version.id, now=now, boundary="crossed")
    sibling = _chain(task, version.id, now=now, boundary=sibling_boundary)
    drain = None
    if existing_drain:
        drain = CoordinationRuntimeDrain.start(
            drain_id=uuid4(),
            tenant_id=tenant,
            task_id=task.id,
            triggering_run_id=target[1].id,
            target=CoordinationRuntimeDrainTarget.FAILED,
            reason="runtime.failed",
            at=now,
        )
    policy = None
    reservation = None
    if with_budget_quota:
        assert sibling[2] is not None
        policy = QuotaPolicy.create(
            tenant_id=tenant,
            scope=QuotaScope.TENANT,
            project_id=None,
            max_concurrent_attempts=10,
            weight=1,
            version=1,
            created_by="qualification",
        )
        reservation = QuotaReservation.acquire(
            policy_id=policy.id,
            attempt_id=sibling[2].id,
            tenant_id=tenant,
            project_id=task.project_id,
        )
        task.reserve_budget(
            tokens=sibling[2].reserved_tokens,
            cost_micros=sibling[2].reserved_cost_micros,
            at=now + timedelta(seconds=4),
        )
    with Session(engine) as session, session.begin():
        session.add(
            PrincipalRecord(
                id=principal_id,
                tenant_id=tenant,
                principal_type="SERVICE",
                status="ACTIVE",
                display_name="barrier qualification",
                created_at=now,
                updated_at=now,
                revision=1,
            )
        )
        session.flush()
        session.add(SqlAlchemyTaskRepository._to_record(task))
        session.flush()
        session.add(
            RuntimeRegistrationRecord(
                id=registration.id,
                tenant_id=None,
                name=registration.name,
                owner_principal_id=principal_id,
                visibility="platform",
                status="ACTIVE",
                default_version_id=version.id,
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
        SqlAlchemyRuntimeRepository(session).add_version(version)
        session.flush()
        for subtask, run, _attempt, _execution in (target, sibling):
            session.add(SqlAlchemySubtaskRepository._to_record(subtask))
            session.flush()
            # The execution FK is use-alter; initially persist the Run without
            # the bound execution and patch the binding after its row exists.
            session.add(
                SqlAlchemyTaskRunRepository._to_record(
                    TaskRun(
                        **{
                            **run.__dict__,
                            "runtime_execution_id": None,
                        }
                    )
                )
            )
            session.flush()
        for _subtask, run, attempt, execution in (target, sibling):
            if attempt is not None:
                session.add(SqlAlchemyTaskAttemptRepository._to_record(attempt))
                session.flush()
            if execution is not None:
                SqlAlchemyRuntimeRepository(session).add_execution(execution)
                session.flush()
                session.execute(
                    text(
                        "UPDATE task_runs SET runtime_execution_id = :execution_id "
                        "WHERE id = :run_id"
                    ),
                    {"execution_id": execution.id, "run_id": run.id},
                )
        if drain is not None:
            session.add(SqlAlchemyCoordinationRuntimeDrainRepository._to_record(drain))
        if policy is not None and reservation is not None:
            from agentmesh.infrastructure.postgres.quota_repositories import (
                SqlAlchemyQuotaRepository,
            )

            quotas = SqlAlchemyQuotaRepository(session)
            quotas.add_policy(policy)
            session.flush()
            quotas.add_reservation(reservation)
    return _Fixture(
        task_id=task.id,
        tenant_id=tenant,
        target_run_id=target[1].id,
        sibling_run_id=sibling[1].id,
        sibling_attempt_id=sibling[2].id if sibling[2] else None,
        sibling_execution_id=sibling[3].id if sibling[3] else None,
        sibling_boundary=sibling_boundary,
        quota_policy_id=policy.id if policy else None,
        runtime_registration_id=registration.id,
        runtime_version_id=version.id,
        principal_id=principal_id,
    )


def _cleanup(engine, fixture: _Fixture) -> None:
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE task_runs SET runtime_execution_id = NULL WHERE task_id = :id"),
            {"id": fixture.task_id},
        )
        connection.execute(
            text(
                "DELETE FROM runtime_executions WHERE run_id IN "
                "(SELECT id FROM task_runs WHERE task_id = :id)"
            ),
            {"id": fixture.task_id},
        )
        connection.execute(text("DELETE FROM tasks WHERE id = :id"), {"id": fixture.task_id})
        connection.execute(
            text("UPDATE runtime_registrations SET default_version_id = NULL WHERE id = :id"),
            {"id": fixture.runtime_registration_id},
        )
        connection.execute(
            text("DELETE FROM runtime_versions WHERE id = :id"),
            {"id": fixture.runtime_version_id},
        )
        connection.execute(
            text("DELETE FROM runtime_registrations WHERE id = :id"),
            {"id": fixture.runtime_registration_id},
        )
        connection.execute(
            text("DELETE FROM principals WHERE id = :id"), {"id": fixture.principal_id}
        )
        if fixture.quota_policy_id is not None:
            connection.execute(
                text("DELETE FROM quota_policies WHERE id = :id"),
                {"id": fixture.quota_policy_id},
            )


def _apply(engine, fixture: _Fixture, *, fail_after: bool = False):
    with _factory(engine)() as uow:
        aggregate = CoordinatedRuntimeAggregateLocker().lock(
            uow, tenant_id=fixture.tenant_id, task_id=fixture.task_id
        )
        assert aggregate.boundary_classifications[fixture.target_run_id] is (
            CoordinationRuntimeBoundary.CROSSED_ACTIVE
        )
        expected_boundary = {
            "queued": CoordinationRuntimeBoundary.NOT_CROSSED_QUEUED,
            "no-execution": CoordinationRuntimeBoundary.NOT_CROSSED_NO_EXECUTION,
            "prepared": CoordinationRuntimeBoundary.NOT_CROSSED_PREPARED,
            "crossed": CoordinationRuntimeBoundary.CROSSED_ACTIVE,
        }[fixture.sibling_boundary]
        assert aggregate.boundary_classifications[fixture.sibling_run_id] is expected_boundary
        plan = plan_known_terminal(
            aggregate,
            triggering_run_id=fixture.target_run_id,
            phase=KnownTerminalPhase.FAILED,
            cancel_intent_present=False,
            safe_error=None,
        )
        result = CoordinatedRuntimeBarrierApplier().apply_in_uow(
            uow,
            aggregate=aggregate,
            plan=plan,
            now=datetime.now(UTC) + timedelta(minutes=1),
            cancel_deadline_window=timedelta(minutes=5),
        )
        if fail_after:
            uow.rollback()
            raise RuntimeError("injected before commit")
        uow.commit()
        return result


@pytest.mark.parametrize("boundary", ["queued", "no-execution", "prepared"])
def test_postgres_barrier_releases_queued_no_execution_and_prepared_siblings(boundary: str) -> None:
    engine = create_engine(get_settings().database_url)
    fixture = _seed(engine, sibling_boundary=boundary)
    try:
        result = _apply(engine, fixture)
        assert result.made_progress
        with engine.connect() as connection:
            assert connection.scalar(
                text("SELECT status FROM task_runs WHERE id = :id"),
                {"id": fixture.sibling_run_id},
            ) == "CANCELED"
            if fixture.sibling_attempt_id is not None:
                assert connection.scalar(
                    text("SELECT status FROM task_attempts WHERE id = :id"),
                    {"id": fixture.sibling_attempt_id},
                ) == "CANCELED"
            if fixture.sibling_execution_id is not None:
                assert connection.scalar(
                    text("SELECT phase FROM runtime_executions WHERE id = :id"),
                    {"id": fixture.sibling_execution_id},
                ) == "CANCELED"
    finally:
        _cleanup(engine, fixture)
        engine.dispose()


def test_postgres_barrier_releases_budget_and_quota_once_on_replay() -> None:
    engine = create_engine(get_settings().database_url)
    fixture = _seed(engine, sibling_boundary="no-execution", with_budget_quota=True)
    try:
        _apply(engine, fixture)
        with engine.connect() as connection:
            first = connection.execute(
                text(
                    "SELECT reserved_tokens, settled_tokens, reserved_cost_micros, "
                    "settled_cost_micros FROM tasks WHERE id = :id"
                ),
                {"id": fixture.task_id},
            ).one()
            assert tuple(first) == (0, 0, 0, 0)
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM quota_reservations "
                    "WHERE attempt_id = :attempt_id AND released_at IS NOT NULL"
                ),
                {"attempt_id": fixture.sibling_attempt_id},
            ) == 1
    finally:
        _cleanup(engine, fixture)
        engine.dispose()


def test_postgres_barrier_cancel_intent_and_outbox_are_stable() -> None:
    engine = create_engine(get_settings().database_url)
    fixture = _seed(engine, sibling_boundary="crossed", existing_drain=True)
    try:
        first = _apply(engine, fixture)
        assert first.operation_ids == (f"runtime-cancel:{fixture.sibling_execution_id}:v1",)
        with engine.connect() as connection:
            before = connection.scalar(
                text(
                    "SELECT count(*) FROM outbox_events "
                    "WHERE envelope->'payload'->>'runtime_execution_id' = :id"
                ),
                {"id": str(fixture.sibling_execution_id)},
            )
        # A second application sees the persisted active drain and stable intent.
        _apply(engine, fixture)
        with engine.connect() as connection:
            after = connection.scalar(
                text(
                    "SELECT count(*) FROM outbox_events "
                    "WHERE envelope->'payload'->>'runtime_execution_id' = :id"
                ),
                {"id": str(fixture.sibling_execution_id)},
            )
            assert before == after == 1
    finally:
        _cleanup(engine, fixture)
        engine.dispose()


def test_postgres_barrier_rollback_removes_drain_and_sibling_writes() -> None:
    engine = create_engine(get_settings().database_url)
    fixture = _seed(engine, sibling_boundary="prepared")
    try:
        with pytest.raises(RuntimeError, match="injected"):
            _apply(engine, fixture, fail_after=True)
        with engine.connect() as connection:
            assert connection.scalar(
                text("SELECT count(*) FROM coordination_runtime_drains WHERE task_id = :id"),
                {"id": fixture.task_id},
            ) == 0
            assert connection.scalar(
                text("SELECT status FROM task_runs WHERE id = :id"),
                {"id": fixture.sibling_run_id},
            ) == "RUNNING"
            assert connection.scalar(
                text("SELECT phase FROM runtime_executions WHERE id = :id"),
                {"id": fixture.sibling_execution_id},
            ) == "PREPARED"
    finally:
        _cleanup(engine, fixture)
        engine.dispose()


def test_postgres_barrier_drain_precedes_crossed_cancel_and_locks_are_serialized() -> None:
    engine = create_engine(get_settings().database_url, pool_size=4, max_overflow=0)
    fixture = _seed(engine, sibling_boundary="crossed")
    try:
        barrier = Barrier(2)

        def apply_once():
            with _factory(engine)() as uow:
                uow._session.execute(text("SET LOCAL lock_timeout = '2s'"))
                barrier.wait(timeout=3)
                aggregate = CoordinatedRuntimeAggregateLocker().lock(
                    uow, tenant_id=fixture.tenant_id, task_id=fixture.task_id
                )
                plan = plan_known_terminal(
                    aggregate,
                    triggering_run_id=fixture.target_run_id,
                    phase=KnownTerminalPhase.FAILED,
                    cancel_intent_present=False,
                    safe_error=None,
                )
                result = CoordinatedRuntimeBarrierApplier().apply_in_uow(
                    uow,
                    aggregate=aggregate,
                    plan=plan,
                    now=datetime.now(UTC) + timedelta(minutes=1),
                    cancel_deadline_window=timedelta(minutes=5),
                )
                uow.commit()
                return result

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _item: apply_once(), (1, 2)))
        assert len(results) == 2
        with engine.connect() as connection:
            assert connection.scalar(
                text("SELECT count(*) FROM coordination_runtime_drains WHERE task_id = :id"),
                {"id": fixture.task_id},
            ) == 1
    finally:
        _cleanup(engine, fixture)
        engine.dispose()
