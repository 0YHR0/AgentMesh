"""Transactional rollback qualification for coordinated reconciliation (c2e4)."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text

from agentmesh.infrastructure.postgres.repositories import (
    SqlAlchemyCoordinationRuntimeDrainRepository,
    SqlAlchemyIdempotencyRepository,
    SqlAlchemyOutboxRepository,
    SqlAlchemySubtaskRepository,
    SqlAlchemyTaskAttemptRepository,
    SqlAlchemyTaskRepository,
    SqlAlchemyTaskResolutionRepository,
    SqlAlchemyTaskRunRepository,
)
from agentmesh.infrastructure.postgres.runtime_repositories import (
    SqlAlchemyRuntimeRepository,
)
from agentmesh.infrastructure.postgres.uow import SqlAlchemyUnitOfWork
from agentmesh.runtime_sdk import RuntimePhase
from tests.integration.test_coordinated_runtime_convergence_postgres import (
    _RecordingScheduler,
)
from tests.integration.test_coordinated_runtime_reconciliation_postgres import (
    _cleanup_reconciliation,
    _parked,
    _reconcile,
    _reconciliation_scope_for_fixture,
    _service,
    _terminal,
)

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("AGENTMESH_RUN_POSTGRES_TESTS") != "1",
        reason="set AGENTMESH_RUN_POSTGRES_TESTS=1 to run reconciliation rollback tests",
    ),
]


_WRITER_BOUNDARIES = {
    "observation": (SqlAlchemyRuntimeRepository, "add_observation"),
    "execution": (SqlAlchemyRuntimeRepository, "save_execution"),
    "attempt": (SqlAlchemyTaskAttemptRepository, "save"),
    "run": (SqlAlchemyTaskRunRepository, "save"),
    "subtask": (SqlAlchemySubtaskRepository, "save"),
    "drain": (SqlAlchemyCoordinationRuntimeDrainRepository, "save"),
    "task": (SqlAlchemyTaskRepository, "save"),
    "resolution": (SqlAlchemyTaskResolutionRepository, "add"),
    "outbox": (SqlAlchemyOutboxRepository, "add"),
    "idempotency": (SqlAlchemyIdempotencyRepository, "add"),
    "commit": (SqlAlchemyUnitOfWork, "commit"),
}


def _json_rows(connection, query: str, parameters: dict[str, object]) -> str:
    return connection.scalar(text(query), parameters) or "[]"


def _transaction_projection(engine, fixture, execution) -> dict[str, str]:
    """Capture every c2e3 business and governance row as stable PostgreSQL JSON."""
    parameters = {
        "task_id": fixture.task.id,
        "tenant_id": fixture.tenant_id,
        "execution_id": execution.id,
        "scope": _reconciliation_scope_for_fixture(fixture, execution),
    }
    queries = {
        "task": (
            "SELECT jsonb_agg(to_jsonb(q) ORDER BY q.id)::text FROM "
            "(SELECT * FROM tasks WHERE id = :task_id) q"
        ),
        "runs": (
            "SELECT jsonb_agg(to_jsonb(q) ORDER BY q.id)::text FROM "
            "(SELECT * FROM task_runs WHERE task_id = :task_id) q"
        ),
        "attempts": (
            "SELECT jsonb_agg(to_jsonb(q) ORDER BY q.id)::text FROM "
            "(SELECT a.* FROM task_attempts a JOIN task_runs r ON r.id = a.run_id "
            "WHERE r.task_id = :task_id) q"
        ),
        "subtasks": (
            "SELECT jsonb_agg(to_jsonb(q) ORDER BY q.id)::text FROM "
            "(SELECT * FROM subtasks WHERE task_id = :task_id) q"
        ),
        "drains": (
            "SELECT jsonb_agg(to_jsonb(q) ORDER BY q.id)::text FROM "
            "(SELECT * FROM coordination_runtime_drains WHERE task_id = :task_id) q"
        ),
        "executions": (
            "SELECT jsonb_agg(to_jsonb(q) ORDER BY q.id)::text FROM "
            "(SELECT * FROM runtime_executions WHERE id = :execution_id) q"
        ),
        "evidence": (
            "SELECT jsonb_agg(to_jsonb(q) ORDER BY q.id)::text FROM "
            "(SELECT * FROM runtime_observations "
            "WHERE runtime_execution_id = :execution_id) q"
        ),
        "resolutions": (
            "SELECT jsonb_agg(to_jsonb(q) ORDER BY q.id)::text FROM "
            "(SELECT * FROM task_resolutions WHERE task_id = :task_id) q"
        ),
        "outbox": (
            "SELECT jsonb_agg(to_jsonb(q) ORDER BY q.id)::text FROM "
            "(SELECT * FROM outbox_events WHERE tenant_id = :tenant_id) q"
        ),
        "idempotency": (
            "SELECT jsonb_agg(to_jsonb(q) ORDER BY q.scope, q.key)::text FROM "
            "(SELECT * FROM idempotency_records WHERE scope = :scope) q"
        ),
        "quota": (
            "SELECT jsonb_agg(to_jsonb(q) ORDER BY q.id)::text FROM "
            "(SELECT qr.* FROM quota_reservations qr "
            "JOIN quota_policies qp ON qp.id = qr.policy_id "
            "WHERE qp.tenant_id = :tenant_id) q"
        ),
    }
    with engine.connect() as connection:
        return {name: _json_rows(connection, query, parameters) for name, query in queries.items()}


@pytest.mark.parametrize("writer", tuple(_WRITER_BOUNDARIES))
def test_postgres_each_reconciliation_writer_failure_rolls_back_all_projections(
    monkeypatch, writer
):
    engine = create_engine(os.environ["AGENTMESH_DATABASE_URL"])
    fixture, execution, now = _parked(engine, budget=True)
    service = _service(fixture, _RecordingScheduler())
    observation = _terminal(execution, now, RuntimePhase.FAILED)
    before = _transaction_projection(engine, fixture, execution)
    owner, method = _WRITER_BOUNDARIES[writer]

    def fail(*args, **kwargs):
        raise RuntimeError(f"injected {writer} failure")

    monkeypatch.setattr(owner, method, fail)
    try:
        with pytest.raises(RuntimeError, match=f"injected {writer} failure"):
            _reconcile(service, fixture, execution, observation)
        assert _transaction_projection(engine, fixture, execution) == before
    finally:
        _cleanup_reconciliation(engine, fixture, execution)
        engine.dispose()
