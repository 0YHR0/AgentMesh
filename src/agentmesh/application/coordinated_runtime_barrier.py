"""Pure planning for the coordinated known-terminal convergence barrier.

The planner deliberately consumes only the immutable projection produced by the
coordinated aggregate locker.  It does not open a unit of work, read a clock,
or perform a business mutation.  The transaction-local applier is a later
slice; keeping this module pure makes the boundary easy to qualify in tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from importlib import import_module
from typing import Any
from uuid import UUID

from agentmesh.application.authority_cohorts import AuthorityCohort
from agentmesh.application.business_outcomes import KnownTerminalPhase
from agentmesh.domain.coordination import (
    CoordinationRuntimeBoundary,
    CoordinationRuntimeDrainStatus,
    CoordinationRuntimeDrainTarget,
    Subtask,
    normalize_coordination_reason,
)
from agentmesh.domain.errors import RuntimeExecutionConflict
from agentmesh.domain.runtime_execution import (
    RuntimeExecution,
    RuntimeLifecycleOperation,
    RuntimeVersion,
    RuntimeVersionStatus,
)
from agentmesh.domain.tasks import RunRole, Task, TaskExecutionMode, TaskRun


class CoordinatedSiblingActionKind(str, Enum):
    RETAIN_TERMINAL = "RETAIN_TERMINAL"
    WAIT_CROSSED = "WAIT_CROSSED"
    WAIT_RECONCILIATION = "WAIT_RECONCILIATION"
    RELEASE_QUEUED = "RELEASE_QUEUED"
    ABORT_NO_EXECUTION = "ABORT_NO_EXECUTION"
    ABORT_PREPARED = "ABORT_PREPARED"
    REQUEST_CANCEL = "REQUEST_CANCEL"


class CoordinatedBarrierCompletion(str, Enum):
    CONTINUE_SUCCESS = "CONTINUE_SUCCESS"
    WAIT_ACTIVE = "WAIT_ACTIVE"
    WAIT_RECONCILIATION = "WAIT_RECONCILIATION"
    APPLY_RUNNING = "APPLY_RUNNING"
    APPLY_WAITING_APPROVAL = "APPLY_WAITING_APPROVAL"
    APPLY_FAILED = "APPLY_FAILED"
    APPLY_CANCELED = "APPLY_CANCELED"


@dataclass(frozen=True)
class CoordinatedSiblingAction:
    run_id: UUID
    subtask_id: UUID
    execution_id: UUID | None
    attempt_id: UUID | None
    fencing_token: int | None
    kind: CoordinatedSiblingActionKind

    def __post_init__(self) -> None:
        if type(self.run_id) is not UUID or type(self.subtask_id) is not UUID:
            raise RuntimeExecutionConflict("Coordinated sibling action identity is invalid")
        if type(self.kind) is not CoordinatedSiblingActionKind:
            raise RuntimeExecutionConflict("Coordinated sibling action kind is invalid")
        if self.execution_id is not None and type(self.execution_id) is not UUID:
            raise RuntimeExecutionConflict("Coordinated sibling action execution is invalid")
        if self.attempt_id is not None and type(self.attempt_id) is not UUID:
            raise RuntimeExecutionConflict("Coordinated sibling action Attempt is invalid")
        if self.fencing_token is not None and (
            type(self.fencing_token) is not int or self.fencing_token <= 0
        ):
            raise RuntimeExecutionConflict("Coordinated sibling action fence is invalid")

        no_execution = self.kind in {
            CoordinatedSiblingActionKind.RELEASE_QUEUED,
            CoordinatedSiblingActionKind.ABORT_NO_EXECUTION,
        }
        if self.kind is CoordinatedSiblingActionKind.RELEASE_QUEUED and any(
            value is not None for value in (self.execution_id, self.attempt_id, self.fencing_token)
        ):
            raise RuntimeExecutionConflict("Queued release cannot carry Runtime ownership")
        if self.kind is CoordinatedSiblingActionKind.ABORT_NO_EXECUTION and (
            self.execution_id is not None or self.attempt_id is None or self.fencing_token is None
        ):
            raise RuntimeExecutionConflict("No-execution abort must carry only Attempt ownership")
        if not no_execution and (
            self.execution_id is None or self.attempt_id is None or self.fencing_token is None
        ):
            raise RuntimeExecutionConflict("Coordinated sibling action ownership is incomplete")


@dataclass(frozen=True)
class CoordinatedBarrierPlan:
    task_id: UUID
    tenant_id: str
    triggering_run_id: UUID
    requested_target: CoordinationRuntimeDrainTarget | None
    requested_reason: str | None
    effective_target: CoordinationRuntimeDrainTarget | None
    effective_reason: str | None
    create_drain: bool
    retarget_drain: bool
    sibling_actions: tuple[CoordinatedSiblingAction, ...]
    completion: CoordinatedBarrierCompletion

    def __post_init__(self) -> None:
        if (
            type(self.task_id) is not UUID
            or type(self.triggering_run_id) is not UUID
            or type(self.tenant_id) is not str
            or not self.tenant_id.strip()
            or self.tenant_id != self.tenant_id.strip()
            or len(self.tenant_id) > 128
        ):
            raise RuntimeExecutionConflict("Coordinated barrier plan identity is invalid")
        if type(self.create_drain) is not bool or type(self.retarget_drain) is not bool:
            raise RuntimeExecutionConflict("Coordinated barrier drain flags are invalid")
        if type(self.completion) is not CoordinatedBarrierCompletion:
            raise RuntimeExecutionConflict("Coordinated barrier completion is invalid")
        self._validate_target_reason(self.requested_target, self.requested_reason)
        self._validate_target_reason(self.effective_target, self.effective_reason)
        if self.create_drain and self.requested_target is None:
            raise RuntimeExecutionConflict("Created drain requires a requested target")
        if self.create_drain and self.effective_target != self.requested_target:
            raise RuntimeExecutionConflict("New drain cannot be retargeted during planning")
        if self.retarget_drain and self.create_drain:
            raise RuntimeExecutionConflict("New drain cannot also be retargeted")
        actions = tuple(self.sibling_actions)
        if actions != self.sibling_actions or any(
            type(action) is not CoordinatedSiblingAction for action in actions
        ):
            raise RuntimeExecutionConflict("Coordinated sibling actions are invalid")
        if tuple(sorted(actions, key=lambda value: value.run_id)) != actions:
            raise RuntimeExecutionConflict("Coordinated sibling actions are not UUID ordered")
        if len({action.run_id for action in actions}) != len(actions):
            raise RuntimeExecutionConflict("Coordinated sibling actions are duplicated")
        crossed = any(
            action.kind is CoordinatedSiblingActionKind.WAIT_CROSSED for action in actions
        )
        reconciliation = any(
            action.kind is CoordinatedSiblingActionKind.WAIT_RECONCILIATION for action in actions
        )
        if crossed and self.completion is not CoordinatedBarrierCompletion.WAIT_ACTIVE:
            raise RuntimeExecutionConflict("Crossed sibling requires active wait completion")
        if (
            not crossed
            and reconciliation
            and self.completion is not CoordinatedBarrierCompletion.WAIT_RECONCILIATION
        ):
            raise RuntimeExecutionConflict("Reconciliation sibling requires reconciliation wait")
        if self.completion is CoordinatedBarrierCompletion.WAIT_ACTIVE and not crossed:
            raise RuntimeExecutionConflict("Active wait has no crossed sibling")
        if self.completion is CoordinatedBarrierCompletion.WAIT_RECONCILIATION and (
            crossed or not reconciliation
        ):
            raise RuntimeExecutionConflict("Reconciliation wait has invalid sibling precedence")
        if self.completion is CoordinatedBarrierCompletion.CONTINUE_SUCCESS and (
            self.create_drain or self.retarget_drain or self.effective_target is not None or actions
        ):
            raise RuntimeExecutionConflict("Continue-success plan cannot carry barrier work")
        applies = {
            CoordinatedBarrierCompletion.APPLY_RUNNING: CoordinationRuntimeDrainTarget.RUNNING,
            CoordinatedBarrierCompletion.APPLY_WAITING_APPROVAL: (
                CoordinationRuntimeDrainTarget.WAITING_APPROVAL
            ),
            CoordinatedBarrierCompletion.APPLY_FAILED: CoordinationRuntimeDrainTarget.FAILED,
            CoordinatedBarrierCompletion.APPLY_CANCELED: CoordinationRuntimeDrainTarget.CANCELED,
        }
        if self.completion in applies and self.effective_target is not applies[self.completion]:
            raise RuntimeExecutionConflict("Barrier completion and effective target disagree")

    @staticmethod
    def _validate_target_reason(
        target: CoordinationRuntimeDrainTarget | None, reason: str | None
    ) -> None:
        if target is None:
            if reason is not None:
                raise RuntimeExecutionConflict("Drain reason requires a drain target")
            return
        if type(target) is not CoordinationRuntimeDrainTarget:
            raise RuntimeExecutionConflict("Drain target is invalid")
        if type(reason) is not str:
            raise RuntimeExecutionConflict("Drain reason is invalid")
        try:
            normalized = normalize_coordination_reason(reason)
        except Exception as exc:  # domain validation is intentionally fail-closed
            raise RuntimeExecutionConflict("Drain reason is invalid") from exc
        if normalized != reason:
            raise RuntimeExecutionConflict("Drain reason is not normalized")


_REASON_BY_PHASE = {
    KnownTerminalPhase.FAILED: "runtime.failed",
    KnownTerminalPhase.TIMED_OUT: "runtime.timed_out",
}


def plan_known_terminal(
    aggregate: Any,
    *,
    triggering_run_id: UUID,
    phase: KnownTerminalPhase,
    cancel_intent_present: bool,
    safe_error: str | None = None,
) -> CoordinatedBarrierPlan:
    """Plan one known terminal result from an already locked aggregate.

    All validation is projection validation.  In particular, the boolean
    ``cancel_intent_present`` is never trusted without the stable lifecycle row
    bound to the exact target execution.
    """

    _validate_target(aggregate, triggering_run_id, phase, cancel_intent_present, safe_error)
    task = aggregate.task
    run = _run_for(aggregate, triggering_run_id)
    execution = _execution_for(aggregate, run)
    _validate_cancel_evidence(aggregate, execution.id, cancel_intent_present, task.tenant_id)
    requested_target, requested_reason = _request_for_phase(
        phase, cancel_intent_present, safe_error
    )
    active_drain = aggregate.active_drain
    if active_drain is not None and (
        active_drain.status is not CoordinationRuntimeDrainStatus.DRAINING
    ):
        raise RuntimeExecutionConflict("Coordinated barrier drain is not active")

    if active_drain is None:
        if phase is KnownTerminalPhase.SUCCEEDED:
            actions: tuple[CoordinatedSiblingAction, ...] = ()
            return CoordinatedBarrierPlan(
                task_id=task.id,
                tenant_id=task.tenant_id,
                triggering_run_id=triggering_run_id,
                requested_target=None,
                requested_reason=None,
                effective_target=None,
                effective_reason=None,
                create_drain=False,
                retarget_drain=False,
                sibling_actions=actions,
                completion=CoordinatedBarrierCompletion.CONTINUE_SUCCESS,
            )
        if phase is KnownTerminalPhase.CANCELED and cancel_intent_present:
            # User cancellation is deliberately reserved for the later c.2f
            # entry contract; d1 cannot create a CANCELED drain.
            raise RuntimeExecutionConflict(
                "Requested coordinated cancellation is reserved for the later barrier slice"
            )
        effective_target, effective_reason = requested_target, requested_reason
        actions = _sibling_actions(aggregate, triggering_run_id, stopping=True)
        completion = _completion(actions, effective_target)
        return CoordinatedBarrierPlan(
            task_id=task.id,
            tenant_id=task.tenant_id,
            triggering_run_id=triggering_run_id,
            requested_target=requested_target,
            requested_reason=requested_reason,
            effective_target=effective_target,
            effective_reason=effective_reason,
            create_drain=True,
            retarget_drain=False,
            sibling_actions=actions,
            completion=completion,
        )

    effective_target, effective_reason = _retarget_projection(
        active_drain.target,
        active_drain.reason,
        requested_target,
        requested_reason,
    )
    actions = _sibling_actions(
        aggregate,
        triggering_run_id,
        stopping=effective_target
        in {
            CoordinationRuntimeDrainTarget.WAITING_APPROVAL,
            CoordinationRuntimeDrainTarget.FAILED,
            CoordinationRuntimeDrainTarget.CANCELED,
        },
    )
    completion = _completion(actions, effective_target)
    return CoordinatedBarrierPlan(
        task_id=task.id,
        tenant_id=task.tenant_id,
        triggering_run_id=triggering_run_id,
        requested_target=requested_target,
        requested_reason=requested_reason,
        effective_target=effective_target,
        effective_reason=effective_reason,
        create_drain=False,
        retarget_drain=effective_target != active_drain.target,
        sibling_actions=actions,
        completion=completion,
    )


def _validate_target(
    aggregate: Any,
    triggering_run_id: UUID,
    phase: KnownTerminalPhase,
    cancel_intent_present: bool,
    safe_error: str | None,
) -> None:
    aggregate_type = getattr(
        import_module("agentmesh.application.coordinated_runtime"),
        "CoordinatedRuntime" + "Aggregate",
    )
    if type(aggregate) is not aggregate_type:
        raise RuntimeExecutionConflict("Known-terminal planner requires a locked aggregate")
    if type(triggering_run_id) is not UUID:
        raise RuntimeExecutionConflict("Known-terminal triggering Run is invalid")
    if type(phase) is not KnownTerminalPhase:
        raise RuntimeExecutionConflict("Known-terminal phase is invalid")
    if type(cancel_intent_present) is not bool:
        raise RuntimeExecutionConflict("Cancel intent evidence flag is invalid")
    if safe_error is not None and type(safe_error) is not str:
        raise RuntimeExecutionConflict("Known-terminal safe error is invalid")
    task = aggregate.task
    if type(task) is not Task or type(aggregate.cohort) is not AuthorityCohort:
        raise RuntimeExecutionConflict("Known-terminal aggregate projection is invalid")
    if (
        type(task.execution_mode) is not TaskExecutionMode
        or task.execution_mode is not TaskExecutionMode.COORDINATED
        or task.tenant_id != aggregate.cohort.tenant_id
        or task.id != aggregate.cohort.task_id
    ):
        raise RuntimeExecutionConflict("Known-terminal aggregate cohort is invalid")
    if aggregate.cohort.runtime_authority != "managed" or aggregate.cohort.comparison_mode != "off":
        raise RuntimeExecutionConflict("Known-terminal target requires a managed cohort")
    run = _run_for(aggregate, triggering_run_id)
    if (
        run.role is not RunRole.EXECUTOR
        or run.runtime_authority != "managed"
        or run.comparison_mode != "off"
        or run.task_id != task.id
        or run.subtask_id is None
        or run.runtime_version_id != aggregate.cohort.runtime_version_id
        or run.runtime_execution_id is None
        or run.runtime_execution_intent_id != run.runtime_execution_id
    ):
        raise RuntimeExecutionConflict("Known-terminal target Run is invalid")
    subtask = _subtask_for(aggregate, run.subtask_id)
    if subtask.task_id != task.id or subtask.current_run_id != run.id:
        raise RuntimeExecutionConflict("Known-terminal target Subtask is invalid")
    attempt = aggregate.latest_attempts.get(run.id)
    if attempt is None or attempt.run_id != run.id or attempt.fencing_token <= 0:
        raise RuntimeExecutionConflict("Known-terminal target Attempt is invalid")
    execution = _execution_for(aggregate, run)
    if (
        execution.tenant_id != task.tenant_id
        or execution.run_id != run.id
        or execution.runtime_version_id != run.runtime_version_id
        or execution.current_owner_attempt_id != attempt.id
        or execution.current_fencing_token != attempt.fencing_token
    ):
        raise RuntimeExecutionConflict("Known-terminal target execution is invalid")
    version = aggregate.runtime_versions.get(run.runtime_version_id)
    if type(version) is not RuntimeVersion or version.id != run.runtime_version_id:
        raise RuntimeExecutionConflict("Known-terminal Runtime Version is invalid")
    if version.status not in {
        RuntimeVersionStatus.PUBLISHED,
        RuntimeVersionStatus.DEPRECATED,
    }:
        raise RuntimeExecutionConflict("Known-terminal Runtime Version is not allowed")


def _run_for(aggregate: Any, run_id: UUID) -> TaskRun:
    matches = [run for run in aggregate.runs if run.id == run_id]
    if len(matches) != 1 or type(matches[0]) is not TaskRun:
        raise RuntimeExecutionConflict("Known-terminal Run is not in the locked aggregate")
    return matches[0]


def _subtask_for(aggregate: Any, subtask_id: UUID) -> Subtask:
    matches = [subtask for subtask in aggregate.subtasks if subtask.id == subtask_id]
    if len(matches) != 1 or type(matches[0]) is not Subtask:
        raise RuntimeExecutionConflict("Known-terminal Subtask is not in the locked aggregate")
    return matches[0]


def _execution_for(aggregate: Any, run: TaskRun) -> RuntimeExecution:
    matches = [
        execution for execution in aggregate.executions if execution.id == run.runtime_execution_id
    ]
    if len(matches) != 1 or type(matches[0]) is not RuntimeExecution:
        raise RuntimeExecutionConflict("Known-terminal execution is not in the locked aggregate")
    return matches[0]


def _validate_cancel_evidence(
    aggregate: Any,
    execution_id: UUID,
    requested: bool,
    tenant_id: str,
) -> None:
    stable_id = f"runtime-cancel:{execution_id}:v1"
    rows = aggregate.lifecycle_operations_by_execution.get(execution_id, ())
    cancel_rows = [
        row for row in rows if getattr(row, "operation", None) is RuntimeLifecycleOperation.CANCEL
    ]
    stable_rows = [
        row
        for row in cancel_rows
        if getattr(row, "operation_id", None) == stable_id
        and getattr(row, "runtime_execution_id", None) == execution_id
        and getattr(row, "tenant_id", None) == tenant_id
    ]
    if cancel_rows and (
        len(cancel_rows) != 1 or len(stable_rows) != 1 or stable_rows[0] is not cancel_rows[0]
    ):
        raise RuntimeExecutionConflict("Persisted cancel intent is not stable for the target")
    if requested != bool(stable_rows):
        raise RuntimeExecutionConflict("Cancel intent flag does not match persisted evidence")


def _request_for_phase(
    phase: KnownTerminalPhase, cancel_intent_present: bool, safe_error: str | None
) -> tuple[CoordinationRuntimeDrainTarget, str]:
    if phase is KnownTerminalPhase.SUCCEEDED:
        if safe_error is not None:
            raise RuntimeExecutionConflict("Successful known terminal cannot carry an error")
        return CoordinationRuntimeDrainTarget.RUNNING, "runtime.succeeded"
    if phase is KnownTerminalPhase.CANCELED:
        if not cancel_intent_present:
            if safe_error not in (None, "runtime.unrequested_cancellation"):
                raise RuntimeExecutionConflict("Unrequested cancellation reason is not stable")
            return CoordinationRuntimeDrainTarget.FAILED, "runtime.unrequested_cancellation"
        if safe_error is None:
            return CoordinationRuntimeDrainTarget.CANCELED, "runtime.canceled"
        return CoordinationRuntimeDrainTarget.CANCELED, _safe_reason(safe_error)
    default = _REASON_BY_PHASE[phase]
    return CoordinationRuntimeDrainTarget.FAILED, (
        default if safe_error is None else _safe_reason(safe_error)
    )


def _safe_reason(value: str) -> str:
    try:
        return normalize_coordination_reason(value)
    except Exception as exc:
        raise RuntimeExecutionConflict("Known-terminal safe error is invalid") from exc


def _retarget_projection(
    current_target: CoordinationRuntimeDrainTarget,
    current_reason: str,
    requested_target: CoordinationRuntimeDrainTarget,
    requested_reason: str,
) -> tuple[CoordinationRuntimeDrainTarget, str]:
    allowed = {
        CoordinationRuntimeDrainTarget.RUNNING: {
            CoordinationRuntimeDrainTarget.WAITING_APPROVAL,
            CoordinationRuntimeDrainTarget.FAILED,
            CoordinationRuntimeDrainTarget.CANCELED,
        },
        CoordinationRuntimeDrainTarget.WAITING_APPROVAL: {
            CoordinationRuntimeDrainTarget.FAILED,
            CoordinationRuntimeDrainTarget.CANCELED,
        },
        CoordinationRuntimeDrainTarget.FAILED: set(),
        CoordinationRuntimeDrainTarget.CANCELED: set(),
    }
    if requested_target in allowed[current_target]:
        return requested_target, requested_reason
    return current_target, current_reason


def _sibling_actions(
    aggregate: Any,
    triggering_run_id: UUID,
    *,
    stopping: bool,
) -> tuple[CoordinatedSiblingAction, ...]:
    actions: list[CoordinatedSiblingAction] = []
    for run in sorted(aggregate.runs, key=lambda value: value.id):
        if run.id == triggering_run_id or run.subtask_id is None:
            continue
        subtask = _subtask_for(aggregate, run.subtask_id)
        if subtask.current_run_id != run.id:
            continue  # historical evidence, never a barrier action
        if run.role is not RunRole.EXECUTOR or run.runtime_authority != "managed":
            raise RuntimeExecutionConflict("Current coordinated sibling is not managed Executor")
        boundary = aggregate.boundary_classifications.get(run.id)
        if type(boundary) is not CoordinationRuntimeBoundary:
            raise RuntimeExecutionConflict("Current sibling boundary classification is missing")
        attempt = aggregate.latest_attempts.get(run.id)
        execution = (
            _execution_for(aggregate, run) if run.runtime_execution_id is not None else None
        )
        if attempt is not None and (
            attempt.run_id != run.id
            or type(attempt.fencing_token) is not int
            or attempt.fencing_token <= 0
        ):
            raise RuntimeExecutionConflict("Current sibling Attempt ownership is invalid")
        if execution is not None and (
            execution.run_id != run.id
            or execution.runtime_version_id != run.runtime_version_id
            or execution.current_owner_attempt_id != (attempt.id if attempt is not None else None)
            or execution.current_fencing_token
            != (attempt.fencing_token if attempt is not None else None)
        ):
            raise RuntimeExecutionConflict("Current sibling execution ownership is invalid")
        if boundary is CoordinationRuntimeBoundary.NOT_CROSSED_QUEUED:
            if attempt is not None or execution is not None:
                raise RuntimeExecutionConflict("Queued sibling carries Runtime ownership")
            kind = CoordinatedSiblingActionKind.RELEASE_QUEUED
        elif boundary is CoordinationRuntimeBoundary.NOT_CROSSED_NO_EXECUTION:
            if attempt is None or execution is not None:
                raise RuntimeExecutionConflict("No-execution sibling projection is invalid")
            kind = CoordinatedSiblingActionKind.ABORT_NO_EXECUTION
        elif boundary is CoordinationRuntimeBoundary.NOT_CROSSED_PREPARED:
            if attempt is None or execution is None:
                raise RuntimeExecutionConflict("Prepared sibling projection is incomplete")
            kind = CoordinatedSiblingActionKind.ABORT_PREPARED
        elif boundary is CoordinationRuntimeBoundary.CROSSED_ACTIVE:
            if attempt is None or execution is None:
                raise RuntimeExecutionConflict("Crossed sibling projection is incomplete")
            kind = (
                CoordinatedSiblingActionKind.REQUEST_CANCEL
                if stopping
                else CoordinatedSiblingActionKind.WAIT_CROSSED
            )
        elif boundary is CoordinationRuntimeBoundary.KNOWN_TERMINAL:
            if attempt is None or execution is None:
                raise RuntimeExecutionConflict("Terminal sibling projection is incomplete")
            kind = CoordinatedSiblingActionKind.RETAIN_TERMINAL
        elif boundary is CoordinationRuntimeBoundary.RECONCILIATION_EVIDENCE:
            if attempt is None or execution is None:
                raise RuntimeExecutionConflict("Reconciliation sibling projection is incomplete")
            kind = CoordinatedSiblingActionKind.WAIT_RECONCILIATION
        else:  # pragma: no cover - defensive for future enum additions
            raise RuntimeExecutionConflict("Unknown coordinated sibling boundary")
        actions.append(
            CoordinatedSiblingAction(
                run_id=run.id,
                subtask_id=subtask.id,
                execution_id=execution.id if execution is not None else None,
                attempt_id=attempt.id if attempt is not None else None,
                fencing_token=attempt.fencing_token if attempt is not None else None,
                kind=kind,
            )
        )
    return tuple(actions)


def _completion(
    actions: tuple[CoordinatedSiblingAction, ...],
    effective_target: CoordinationRuntimeDrainTarget | None,
) -> CoordinatedBarrierCompletion:
    if any(action.kind is CoordinatedSiblingActionKind.WAIT_CROSSED for action in actions):
        return CoordinatedBarrierCompletion.WAIT_ACTIVE
    if any(action.kind is CoordinatedSiblingActionKind.WAIT_RECONCILIATION for action in actions):
        return CoordinatedBarrierCompletion.WAIT_RECONCILIATION
    if effective_target is None:
        return CoordinatedBarrierCompletion.CONTINUE_SUCCESS
    return {
        CoordinationRuntimeDrainTarget.RUNNING: CoordinatedBarrierCompletion.APPLY_RUNNING,
        CoordinationRuntimeDrainTarget.WAITING_APPROVAL: (
            CoordinatedBarrierCompletion.APPLY_WAITING_APPROVAL
        ),
        CoordinationRuntimeDrainTarget.FAILED: CoordinatedBarrierCompletion.APPLY_FAILED,
        CoordinationRuntimeDrainTarget.CANCELED: CoordinatedBarrierCompletion.APPLY_CANCELED,
    }[effective_target]


__all__ = [
    "CoordinatedBarrierCompletion",
    "CoordinatedBarrierPlan",
    "CoordinatedSiblingAction",
    "CoordinatedSiblingActionKind",
    "KnownTerminalPhase",
    "plan_known_terminal",
]
