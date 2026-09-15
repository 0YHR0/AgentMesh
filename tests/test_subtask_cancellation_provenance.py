from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from agentmesh.domain.coordination import (
    Subtask,
    SubtaskCancellationSource,
    SubtaskStatus,
    utc_now,
)
from agentmesh.domain.errors import InvalidTaskInput, InvalidTaskTransition
from agentmesh.infrastructure.postgres.models import (
    CoordinationRuntimeDrainRecord,
    SubtaskRecord,
)
from agentmesh.infrastructure.postgres.repositories import SqlAlchemySubtaskRepository


def _queued_subtask() -> tuple[Subtask, object]:
    task_id = uuid4()
    run_id = uuid4()
    subtask = Subtask.create(
        subtask_id=uuid4(),
        task_id=task_id,
        key="worker",
        objective="bounded work",
        input={},
        required_capabilities=(),
        preferred_agent_id=None,
        initially_ready=True,
    )
    at = subtask.updated_at + timedelta(seconds=1)
    subtask.queue(run_id, at=at)
    return subtask, run_id


def test_budget_drain_cancellation_requires_matching_provenance() -> None:
    subtask, run_id = _queued_subtask()
    drain_id = uuid4()
    at = subtask.updated_at + timedelta(seconds=1)

    subtask.cancel_by_drain(run_id, drain_id, at=at)

    assert subtask.status is SubtaskStatus.CANCELED
    assert subtask.cancellation_source is SubtaskCancellationSource.BUDGET_DRAIN
    assert subtask.canceled_by_drain_id == drain_id

    with pytest.raises(InvalidTaskTransition, match="not this budget Drain"):
        subtask.reopen_after_budget_drain(uuid4(), at=at + timedelta(seconds=1))

    subtask.reopen_after_budget_drain(drain_id, at=at + timedelta(seconds=1))
    assert subtask.status is SubtaskStatus.BLOCKED
    assert subtask.cancellation_source is None
    assert subtask.canceled_by_drain_id is None


def test_legacy_null_cancellation_provenance_fails_closed() -> None:
    subtask, _ = _queued_subtask()
    at = subtask.updated_at + timedelta(seconds=1)
    subtask.cancel(at=at)

    assert subtask.status is SubtaskStatus.CANCELED
    assert subtask.cancellation_source is None
    with pytest.raises(InvalidTaskTransition, match="not this budget Drain"):
        subtask.reopen_after_budget_drain(uuid4(), at=at + timedelta(seconds=1))


def test_legacy_reopen_method_keeps_unscoped_compatibility() -> None:
    subtask, _ = _queued_subtask()
    subtask.cancel()

    subtask.reopen_after_budget()

    assert subtask.status is SubtaskStatus.BLOCKED


def test_cancellation_source_and_drain_identity_must_be_consistent() -> None:
    with pytest.raises(InvalidTaskInput, match="provenance is incomplete"):
        Subtask(
            id=uuid4(),
            task_id=uuid4(),
            key="worker",
            objective="bounded work",
            input={},
            required_capabilities=(),
            preferred_agent_id=None,
            status=SubtaskStatus.CANCELED,
            current_run_id=None,
            output=None,
            error="canceled",
            version=1,
            created_at=utc_now(),
            updated_at=utc_now(),
            cancellation_source=SubtaskCancellationSource.BUDGET_DRAIN,
        )

    with pytest.raises(InvalidTaskInput, match="requires CANCELED status"):
        Subtask(
            id=uuid4(),
            task_id=uuid4(),
            key="worker",
            objective="bounded work",
            input={},
            required_capabilities=(),
            preferred_agent_id=None,
            status=SubtaskStatus.READY,
            current_run_id=None,
            output=None,
            error=None,
            version=1,
            created_at=utc_now(),
            updated_at=utc_now(),
            cancellation_source=SubtaskCancellationSource.USER,
        )


def test_drain_cancellation_rejects_nonempty_projection() -> None:
    subtask, run_id = _queued_subtask()
    subtask.output = {"unexpected": True}

    with pytest.raises(InvalidTaskTransition, match="projection is not empty"):
        subtask.cancel_by_drain(run_id, uuid4())


def test_postgres_model_contains_tenant_safe_provenance_constraints() -> None:
    constraint_names = {
        constraint.name for constraint in SubtaskRecord.__table__.constraints
    }
    assert "ck_subtasks_cancellation_source" in constraint_names
    assert "ck_subtasks_cancellation_provenance" in constraint_names
    assert "fk_subtasks_canceled_by_drain_task" in constraint_names
    provenance_check = next(
        constraint.sqltext
        for constraint in SubtaskRecord.__table__.constraints
        if constraint.name == "ck_subtasks_cancellation_provenance"
    )
    assert "cancellation_source IS NOT NULL" in str(provenance_check)
    assert "uq_coordination_runtime_drains_id_task" in {
        constraint.name for constraint in CoordinationRuntimeDrainRecord.__table__.constraints
    }
    assert "ix_subtasks_canceled_by_drain" in {
        index.name for index in SubtaskRecord.__table__.indexes
    }


def test_postgres_repository_round_trips_enum_as_storage_value() -> None:
    subtask, run_id = _queued_subtask()
    drain_id = uuid4()
    at = subtask.updated_at + timedelta(seconds=1)
    subtask.cancel_by_drain(run_id, drain_id, at=at)

    record = SqlAlchemySubtaskRepository._to_record(subtask)
    assert record.cancellation_source == "budget_drain"
    assert record.canceled_by_drain_id == drain_id

    loaded = SqlAlchemySubtaskRepository._to_domain(record)
    assert loaded.cancellation_source is SubtaskCancellationSource.BUDGET_DRAIN
    assert loaded.canceled_by_drain_id == drain_id


def test_migration_is_incremental_and_preserves_legacy_nulls() -> None:
    migration = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "20260915_0053_subtask_cancellation_provenance.py"
    ).read_text(encoding="utf-8")
    assert 'revision = "20260915_0053"' in migration
    assert 'down_revision = "20260909_0052"' in migration
    assert 'sa.Column("cancellation_source"' in migration
    assert 'sa.Column("canceled_by_drain_id"' in migration
    assert "UPDATE subtasks" not in migration
    assert "fk_subtasks_canceled_by_drain_task" in migration
