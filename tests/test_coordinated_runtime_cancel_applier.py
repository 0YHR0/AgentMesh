from __future__ import annotations

import ast
from dataclasses import replace
from datetime import timedelta, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from agentmesh.application.coordinated_runtime_cancel_applier import (
    CoordinatedRuntimeCancelApplier,
)
from agentmesh.application.coordinated_runtime_control import (
    CoordinatedCancelActionKind,
    CoordinatedCancelCompletion,
    plan_cancel_request,
)
from agentmesh.domain.budgets import BudgetSettlementSource, TaskBudget
from agentmesh.domain.coordination import (
    COORDINATION_USER_CANCEL_REQUESTED,
    CoordinationRuntimeBoundary,
    CoordinationRuntimeDrainStatus,
    CoordinationRuntimeDrainTarget,
    SubtaskStatus,
)
from agentmesh.domain.errors import RuntimeExecutionConflict
from agentmesh.domain.runtime_execution import RuntimeExecutionPhase
from agentmesh.domain.tasks import AttemptStatus, RunRole, RunStatus, TaskStatus
from tests.test_coordinated_runtime_barrier import _aggregate_for_sibling_boundaries
from tests.test_coordinated_runtime_barrier_applier import _Uow

UTC = timezone.utc


def _local_case(boundary: CoordinationRuntimeBoundary, *, drain_target=None):
    _task, _target, siblings, aggregate = _aggregate_for_sibling_boundaries(
        (boundary,),
        drain_target=drain_target,
    )
    subtask, run, attempt, execution = siblings[0]
    active_drain = aggregate.active_drain
    if active_drain is not None:
        active_drain = replace(
            active_drain,
            id=uuid5(
                NAMESPACE_URL,
                f"coordination-runtime-drain:{aggregate.task.tenant_id}:{aggregate.task.id}",
            ),
            triggering_run_id=run.id,
        )
    aggregate = replace(
        aggregate,
        active_drain=active_drain,
        subtasks=(subtask,),
        runs=(run,),
        latest_attempts={run.id: attempt},
        executions=(() if execution is None else (execution,)),
        assignment_snapshots=tuple(
            value
            for value in aggregate.assignment_snapshots
            if execution is not None and value.runtime_execution_id == execution.id
        ),
        handle_snapshots=tuple(
            value
            for value in aggregate.handle_snapshots
            if execution is not None and value.runtime_execution_id == execution.id
        ),
        lifecycle_operations=tuple(
            value
            for value in aggregate.lifecycle_operations
            if execution is not None and value.runtime_execution_id == execution.id
        ),
        integrity_incidents=tuple(
            value
            for value in aggregate.integrity_incidents
            if execution is not None and value.runtime_execution_id == execution.id
        ),
        boundary_classifications={run.id: boundary},
    )
    now = max(
        aggregate.task.updated_at,
        run.started_at or run.queued_at,
        subtask.updated_at,
        attempt.heartbeat_at if attempt is not None else run.queued_at,
        execution.updated_at if execution is not None else run.queued_at,
        aggregate.active_drain.updated_at if aggregate.active_drain is not None else run.queued_at,
    ) + timedelta(seconds=1)
    plan = plan_cancel_request(aggregate, "operator.requested")
    return aggregate, subtask, run, attempt, execution, now, plan


@pytest.mark.parametrize(
    ("boundary", "expected_action"),
    (
        (
            CoordinationRuntimeBoundary.NOT_CROSSED_QUEUED,
            CoordinatedCancelActionKind.CANCEL_QUEUED,
        ),
        (
            CoordinationRuntimeBoundary.NOT_CROSSED_NO_EXECUTION,
            CoordinatedCancelActionKind.CANCEL_NO_EXECUTION,
        ),
        (
            CoordinationRuntimeBoundary.NOT_CROSSED_PREPARED,
            CoordinatedCancelActionKind.ABORT_PREPARED,
        ),
    ),
)
def test_provider_free_actions_complete_canceled_task(boundary, expected_action) -> None:
    aggregate, subtask, run, attempt, execution, now, plan = _local_case(boundary)
    uow = _Uow()

    result = CoordinatedRuntimeCancelApplier().apply_in_uow(
        uow,
        aggregate=aggregate,
        plan=plan,
        now=now,
        cancel_deadline_window=timedelta(minutes=5),
    )

    assert plan.actions[0].kind is expected_action
    assert result.completion is CoordinatedCancelCompletion.APPLY_CANCELED
    assert result.effective_drain.status is CoordinationRuntimeDrainStatus.COMPLETE
    assert aggregate.task.status is TaskStatus.CANCELED
    assert aggregate.task.error == COORDINATION_USER_CANCEL_REQUESTED
    assert run.status is RunStatus.CANCELED
    assert subtask.status is SubtaskStatus.CANCELED
    assert subtask.current_run_id == run.id
    if attempt is not None:
        assert attempt.status is AttemptStatus.CANCELED
        assert attempt.error == COORDINATION_USER_CANCEL_REQUESTED
    if boundary is CoordinationRuntimeBoundary.NOT_CROSSED_PREPARED:
        assert execution.phase is RuntimeExecutionPhase.PREPARED
        saved_execution = next(value for kind, value in uow.saves if kind == "execution")
        assert saved_execution.phase is RuntimeExecutionPhase.CANCELED
        assert any(
            value.schema_name == "agentmesh.runtime.dispatch.aborted"
            for value in uow.outbox.values
        )
    assert result.made_progress
    assert not result.lifecycle_operation_ids


