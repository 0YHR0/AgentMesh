from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from types import MappingProxyType
from uuid import uuid4

import pytest

from agentmesh.application.business_outcomes import KnownTerminalPhase
from agentmesh.application.coordinated_runtime_barrier import (
    CoordinatedBarrierCompletion,
    CoordinatedBarrierTriggerDisposition,
    CoordinatedRuntimeBarrierApplier,
    CoordinatedSiblingActionKind,
    plan_reconciled_terminal,
    plan_unknown_outcome,
)
from agentmesh.domain.coordination import (
    CoordinationRuntimeBoundary,
    CoordinationRuntimeDrainTarget,
    SubtaskStatus,
)
from agentmesh.domain.errors import RuntimeExecutionConflict
from agentmesh.domain.runtime_execution import RuntimeExecutionPhase
from agentmesh.domain.tasks import AttemptStatus, RunRole, RunStatus, TaskStatus
from tests.test_coordinated_runtime_aggregate import _project_phase_statuses
from tests.test_coordinated_runtime_barrier import (
    _aggregate,
    _aggregate_for_sibling_boundaries,
    _cancel_intent,
)


def _parked_executor(*, drain_target=CoordinationRuntimeDrainTarget.RUNNING):
    task, target, aggregate = _aggregate(drain_target=drain_target)
    execution = target[3].apply_observation(
        phase=RuntimeExecutionPhase.LOST,
        provider_sequence=2,
        now=target[3].updated_at,
    )
    _project_phase_statuses(target[0], target[1], target[2], RuntimeExecutionPhase.LOST)
    aggregate = replace(
        aggregate,
        executions=(execution,),
        boundary_classifications=MappingProxyType(
            {target[1].id: CoordinationRuntimeBoundary.RECONCILIATION_EVIDENCE}
        ),
    )
    task.status = TaskStatus.RECONCILIATION_REQUIRED
    return task, target, aggregate


def _parked_executor_with_sibling(boundary):
    task, target, aggregate = _parked_executor()
    _sibling_task, _sibling_target, siblings, sibling_aggregate = (
        _aggregate_for_sibling_boundaries(
            (boundary,), drain_target=CoordinationRuntimeDrainTarget.RUNNING
        )
    )
    sibling_subtask, sibling_run, sibling_attempt, sibling_execution = siblings[0]
    attempts = {target[1].id: target[2]}
    if sibling_attempt is not None:
        attempts[sibling_run.id] = sibling_attempt
    executions = [aggregate.executions[0]]
    if sibling_execution is not None:
        executions.append(sibling_execution)
    aggregate = replace(
        aggregate,
        subtasks=(target[0], sibling_subtask),
        runs=(target[1], sibling_run),
        latest_attempts=MappingProxyType(attempts),
        executions=tuple(executions),
        boundary_classifications=MappingProxyType(
            {
                target[1].id: CoordinationRuntimeBoundary.RECONCILIATION_EVIDENCE,
                sibling_run.id: boundary,
            }
        ),
    )
    return task, target, aggregate


def _supervisor_unknown():
    task, target, aggregate = _aggregate()
    target[0].status = SubtaskStatus.COMPLETED
    target[0].current_run_id = None
    run = replace(target[1], role=RunRole.SUPERVISOR, subtask_id=None)
    task.current_run_id = run.id
    aggregate = replace(
        aggregate,
        runs=(run,),
        subtasks=(target[0],),
        boundary_classifications=MappingProxyType({}),
    )
    return task, target, aggregate


def _supervisor_reconciled():
    task, target, aggregate = _aggregate(drain_target=CoordinationRuntimeDrainTarget.RUNNING)
    target[0].status = SubtaskStatus.COMPLETED
    target[0].current_run_id = None
    run = replace(
        target[1],
        role=RunRole.SUPERVISOR,
        subtask_id=None,
        status=RunStatus.RECONCILIATION_REQUIRED,
    )
    target[2].status = AttemptStatus.OUTCOME_UNKNOWN
    execution = target[3].apply_observation(
        phase=RuntimeExecutionPhase.LOST,
        provider_sequence=2,
        now=target[3].updated_at,
    )
    task.status = TaskStatus.RECONCILIATION_REQUIRED
    task.current_run_id = run.id
    aggregate = replace(
        aggregate,
        runs=(run,),
        subtasks=(target[0],),
        executions=(execution,),
        boundary_classifications=MappingProxyType({}),
    )
    return task, target, aggregate


