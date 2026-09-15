from __future__ import annotations

import ast
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from agentmesh.application.coordinated_runtime_control import (
    CoordinatedCancelActionKind,
    CoordinatedCancelCompletion,
    CoordinatedCancelDrainGuard,
    CoordinatedCancelKind,
    CoordinatedCancelResult,
    plan_cancel_request,
)
from agentmesh.domain.coordination import (
    CoordinationRuntimeBoundary,
    CoordinationRuntimeDrain,
    CoordinationRuntimeDrainTarget,
    SubtaskStatus,
)
from agentmesh.domain.errors import InvalidTaskInput, RuntimeExecutionConflict
from agentmesh.domain.runtime_execution import RuntimeExecutionPhase
from agentmesh.domain.tasks import AttemptStatus, RunRole, RunStatus, TaskStatus
from tests.test_coordinated_runtime_barrier import (
    _aggregate,
    _aggregate_for_sibling_boundaries,
)


def test_plan_covers_every_boundary_and_uses_no_synthetic_trigger() -> None:
    boundaries = (
        CoordinationRuntimeBoundary.NOT_CROSSED_QUEUED,
        CoordinationRuntimeBoundary.NOT_CROSSED_NO_EXECUTION,
        CoordinationRuntimeBoundary.NOT_CROSSED_PREPARED,
        CoordinationRuntimeBoundary.CROSSED_ACTIVE,
        CoordinationRuntimeBoundary.KNOWN_TERMINAL,
        CoordinationRuntimeBoundary.RECONCILIATION_EVIDENCE,
    )
    _task, target, siblings, aggregate = _aggregate_for_sibling_boundaries(boundaries)

    plan = plan_cancel_request(aggregate, "operator.requested")

    assert len(plan.actions) == len(aggregate.runs)
    actions = {action.boundary: action.kind for action in plan.actions}
    assert actions[CoordinationRuntimeBoundary.NOT_CROSSED_QUEUED] is (
        CoordinatedCancelActionKind.CANCEL_QUEUED
    )
    assert actions[CoordinationRuntimeBoundary.NOT_CROSSED_NO_EXECUTION] is (
        CoordinatedCancelActionKind.CANCEL_NO_EXECUTION
    )
    assert actions[CoordinationRuntimeBoundary.NOT_CROSSED_PREPARED] is (
        CoordinatedCancelActionKind.ABORT_PREPARED
    )
    assert actions[CoordinationRuntimeBoundary.CROSSED_ACTIVE] is (
        CoordinatedCancelActionKind.REQUEST_CANCEL
    )
    assert actions[CoordinationRuntimeBoundary.KNOWN_TERMINAL] is (
        CoordinatedCancelActionKind.RETAIN_TERMINAL
    )
    assert actions[CoordinationRuntimeBoundary.RECONCILIATION_EVIDENCE] is (
        CoordinatedCancelActionKind.WAIT_RECONCILIATION
    )
    assert plan.audit_anchor_run_id == min(
        (
            action.run_id
            for action in plan.actions
            if action.kind is not CoordinatedCancelActionKind.RETAIN_TERMINAL
        ),
        key=str,
    )
    assert all(action.subtask_id is not None for action in plan.actions)
    assert siblings
    assert plan.completion is CoordinatedCancelCompletion.WAIT_ACTIVE


def test_supervisor_without_subtask_is_an_action_not_a_trigger() -> None:
    _task, target, aggregate = _aggregate()
    supervisor = replace(target[1], role=RunRole.SUPERVISOR, subtask_id=None)
    aggregate.task.current_run_id = supervisor.id
    aggregate = replace(
        aggregate,
        runs=tuple(sorted((supervisor,), key=lambda value: value.id)),
        latest_attempts={supervisor.id: target[2]},
        boundary_classifications={supervisor.id: CoordinationRuntimeBoundary.CROSSED_ACTIVE},
    )

    plan = plan_cancel_request(aggregate, "operator.requested")

    assert len(plan.actions) == 1
    assert plan.actions[0].subtask_id is None
    assert plan.actions[0].kind is CoordinatedCancelActionKind.REQUEST_CANCEL
    assert plan.audit_anchor_run_id == supervisor.id


def test_supervisor_current_pointer_is_required_and_task_scoped() -> None:
    _task, target, aggregate = _aggregate()
    supervisor = replace(target[1], role=RunRole.SUPERVISOR, subtask_id=None)
    missing_pointer = replace(
        aggregate,
        runs=(supervisor,),
        latest_attempts={supervisor.id: target[2]},
        boundary_classifications={supervisor.id: CoordinationRuntimeBoundary.CROSSED_ACTIVE},
    )
    with pytest.raises(RuntimeExecutionConflict):
        plan_cancel_request(missing_pointer, "operator.requested")

    aggregate.task.current_run_id = target[1].id
    with pytest.raises(RuntimeExecutionConflict):
        plan_cancel_request(aggregate, "operator.requested")


