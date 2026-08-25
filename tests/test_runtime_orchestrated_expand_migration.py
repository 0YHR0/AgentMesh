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
        / "20260825_0049_runtime_orchestrated_expand.py"
    )
    spec = importlib.util.spec_from_file_location("runtime_orchestrated_expand_0049", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_follows_0048_and_upgrade_is_expand_only(monkeypatch: pytest.MonkeyPatch) -> None:
    migration = _load_migration()
    assert migration.down_revision == "20260821_0048"

    calls: list[tuple[str, tuple[object, ...]]] = []
    for name in (
        "create_table",
        "create_index",
        "add_column",
        "create_check_constraint",
    ):
        monkeypatch.setattr(
            migration.op,
            name,
            lambda *args, _name=name, **kwargs: calls.append((_name, args)),
        )

    migration.upgrade()

    tables = [args[0] for name, args in calls if name == "create_table"]
    assert tables == [
        "runtime_assignment_snapshots",
        "runtime_handle_snapshots",
        "runtime_integrity_incidents",
    ]
    columns = [args[1].name for name, args in calls if name == "add_column"]
    assert columns == [
        "attempt_count",
        "next_attempt_at",
        "claim_token",
        "claim_acquired_at",
        "claim_expires_at",
        "last_error_code",
    ]
    assert not any(name in {"execute", "update"} for name, _ in calls)


def test_default_only_downgrade_drops_expand_objects(monkeypatch: pytest.MonkeyPatch) -> None:
    migration = _load_migration()
    bind = Mock()
    bind.execute.return_value.first.return_value = None
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    calls: list[tuple[str, str]] = []
    for name in ("drop_index", "drop_constraint", "drop_column", "drop_table"):
        monkeypatch.setattr(
            migration.op,
            name,
            lambda *args, _name=name, **kwargs: calls.append((_name, str(args))),
        )

    migration.downgrade()

    assert [name for name, _ in calls].count("drop_table") == 3
    assert [name for name, _ in calls].count("drop_column") == 6


def test_downgrade_refuses_lifecycle_writer_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    bind = Mock()
    bind.execute.return_value.first.return_value = (1,)
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    with pytest.raises(RuntimeError, match="writer markers"):
        migration.downgrade()


@pytest.mark.parametrize(
    "table",
    [
        "runtime_assignment_snapshots",
        "runtime_handle_snapshots",
        "runtime_integrity_incidents",
    ],
)
def test_downgrade_refuses_rows_without_cross_tenant_cleanup(
    monkeypatch: pytest.MonkeyPatch, table: str
) -> None:
    migration = _load_migration()
    bind = Mock()
    results = iter(
        [
            None,
            (1,) if table == "runtime_assignment_snapshots" else None,
            (1,) if table == "runtime_handle_snapshots" else None,
            (1,) if table == "runtime_integrity_incidents" else None,
        ]
    )
    bind.execute.return_value.first.side_effect = lambda: next(results)
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    with pytest.raises(RuntimeError, match=f"{table} contains rows"):
        migration.downgrade()