def test_unknown_parking_creates_running_drain_and_never_applies_it() -> None:
    _task, target, aggregate = _aggregate()
    plan = plan_unknown_outcome(
        aggregate,
        triggering_run_id=target[1].id,
        reason="provider lost after dispatch",
    )
    assert plan.trigger_disposition is CoordinatedBarrierTriggerDisposition.RECONCILIATION_EVIDENCE
    assert plan.create_drain is True
    assert plan.effective_target is CoordinationRuntimeDrainTarget.RUNNING
    assert plan.effective_reason == "coordination.runtime_reconciliation_required"
    assert plan.completion is CoordinatedBarrierCompletion.WAIT_RECONCILIATION
    assert plan.sibling_actions == ()


def test_unknown_crossed_sibling_has_active_precedence_and_requests_no_cancel() -> None:
    _task, target, _siblings, aggregate = _aggregate_for_sibling_boundaries(
        (CoordinationRuntimeBoundary.CROSSED_ACTIVE,),
    )
    plan = plan_unknown_outcome(
        aggregate,
        triggering_run_id=target[1].id,
        reason="lost",
    )
    assert plan.completion is CoordinatedBarrierCompletion.WAIT_ACTIVE
    assert plan.sibling_actions[0].kind is CoordinatedSiblingActionKind.WAIT_CROSSED


def test_unknown_stopping_drain_requests_cancel_and_preserves_first_cause() -> None:
    _task, target, _siblings, aggregate = _aggregate_for_sibling_boundaries(
        (CoordinationRuntimeBoundary.CROSSED_ACTIVE,),
        drain_target=CoordinationRuntimeDrainTarget.FAILED,
    )
    plan = plan_unknown_outcome(
        aggregate,
        triggering_run_id=target[1].id,
        reason="lost",
    )
    assert plan.effective_target is CoordinationRuntimeDrainTarget.FAILED
    assert plan.sibling_actions[0].kind is CoordinatedSiblingActionKind.REQUEST_CANCEL
    assert plan.completion is CoordinatedBarrierCompletion.WAIT_ACTIVE


@pytest.mark.parametrize("phase", list(KnownTerminalPhase))
def test_reconciled_executor_uses_known_terminal_lattice(phase) -> None:
    task, target, aggregate = _parked_executor()
    cancel = phase is KnownTerminalPhase.CANCELED
    if cancel:
        row = _cancel_intent(tenant_id=task.tenant_id, execution_id=target[3].id)
        aggregate = replace(aggregate, lifecycle_operations=(row,))
    plan = plan_reconciled_terminal(
        aggregate,
        triggering_run_id=target[1].id,
        phase=phase,
        cancel_intent_present=cancel,
        safe_error=None,
    )
    assert plan.trigger_disposition is CoordinatedBarrierTriggerDisposition.RECONCILIATION_EVIDENCE
    assert plan.completion is {
        KnownTerminalPhase.SUCCEEDED: CoordinatedBarrierCompletion.APPLY_RUNNING,
        KnownTerminalPhase.FAILED: CoordinatedBarrierCompletion.APPLY_FAILED,
        KnownTerminalPhase.TIMED_OUT: CoordinatedBarrierCompletion.APPLY_FAILED,
        KnownTerminalPhase.CANCELED: CoordinatedBarrierCompletion.APPLY_CANCELED,
    }[phase]


def test_reconciled_unrequested_cancel_is_failed_and_stable() -> None:
    _task, target, aggregate = _parked_executor()
    plan = plan_reconciled_terminal(
        aggregate,
        triggering_run_id=target[1].id,
        phase=KnownTerminalPhase.CANCELED,
        cancel_intent_present=False,
        safe_error=None,
    )
    assert plan.requested_target is CoordinationRuntimeDrainTarget.FAILED
    assert plan.requested_reason == "runtime.unrequested_cancellation"


