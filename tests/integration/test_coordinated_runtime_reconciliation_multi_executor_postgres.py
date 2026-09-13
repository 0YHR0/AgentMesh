"""Multi-Executor PostgreSQL qualification for coordinated reconciliation (c2e4)."""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from agentmesh.application.coordinated_runtime_convergence import (
    CoordinatedRuntimeConvergenceService,
)
from agentmesh.application.runtime_contracts import TerminalObservationValidator
from agentmesh.domain.budgets import TaskBudget
from agentmesh.domain.quotas import QuotaReservation
from agentmesh.infrastructure.postgres.models import TaskAttemptRecord, TaskRecord
from agentmesh.runtime_sdk import RuntimePhase
from tests.integration.test_coordinated_runtime_convergence_postgres import (
    _add_quota_reservation,
    _add_sibling,
    _budget_projection,
    _cleanup,
    _counts,
    _real_scheduler,
    _RecordingScheduler,
    _running_fixture,
)
from tests.integration.test_coordinated_runtime_reconciliation_postgres import (
    _principal,
    _reconciliation_scope_for_fixture,
    _terminal,
)
from tests.integration.test_coordinated_runtime_reconciliation_postgres import (
    _service as _reconciliation_service,
)
from tests.integration.test_coordinated_runtime_unknown_postgres import (
    _observation as _unknown_observation,
)
from tests.integration.test_coordinated_runtime_unknown_postgres import (
    _service as _unknown_service,
)

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("AGENTMESH_RUN_POSTGRES_TESTS") != "1",
        reason="set AGENTMESH_RUN_POSTGRES_TESTS=1 to run multi-Executor tests",
    ),
]


def _execution_for(fixture, run):
    with fixture.factory() as uow:
        executions = uow.runtimes.list_executions_for_run(run.id, tenant_id=fixture.tenant_id)
    assert len(executions) == 1
    return executions[0]


def _install_accounting(engine, fixture, sibling_attempt):
    budget = TaskBudget.create(
        max_attempts=10,
        max_tokens=100,
        token_reservation_per_attempt=25,
        max_cost_micros=1000,
        cost_reservation_micros_per_attempt=100,
    )
    with Session(engine) as session, session.begin():
        task = session.get(TaskRecord, fixture.task.id)
        primary = session.get(TaskAttemptRecord, fixture.attempt.id)
        sibling = session.get(TaskAttemptRecord, sibling_attempt.id)
        assert task is not None and primary is not None and sibling is not None
        task.budget = budget.to_dict()
        task.reserved_tokens = 50
        task.reserved_cost_micros = 200
        for attempt in (primary, sibling):
            attempt.reserved_tokens = 25
            attempt.reserved_cost_micros = 100

    policy, _primary_reservation = _add_quota_reservation(fixture)
    sibling_reservation = QuotaReservation.acquire(
        policy_id=policy.id,
        attempt_id=sibling_attempt.id,
        tenant_id=fixture.tenant_id,
        project_id=fixture.task.project_id,
    )
    with fixture.factory() as uow:
        uow.quotas.add_reservation(sibling_reservation)
        uow.commit()


def _park(fixture, *, run, attempt, execution, observed_at):
    observation = _unknown_observation(
        execution,
        phase=RuntimePhase.OUTCOME_UNKNOWN,
        observed_at=observed_at,
        provider_event_present=False,
    )
    return _unknown_service(fixture).park_unknown(
        tenant_id=fixture.tenant_id,
        task_id=fixture.task.id,
        run_id=run.id,
        attempt_id=attempt.id,
        fencing_token=attempt.fencing_token,
        runtime_execution_id=execution.id,
        observation=observation,
        received_at=observed_at + timedelta(seconds=1),
        causation_id=uuid4(),
    )


def _reconcile(
    service,
    fixture,
    *,
    run,
    attempt,
    execution,
    phase,
    observed_at,
    key,
):
    observation = _terminal(execution, observed_at - timedelta(seconds=1), phase)
    return service.reconcile_known_terminal(
        tenant_id=fixture.tenant_id,
        task_id=fixture.task.id,
        run_id=run.id,
        attempt_id=attempt.id,
        fencing_token=attempt.fencing_token,
        runtime_execution_id=execution.id,
        principal=_principal(fixture.tenant_id),
        observation=observation,
        evidence_digest=TerminalObservationValidator.digest(observation),
        evidence_reference="audit://postgres/multi-executor-proof",
        reason="multi-Executor independently verified conclusion",
        idempotency_key=key,
        received_at=observed_at + timedelta(seconds=1),
    )


