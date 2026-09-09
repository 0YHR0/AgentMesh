from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock
from uuid import uuid4

import pytest

from agentmesh.domain.coordination import (
    CoordinationRuntimeDrain,
    CoordinationRuntimeDrainStatus,
    CoordinationRuntimeDrainTarget,
)
from agentmesh.domain.errors import InvalidTaskInput
from agentmesh.infrastructure.postgres.models import CoordinationRuntimeDrainRecord
from agentmesh.infrastructure.postgres.repositories import (
    SqlAlchemyCoordinationRuntimeDrainRepository,
)


def _load_migration() -> ModuleType:
    path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "20260909_0052_coordination_runtime_drains.py"
    )
    spec = importlib.util.spec_from_file_location("coordination_runtime_drains_0052", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _drain(**changes) -> CoordinationRuntimeDrain:
    created = datetime(2026, 9, 9, 1, tzinfo=timezone.utc)
    values = {
        "id": uuid4(),
        "tenant_id": "tenant-a",
        "task_id": uuid4(),
        "triggering_run_id": uuid4(),
        "target": CoordinationRuntimeDrainTarget.RUNNING,
        "reason": "operator requested drain",
        "status": CoordinationRuntimeDrainStatus.DRAINING,
        "version": 1,
        "created_at": created,
        "updated_at": created,
        "completed_at": None,
    }
    values.update(changes)
    return CoordinationRuntimeDrain(**values)


def test_drain_projection_round_trips_and_has_closed_values() -> None:
    assert {value.value for value in CoordinationRuntimeDrainStatus} == {
        "DRAINING",
        "COMPLETE",
    }
    assert {value.value for value in CoordinationRuntimeDrainTarget} == {
        "RUNNING",
        "WAITING_APPROVAL",
        "FAILED",
        "CANCELED",
    }
    value = _drain()
    record = SqlAlchemyCoordinationRuntimeDrainRepository._to_record(value)
    assert record.status == "DRAINING"
    assert (
        SqlAlchemyCoordinationRuntimeDrainRepository._to_domain(record)
        == value
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "not-a-uuid"},
        {"tenant_id": ""},
        {"tenant_id": " "},
        {"tenant_id": "t" * 129},
        {"reason": ""},
        {"reason": " "},
        {"reason": " padded "},
        {"reason": "r" * 4097},
        {"status": "DRAINING"},
        {"target": "RUNNING"},
        {"version": 0},
        {"version": True},
        {"created_at": datetime(2026, 9, 9, 1)},
        {"created_at": datetime(2026, 9, 9, 1, tzinfo=timezone(timedelta(hours=1)))},
        {
            "updated_at": datetime(2026, 9, 9, tzinfo=timezone.utc) - timedelta(seconds=1)
        },
        {
            "status": CoordinationRuntimeDrainStatus.DRAINING,
            "completed_at": datetime(2026, 9, 9, 1, tzinfo=timezone.utc),
        },
        {"status": CoordinationRuntimeDrainStatus.COMPLETE},
        {
            "status": CoordinationRuntimeDrainStatus.COMPLETE,
            "completed_at": datetime(2026, 9, 8, 1, tzinfo=timezone.utc),
        },
        {
            "status": CoordinationRuntimeDrainStatus.COMPLETE,
            "completed_at": datetime(2026, 9, 9, 2, tzinfo=timezone.utc),
            "updated_at": datetime(2026, 9, 9, 1, tzinfo=timezone.utc),
        },
    ],
)
def test_drain_projection_rejects_malformed_values(changes) -> None:
    with pytest.raises(InvalidTaskInput):
        _drain(**changes)


def test_complete_projection_requires_ordered_completion_timestamp() -> None:
    completed = datetime(2026, 9, 9, 2, tzinfo=timezone.utc)
    value = _drain(
        status=CoordinationRuntimeDrainStatus.COMPLETE,
        completed_at=completed,
        updated_at=completed,
    )
    assert value.completed_at == completed


def test_orm_constraint_and_index_parity() -> None:
    constraints = {
        item.name: str(item.sqltext)
        for item in CoordinationRuntimeDrainRecord.__table__.constraints
        if item.name
    }
    assert constraints["ck_coordination_runtime_drains_status"] == (
        "status IN ('DRAINING', 'COMPLETE')"
    )
    assert constraints["ck_coordination_runtime_drains_target"] == (
        "target IN ('RUNNING', 'WAITING_APPROVAL', 'FAILED', 'CANCELED')"
    )
    assert "btrim(tenant_id)" in constraints["ck_coordination_runtime_drains_tenant"]
    assert "btrim(reason)" in constraints["ck_coordination_runtime_drains_reason"]
    assert "completed_at >= created_at" in constraints["ck_coordination_runtime_drains_completion"]
    indexes = {item.name: item for item in CoordinationRuntimeDrainRecord.__table__.indexes}
    assert set(indexes) == {
        "uq_coordination_runtime_drains_active_task",
        "ix_coordination_runtime_drains_tenant_status_updated",
        "ix_coordination_runtime_drains_task_created",
    }
    assert indexes["uq_coordination_runtime_drains_active_task"].unique is True


def test_migration_upgrade_only_creates_drain_table_and_indexes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    calls: list[tuple[str, tuple[object, ...]]] = []
    for name in ("create_table", "create_index"):
        monkeypatch.setattr(
            migration.op,
            name,
            lambda *args, _name=name, **kwargs: calls.append((_name, args)),
        )
    migration.upgrade()
    assert [name for name, _ in calls] == [
        "create_table",
        "create_index",
        "create_index",
        "create_index",
    ]
    assert calls[0][1][0] == "coordination_runtime_drains"
    assert [args[0] for name, args in calls if name == "create_index"] == [
        "uq_coordination_runtime_drains_active_task",
        "ix_coordination_runtime_drains_tenant_status_updated",
        "ix_coordination_runtime_drains_task_created",
    ]


def test_migration_downgrade_refuses_before_ddl_then_drops_exact_objects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    bind = Mock()
    bind.execute.return_value.first.return_value = (1,)
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    ddl: list[str] = []
    for name in ("drop_index", "drop_table"):
        monkeypatch.setattr(
            migration.op,
            name,
            lambda *args, _name=name, **kwargs: ddl.append(_name),
        )
    with pytest.raises(RuntimeError, match="0052.*schema and data are unchanged"):
        migration.downgrade()
    assert ddl == []

    bind.execute.return_value.first.return_value = None
    migration.downgrade()
    assert ddl == ["drop_index", "drop_index", "drop_index", "drop_table"]