def test_reconciled_active_sibling_still_blocks_terminal_conclusion() -> None:
    task, target, aggregate = _parked_executor()
    (
        _sibling_task,
        _sibling_target,
        _siblings,
        sibling_aggregate,
    ) = _aggregate_for_sibling_boundaries(
        (CoordinationRuntimeBoundary.CROSSED_ACTIVE,),
        drain_target=CoordinationRuntimeDrainTarget.RUNNING,
    )
    # Reuse the parked target and the untouched sibling projection in one aggregate.
    sibling = sibling_aggregate.runs[1]
    sibling_subtask = next(
        subtask
        for subtask in sibling_aggregate.subtasks
        if subtask.id == sibling.subtask_id
    )
    aggregate = replace(
        aggregate,
        subtasks=(target[0], sibling_subtask),
        runs=(target[1], sibling),
        latest_attempts=MappingProxyType(
            {target[1].id: target[2], sibling.id: sibling_aggregate.latest_attempts[sibling.id]}
        ),
        executions=(aggregate.executions[0], sibling_aggregate.executions[1]),
        boundary_classifications=MappingProxyType(
            {
                target[1].id: CoordinationRuntimeBoundary.RECONCILIATION_EVIDENCE,
                sibling.id: CoordinationRuntimeBoundary.CROSSED_ACTIVE,
            }
        ),
    )
    plan = plan_reconciled_terminal(
        aggregate,
        triggering_run_id=target[1].id,
        phase=KnownTerminalPhase.FAILED,
        cancel_intent_present=False,
        safe_error=None,
    )
    assert plan.completion is CoordinatedBarrierCompletion.WAIT_ACTIVE


@pytest.mark.parametrize("bad", ["", "bad\nreason"])
def test_unknown_reason_is_bounded_and_fail_closed(bad) -> None:
    _task, target, aggregate = _aggregate()
    with pytest.raises(RuntimeExecutionConflict):
        plan_unknown_outcome(aggregate, triggering_run_id=target[1].id, reason=bad)


def test_reconciled_cancel_requires_exact_stable_intent() -> None:
    task, target, aggregate = _parked_executor()
    row = _cancel_intent(
        tenant_id=task.tenant_id,
        execution_id=target[3].id,
        operation_id="runtime-cancel:not-the-execution:v1",
    )
    aggregate = replace(aggregate, lifecycle_operations=(row,))
    with pytest.raises(RuntimeExecutionConflict):
        plan_reconciled_terminal(
            aggregate,
            triggering_run_id=target[1].id,
            phase=KnownTerminalPhase.CANCELED,
            cancel_intent_present=True,
            safe_error=None,
        )


def test_reconciled_failure_with_reconciliation_reason_is_terminal_apply() -> None:
    _task, target, aggregate = _parked_executor()
    plan = plan_reconciled_terminal(
        aggregate,
        triggering_run_id=target[1].id,
        phase=KnownTerminalPhase.FAILED,
        cancel_intent_present=False,
        safe_error="coordination.runtime_reconciliation_required",
    )
    assert plan.completion is CoordinatedBarrierCompletion.APPLY_FAILED
    assert plan.trigger_guard.boundary is CoordinationRuntimeBoundary.RECONCILIATION_EVIDENCE


