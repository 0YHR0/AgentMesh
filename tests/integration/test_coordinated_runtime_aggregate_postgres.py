"""PostgreSQL qualification for the coordinated aggregate lock reader."""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier, Event
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from agentmesh.application.coordinated_runtime import CoordinatedRuntimeAggregateLocker
from agentmesh.config import get_settings
from agentmesh.domain.coordination import Subtask, SubtaskStatus
from agentmesh.domain.tasks import RunRole, RunStatus, Task, TaskExecutionMode, TaskRun
from agentmesh.infrastructure.postgres.models import (
    RuntimeExecutionRecord,
    RuntimeLifecycleOperationRecord,
    RuntimeRegistrationRecord,
    RuntimeVersionRecord,
    TaskRecord,
    TaskRunRecord,
)
from agentmesh.infrastructure.postgres.repositories import (
    SqlAlchemySubtaskRepository,
    SqlAlchemyTaskRepository,
    SqlAlchemyTaskRunRepository,
)
from agentmesh.infrastructure.postgres.uow import SqlAlchemyUnitOfWorkFactory
from tests.integration.test_runtime_control_plane_postgres import _fixture

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("AGENTMESH_RUN_POSTGRES_TESTS") != "1",
        reason="set AGENTMESH_RUN_POSTGRES_TESTS=1 to run coordinated aggregate PostgreSQL tests",
    ),
]


def test_postgres_locker_returns_stable_empty_coordinated_projection() -> None:
    engine = create_engine(get_settings().database_url)
    task = Task.create(
        tenant_id=f"aggregate-pg-{uuid4().hex}",
        objective="aggregate lock fixture",
        execution_mode=TaskExecutionMode.COORDINATED,
        plan_version=1,
        plan_digest=f"sha256:{uuid4().hex}",
        max_concurrency=2,
    )
    try:
        with Session(engine) as session, session.begin():
            session.add(SqlAlchemyTaskRepository._to_record(task))
        factory = SqlAlchemyUnitOfWorkFactory(
            sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        )
        with factory() as uow:
            aggregate = CoordinatedRuntimeAggregateLocker().lock(
                uow, tenant_id=task.tenant_id, task_id=task.id
            )
            assert aggregate.task.id == task.id
            assert aggregate.active_drain is None
            assert aggregate.subtasks == ()
            assert aggregate.runs == ()
            assert aggregate.executions == ()
            assert dict(aggregate.runtime_versions) == {}
    finally:
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM tasks WHERE id = :task_id"), {"task_id": task.id})
        engine.dispose()


def _empty_task(engine):
    task = Task.create(
        tenant_id=f"aggregate-pg-{uuid4().hex}",
        objective="aggregate lock fixture",
        execution_mode=TaskExecutionMode.COORDINATED,
        plan_version=1,
        plan_digest=f"sha256:{uuid4().hex}",
        max_concurrency=2,
    )
    with Session(engine) as session, session.begin():
        session.add(SqlAlchemyTaskRepository._to_record(task))
    return task


def _factory(engine):
    return SqlAlchemyUnitOfWorkFactory(
        sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    )


def test_postgres_task_lock_serializes_two_helpers() -> None:
    engine = create_engine(get_settings().database_url, pool_size=4, max_overflow=0)
    task = _empty_task(engine)
    factory = _factory(engine)
    second_ready = Event()
    second_done = Event()
    errors: list[BaseException] = []

    first = factory()
    first.__enter__()
    try:
        CoordinatedRuntimeAggregateLocker().lock(
            first, tenant_id=task.tenant_id, task_id=task.id
        )

        def second() -> None:
            uow = factory()
            uow.__enter__()
            try:
                uow._session.execute(text("SET LOCAL lock_timeout = '2s'"))
                second_ready.set()
                CoordinatedRuntimeAggregateLocker().lock(
                    uow, tenant_id=task.tenant_id, task_id=task.id
                )
                uow.commit()
            except BaseException as exc:  # pragma: no cover - reported below
                errors.append(exc)
            finally:
                uow.__exit__(None, None, None)
                second_done.set()

        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(second)
            assert second_ready.wait(2)
            time.sleep(0.25)
            assert not second_done.is_set()
            first.commit()
            assert second_done.wait(3)
        assert errors == []
    finally:
        first.__exit__(None, None, None)
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM tasks WHERE id = :task_id"), {"task_id": task.id})
        engine.dispose()


