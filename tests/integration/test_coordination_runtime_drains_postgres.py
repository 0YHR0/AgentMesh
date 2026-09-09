"""Real PostgreSQL qualification for the A4.2c.2a drain reader floor."""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from agentmesh.config import get_settings
from agentmesh.domain.coordination import (
    CoordinationRuntimeDrain,
    CoordinationRuntimeDrainStatus,
    CoordinationRuntimeDrainTarget,
)
from agentmesh.domain.tasks import Task, TaskExecutionMode, TaskRun
from agentmesh.infrastructure.postgres.repositories import (
    SqlAlchemyCoordinationRuntimeDrainRepository,
    SqlAlchemyTaskRepository,
    SqlAlchemyTaskRunRepository,
)
from alembic import command

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("AGENTMESH_RUN_POSTGRES_TESTS") != "1",
        reason="set AGENTMESH_RUN_POSTGRES_TESTS=1 to run PostgreSQL migration tests",
    ),
]


def _config() -> Config:
    return Config("alembic.ini")


def _fixture(engine, *, task_tenant: str = "drain-tenant") -> tuple[Task, TaskRun]:
    task = Task.create(
        tenant_id=task_tenant,
        objective="coordination drain fixture",
        execution_mode=TaskExecutionMode.COORDINATED,
        plan_version=1,
        plan_digest=f"sha256:{uuid4().hex}",
        max_concurrency=1,
    )
    run = TaskRun.request(task.id, "drain-fixture")
    with Session(engine) as session, session.begin():
        session.add(SqlAlchemyTaskRepository._to_record(task))
        session.flush()
        session.add(SqlAlchemyTaskRunRepository._to_record(run))
    return task, run


def _drain(
    task: Task,
    run: TaskRun,
    *,
    drain_id: UUID | None = None,
    status: CoordinationRuntimeDrainStatus = CoordinationRuntimeDrainStatus.DRAINING,
    created_at: datetime | None = None,
    tenant_id: str | None = None,
) -> CoordinationRuntimeDrain:
    created = created_at or datetime(2026, 9, 9, 1, tzinfo=timezone.utc)
    completed_at = (
        created + timedelta(minutes=1)
        if status is CoordinationRuntimeDrainStatus.COMPLETE
        else None
    )
    return CoordinationRuntimeDrain(
        id=drain_id or uuid4(),
        tenant_id=tenant_id or task.tenant_id,
        task_id=task.id,
        triggering_run_id=run.id,
        target=CoordinationRuntimeDrainTarget.RUNNING,
        reason="operator requested drain",
        status=status,
        version=1,
        created_at=created,
        updated_at=completed_at or created,
        completed_at=completed_at,
    )


def _cleanup(engine, task_id: UUID) -> None:
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM tasks WHERE id = :id"), {"id": task_id})


def test_postgres_drain_round_trip_scope_order_lock_and_save() -> None:
    engine = create_engine(get_settings().database_url)
    task_id = None
    try:
        command.upgrade(_config(), "head")
        task, run = _fixture(engine)
        task_id = task.id
        repository = None
        first = _drain(task, run, created_at=datetime(2026, 9, 9, 1, tzinfo=timezone.utc))
        second = _drain(
            task,
            run,
            status=CoordinationRuntimeDrainStatus.COMPLETE,
            created_at=datetime(2026, 9, 9, 2, tzinfo=timezone.utc),
        )
        with Session(engine) as session, session.begin():
            repository = SqlAlchemyCoordinationRuntimeDrainRepository(session)
            repository.add(first)
            session.flush()
            repository.add(second)
            session.flush()
            assert repository.get(first.id, tenant_id=task.tenant_id) == first
            assert repository.get(first.id, tenant_id=task.tenant_id, for_update=True) == first
            assert repository.get_active_for_task(
                task.id, tenant_id=task.tenant_id, for_update=True
            ) == first
            assert repository.list_for_task(task.id, tenant_id=task.tenant_id) == [first, second]

            completed_at = datetime(2026, 9, 9, 3, tzinfo=timezone.utc)
            updated = replace(
                first,
                status=CoordinationRuntimeDrainStatus.COMPLETE,
                reason="operator completed drain",
                version=2,
                updated_at=completed_at,
                completed_at=completed_at,
            )
            repository.save(updated, tenant_id=task.tenant_id)
            session.flush()
            assert repository.get(first.id, tenant_id=task.tenant_id) == updated

        with Session(engine) as session:
            repository = SqlAlchemyCoordinationRuntimeDrainRepository(session)
            assert repository.get(first.id, tenant_id="other-tenant") is None
            assert repository.get_active_for_task(task.id, tenant_id="other-tenant") is None
            assert repository.list_for_task(task.id, tenant_id="other-tenant") == []
    finally:
        if task_id is not None:
            _cleanup(engine, task_id)
        command.upgrade(_config(), "head")
        engine.dispose()