@pytest.mark.parametrize(
    ("boundary", "kind"),
    [
        (
            CoordinationRuntimeBoundary.NOT_CROSSED_QUEUED,
            CoordinatedSiblingActionKind.RELEASE_QUEUED,
        ),
        (
            CoordinationRuntimeBoundary.NOT_CROSSED_NO_EXECUTION,
            CoordinatedSiblingActionKind.ABORT_NO_EXECUTION,
        ),
        (
            CoordinationRuntimeBoundary.NOT_CROSSED_PREPARED,
            CoordinatedSiblingActionKind.ABORT_PREPARED,
        ),
        (
            CoordinationRuntimeBoundary.CROSSED_ACTIVE,
            CoordinatedSiblingActionKind.WAIT_CROSSED,
        ),
        (
            CoordinationRuntimeBoundary.KNOWN_TERMINAL,
            CoordinatedSiblingActionKind.RETAIN_TERMINAL,
        ),
        (
            CoordinationRuntimeBoundary.RECONCILIATION_EVIDENCE,
            CoordinatedSiblingActionKind.WAIT_RECONCILIATION,
        ),
    ],
)
def test_unknown_exhausts_sibling_boundaries(boundary, kind) -> None:
    _task, target, _siblings, aggregate = _aggregate_for_sibling_boundaries((boundary,))
    plan = plan_unknown_outcome(aggregate, triggering_run_id=target[1].id, reason="lost")
    assert plan.sibling_actions[0].kind is kind
    expected = (
        CoordinatedBarrierCompletion.WAIT_ACTIVE
        if boundary is CoordinationRuntimeBoundary.CROSSED_ACTIVE
        else CoordinatedBarrierCompletion.WAIT_RECONCILIATION
        if boundary is CoordinationRuntimeBoundary.RECONCILIATION_EVIDENCE
        else CoordinatedBarrierCompletion.WAIT_RECONCILIATION
    )
    assert plan.completion is expected


@pytest.mark.parametrize("boundary", list(CoordinationRuntimeBoundary))
def test_reconciled_exhausts_sibling_boundaries(boundary) -> None:
    _task, target, aggregate = _parked_executor_with_sibling(boundary)
    plan = plan_reconciled_terminal(
        aggregate,
        triggering_run_id=target[1].id,
        phase=KnownTerminalPhase.FAILED,
        cancel_intent_present=False,
        safe_error=None,
    )
    action = plan.sibling_actions[0]
    if boundary is CoordinationRuntimeBoundary.CROSSED_ACTIVE:
        assert action.kind is CoordinatedSiblingActionKind.REQUEST_CANCEL
        assert plan.completion is CoordinatedBarrierCompletion.WAIT_ACTIVE
    elif boundary is CoordinationRuntimeBoundary.RECONCILIATION_EVIDENCE:
        assert action.kind is CoordinatedSiblingActionKind.WAIT_RECONCILIATION
        assert plan.completion is CoordinatedBarrierCompletion.WAIT_RECONCILIATION
    else:
        expected_kind = {
            CoordinationRuntimeBoundary.NOT_CROSSED_QUEUED: (
                CoordinatedSiblingActionKind.RELEASE_QUEUED
            ),
            CoordinationRuntimeBoundary.NOT_CROSSED_NO_EXECUTION: (
                CoordinatedSiblingActionKind.ABORT_NO_EXECUTION
            ),
            CoordinationRuntimeBoundary.NOT_CROSSED_PREPARED: (
                CoordinatedSiblingActionKind.ABORT_PREPARED
            ),
            CoordinationRuntimeBoundary.KNOWN_TERMINAL: (
                CoordinatedSiblingActionKind.RETAIN_TERMINAL
            ),
        }[boundary]
        assert action.kind is expected_kind
        assert plan.completion is CoordinatedBarrierCompletion.APPLY_FAILED


def test_supervisor_unknown_requires_terminal_subtasks_and_keeps_pointer() -> None:
    task, target, aggregate = _supervisor_unknown()
    plan = plan_unknown_outcome(aggregate, triggering_run_id=target[1].id, reason="lost")
    assert plan.trigger_guard.subtask_id is None
    assert plan.trigger_guard.boundary is CoordinationRuntimeBoundary.CROSSED_ACTIVE
    assert plan.completion is CoordinatedBarrierCompletion.WAIT_RECONCILIATION
    task.current_run_id = None
    with pytest.raises(RuntimeExecutionConflict):
        plan_unknown_outcome(aggregate, triggering_run_id=target[1].id, reason="lost")