def test_postgres_reverse_insertion_has_uuid_ordered_locked_projection() -> None:
    engine = create_engine(get_settings().database_url)
    task = _empty_task(engine)
    now = datetime.now(timezone.utc)
    runtime_id = uuid4()
    version_id = uuid4()
    subtasks = []
    runs = []
    for index in range(3):
        subtask = Subtask.create(
            subtask_id=uuid4(),
            task_id=task.id,
            key=f"key-{index}",
            objective="ordered",
            input={},
            required_capabilities=("general.task",),
            preferred_agent_id=None,
            initially_ready=True,
        )
        subtask.status = SubtaskStatus.COMPLETED
        run = TaskRun.request_deterministic_shadow(
            task.id,
            f"agent-{index}",
            runtime_version_id=version_id,
            role=RunRole.EXECUTOR,
            subtask_id=subtask.id,
            at=now,
        )
        run.status = RunStatus.SUCCEEDED
        run.completed_at = now
        subtasks.append(subtask)
        runs.append(run)
    execution_ids = [uuid4(), uuid4()]
    try:
        with Session(engine) as session, session.begin():
            owner_principal_id = session.scalar(text("SELECT id FROM principals LIMIT 1"))
            assert owner_principal_id is not None
            session.add(
                RuntimeRegistrationRecord(
                    id=runtime_id,
                    tenant_id=None,
                    name=f"aggregate-pg-{uuid4().hex}",
                    owner_principal_id=owner_principal_id,
                    visibility="platform",
                    status="ACTIVE",
                    default_version_id=None,
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                RuntimeVersionRecord(
                    id=version_id,
                    runtime_id=runtime_id,
                    api_version=1,
                    adapter_kind="python-in-process",
                    artifact_digest="a" * 64,
                    configuration_digest="b" * 64,
                    descriptor={},
                    trust_profile="built_in",
                    compatibility={},
                    status="PUBLISHED",
                    created_at=now,
                    published_at=now,
                    revoked_at=None,
                )
            )
            session.flush()
            session.execute(
                text(
                    "UPDATE runtime_registrations SET default_version_id = :version_id "
                    "WHERE id = :runtime_id"
                ),
                {"version_id": version_id, "runtime_id": runtime_id},
            )
            session.add_all(
                [SqlAlchemySubtaskRepository._to_record(value) for value in reversed(subtasks)]
            )
            session.flush()
            session.add_all(
                [SqlAlchemyTaskRunRepository._to_record(value) for value in reversed(runs)]
            )
            session.flush()
            session.add_all(
                [
                    RuntimeExecutionRecord(
                        id=execution_id,
                        tenant_id=task.tenant_id,
                        run_id=runs[0].id,
                        runtime_version_id=version_id,
                        assignment_id=uuid4(),
                        assignment_digest="c" * 64,
                        dispatch_key=f"aggregate-pg:{execution_id}",
                        dispatch_digest="d" * 64,
                        provider_execution_ref=None,
                        provider_generation=None,
                        phase="SUCCEEDED",
                        current_owner_attempt_id=None,
                        current_fencing_token=None,
                        provider_sequence=None,
                        checkpoint_ref=None,
                        workspace_ref=None,
                        version=1,
                        created_at=now,
                        updated_at=now,
                        terminal_at=now,
                    )
                    for execution_id in reversed(execution_ids)
                ]
            )
        # Repository ordering is verified in a separate transaction so the
        # qualification test never demonstrates a Run -> Task lock sequence.
        with _factory(engine)() as uow:
            locked_subtasks = uow.subtasks.list_for_task(task.id, for_update=True)
            locked_runs = uow.runs.list_for_task(task.id, for_update=True)
            uow.commit()
        with _factory(engine)() as uow:
            aggregate = CoordinatedRuntimeAggregateLocker().lock(
                uow, tenant_id=task.tenant_id, task_id=task.id
            )
            expected_subtasks = sorted(value.id for value in subtasks)
            expected_runs = sorted(value.id for value in runs)
            assert [value.id for value in locked_subtasks] == expected_subtasks
            assert [value.id for value in locked_runs] == expected_runs
            assert [value.id for value in aggregate.subtasks] == expected_subtasks
            assert [value.id for value in aggregate.runs] == expected_runs
            assert [value.id for value in aggregate.executions] == sorted(execution_ids)
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM runtime_executions WHERE run_id IN (:run_a, :run_b, :run_c)"),
                {"run_a": runs[0].id, "run_b": runs[1].id, "run_c": runs[2].id},
            )
            connection.execute(text("DELETE FROM tasks WHERE id = :task_id"), {"task_id": task.id})
            connection.execute(
                text("UPDATE runtime_registrations SET default_version_id = NULL WHERE id = :id"),
                {"id": runtime_id},
            )
            connection.execute(
                text("DELETE FROM runtime_versions WHERE id = :id"), {"id": version_id}
            )
            connection.execute(
                text("DELETE FROM runtime_registrations WHERE id = :id"), {"id": runtime_id}
            )
        engine.dispose()