def _prepare_pair(engine):
    fixture, primary_execution, now = _running_fixture(engine)
    executions = (primary_execution,)
    try:
        sibling_subtask, sibling_run, sibling_attempt = _add_sibling(
            engine, fixture, state="active"
        )
        assert sibling_attempt is not None
        sibling_execution = _execution_for(fixture, sibling_run)
        executions = (primary_execution, sibling_execution)
        _install_accounting(engine, fixture, sibling_attempt)
        _park(
            fixture,
            run=fixture.run,
            attempt=fixture.attempt,
            execution=primary_execution,
            observed_at=now + timedelta(seconds=1),
        )
        _park(
            fixture,
            run=sibling_run,
            attempt=sibling_attempt,
            execution=sibling_execution,
            observed_at=now + timedelta(seconds=3),
        )
    except Exception:
        _cleanup_pair(engine, fixture, executions)
        raise
    return (
        fixture,
        (fixture.run, fixture.attempt, primary_execution),
        (sibling_run, sibling_attempt, sibling_execution),
        sibling_subtask,
        now,
    )


def _projection(engine, fixture):
    with engine.connect() as connection:
        return {
            "task": tuple(
                connection.execute(
                    text(
                        "SELECT status, error, output, candidate_output, reserved_tokens, "
                        "settled_tokens, reserved_cost_micros, settled_cost_micros "
                        "FROM tasks WHERE id = :task_id"
                    ),
                    {"task_id": fixture.task.id},
                ).one()
            ),
            "runs": connection.execute(
                text(
                    "SELECT id, status, error, output FROM task_runs "
                    "WHERE task_id = :task_id AND role = 'EXECUTOR' ORDER BY id"
                ),
                {"task_id": fixture.task.id},
            ).all(),
            "attempts": connection.execute(
                text(
                    "SELECT ta.id, ta.status, ta.error, ta.settled_tokens, "
                    "ta.settled_cost_micros, ta.budget_settlement_source "
                    "FROM task_attempts ta JOIN task_runs tr ON tr.id = ta.run_id "
                    "WHERE tr.task_id = :task_id ORDER BY ta.id"
                ),
                {"task_id": fixture.task.id},
            ).all(),
            "subtasks": connection.execute(
                text(
                    "SELECT id, status, error, output FROM subtasks "
                    "WHERE task_id = :task_id ORDER BY id"
                ),
                {"task_id": fixture.task.id},
            ).all(),
            "executions": connection.execute(
                text(
                    "SELECT re.id, re.phase, re.provider_sequence "
                    "FROM runtime_executions re JOIN task_runs tr ON tr.id = re.run_id "
                    "WHERE tr.task_id = :task_id ORDER BY re.id"
                ),
                {"task_id": fixture.task.id},
            ).all(),
            "drain": connection.execute(
                text(
                    "SELECT target, status FROM coordination_runtime_drains "
                    "WHERE task_id = :task_id"
                ),
                {"task_id": fixture.task.id},
            ).one(),
        }


