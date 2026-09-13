"""Real PostgreSQL qualification for coordinated unknown-outcome parking."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text

from agentmesh.application.coordinated_runtime_convergence import (
    CoordinatedRuntimeConvergenceService,
)
from agentmesh.application.coordinated_runtime_unknown import (
    CoordinatedRuntimeUnknownOutcomeService,
    CoordinatedUnknownOutcomeKind,
)
from agentmesh.application.runtime_snapshots import assignment_snapshot_for
from agentmesh.domain.coordination import (
    CoordinationRuntimeDrain,
    CoordinationRuntimeDrainTarget,
)
from agentmesh.domain.runtime_execution import RuntimeExecution, RuntimeExecutionPhase
from agentmesh.domain.tasks import TaskAttempt
from agentmesh.infrastructure.postgres.repositories import SqlAlchemyOutboxRepository
from agentmesh.infrastructure.postgres.runtime_repositories import SqlAlchemyRuntimeRepository
from agentmesh.runtime_sdk import RuntimeObservation, RuntimePhase
from tests.integration.test_coordinated_runtime_convergence_postgres import (
    _add_budget_reservation,
    _add_quota_reservation,
    _add_sibling,
    _budget_projection,
    _cleanup,
    _counts,
    _real_scheduler,
    _running_fixture,
)

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("AGENTMESH_RUN_POSTGRES_TESTS") != "1",
        reason="set AGENTMESH_RUN_POSTGRES_TESTS=1 to run unknown-outcome PostgreSQL tests",
    ),
]


def _service(fixture):
    return CoordinatedRuntimeUnknownOutcomeService(
        uow_factory=fixture.factory,
        cancel_deadline_window=timedelta(minutes=5),
    )


def _observation(execution, *, phase, observed_at, provider_event_present=True):
    return RuntimeObservation(
        observation_id=str(uuid4()),
        runtime_execution_id=str(execution.id),
        assignment_id=str(execution.assignment_id),
        assignment_digest=execution.assignment_digest,
        phase=phase,
        observed_at=observed_at,
        provider_event_id=(
            f"unknown-pg-{uuid4().hex}" if provider_event_present else None
        ),
        snapshot_digest=None if provider_event_present else "c" * 64,
        provider_sequence=2,
    )


def _deliver(service, fixture, execution, observation, *, received_at):
    return service.park_unknown(
        tenant_id=fixture.tenant_id,
        task_id=fixture.task.id,
        run_id=fixture.run.id,
        attempt_id=fixture.attempt.id,
        fencing_token=fixture.attempt.fencing_token,
        runtime_execution_id=execution.id,
        observation=observation,
        received_at=received_at,
        causation_id=uuid4(),
    )


def _projection(engine, fixture, execution_id):
    with engine.connect() as connection:
        return {
            "task": tuple(
                connection.execute(
                    text(
                        "SELECT status, current_run_id, error, output, candidate_output "
                        "FROM tasks WHERE id = :id"
                    ),
                    {"id": fixture.task.id},
                ).one()
            ),
            "run": tuple(
                connection.execute(
                    text("SELECT status, error, output FROM task_runs WHERE id = :id"),
                    {"id": fixture.run.id},
                ).one()
            ),
            "attempt": tuple(
                connection.execute(
                    text(
                        "SELECT status, error, settled_tokens, settled_cost_micros, "
                        "budget_settlement_source FROM task_attempts WHERE id = :id"
                    ),
                    {"id": fixture.attempt.id},
                ).one()
            ),
            "subtask": (
                tuple(
                    connection.execute(
                        text("SELECT status, error, output FROM subtasks WHERE id = :id"),
                        {"id": fixture.run.subtask_id},
                    ).one()
                )
                if fixture.run.subtask_id is not None
                else None
            ),
            "execution": tuple(
                connection.execute(
                    text(
                        "SELECT phase, provider_sequence, current_owner_attempt_id, "
                        "current_fencing_token FROM runtime_executions WHERE id = :id"
                    ),
                    {"id": execution_id},
                ).one()
            ),
        }


@pytest.mark.parametrize(
    ("phase", "provider_event_present"),
    [(RuntimePhase.LOST, True), (RuntimePhase.OUTCOME_UNKNOWN, False)],
)
def test_postgres_executor_unknown_parks_once_and_exact_replay_is_read_only(
    phase, provider_event_present
):
    engine = create_engine(os.environ["AGENTMESH_DATABASE_URL"])
    fixture, execution, now = _running_fixture(engine)
    _add_budget_reservation(engine, fixture)
    _add_quota_reservation(fixture)
    service = _service(fixture)
    observation = _observation(
        execution,
        phase=phase,
        observed_at=now + timedelta(seconds=1),
        provider_event_present=provider_event_present,
    )
    try:
        first = _deliver(
            service,
            fixture,
            execution,
            observation,
            received_at=now + timedelta(seconds=2),
        )
        assert first.kind is CoordinatedUnknownOutcomeKind.PARKED
        before_counts = _counts(engine, fixture)
        before_budget = _budget_projection(engine, fixture)
        before_projection = _projection(engine, fixture, execution.id)
        assert before_counts["observations"] == 1
        assert before_counts["drains"] == 1
        assert before_counts["outbox"] == 1
        assert before_counts["active_quota"] == 0
        assert before_budget[0] == (0, 25, 0, 100)
        assert before_budget[1] == (25, 25, 100, 100, "CONSERVATIVE_ESTIMATE")
        assert before_budget[2][0][0] is not None
        expected_reason = (
            "runtime.lost" if phase is RuntimePhase.LOST else "runtime.outcome_unknown"
        )
        assert before_projection == {
            "task": (
                "RECONCILIATION_REQUIRED",
                None,
                "coordination.runtime_reconciliation_required",
                None,
                None,
            ),
            "run": ("RECONCILIATION_REQUIRED", expected_reason, None),
            "attempt": (
                "OUTCOME_UNKNOWN",
                expected_reason,
                25,
                100,
                "CONSERVATIVE_ESTIMATE",
            ),
            "subtask": ("RECONCILIATION_REQUIRED", expected_reason, None),
            "execution": (phase.value, 2, fixture.attempt.id, fixture.attempt.fencing_token),
        }
        with engine.connect() as connection:
            assert connection.scalar(
                text(
                    "SELECT provider_event_id IS NOT NULL FROM runtime_observations "
                    "WHERE tenant_id = :tenant_id"
                ),
                {"tenant_id": fixture.tenant_id},
            ) is provider_event_present
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM outbox_events WHERE tenant_id = :tenant_id "
                        "AND topic = 'agentmesh.runtime.reconciliation.required'"
                    ),
                    {"tenant_id": fixture.tenant_id},
                )
                == 1
            )
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM coordination_runtime_drains "
                        "WHERE task_id = :task_id AND status = 'DRAINING' AND target = 'RUNNING'"
                    ),
                    {"task_id": fixture.task.id},
                )
                == 1
            )
        replay = _deliver(
            service,
            fixture,
            execution,
            observation,
            received_at=now + timedelta(seconds=3),
        )
        assert replay.kind is CoordinatedUnknownOutcomeKind.REPLAY
        assert replay.drain_id == first.drain_id
        assert _counts(engine, fixture) == before_counts
        assert _budget_projection(engine, fixture) == before_budget
        assert _projection(engine, fixture, execution.id) == before_projection
    finally:
        _cleanup(engine, fixture)
        engine.dispose()


def _supervisor_fixture(engine):
    fixture, executor_execution, now = _running_fixture(engine)
    scheduler = _real_scheduler(engine, fixture)
    known = CoordinatedRuntimeConvergenceService(
        uow_factory=fixture.factory,
        coordinated_scheduler=scheduler,
        cancel_deadline_window=timedelta(minutes=5),
    )
    success = RuntimeObservation(
        observation_id=str(uuid4()),
        runtime_execution_id=str(executor_execution.id),
        assignment_id=str(executor_execution.assignment_id),
        assignment_digest=executor_execution.assignment_digest,
        phase=RuntimePhase.SUCCEEDED,
        observed_at=now + timedelta(seconds=1),
        provider_event_id=f"supervisor-seed-{uuid4().hex}",
        provider_sequence=2,
        output={"executor": "complete"},
    )
    known.apply_known_terminal(
        tenant_id=fixture.tenant_id,
        task_id=fixture.task.id,
        run_id=fixture.run.id,
        attempt_id=fixture.attempt.id,
        fencing_token=fixture.attempt.fencing_token,
        runtime_execution_id=executor_execution.id,
        observation=success,
        received_at=now + timedelta(seconds=2),
        causation_id=uuid4(),
    )
    with fixture.factory() as uow:
        runs = uow.runs.list_for_task(fixture.task.id)
        supervisor = next(value for value in runs if value.role.value == "SUPERVISOR")
        supervisor.start(at=now + timedelta(seconds=3))
        uow.runs.save(supervisor)
        uow.commit()
    attempt = TaskAttempt.lease(
        run_id=supervisor.id,
        worker_id="supervisor-unknown-pg",
        fencing_token=23,
        lease_expires_at=now + timedelta(hours=1),
    )
    with fixture.factory() as uow:
        uow.attempts.add(attempt)
        uow.commit()
    assignment = replace(
        fixture.assignment,
        assignment_id=str(uuid4()),
        run_id=str(supervisor.id),
        run_role="SUPERVISOR",
        structured_input={"source": "postgres", "role": "supervisor"},
        correlation_ids={"runtime_execution_id": str(supervisor.runtime_execution_intent_id)},
        assignment_digest=None,
    )
    execution_id = supervisor.runtime_execution_intent_id
    assert execution_id is not None
    snapshot = assignment_snapshot_for(
        assignment,
        tenant_id=fixture.tenant_id,
        runtime_execution_id=execution_id,
        created_at=now + timedelta(seconds=4),
    )
    execution = RuntimeExecution.prepare(
        tenant_id=fixture.tenant_id,
        run_id=supervisor.id,
        runtime_version_id=supervisor.runtime_version_id,
        assignment_id=UUID(assignment.assignment_id),
        assignment_digest=assignment.assignment_digest,
        dispatch_key=f"supervisor-pg:{execution_id}",
        dispatch_digest="b" * 64,
        execution_id=execution_id,
        now=now + timedelta(seconds=4),
    ).claim(
        attempt_id=attempt.id,
        fencing_token=attempt.fencing_token,
        expected_owner_attempt_id=None,
        expected_fencing_token=None,
        expected_version=1,
        now=now + timedelta(seconds=4),
    )
    execution = execution.apply_observation(
        phase=RuntimeExecutionPhase.RUNNING,
        provider_sequence=1,
        now=now + timedelta(seconds=5),
    )
    with fixture.factory() as uow:
        uow.runtimes.add_execution(execution)
        supervisor.bind_runtime_execution(execution_id)
        uow.runs.save(supervisor)
        uow.runtimes.add_assignment_snapshot(snapshot)
        uow.commit()
    values = vars(fixture).copy()
    values.update(run=supervisor, attempt=attempt)
    return SimpleNamespace(**values), execution, now + timedelta(seconds=5)


def test_postgres_supervisor_unknown_preserves_pointer_and_never_schedules():
    engine = create_engine(os.environ["AGENTMESH_DATABASE_URL"])
    fixture, execution, now = _supervisor_fixture(engine)
    observation = _observation(
        execution, phase=RuntimePhase.OUTCOME_UNKNOWN, observed_at=now + timedelta(seconds=1)
    )
    try:
        before_runs = _counts(engine, fixture)["runs"]
        result = _deliver(
            _service(fixture),
            fixture,
            execution,
            observation,
            received_at=now + timedelta(seconds=2),
        )
        assert result.kind is CoordinatedUnknownOutcomeKind.PARKED
        projection = _projection(engine, fixture, execution.id)
        assert projection["task"][:3] == (
            "RECONCILIATION_REQUIRED",
            fixture.run.id,
            "coordination.runtime_reconciliation_required",
        )
        assert projection["subtask"] is None
        assert _counts(engine, fixture)["runs"] == before_runs
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM task_runs WHERE task_id = :task_id "
                        "AND role = 'SUPERVISOR'"
                    ),
                    {"task_id": fixture.task.id},
                )
                == 1
            )
    finally:
        _cleanup(engine, fixture)
        engine.dispose()


def test_postgres_stopping_drain_requests_one_stable_sibling_cancel_on_replay():
    engine = create_engine(os.environ["AGENTMESH_DATABASE_URL"])
    fixture, execution, now = _running_fixture(engine)
    _add_sibling(engine, fixture, state="active")
    drain = CoordinationRuntimeDrain.start(
        drain_id=uuid4(),
        tenant_id=fixture.tenant_id,
        task_id=fixture.task.id,
        triggering_run_id=fixture.run.id,
        target=CoordinationRuntimeDrainTarget.FAILED,
        reason="existing first cause",
        at=now,
    )
    with fixture.factory() as uow:
        uow.coordination_runtime_drains.add(drain)
        uow.commit()
    service = _service(fixture)
    observation = _observation(
        execution, phase=RuntimePhase.LOST, observed_at=now + timedelta(seconds=1)
    )
    try:
        first = _deliver(
            service,
            fixture,
            execution,
            observation,
            received_at=now + timedelta(seconds=2),
        )
        assert first.kind is CoordinatedUnknownOutcomeKind.DRAINING_ACTIVE
        assert len(first.lifecycle_operation_ids) == 1
        before = _counts(engine, fixture)
        replay = _deliver(
            service,
            fixture,
            execution,
            observation,
            received_at=now + timedelta(seconds=3),
        )
        assert replay.kind is CoordinatedUnknownOutcomeKind.REPLAY
        assert replay.lifecycle_operation_ids == first.lifecycle_operation_ids
        assert _counts(engine, fixture) == before
        assert before["lifecycle"] == 1
    finally:
        _cleanup(engine, fixture)
        engine.dispose()


def test_postgres_concurrent_unknown_delivery_has_one_parking_and_one_replay():
    engine = create_engine(os.environ["AGENTMESH_DATABASE_URL"], pool_size=4, max_overflow=0)
    fixture, execution, now = _running_fixture(engine)
    _add_budget_reservation(engine, fixture)
    _add_quota_reservation(fixture)
    service = _service(fixture)
    observation = _observation(
        execution,
        phase=RuntimePhase.OUTCOME_UNKNOWN,
        observed_at=now + timedelta(seconds=1),
    )
    received = now + timedelta(seconds=2)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda _: _deliver(
                        service, fixture, execution, observation, received_at=received
                    ),
                    range(2),
                )
            )
        assert sorted(value.kind for value in results) == sorted(
            [CoordinatedUnknownOutcomeKind.PARKED, CoordinatedUnknownOutcomeKind.REPLAY]
        )
        counts = _counts(engine, fixture)
        assert counts["observations"] == 1
        assert counts["drains"] == 1
        assert counts["outbox"] == 1
        assert counts["active_quota"] == 0
        assert _budget_projection(engine, fixture)[1] == (
            25,
            25,
            100,
            100,
            "CONSERVATIVE_ESTIMATE",
        )
    finally:
        _cleanup(engine, fixture)
        engine.dispose()


@pytest.mark.parametrize("writer", ["execution", "outbox"])
def test_postgres_unknown_writer_failure_rolls_back_every_projection(monkeypatch, writer):
    engine = create_engine(os.environ["AGENTMESH_DATABASE_URL"])
    fixture, execution, now = _running_fixture(engine)
    _add_budget_reservation(engine, fixture)
    _add_quota_reservation(fixture)
    observation = _observation(
        execution, phase=RuntimePhase.LOST, observed_at=now + timedelta(seconds=1)
    )
    before_counts = _counts(engine, fixture)
    before_budget = _budget_projection(engine, fixture)
    before_projection = _projection(engine, fixture, execution.id)

    def fail(*args, **kwargs):
        raise RuntimeError(f"injected {writer} failure")

    if writer == "execution":
        monkeypatch.setattr(SqlAlchemyRuntimeRepository, "save_execution", fail)
    else:
        monkeypatch.setattr(SqlAlchemyOutboxRepository, "add", fail)
    try:
        with pytest.raises(RuntimeError, match=f"injected {writer} failure"):
            _deliver(
                _service(fixture),
                fixture,
                execution,
                observation,
                received_at=now + timedelta(seconds=2),
            )
        assert _counts(engine, fixture) == before_counts
        assert _budget_projection(engine, fixture) == before_budget
        assert _projection(engine, fixture, execution.id) == before_projection
    finally:
        _cleanup(engine, fixture)
        engine.dispose()