def test_postgres_tenant_mismatched_runtime_row_is_invisible_to_helper() -> None:
    engine = create_engine(get_settings().database_url)
    fixture_ids: tuple[object, object, object] | None = None
    try:
        with Session(engine) as session, session.begin():
            _repository, execution = _fixture(session)
            run = session.get(TaskRunRecord, execution.run_id)
            assert run is not None
            task = session.get(TaskRecord, run.task_id)
            assert task is not None
            task.execution_mode = "COORDINATED"
            task.plan_version = 1
            task.plan_digest = f"sha256:{uuid4().hex}"
            task.max_concurrency = 2
            run.comparison_mode = "deterministic_shadow"
            run.runtime_execution_intent_id = execution.id
            execution_record = session.get(RuntimeExecutionRecord, execution.id)
            assert execution_record is not None
            execution_record.tenant_id = "other-tenant"
            now = datetime.now(timezone.utc)
            session.add(
                RuntimeLifecycleOperationRecord(
                    id=uuid4(),
                    tenant_id="other-tenant",
                    runtime_execution_id=execution.id,
                    operation_id=f"mismatched:{execution.id}",
                    operation="cancel",
                    intent_digest="d" * 64,
                    status="REQUESTED",
                    deadline=now,
                    receipt_summary=None,
                    attempt_count=0,
                    next_attempt_at=None,
                    claim_token=None,
                    claim_acquired_at=None,
                    claim_expires_at=None,
                    last_error_code=None,
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            fixture_ids = (task.id, execution.id, execution.runtime_version_id)
            tenant_id = task.tenant_id
        with _factory(engine)() as uow:
            aggregate = CoordinatedRuntimeAggregateLocker().lock(
                uow, tenant_id=tenant_id, task_id=fixture_ids[0]
            )
            assert aggregate.executions == ()
        with engine.connect() as connection:
            assert connection.scalar(
                text("SELECT tenant_id FROM runtime_executions WHERE id = :id"),
                {"id": fixture_ids[1]},
            ) == "other-tenant"
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM runtime_lifecycle_operations "
                    "WHERE runtime_execution_id = :id AND tenant_id = 'other-tenant'"
                ),
                {"id": fixture_ids[1]},
            ) == 1
    finally:
        if fixture_ids is not None:
            with engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM runtime_executions WHERE id = :id"),
                    {"id": fixture_ids[1]},
                )
                connection.execute(text("DELETE FROM tasks WHERE id = :id"), {"id": fixture_ids[0]})
        engine.dispose()


def test_postgres_two_helpers_same_aggregate_do_not_deadlock() -> None:
    engine = create_engine(get_settings().database_url, pool_size=4, max_overflow=0)
    task = _empty_task(engine)
    factory = _factory(engine)
    barrier = Barrier(2)

    def run_helper() -> bool:
        uow = factory()
        uow.__enter__()
        try:
            uow._session.execute(text("SET LOCAL lock_timeout = '2s'"))
            barrier.wait(timeout=2)
            CoordinatedRuntimeAggregateLocker().lock(
                uow, tenant_id=task.tenant_id, task_id=task.id
            )
            uow.commit()
            return True
        finally:
            uow.__exit__(None, None, None)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _value: run_helper(), (1, 2)))
        assert results == [True, True]
    finally:
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM tasks WHERE id = :task_id"), {"task_id": task.id})
        engine.dispose()