def _assert_exact_audit(engine, fixture, executions, *, expected_terminals):
    with engine.connect() as connection:
        observations = connection.execute(
            text(
                "SELECT runtime_execution_id, phase, processing_outcome "
                "FROM runtime_observations WHERE tenant_id = :tenant_id "
                "ORDER BY runtime_execution_id, received_at"
            ),
            {"tenant_id": fixture.tenant_id},
        ).all()
        resolutions = (
            connection.execute(
                text(
                    "SELECT details FROM task_resolutions WHERE task_id = :task_id "
                    "ORDER BY created_at"
                ),
                {"task_id": fixture.task.id},
            )
            .scalars()
            .all()
        )
        topics = (
            connection.execute(
                text(
                    "SELECT topic FROM outbox_events WHERE tenant_id = :tenant_id "
                    "ORDER BY created_at, id"
                ),
                {"tenant_id": fixture.tenant_id},
            )
            .scalars()
            .all()
        )
        idempotency_count = sum(
            connection.scalar(
                text("SELECT count(*) FROM idempotency_records WHERE scope = :scope"),
                {"scope": _reconciliation_scope_for_fixture(fixture, execution)},
            )
            for execution in executions
        )
    assert len(observations) == 4
    assert {row[0] for row in observations} == {value.id for value in executions}
    assert sorted(row[1] for row in observations) == sorted(
        ["OUTCOME_UNKNOWN", "OUTCOME_UNKNOWN", *expected_terminals]
    )
    assert [row[2] for row in observations].count("APPLIED") == 2
    assert [row[2] for row in observations].count("RECONCILED") == 2
    assert len(resolutions) == 2
    assert {value["execution_id"] for value in resolutions} == {
        str(value.id) for value in executions
    }
    assert topics.count("agentmesh.runtime.reconciliation.required") == 2
    assert topics.count("agentmesh.runtime.outcome-reconciled") == 2
    assert idempotency_count == 2


def _cleanup_pair(engine, fixture, executions):
    with engine.begin() as connection:
        for execution in executions:
            connection.execute(
                text("DELETE FROM idempotency_records WHERE scope = :scope"),
                {"scope": _reconciliation_scope_for_fixture(fixture, execution)},
            )
    _cleanup(engine, fixture)


@pytest.mark.parametrize("order", [(0, 1), (1, 0)])
def test_two_uncertain_executors_reconcile_in_either_order_and_schedule_once(order):
    engine = create_engine(os.environ["AGENTMESH_DATABASE_URL"])
    fixture, primary, sibling, _sibling_subtask, now = _prepare_pair(engine)
    targets = (primary, sibling)
    executions = tuple(value[2] for value in targets)
    scheduler = _RecordingScheduler()
    service = _reconciliation_service(fixture, scheduler)
    try:
        accounting_after_park = _budget_projection(engine, fixture)
        first = targets[order[0]]
        second = targets[order[1]]
        _reconcile(
            service,
            fixture,
            run=first[0],
            attempt=first[1],
            execution=first[2],
            phase=RuntimePhase.SUCCEEDED,
            observed_at=now + timedelta(seconds=5),
            key=f"multi-success-{order[0]}",
        )
        assert scheduler.calls == []
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT status FROM coordination_runtime_drains WHERE task_id = :id"),
                    {"id": fixture.task.id},
                )
                == "DRAINING"
            )
        _reconcile(
            service,
            fixture,
            run=second[0],
            attempt=second[1],
            execution=second[2],
            phase=RuntimePhase.SUCCEEDED,
            observed_at=now + timedelta(seconds=7),
            key=f"multi-success-{order[1]}",
        )
        projection = _projection(engine, fixture)
        assert projection["task"] == ("RUNNING", None, None, None, 0, 50, 0, 200)
        assert {row[1] for row in projection["runs"]} == {"SUCCEEDED"}
        assert {row[1] for row in projection["attempts"]} == {"SUCCEEDED"}
        assert {row[1] for row in projection["subtasks"]} == {"COMPLETED"}
        assert {row[1] for row in projection["executions"]} == {"SUCCEEDED"}
        assert projection["drain"] == ("RUNNING", "COMPLETE")
        assert _budget_projection(engine, fixture) == accounting_after_park
        assert _counts(engine, fixture)["active_quota"] == 0
        assert len(scheduler.calls) == 1
        _assert_exact_audit(
            engine, fixture, executions, expected_terminals=["SUCCEEDED", "SUCCEEDED"]
        )
    finally:
        _cleanup_pair(engine, fixture, executions)
        engine.dispose()


