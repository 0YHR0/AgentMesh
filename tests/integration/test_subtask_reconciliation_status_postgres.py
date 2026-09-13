"""Real PostgreSQL qualification for the A4.2c.1 reader-only status floor."""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agentmesh.api.schemas import TaskResponse
from agentmesh.config import get_settings
from agentmesh.domain.coordination import Subtask, SubtaskStatus
from agentmesh.domain.tasks import Task, TaskAggregate, TaskExecutionMode
from agentmesh.infrastructure.postgres.repositories import (
    SqlAlchemySubtaskRepository,
    SqlAlchemyTaskRepository,
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


def _at_0050() -> None:
    command.upgrade(_config(), "head")
    command.downgrade(_config(), "20260826_0050")


def _insert_subtask(engine, *, status: str) -> tuple[UUID, UUID]:
    task = Task.create(
        tenant_id=f"subtask-status-{uuid4().hex}",
        objective="migration fixture",
        execution_mode=TaskExecutionMode.COORDINATED,
        plan_version=1,
        plan_digest=f"sha256:{uuid4().hex}",
        max_concurrency=1,
    )
    subtask = Subtask.create(
        subtask_id=uuid4(),
        task_id=task.id,
        key="reader",
        objective="reader compatibility",
        input={"bounded": True},
        required_capabilities=("general.task",),
        preferred_agent_id=None,
        initially_ready=True,
    )
    if status in {value.value for value in SubtaskStatus}:
        subtask.status = SubtaskStatus(status)
    with Session(engine) as session, session.begin():
        session.add(SqlAlchemyTaskRepository._to_record(task))
        session.flush()
        record = SqlAlchemySubtaskRepository._to_record(subtask)
        record.status = status
        session.add(record)
    return task.id, subtask.id


def _delete_task(engine, task_id: UUID) -> None:
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM tasks WHERE id = :id"), {"id": task_id})


def test_upgrade_preserves_existing_status_rows_and_replaces_only_constraint() -> None:
    engine = create_engine(get_settings().database_url)
    task_id = None
    try:
        _at_0050()
        task_id, subtask_id = _insert_subtask(engine, status="READY")
        command.upgrade(_config(), "20260909_0051")
        with engine.connect() as connection:
            assert connection.scalar(
                text("SELECT status FROM subtasks WHERE id = :id"), {"id": subtask_id}
            ) == "READY"
            assert connection.scalar(
                text("SELECT version_num FROM alembic_version")
            ) == "20260909_0051"
            checks = inspect(connection).get_check_constraints("subtasks")
            status_check = next(item for item in checks if item["name"] == "ck_subtasks_status")
            assert "RECONCILIATION_REQUIRED" in status_check["sqltext"]
        _delete_task(engine, task_id)
        command.downgrade(_config(), "20260826_0050")
    finally:
        if task_id is not None:
            _delete_task(engine, task_id)
        command.upgrade(_config(), "head")
        engine.dispose()


def test_orm_and_database_constraint_parity_and_reader_round_trip() -> None:
    engine = create_engine(get_settings().database_url)
    task_id = None
    try:
        command.upgrade(_config(), "head")
        task_id, subtask_id = _insert_subtask(
            engine, status="RECONCILIATION_REQUIRED"
        )
        with Session(engine) as session:
            loaded = SqlAlchemySubtaskRepository(session).get(subtask_id)
            assert loaded is not None
            assert loaded.status is SubtaskStatus.RECONCILIATION_REQUIRED
            task = Task.create(
                tenant_id=loaded.task_id.hex,
                objective="projection",
                execution_mode=TaskExecutionMode.COORDINATED,
                plan_version=1,
                plan_digest="sha256:reader-projection",
                max_concurrency=1,
            )
            task.id = loaded.task_id
            response = TaskResponse._subtask_responses(
                TaskAggregate(task=task, subtasks=[loaded])
            )[0]
            assert response.status is SubtaskStatus.RECONCILIATION_REQUIRED
            assert response.model_dump(mode="json")["status"] == "RECONCILIATION_REQUIRED"

        with pytest.raises(IntegrityError):
            _insert_subtask(engine, status="NOT_A_SUBTASK_STATUS")
    finally:
        if task_id is not None:
            _delete_task(engine, task_id)
        command.upgrade(_config(), "head")
        engine.dispose()


def test_downgrade_refuses_post_write_without_ddl_or_data_loss_then_succeeds_after_cleanup(
    ) -> None:
    engine = create_engine(get_settings().database_url)
    task_id = None
    try:
        _at_0050()
        command.upgrade(_config(), "head")
        task_id, subtask_id = _insert_subtask(
            engine, status="RECONCILIATION_REQUIRED"
        )
        with engine.connect() as connection:
            before_version = connection.scalar(text("SELECT version_num FROM alembic_version"))
            before_status = connection.scalar(
                text("SELECT status FROM subtasks WHERE id = :id"), {"id": subtask_id}
            )
            before_constraint = connection.scalar(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = 'ck_subtasks_status'"
                )
            )
        with pytest.raises(RuntimeError, match="0051.*schema and data are unchanged"):
            command.downgrade(_config(), "20260826_0050")
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == before_version
            )
            assert connection.scalar(
                text("SELECT status FROM subtasks WHERE id = :id"), {"id": subtask_id}
            ) == before_status
            assert connection.scalar(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = 'ck_subtasks_status'"
                )
            ) == before_constraint

        _delete_task(engine, task_id)
        task_id = None
        command.downgrade(_config(), "20260826_0050")
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == "20260826_0050"
            )
    finally:
        if task_id is not None:
            _delete_task(engine, task_id)
        command.upgrade(_config(), "head")
        engine.dispose()
