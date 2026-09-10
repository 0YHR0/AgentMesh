"""PostgreSQL qualification for the coordinated aggregate lock reader."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from agentmesh.application.coordinated_runtime import CoordinatedRuntimeAggregateLocker
from agentmesh.config import get_settings
from agentmesh.domain.tasks import Task, TaskExecutionMode
from agentmesh.infrastructure.postgres.repositories import SqlAlchemyTaskRepository
from agentmesh.infrastructure.postgres.uow import SqlAlchemyUnitOfWorkFactory

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