def test_postgres_tenant_join_rejects_mismatched_duplicate_tenant_projection() -> None:
    engine = create_engine(get_settings().database_url)
    task_id = None
    try:
        command.upgrade(_config(), "head")
        task, run = _fixture(engine, task_tenant="owner-tenant")
        task_id = task.id
        mismatched = _drain(task, run, tenant_id="duplicate-tenant")
        with Session(engine) as session, session.begin():
            session.add(SqlAlchemyCoordinationRuntimeDrainRepository._to_record(mismatched))
        with Session(engine) as session:
            repository = SqlAlchemyCoordinationRuntimeDrainRepository(session)
            assert repository.get(mismatched.id, tenant_id="duplicate-tenant") is None
            assert repository.list_for_task(task.id, tenant_id="duplicate-tenant") == []
    finally:
        if task_id is not None:
            _cleanup(engine, task_id)
        command.upgrade(_config(), "head")
        engine.dispose()


@pytest.mark.parametrize(
    "column,value",
    [
        ("tenant_id", ""),
        ("tenant_id", " "),
        ("reason", " "),
        ("status", "UNKNOWN"),
        ("target", "UNKNOWN"),
        ("version", 0),
        ("updated_at", datetime(2026, 9, 9, tzinfo=timezone.utc)),
        ("completed_at", datetime(2026, 9, 9, 2, tzinfo=timezone.utc)),
        ("status", "COMPLETE"),
    ],
)
def test_postgres_rejects_invalid_drain_projection(column: str, value: object) -> None:
    engine = create_engine(get_settings().database_url)
    task_id = None
    try:
        command.upgrade(_config(), "head")
        task, run = _fixture(engine)
        task_id = task.id
        values = _drain(task, run)
        payload = {
            "id": values.id,
            "tenant_id": values.tenant_id,
            "task_id": values.task_id,
            "triggering_run_id": values.triggering_run_id,
            "target": values.target.value,
            "reason": values.reason,
            "status": values.status.value,
            "version": values.version,
            "created_at": values.created_at,
            "updated_at": values.updated_at,
            "completed_at": values.completed_at,
        }
        payload[column] = value
        with pytest.raises(SQLAlchemyError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO coordination_runtime_drains "
                        "(id, tenant_id, task_id, triggering_run_id, target, reason, status, "
                        "version, created_at, updated_at, completed_at) "
                        "VALUES (:id, :tenant_id, :task_id, :triggering_run_id, :target, :reason, "
                        ":status, :version, :created_at, :updated_at, :completed_at)"
                    ),
                    payload,
                )
    finally:
        if task_id is not None:
            _cleanup(engine, task_id)
        command.upgrade(_config(), "head")
        engine.dispose()


def test_postgres_active_partial_unique_and_constraint_index_parity() -> None:
    engine = create_engine(get_settings().database_url)
    task_id = None
    try:
        command.upgrade(_config(), "head")
        task, run = _fixture(engine)
        task_id = task.id
        first = _drain(task, run)
        second = _drain(task, run)
        with Session(engine) as session, session.begin():
            repository = SqlAlchemyCoordinationRuntimeDrainRepository(session)
            repository.add(first)
            session.flush()
            repository.add(second)
            with pytest.raises(SQLAlchemyError):
                session.flush()
                session.rollback()
        with engine.connect() as connection:
            indexes = {item["name"] for item in inspect(connection).get_indexes(
                "coordination_runtime_drains"
            )}
            assert {
                "uq_coordination_runtime_drains_active_task",
                "ix_coordination_runtime_drains_tenant_status_updated",
                "ix_coordination_runtime_drains_task_created",
            } <= indexes
            checks = {
                item["name"]: item["sqltext"]
                for item in inspect(connection).get_check_constraints(
                    "coordination_runtime_drains"
                )
            }
            assert "DRAINING" in checks["ck_coordination_runtime_drains_status"]
            assert "completed_at >= completed_at" not in str(checks)
    finally:
        if task_id is not None:
            _cleanup(engine, task_id)
        command.upgrade(_config(), "head")
        engine.dispose()


def test_postgres_clean_and_post_write_downgrade_floor() -> None:
    engine = create_engine(get_settings().database_url)
    task_id = None
    try:
        command.upgrade(_config(), "head")
        command.downgrade(_config(), "20260909_0051")
        assert not inspect(engine).has_table("coordination_runtime_drains")
        command.upgrade(_config(), "head")

        task, run = _fixture(engine)
        task_id = task.id
        value = _drain(task, run)
        with Session(engine) as session, session.begin():
            session.add(SqlAlchemyCoordinationRuntimeDrainRepository._to_record(value))
        with pytest.raises(RuntimeError, match="0052.*schema and data are unchanged"):
            command.downgrade(_config(), "20260909_0051")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "20260909_0052"
            )
            assert connection.scalar(
                text("SELECT count(*) FROM coordination_runtime_drains WHERE id = :id"),
                {"id": value.id},
            ) == 1
        _cleanup(engine, task_id)
        task_id = None
        command.downgrade(_config(), "20260909_0051")
        command.upgrade(_config(), "head")
    finally:
        if task_id is not None:
            _cleanup(engine, task_id)
        command.upgrade(_config(), "head")
        engine.dispose()
