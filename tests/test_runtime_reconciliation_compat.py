from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from agentmesh.domain.resolutions import TaskResolutionAction
from agentmesh.domain.runtime_execution import RuntimeObservationOutcome
from agentmesh.infrastructure.postgres.models import (
    RuntimeObservationRecord,
    TaskResolutionRecord,
)


@pytest.mark.parametrize(
    "value",
    [
        "RECONCILE_RUNTIME_SUCCEEDED",
        "RECONCILE_RUNTIME_FAILED",
        "RECONCILE_RUNTIME_CANCELED",
        "RECONCILE_RUNTIME_TIMED_OUT",
    ],
)
def test_expand_phase_resolution_reader_accepts_future_runtime_actions(value: str) -> None:
    assert TaskResolutionAction(value).value == value


def test_expand_phase_observation_reader_accepts_reconciled() -> None:
    assert (
        RuntimeObservationOutcome("RECONCILED")
        is RuntimeObservationOutcome.RECONCILED
    )


def test_orm_constraint_matches_0048_expand_value() -> None:
    constraint = next(
        value
        for value in RuntimeObservationRecord.__table__.constraints
        if value.name == "ck_runtime_observations_outcome"
    )
    assert "RECONCILED" in str(constraint.sqltext)
    resolution_constraint = next(
        value
        for value in TaskResolutionRecord.__table__.constraints
        if value.name == "ck_task_resolutions_action"
    )
    assert "RECONCILE_RUNTIME_SUCCEEDED" in str(resolution_constraint.sqltext)
    assert "RECONCILE_RUNTIME_TIMED_OUT" in str(resolution_constraint.sqltext)


def test_0048_clean_downgrade_restores_old_constraint_without_rewriting_data(
    monkeypatch,
) -> None:
    migration = _load_migration()
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        migration.op,
        "drop_constraint",
        lambda name, table, **kwargs: calls.append(("drop", name)),
    )
    monkeypatch.setattr(
        migration.op,
        "create_check_constraint",
        lambda name, table, condition: calls.append(("create", str(condition))),
    )

    migration.downgrade()

    assert [kind for kind, _ in calls] == ["drop", "create", "drop", "create"]
    assert "RECONCILED" not in calls[1][1]
    assert "RECONCILE_RUNTIME" not in calls[-1][1]


def _load_migration() -> ModuleType:
    path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "20260821_0048_runtime_outcome_reconciliation.py"
    )
    spec = importlib.util.spec_from_file_location("runtime_reconciliation_0048", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