def test_multiple_active_supervisors_fail_closed() -> None:
    _task, target, aggregate = _aggregate()
    first = replace(target[1], role=RunRole.SUPERVISOR, subtask_id=None)
    second = replace(
        first,
        id=uuid4(),
        runtime_execution_id=None,
        runtime_execution_intent_id=None,
    )
    first_attempt = replace(target[2], run_id=first.id)
    second_attempt = replace(target[2], id=uuid4(), run_id=second.id)
    aggregate.task.current_run_id = first.id
    invalid = replace(
        aggregate,
        runs=tuple(sorted((first, second), key=lambda value: value.id)),
        latest_attempts={first.id: first_attempt, second.id: second_attempt},
        executions=(),
        boundary_classifications={
            first.id: CoordinationRuntimeBoundary.NOT_CROSSED_NO_EXECUTION,
            second.id: CoordinationRuntimeBoundary.NOT_CROSSED_NO_EXECUTION,
        },
    )
    with pytest.raises(RuntimeExecutionConflict):
        plan_cancel_request(invalid, "operator.requested")


def test_historical_terminal_run_may_share_subtask_with_new_current_run() -> None:
    _task, _target, _siblings, aggregate = _aggregate_for_sibling_boundaries(
        (CoordinationRuntimeBoundary.KNOWN_TERMINAL,)
    )
    current_run = next(
        run
        for run in aggregate.runs
        if aggregate.boundary_classifications.get(run.id)
        is CoordinationRuntimeBoundary.CROSSED_ACTIVE
    )
    historical_run = next(
        run
        for run in aggregate.runs
        if aggregate.boundary_classifications.get(run.id)
        is CoordinationRuntimeBoundary.KNOWN_TERMINAL
    )
    historical_attempt = aggregate.latest_attempts[historical_run.id]
    historical_execution = aggregate.executions_by_run[historical_run.id][0]
    failed_run = replace(historical_run, status=RunStatus.FAILED)
    failed_attempt = replace(historical_attempt, status=AttemptStatus.FAILED)
    failed_execution = replace(
        historical_execution,
        phase=RuntimeExecutionPhase.FAILED,
        current_owner_attempt_id=failed_attempt.id,
        current_fencing_token=failed_attempt.fencing_token,
    )
    shared_subtask = next(
        subtask for subtask in aggregate.subtasks if subtask.id == current_run.subtask_id
    )
    old_to_current = replace(
        aggregate,
        runs=tuple(
            sorted(
                (current_run, replace(failed_run, subtask_id=shared_subtask.id)),
                key=lambda value: value.id,
            )
        ),
        subtasks=(shared_subtask,),
        latest_attempts={
            current_run.id: aggregate.latest_attempts[current_run.id],
            failed_run.id: failed_attempt,
        },
        executions=tuple(
            value
            for value in aggregate.executions
            if value.run_id in {current_run.id, historical_run.id}
        ),
        boundary_classifications={
            current_run.id: CoordinationRuntimeBoundary.CROSSED_ACTIVE,
            failed_run.id: CoordinationRuntimeBoundary.KNOWN_TERMINAL,
        },
    )
    old_to_current = replace(
        old_to_current,
        executions=tuple(
            failed_execution if value.id == historical_execution.id else value
            for value in old_to_current.executions
        ),
    )

    plan = plan_cancel_request(old_to_current, "operator.requested")
    historical_action = next(action for action in plan.actions if action.run_id == failed_run.id)
    assert historical_action.kind is CoordinatedCancelActionKind.RETAIN_TERMINAL

    omitted = replace(
        old_to_current,
        boundary_classifications={current_run.id: CoordinationRuntimeBoundary.CROSSED_ACTIVE},
    )
    omitted_plan = plan_cancel_request(omitted, "operator.requested")
    assert next(
        action for action in omitted_plan.actions if action.run_id == failed_run.id
    ).kind is (CoordinatedCancelActionKind.RETAIN_TERMINAL)

    mismatched = replace(
        old_to_current,
        executions=tuple(
            replace(value, phase=RuntimeExecutionPhase.SUCCEEDED)
            if value.id == historical_execution.id
            else value
            for value in old_to_current.executions
        ),
    )
    with pytest.raises(RuntimeExecutionConflict):
        plan_cancel_request(mismatched, "operator.requested")
    with pytest.raises(RuntimeExecutionConflict):
        plan_cancel_request(
            replace(
                mismatched,
                boundary_classifications={
                    current_run.id: CoordinationRuntimeBoundary.CROSSED_ACTIVE
                },
            ),
            "operator.requested",
        )


