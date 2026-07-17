from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock
from uuid import uuid4

import pytest


def test_downgrade_rejects_cross_tenant_legacy_key_conflicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    bind = Mock()
    bind.execute.return_value.first.return_value = ("worker", uuid4())
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)

    with pytest.raises(
        RuntimeError,
        match="multiple tenants share an Inbox",
    ):
        migration.downgrade()


def _load_migration() -> ModuleType:
    path = (
        Path(__file__).parents[1] / "alembic" / "versions" / "20260717_0010_messaging_retention.py"
    )
    spec = importlib.util.spec_from_file_location("messaging_retention_migration", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
