from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest


def _load_migration() -> ModuleType:
    path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "20260826_0050_runtime_integrity_incident_actions.py"
    )
    spec = importlib.util.spec_from_file_location("runtime_integrity_actions_0050", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_and_upgrade_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    migration = _load_migration()
    assert migration.down_revision == "20260825_0049"
    calls = []
    for name in ("drop_constraint", "create_check_constraint", "create_table", "create_index"):
        monkeypatch.setattr(
            migration.op,
            name,
            lambda *args, _name=name, **kwargs: calls.append((_name, args, kwargs)),
        )
    migration.upgrade()
    create_table = next(args for name, args, _ in calls if name == "create_table")
    assert create_table[0] == "runtime_integrity_incident_actions"
    columns = [column for column in create_table[1:] if hasattr(column, "name")]
    assert {column.name for column in columns} >= {
        "id",
        "tenant_id",
        "incident_id",
        "action",
        "from_status",
        "to_status",
        "actor_principal_id",
        "reason",
        "request_digest",
        "created_at",
    }
    assert any(
        "accepted_phase IN ('SUCCEEDED', 'FAILED', 'CANCELED', 'TIMED_OUT')" in str(args)
        for name, args, _ in calls
        if name == "create_check_constraint"
    )
    checks = {
        args[0]: args[2]
        for name, args, _ in calls
        if name == "create_check_constraint"
    }
    assert checks["ck_runtime_integrity_incident_digests"] == (
        "accepted_observation_digest ~ '^[0-9a-f]{64}$' AND "
        "conflicting_observation_digest ~ '^[0-9a-f]{64}$' AND "
        "accepted_observation_digest <> conflicting_observation_digest"
    )
    assert checks["ck_runtime_integrity_incident_timestamps"] == "updated_at >= created_at"


def test_downgrade_refuses_written_action_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    migration = _load_migration()
    bind = Mock()
    bind.execute.return_value.first.return_value = (1,)
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    with pytest.raises(RuntimeError, match="actions contain rows"):
        migration.downgrade()


def test_downgrade_empty_ledger_drops_and_restores_old_constraint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    bind = Mock()
    bind.execute.return_value.first.return_value = None
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    calls = []
    for name in ("drop_index", "drop_table", "drop_constraint", "create_check_constraint"):
        monkeypatch.setattr(
            migration.op,
            name,
            lambda *args, _name=name, **kwargs: calls.append((_name, args, kwargs)),
        )
    migration.downgrade()
    assert ("drop_table", "runtime_integrity_incident_actions") in [
        (name, args[0]) for name, args, _ in calls if name == "drop_table"
    ]
    assert any(
        "accepted_phase IN ('SUCCEEDED', 'FAILED', 'CANCELED', 'TIMED_OUT', 'LOST')" in str(args)
        for name, args, _ in calls
        if name == "create_check_constraint"
    )