@pytest.mark.parametrize(
    ("target", "retarget", "effective", "completion"),
    [
        (
            CoordinationRuntimeDrainTarget.RUNNING,
            True,
            CoordinationRuntimeDrainTarget.CANCELED,
            CoordinatedCancelCompletion.WAIT_ACTIVE,
        ),
        (
            CoordinationRuntimeDrainTarget.WAITING_APPROVAL,
            True,
            CoordinationRuntimeDrainTarget.CANCELED,
            CoordinatedCancelCompletion.WAIT_ACTIVE,
        ),
        (
            CoordinationRuntimeDrainTarget.CANCELED,
            False,
            CoordinationRuntimeDrainTarget.CANCELED,
            CoordinatedCancelCompletion.WAIT_ACTIVE,
        ),
        (
            CoordinationRuntimeDrainTarget.FAILED,
            False,
            CoordinationRuntimeDrainTarget.FAILED,
            CoordinatedCancelCompletion.WAIT_ACTIVE,
        ),
    ],
)
def test_drain_target_precedence(target, retarget, effective, completion) -> None:
    _task, target_chain, aggregate = _aggregate()
    drain = CoordinationRuntimeDrain.start(
        drain_id=uuid5(
            NAMESPACE_URL,
            f"coordination-runtime-drain:{aggregate.task.tenant_id}:{aggregate.task.id}",
        ),
        tenant_id=aggregate.task.tenant_id,
        task_id=aggregate.task.id,
        triggering_run_id=target_chain[1].id,
        target=target,
        reason="first.cause",
        at=target_chain[3].updated_at + timedelta(seconds=1),
    )
    plan = plan_cancel_request(replace(aggregate, active_drain=drain), "second.cause")

    assert plan.retarget_drain is retarget
    assert plan.effective_target is effective
    assert plan.effective_reason == ("second.cause" if retarget else "first.cause")
    assert plan.completion is completion


def test_existing_drain_freezes_source_guard_and_epoch() -> None:
    _task, target, aggregate = _aggregate()
    drain = CoordinationRuntimeDrain.start(
        drain_id=uuid5(
            NAMESPACE_URL,
            f"coordination-runtime-drain:{aggregate.task.tenant_id}:{aggregate.task.id}",
        ),
        tenant_id=aggregate.task.tenant_id,
        task_id=aggregate.task.id,
        triggering_run_id=target[1].id,
        target=CoordinationRuntimeDrainTarget.CANCELED,
        reason="first.cause",
        at=target[3].updated_at + timedelta(seconds=1),
    )

    plan = plan_cancel_request(replace(aggregate, active_drain=drain), "second.cause")

    assert type(plan.source_drain_guard) is CoordinatedCancelDrainGuard
    assert plan.source_drain_guard.id == drain.id
    assert plan.source_drain_guard.version == drain.version
    assert plan.source_drain_guard.created_at == drain.created_at
    assert plan.source_drain_guard.updated_at == drain.updated_at
    assert plan.sibling_actions == plan.actions


