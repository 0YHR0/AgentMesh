from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock
from uuid import uuid4

import pytest

from agentmesh.api.schemas import TaskResponse
from agentmesh.domain.coordination import (
    TERMINAL_SUBTASK_STATUSES,
    Subtask,
    SubtaskStatus,
)
from agentmesh.domain.errors import InvalidTaskTransition
from agentmesh.domain.tasks import Task, TaskAggregate, TaskExecutionMode
from agentmesh.infrastructure.postgres.models import SubtaskRecord
from agentmesh.infrastructure.postgres.repositories import SqlAlchemySubtaskRepository


def _load_migration() -> ModuleType:
    path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "20260909_0051_subtask_reconciliation_status.py"
    )
    spec = importlib.util.spec_from_file_location("subtask_reconciliation_status_0051", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _coordinated_task() -> Task:
    return Task.create(
        tenant_id="tenant",
        objective="coordinated reader fixture",
        execution_mode=TaskExecutionMode.COORDINATED,
        plan_version=1,
        plan_digest="sha256:reader-fixture",
        max_concurrency=1,
    )


def _subtask() -> Subtask:
    now = datetime.now(timezone.utc)
    task = _coordinated_task()
    value = Subtask.create(
        subtask_id=uuid4(),
        task_id=task.id,
        key="reader",
        objective="reader compatibility",
        input={"bounded": True},
        required_capabilities=("general.task",),
        preferred_agent_id=None,
        initially_ready=True,
    )
    value.status = SubtaskStatus.RECONCILIATION_REQUIRED
    value.current_run_id = uuid4()
    value.updated_at = now
    return value


def test_reconciliation_required_is_nonterminal_and_reader_round_trips() -> None:
    assert SubtaskStatus.RECONCILIATION_REQUIRED not in TERMINAL_SUBTASK_STATUSES
    value = _subtask()
    record = SqlAlchemySubtaskRepository._to_record(value)
    assert record.status == "RECONCILIATION_REQUIRED"
    loaded = SqlAlchemySubtaskRepository._to_domain(record)
    assert loaded.status is SubtaskStatus.RECONCILIATION_REQUIRED

    task = _coordinated_task()
    loaded.task_id = task.id
    response = TaskResponse._subtask_responses(
        TaskAggregate(task=task, subtasks=[loaded])
    )[0]
    assert response.status is SubtaskStatus.RECONCILIATION_REQUIRED
    assert TaskResponse._subtask_responses(
        TaskAggregate(task=task, subtasks=[loaded])
    )[0].model_dump(mode="json")["status"] == "RECONCILIATION_REQUIRED"


@pytest.mark.parametrize(
    "transition",
    [
        lambda value: value.mark_ready(),
        lambda value: value.queue(uuid4()),
        lambda value: value.start(value.current_run_id),
        lambda value: value.complete(value.current_run_id, {"ok": True}),
        lambda value: value.fail(value.current_run_id, "failed"),
        lambda value: value.cancel(),
        lambda value: value.reopen_after_budget(),
    ],
    ids=["mark-ready", "queue", "start", "complete", "fail", "cancel", "reopen"],
)
def test_ordinary_subtask_transitions_reject_reader_loaded_status(transition) -> None:
    value = _subtask()
    before = (value.status, value.current_run_id, value.version)
    with pytest.raises(InvalidTaskTransition, match="RECONCILIATION_REQUIRED"):
        transition(value)
    assert (value.status, value.current_run_id, value.version) == before


def test_orm_constraint_has_exact_reader_status_set() -> None:
    constraint = next(
        item
        for item in SubtaskRecord.__table__.constraints
        if item.name == "ck_subtasks_status"
    )
    assert str(constraint.sqltext) == (
        "status IN ('BLOCKED', 'READY', 'RUNNING', 'RECONCILIATION_REQUIRED', "
        "'COMPLETED', 'FAILED', 'CANCELED')"
    )


def test_upgrade_only_replaces_subtask_status_constraint(monkeypatch: pytest.MonkeyPatch) -> None:
    migration = _load_migration()
    calls: list[tuple[str, tuple[object, ...]]] = []
    for name in ("drop_constraint", "create_check_constraint"):
        monkeypatch.setattr(
            migration.op,
            name,
            lambda *args, _name=name, **kwargs: calls.append((_name, args)),
        )
    migration.upgrade()
    assert [name for name, _ in calls] == ["drop_constraint", "create_check_constraint"]
    assert calls[0][1] == ("ck_subtasks_status", "subtasks")
    assert calls[1][1][:2] == ("ck_subtasks_status", "subtasks")
    assert "RECONCILIATION_REQUIRED" in calls[1][1][2]


def test_downgrade_refuses_before_ddl_and_restores_exact_old_constraint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    bind = Mock()
    bind.execute.return_value.first.return_value = (1,)
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    ddl: list[str] = []
    monkeypatch.setattr(migration.op, "drop_constraint", lambda *args, **kwargs: ddl.append("drop"))
    monkeypatch.setattr(
        migration.op,
        "create_check_constraint",
        lambda *args, **kwargs: ddl.append("create"),
    )
    with pytest.raises(RuntimeError, match="0051.*schema and data are unchanged"):
        migration.downgrade()
    assert ddl == []

    bind.execute.return_value.first.return_value = None
    migration.downgrade()
    assert ddl == ["drop", "create"]
    assert migration._OLD_STATUS_CHECK == (
        "status IN ('BLOCKED', 'READY', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELED')"
    )