@pytest.mark.parametrize("phase", list(KnownTerminalPhase))
def test_supervisor_reconciled_supports_all_terminal_conclusions(phase) -> None:
    task, target, aggregate = _supervisor_reconciled()
    cancel = phase is KnownTerminalPhase.CANCELED
    if cancel:
        aggregate = replace(
            aggregate,
            lifecycle_operations=(
                _cancel_intent(tenant_id=task.tenant_id, execution_id=target[3].id),
            ),
        )
    plan = plan_reconciled_terminal(
        aggregate,
        triggering_run_id=target[1].id,
        phase=phase,
        cancel_intent_present=cancel,
        safe_error=None,
    )
    assert plan.trigger_guard.subtask_id is None
    assert plan.trigger_guard.boundary is CoordinationRuntimeBoundary.RECONCILIATION_EVIDENCE
    assert plan.completion is {
        KnownTerminalPhase.SUCCEEDED: CoordinatedBarrierCompletion.APPLY_RUNNING,
        KnownTerminalPhase.FAILED: CoordinatedBarrierCompletion.APPLY_FAILED,
        KnownTerminalPhase.TIMED_OUT: CoordinatedBarrierCompletion.APPLY_FAILED,
        KnownTerminalPhase.CANCELED: CoordinatedBarrierCompletion.APPLY_CANCELED,
    }[phase]


@pytest.mark.parametrize(
    "mutation",
    ["missing_attempt", "wrong_owner", "missing_version", "wrong_role", "wrong_boundary"],
)
def test_unknown_partial_lifecycle_and_binding_fail_closed(mutation) -> None:
    _task, target, aggregate = _aggregate()
    if mutation == "missing_attempt":
        attempts = dict(aggregate.latest_attempts)
        attempts[target[1].id] = None
        aggregate = replace(aggregate, latest_attempts=MappingProxyType(attempts))
    elif mutation == "wrong_owner":
        execution = replace(aggregate.executions[0], current_owner_attempt_id=None)
        aggregate = replace(aggregate, executions=(execution,))
    elif mutation == "missing_version":
        aggregate = replace(aggregate, runtime_versions=MappingProxyType({}))
    elif mutation == "wrong_role":
        run = replace(target[1], role=RunRole.REVIEWER)
        aggregate = replace(aggregate, runs=(run,))
    else:
        aggregate = replace(
            aggregate,
            boundary_classifications=MappingProxyType(
                {target[1].id: CoordinationRuntimeBoundary.NOT_CROSSED_PREPARED}
            ),
        )
    with pytest.raises(RuntimeExecutionConflict):
        plan_unknown_outcome(aggregate, triggering_run_id=target[1].id, reason="lost")


@pytest.mark.parametrize("entry", ["unknown", "reconciled"])
@pytest.mark.parametrize(
    "mutation",
    ["cohort_tenant", "cohort_task", "attempt_run", "invalid_fence"],
)
def test_both_planners_reject_cohort_and_attempt_identity_gaps(entry, mutation) -> None:
    if entry == "unknown":
        task, target, aggregate = _aggregate()
    else:
        task, target, aggregate = _parked_executor()
    if mutation == "cohort_tenant":
        aggregate = replace(
            aggregate, cohort=replace(aggregate.cohort, tenant_id="other")
        )
    elif mutation == "cohort_task":
        aggregate = replace(aggregate, cohort=replace(aggregate.cohort, task_id=target[1].id))
    else:
        attempt = replace(
            target[2],
            run_id=target[1].id if mutation == "invalid_fence" else uuid4(),
            fencing_token=0 if mutation == "invalid_fence" else target[2].fencing_token,
        )
        attempts = dict(aggregate.latest_attempts)
        attempts[target[1].id] = attempt
        aggregate = replace(aggregate, latest_attempts=MappingProxyType(attempts))
    with pytest.raises(RuntimeExecutionConflict):
        if entry == "unknown":
            plan_unknown_outcome(aggregate, triggering_run_id=target[1].id, reason="lost")
        else:
            plan_reconciled_terminal(
                aggregate,
                triggering_run_id=target[1].id,
                phase=KnownTerminalPhase.FAILED,
                cancel_intent_present=False,
                safe_error=None,
            )


def test_d2_applier_rejects_reconciliation_plan() -> None:
    _task, target, aggregate = _aggregate()
    plan = plan_unknown_outcome(aggregate, triggering_run_id=target[1].id, reason="lost")
    with pytest.raises(RuntimeExecutionConflict):
        CoordinatedRuntimeBarrierApplier().apply_in_uow(
            object(),
            aggregate=aggregate,
            plan=plan,
            now=target[3].updated_at,
            cancel_deadline_window=timedelta(minutes=5),
        )