def test_result_is_closed_and_replay_keeps_only_verified_projection() -> None:
    _task, target, aggregate = _aggregate()
    drain_id = uuid4()
    audit_id = uuid4()
    anchor_id = uuid4()
    with pytest.raises(InvalidTaskInput):
        CoordinatedCancelResult(
            kind=CoordinatedCancelKind.APPLIED,
            tenant_id=aggregate.task.tenant_id,
            task_id=aggregate.task.id,
            task_status=TaskStatus.RUNNING,
            effective_target=CoordinationRuntimeDrainTarget.CANCELED,
            drain_id=drain_id,
            reason="operator requested",
        )

    result = CoordinatedCancelResult(
        kind=CoordinatedCancelKind.APPLIED,
        tenant_id=aggregate.task.tenant_id,
        task_id=aggregate.task.id,
        task_status=TaskStatus.RUNNING,
        effective_target=CoordinationRuntimeDrainTarget.CANCELED,
        drain_id=drain_id,
        audit_event_id=audit_id,
        audit_anchor_run_id=anchor_id,
        reason="operator requested",
        lifecycle_operation_ids=(uuid4(),),
    )
    replay = replace(result, kind=CoordinatedCancelKind.REPLAY)

    assert result.reason == "operator requested"
    assert result.effective_reason == result.reason
    assert replay.audit_event_id == audit_id
    assert replay.audit_anchor_run_id == anchor_id
    with pytest.raises(InvalidTaskInput):
        CoordinatedCancelResult(
            kind=CoordinatedCancelKind.REPLAY,
            tenant_id=aggregate.task.tenant_id,
            task_id=aggregate.task.id,
            audit_event_id=audit_id,
        )
    with pytest.raises(InvalidTaskInput):
        replace(result, audit_anchor_run_id=None)
    for unsafe_reason in (
        "contains secret",
        "contains client_secret",
        "contains private_key",
        "contains authorization",
    ):
        with pytest.raises(InvalidTaskInput):
            replace(result, reason=unsafe_reason)

    terminal = CoordinatedCancelResult(
        kind=CoordinatedCancelKind.ALREADY_TERMINAL,
        tenant_id=aggregate.task.tenant_id,
        task_id=aggregate.task.id,
        task_status=TaskStatus.CANCELED,
        audit_event_id=audit_id,
    )
    assert terminal.drain_id is None
    assert terminal.audit_event_id == audit_id
    terminal_replay = replace(terminal, kind=CoordinatedCancelKind.REPLAY)
    assert terminal_replay.task_status is TaskStatus.CANCELED
    assert terminal_replay.audit_event_id == audit_id
    assert terminal_replay.effective_target is None

    with pytest.raises(InvalidTaskInput):
        replace(terminal, audit_event_id=None)
    with pytest.raises(InvalidTaskInput):
        replace(terminal, task_status=TaskStatus.RUNNING)
    with pytest.raises(InvalidTaskInput):
        replace(
            terminal_replay,
            effective_target=CoordinationRuntimeDrainTarget.CANCELED,
            reason="operator requested",
        )
    with pytest.raises(InvalidTaskInput):
        replace(terminal, kind=CoordinatedCancelKind.APPLIED)


def test_result_action_enum_has_no_wire_aliases() -> None:
    values = [member.value for member in CoordinatedCancelActionKind]
    assert len(values) == len(set(values))


def test_boundary_and_terminal_conclusion_mismatches_fail_closed() -> None:
    _task, _target, _siblings, aggregate = _aggregate_for_sibling_boundaries(
        (CoordinationRuntimeBoundary.KNOWN_TERMINAL,)
    )
    known_run = next(
        run
        for run in aggregate.runs
        if aggregate.boundary_classifications.get(run.id)
        is CoordinationRuntimeBoundary.KNOWN_TERMINAL
    )
    known_execution = aggregate.executions_by_run[known_run.id][0]
    bad_execution = replace(known_execution, phase=RuntimeExecutionPhase.FAILED)
    bad = replace(
        aggregate,
        executions=tuple(
            bad_execution if value.id == known_execution.id else value
            for value in aggregate.executions
        ),
    )

    with pytest.raises(RuntimeExecutionConflict):
        plan_cancel_request(bad, "operator requested")

    bad_task = replace(aggregate.task, status=TaskStatus.CANCELED)
    with pytest.raises(RuntimeExecutionConflict):
        plan_cancel_request(replace(aggregate, task=bad_task), "operator requested")

    queued_aggregate = _aggregate_for_sibling_boundaries(
        (CoordinationRuntimeBoundary.NOT_CROSSED_QUEUED,)
    )[3]
    bad_subtask = replace(queued_aggregate.subtasks[0], status=SubtaskStatus.RUNNING)
    with pytest.raises(RuntimeExecutionConflict):
        plan_cancel_request(
            replace(queued_aggregate, subtasks=(bad_subtask,)), "operator requested"
        )


def test_reason_and_projection_validation_fail_closed() -> None:
    _task, _target, aggregate = _aggregate()
    with pytest.raises(InvalidTaskInput):
        plan_cancel_request(aggregate, " trailing")
    with pytest.raises(RuntimeExecutionConflict):
        plan_cancel_request(object(), "operator.requested")

    broken = replace(
        aggregate,
        boundary_classifications={
            aggregate.runs[0].id: CoordinationRuntimeBoundary.NOT_CROSSED_QUEUED
        },
    )
    with pytest.raises(RuntimeExecutionConflict):
        plan_cancel_request(broken, "operator.requested")


def test_planner_module_is_pure_and_does_not_open_or_commit_transactions() -> None:
    path = Path("src/agentmesh/application/coordinated_runtime_control.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    forbidden_imports = {
        "UnitOfWork",
        "UnitOfWorkFactory",
        "ManagedAgentRuntime",
        "RuntimeLifecycleService",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not forbidden_imports.intersection(alias.name for alias in node.names)
        if isinstance(node, ast.Attribute):
            assert node.attr not in {"commit", "rollback", "flush"}