def test_crossed_action_requests_cancel_and_keeps_task_and_drain_active() -> None:
    aggregate, _subtask, _run, _attempt, execution, now, plan = _local_case(
        CoordinationRuntimeBoundary.CROSSED_ACTIVE
    )
    uow = _Uow()

    result = CoordinatedRuntimeCancelApplier().apply_in_uow(
        uow,
        aggregate=aggregate,
        plan=plan,
        now=now,
        cancel_deadline_window=timedelta(minutes=5),
    )

    assert result.completion is CoordinatedCancelCompletion.WAIT_ACTIVE
    assert result.effective_drain.status is CoordinationRuntimeDrainStatus.DRAINING
    assert aggregate.task.status is TaskStatus.RUNNING
    lifecycle = next(value for kind, value in uow.saves if kind == "lifecycle")
    assert result.lifecycle_operation_ids == (lifecycle.id,)
    assert lifecycle.deadline == now + timedelta(minutes=5)
    saved_execution = next(value for kind, value in uow.saves if kind == "execution")
    assert saved_execution.id == execution.id
    assert saved_execution.phase is RuntimeExecutionPhase.CANCEL_REQUESTED


def test_retarget_freezes_new_cancel_deadline_at_application_time() -> None:
    aggregate, _subtask, _run, _attempt, _execution, now, plan = _local_case(
        CoordinationRuntimeBoundary.CROSSED_ACTIVE,
        drain_target=CoordinationRuntimeDrainTarget.RUNNING,
    )
    assert plan.retarget_drain
    uow = _Uow()

    result = CoordinatedRuntimeCancelApplier().apply_in_uow(
        uow,
        aggregate=aggregate,
        plan=plan,
        now=now,
        cancel_deadline_window=timedelta(minutes=5),
    )

    lifecycle = next(value for kind, value in uow.saves if kind == "lifecycle")
    assert lifecycle.deadline == now + timedelta(minutes=5)
    assert result.effective_drain.target is CoordinationRuntimeDrainTarget.CANCELED
    assert result.effective_drain.updated_at == now


def test_no_execution_action_releases_budget_and_quota_once_before_terminal_marker() -> None:
    aggregate, _subtask, _run, attempt, _execution, now, plan = _local_case(
        CoordinationRuntimeBoundary.NOT_CROSSED_NO_EXECUTION
    )
    aggregate.task.budget = TaskBudget.create(
        max_tokens=100,
        token_reservation_per_attempt=10,
        max_cost_micros=1000,
        cost_reservation_micros_per_attempt=100,
    )
    aggregate.task.reserved_tokens = 10
    aggregate.task.reserved_cost_micros = 100
    attempt.reserved_tokens = 10
    attempt.reserved_cost_micros = 100
    calls: list[object] = []
    uow = _Uow()
    uow.quotas.list_reservations_for_attempt = (
        lambda attempt_id, for_update=False: calls.append((attempt_id, for_update)) or []
    )

    CoordinatedRuntimeCancelApplier().apply_in_uow(
        uow,
        aggregate=aggregate,
        plan=plan,
        now=now,
        cancel_deadline_window=timedelta(minutes=5),
    )

    assert aggregate.task.reserved_tokens == aggregate.task.reserved_cost_micros == 0
    assert attempt.budget_settlement_source is BudgetSettlementSource.RELEASED
    assert calls == [(attempt.id, True)]


def test_reconciliation_action_is_a_zero_write_wait_after_drain_creation() -> None:
    aggregate, _subtask, _run, _attempt, _execution, now, plan = _local_case(
        CoordinationRuntimeBoundary.RECONCILIATION_EVIDENCE
    )
    uow = _Uow()

    result = CoordinatedRuntimeCancelApplier().apply_in_uow(
        uow,
        aggregate=aggregate,
        plan=plan,
        now=now,
        cancel_deadline_window=timedelta(minutes=5),
    )

    assert plan.actions[0].kind is CoordinatedCancelActionKind.WAIT_RECONCILIATION
    assert result.completion is CoordinatedCancelCompletion.WAIT_RECONCILIATION
    assert result.effective_drain.status is CoordinationRuntimeDrainStatus.DRAINING
    assert {kind for kind, _value in uow.saves} == {"drain.add"}
    assert aggregate.task.status is TaskStatus.RUNNING