@pytest.mark.parametrize(
    ("active_phase", "expected_task", "expected_drain", "expected_schedules"),
    [
        (RuntimePhase.SUCCEEDED, "RUNNING", "RUNNING", 1),
        (RuntimePhase.FAILED, "FAILED", "FAILED", 0),
    ],
)
def test_uncertain_success_then_active_sibling_terminal_converges_once(
    active_phase, expected_task, expected_drain, expected_schedules
):
    engine = create_engine(os.environ["AGENTMESH_DATABASE_URL"])
    fixture, primary_execution, now = _running_fixture(engine)
    _subtask, sibling_run, sibling_attempt = _add_sibling(engine, fixture, state="active")
    assert sibling_attempt is not None
    sibling_execution = _execution_for(fixture, sibling_run)
    _install_accounting(engine, fixture, sibling_attempt)
    scheduler = (
        _real_scheduler(engine, fixture)
        if active_phase is RuntimePhase.SUCCEEDED
        else _RecordingScheduler()
    )
    reconcile_service = _reconciliation_service(fixture, scheduler)
    known_service = CoordinatedRuntimeConvergenceService(
        uow_factory=fixture.factory,
        coordinated_scheduler=scheduler,
        cancel_deadline_window=timedelta(minutes=5),
    )
    executions = (primary_execution, sibling_execution)
    try:
        _park(
            fixture,
            run=fixture.run,
            attempt=fixture.attempt,
            execution=primary_execution,
            observed_at=now + timedelta(seconds=1),
        )
        accounting_after_park = _budget_projection(engine, fixture)
        _reconcile(
            reconcile_service,
            fixture,
            run=fixture.run,
            attempt=fixture.attempt,
            execution=primary_execution,
            phase=RuntimePhase.SUCCEEDED,
            observed_at=now + timedelta(seconds=3),
            key="uncertain-success",
        )
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM task_runs WHERE task_id = :id AND role = 'SUPERVISOR'"
                    ),
                    {"id": fixture.task.id},
                )
                == 0
            )
        terminal = _terminal(sibling_execution, now + timedelta(seconds=4), active_phase)
        terminal = replace(terminal, provider_sequence=2)
        known_result = known_service.apply_known_terminal(
            tenant_id=fixture.tenant_id,
            task_id=fixture.task.id,
            run_id=sibling_run.id,
            attempt_id=sibling_attempt.id,
            fencing_token=sibling_attempt.fencing_token,
            runtime_execution_id=sibling_execution.id,
            observation=terminal,
            received_at=now + timedelta(seconds=6),
            causation_id=uuid4(),
        )
        projection = _projection(engine, fixture)
        assert projection["task"][0] == expected_task
        assert projection["drain"] == (expected_drain, "COMPLETE")
        assert {row[1] for row in projection["executions"]} == {
            "SUCCEEDED",
            active_phase.value,
        }
        accounting_after_terminal = _budget_projection(engine, fixture)
        assert accounting_after_park[0] == (25, 25, 100, 100)
        assert accounting_after_terminal[0] == (
            (0, 50, 0, 200) if active_phase is RuntimePhase.SUCCEEDED else (0, 25, 0, 100)
        )
        assert accounting_after_terminal[1] == (
            25,
            25,
            100,
            100,
            "CONSERVATIVE_ESTIMATE",
        )
        expected_attempt_accounting = {(25, 100, "CONSERVATIVE_ESTIMATE")}
        if active_phase is RuntimePhase.FAILED:
            expected_attempt_accounting.add((0, 0, "RELEASED"))
        assert {
            (row[3], row[4], row[5]) for row in projection["attempts"]
        } == expected_attempt_accounting
        assert all(row[0] is not None for row in accounting_after_terminal[2])
        assert _counts(engine, fixture)["active_quota"] == 0
        with engine.connect() as connection:
            supervisor_ids = (
                connection.execute(
                    text("SELECT id FROM task_runs WHERE task_id = :id AND role = 'SUPERVISOR'"),
                    {"id": fixture.task.id},
                )
                .scalars()
                .all()
            )
            assert len(supervisor_ids) == expected_schedules
            assert set(known_result.scheduled_run_ids) == set(supervisor_ids)
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM task_resolutions WHERE task_id = :id"),
                    {"id": fixture.task.id},
                )
                == 1
            )
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM outbox_events WHERE tenant_id = :tenant "
                        "AND topic = 'agentmesh.runtime.outcome-reconciled'"
                    ),
                    {"tenant": fixture.tenant_id},
                )
                == 1
            )
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM runtime_observations "
                        "WHERE tenant_id = :tenant AND processing_outcome = 'RECONCILED'"
                    ),
                    {"tenant": fixture.tenant_id},
                )
                == 1
            )
    finally:
        _cleanup_pair(engine, fixture, executions)
        engine.dispose()