def test_terminal_action_is_retained_while_local_sibling_finishes() -> None:
    _task, _target, siblings, aggregate = _aggregate_for_sibling_boundaries(
        (
            CoordinationRuntimeBoundary.KNOWN_TERMINAL,
            CoordinationRuntimeBoundary.NOT_CROSSED_QUEUED,
        )
    )
    now = max(value.updated_at for value in aggregate.subtasks) + timedelta(seconds=2)
    plan = plan_cancel_request(aggregate, "operator.requested")
    terminal = next(
        chain
        for chain in siblings
        if aggregate.boundary_classifications[chain[1].id]
        is CoordinationRuntimeBoundary.KNOWN_TERMINAL
    )
    uow = _Uow()

    result = CoordinatedRuntimeCancelApplier().apply_in_uow(
        uow,
        aggregate=aggregate,
        plan=plan,
        now=now,
        cancel_deadline_window=timedelta(minutes=5),
    )

    assert terminal[1].status is RunStatus.SUCCEEDED
    assert terminal[0].status is SubtaskStatus.COMPLETED
    assert terminal[1].id not in result.changed_ids
    assert result.completion is CoordinatedCancelCompletion.WAIT_ACTIVE


def test_failed_drain_first_cause_is_retained_and_completed() -> None:
    aggregate, _subtask, _run, _attempt, _execution, now, plan = _local_case(
        CoordinationRuntimeBoundary.NOT_CROSSED_QUEUED,
        drain_target=CoordinationRuntimeDrainTarget.FAILED,
    )
    uow = _Uow()

    result = CoordinatedRuntimeCancelApplier().apply_in_uow(
        uow,
        aggregate=aggregate,
        plan=plan,
        now=now,
        cancel_deadline_window=timedelta(minutes=5),
    )

    assert result.completion is CoordinatedCancelCompletion.RETAIN_FAILED
    assert result.effective_drain.status is CoordinationRuntimeDrainStatus.COMPLETE
    assert result.effective_drain.reason == "initial.failure"
    assert aggregate.task.status is TaskStatus.FAILED
    assert aggregate.task.error == "initial.failure"


def test_supervisor_without_subtask_releases_task_pointer_before_completion() -> None:
    aggregate, _subtask, run, _attempt, _execution, now, _plan = _local_case(
        CoordinationRuntimeBoundary.NOT_CROSSED_QUEUED
    )
    supervisor = replace(run, role=RunRole.SUPERVISOR, subtask_id=None)
    aggregate.task.current_run_id = supervisor.id
    aggregate = replace(
        aggregate,
        subtasks=(),
        runs=(supervisor,),
        latest_attempts={supervisor.id: None},
        boundary_classifications={
            supervisor.id: CoordinationRuntimeBoundary.NOT_CROSSED_QUEUED
        },
    )
    plan = plan_cancel_request(aggregate, "operator.requested")
    uow = _Uow()

    result = CoordinatedRuntimeCancelApplier().apply_in_uow(
        uow,
        aggregate=aggregate,
        plan=plan,
        now=now,
        cancel_deadline_window=timedelta(minutes=5),
    )

    assert result.completion is CoordinatedCancelCompletion.APPLY_CANCELED
    assert aggregate.task.current_run_id is None
    assert aggregate.task.status is TaskStatus.CANCELED
    assert supervisor.status is RunStatus.CANCELED
    assert not [kind for kind, _value in uow.saves if kind == "subtask"]


def test_stale_plan_and_expired_deadline_fail_before_writes() -> None:
    aggregate, _subtask, _run, _attempt, _execution, now, plan = _local_case(
        CoordinationRuntimeBoundary.CROSSED_ACTIVE,
        drain_target=CoordinationRuntimeDrainTarget.CANCELED,
    )
    with pytest.raises(RuntimeExecutionConflict, match="stale"):
        CoordinatedRuntimeCancelApplier().apply_in_uow(
            _Uow(),
            aggregate=aggregate,
            plan=replace(plan, effective_reason="different.reason"),
            now=now,
            cancel_deadline_window=timedelta(minutes=5),
        )

    uow = _Uow()
    with pytest.raises(RuntimeExecutionConflict, match="deadline"):
        CoordinatedRuntimeCancelApplier().apply_in_uow(
            uow,
            aggregate=aggregate,
            plan=plan,
            now=aggregate.active_drain.created_at + timedelta(minutes=5),
            cancel_deadline_window=timedelta(minutes=5),
        )
    assert not uow.saves and not uow.outbox.values


def test_applier_has_no_transaction_or_external_service_calls() -> None:
    source = Path(
        "src/agentmesh/application/coordinated_runtime_cancel_applier.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden = {"commit", "rollback", "lock", "schedule", "dispatch", "capture_completed_task"}

    assert not {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr in forbidden
    }
    assert "uow_factory" not in source
