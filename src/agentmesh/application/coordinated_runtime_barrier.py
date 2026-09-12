"""Pure planning for the coordinated known-terminal convergence barrier.

The planner deliberately consumes only the immutable projection produced by the
coordinated aggregate locker.  It does not open a unit of work, read a clock,
or perform a business mutation.  The transaction-local applier is a later
slice; keeping this module pure makes the boundary easy to qualify in tests.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from agentmesh.application.authority_cohorts import AuthorityCohort, AuthorityCohortResolver
from agentmesh.application.budget_services import BudgetController
from agentmesh.application.business_outcomes import KnownTerminalPhase
from agentmesh.application.coordinated_runtime import CoordinatedRuntimeAggregate
from agentmesh.application.quota_services import QuotaController
from agentmesh.domain.coordination import (
    CoordinationRuntimeBoundary,
    CoordinationRuntimeDrain,
    CoordinationRuntimeDrainStatus,
    CoordinationRuntimeDrainTarget,
    Subtask,
    SubtaskStatus,
    normalize_coordination_reason,
)
from agentmesh.domain.errors import (
    InvalidTaskInput,
    RuntimeExecutionConflict,
    RuntimeVersionNotFound,
)
from agentmesh.domain.messaging import MessageEnvelope
from agentmesh.domain.runtime_execution import (
    RuntimeExecution,
    RuntimeExecutionPhase,
    RuntimeLifecycleIntent,
    RuntimeLifecycleOperation,
    RuntimeLifecycleStatus,
    RuntimeVersion,
)
from agentmesh.domain.tasks import (
    AttemptStatus,
    RunRole,
    RunStatus,
    Task,
    TaskExecutionMode,
    TaskRun,
    TaskStatus,
)


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


class CoordinatedBarrierTriggerDisposition(str, Enum):
    """The authoritative evidence class that caused a barrier plan."""

    KNOWN_TERMINAL = "KNOWN_TERMINAL"
    RECONCILIATION_EVIDENCE = "RECONCILIATION_EVIDENCE"


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
class CoordinatedBarrierTriggerGuard:
    """Immutable identity and boundary of the triggering Runtime before apply."""

    run_id: UUID
    subtask_id: UUID | None
    execution_id: UUID
    attempt_id: UUID
    fencing_token: int
    boundary: CoordinationRuntimeBoundary
    disposition: CoordinatedBarrierTriggerDisposition = (
        CoordinatedBarrierTriggerDisposition.KNOWN_TERMINAL
    )

    def __post_init__(self) -> None:
        if any(
            type(value) is not UUID
            for value in (self.run_id, self.execution_id, self.attempt_id)
        ):
            raise RuntimeExecutionConflict("Coordinated barrier trigger guard identity is invalid")
        if self.subtask_id is not None and type(self.subtask_id) is not UUID:
            raise RuntimeExecutionConflict("Coordinated barrier trigger guard Subtask is invalid")
        if type(self.fencing_token) is not int or self.fencing_token <= 0:
            raise RuntimeExecutionConflict("Coordinated barrier trigger guard fence is invalid")
        if (
            type(self.boundary) is not CoordinationRuntimeBoundary
            or self.boundary
            not in {
                CoordinationRuntimeBoundary.CROSSED_ACTIVE,
                CoordinationRuntimeBoundary.RECONCILIATION_EVIDENCE,
            }
        ):
            raise RuntimeExecutionConflict("Coordinated barrier trigger guard boundary is invalid")
        if type(self.disposition) is not CoordinatedBarrierTriggerDisposition:
            raise RuntimeExecutionConflict("Coordinated barrier trigger disposition is invalid")
        if self.disposition is CoordinatedBarrierTriggerDisposition.KNOWN_TERMINAL:
            if self.boundary is not CoordinationRuntimeBoundary.CROSSED_ACTIVE:
                raise RuntimeExecutionConflict("Known-terminal trigger boundary is invalid")
        elif self.boundary not in {
            CoordinationRuntimeBoundary.CROSSED_ACTIVE,
            CoordinationRuntimeBoundary.RECONCILIATION_EVIDENCE,
        }:
            raise RuntimeExecutionConflict("Reconciliation trigger boundary is invalid")


@dataclass(frozen=True)
class CoordinatedBarrierDrainGuard:
    """Immutable source identity and state of the active drain before apply."""

    id: UUID
    version: int
    status: CoordinationRuntimeDrainStatus
    target: CoordinationRuntimeDrainTarget
    reason: str
    triggering_run_id: UUID
    created_at: datetime

    def __post_init__(self) -> None:
        if type(self.id) is not UUID or type(self.triggering_run_id) is not UUID:
            raise RuntimeExecutionConflict("Coordinated barrier drain guard identity is invalid")
        if type(self.version) is not int or self.version <= 0:
            raise RuntimeExecutionConflict("Coordinated barrier drain guard version is invalid")
        if (
            type(self.status) is not CoordinationRuntimeDrainStatus
            or self.status is not CoordinationRuntimeDrainStatus.DRAINING
        ):
            raise RuntimeExecutionConflict("Coordinated barrier drain guard status is invalid")
        if type(self.target) is not CoordinationRuntimeDrainTarget:
            raise RuntimeExecutionConflict("Coordinated barrier drain guard target is invalid")
        if type(self.reason) is not str:
            raise RuntimeExecutionConflict("Coordinated barrier drain guard reason is invalid")
        try:
            normalized = normalize_coordination_reason(self.reason)
        except Exception as exc:  # domain validation is intentionally fail-closed
            raise RuntimeExecutionConflict(
                "Coordinated barrier drain guard reason is invalid"
            ) from exc
        if normalized != self.reason:
            raise RuntimeExecutionConflict(
                "Coordinated barrier drain guard reason is not normalized"
            )
        if (
            type(self.created_at) is not datetime
            or self.created_at.tzinfo is None
            or self.created_at.utcoffset() is None
            or self.created_at.utcoffset() != timedelta(0)
        ):
            raise RuntimeExecutionConflict(
                "Coordinated barrier drain guard creation time is invalid"
            )


@dataclass(frozen=True)
class CoordinatedBarrierPlan:
    task_id: UUID
    tenant_id: str
    triggering_run_id: UUID
    trigger_guard: CoordinatedBarrierTriggerGuard
    source_drain_guard: CoordinatedBarrierDrainGuard | None
    requested_target: CoordinationRuntimeDrainTarget | None
    requested_reason: str | None
    effective_target: CoordinationRuntimeDrainTarget | None
    effective_reason: str | None
    create_drain: bool
    retarget_drain: bool
    sibling_actions: tuple[CoordinatedSiblingAction, ...]
    completion: CoordinatedBarrierCompletion
    trigger_disposition: CoordinatedBarrierTriggerDisposition = (
        CoordinatedBarrierTriggerDisposition.KNOWN_TERMINAL
    )

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
        if type(self.trigger_disposition) is not CoordinatedBarrierTriggerDisposition:
            raise RuntimeExecutionConflict("Coordinated barrier trigger disposition is invalid")
        if type(self.trigger_guard) is not CoordinatedBarrierTriggerGuard:
            raise RuntimeExecutionConflict("Coordinated barrier trigger guard is invalid")
        if self.trigger_guard.run_id != self.triggering_run_id:
            raise RuntimeExecutionConflict("Coordinated barrier trigger guard is inconsistent")
        if self.trigger_guard.disposition is not self.trigger_disposition:
            raise RuntimeExecutionConflict(
                "Coordinated barrier trigger disposition is inconsistent"
            )
        if self.trigger_disposition is CoordinatedBarrierTriggerDisposition.KNOWN_TERMINAL and (
            self.trigger_guard.boundary is not CoordinationRuntimeBoundary.CROSSED_ACTIVE
        ):
            raise RuntimeExecutionConflict(
                "Coordinated barrier trigger guard boundary is inconsistent"
            )
        if self.source_drain_guard is not None and type(
            self.source_drain_guard
        ) is not CoordinatedBarrierDrainGuard:
            raise RuntimeExecutionConflict("Coordinated barrier drain guard is invalid")
        if self.source_drain_guard is not None and self.source_drain_guard.status is not (
            CoordinationRuntimeDrainStatus.DRAINING
        ):
            raise RuntimeExecutionConflict("Coordinated barrier source drain guard is not active")
        self._validate_target_reason(self.requested_target, self.requested_reason)
        self._validate_target_reason(self.effective_target, self.effective_reason)
        if self.create_drain and self.requested_target is None:
            raise RuntimeExecutionConflict("Created drain requires a requested target")
        if self.create_drain and self.effective_target != self.requested_target:
            raise RuntimeExecutionConflict("New drain cannot be retargeted during planning")
        if self.retarget_drain and self.create_drain:
            raise RuntimeExecutionConflict("New drain cannot also be retargeted")
        if self.create_drain and self.source_drain_guard is not None:
            raise RuntimeExecutionConflict("New drain cannot carry a source drain guard")
        if self.retarget_drain and self.source_drain_guard is None:
            raise RuntimeExecutionConflict("Retargeted drain requires a source drain guard")
        if self.effective_target is None and self.source_drain_guard is not None:
            raise RuntimeExecutionConflict("No effective drain cannot carry a source drain guard")
        if self.effective_target is not None and not (
            self.create_drain or self.source_drain_guard is not None
        ):
            raise RuntimeExecutionConflict("Effective drain requires a source or creation guard")
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
            action.kind
            in {
                CoordinatedSiblingActionKind.WAIT_CROSSED,
                CoordinatedSiblingActionKind.REQUEST_CANCEL,
            }
            for action in actions
        )
        reconciliation = any(
            action.kind is CoordinatedSiblingActionKind.WAIT_RECONCILIATION for action in actions
        )
        if crossed and self.completion is not CoordinatedBarrierCompletion.WAIT_ACTIVE:
            raise RuntimeExecutionConflict("Crossed sibling requires active wait completion")
        trigger_uncertain = (
            self.trigger_disposition
            is CoordinatedBarrierTriggerDisposition.RECONCILIATION_EVIDENCE
            and self.trigger_guard.boundary is CoordinationRuntimeBoundary.CROSSED_ACTIVE
        )
        if (
            not crossed
            and (reconciliation or trigger_uncertain)
            and self.completion is not CoordinatedBarrierCompletion.WAIT_RECONCILIATION
        ):
            raise RuntimeExecutionConflict("Reconciliation sibling requires reconciliation wait")
        if self.completion is CoordinatedBarrierCompletion.WAIT_ACTIVE and not crossed:
            raise RuntimeExecutionConflict("Active wait has no crossed sibling")
        if self.completion is CoordinatedBarrierCompletion.WAIT_RECONCILIATION and (
            crossed or (not reconciliation and not trigger_uncertain)
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
    aggregate: CoordinatedRuntimeAggregate,
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
    trigger_guard = _trigger_guard_for(aggregate, triggering_run_id)
    requested_target, requested_reason = _request_for_phase(
        phase, cancel_intent_present, safe_error
    )
    active_drain = aggregate.active_drain
    source_drain_guard = _drain_guard_for(active_drain)
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
                trigger_guard=trigger_guard,
                source_drain_guard=source_drain_guard,
                requested_target=None,
                requested_reason=None,
                effective_target=None,
                effective_reason=None,
                create_drain=False,
                retarget_drain=False,
                sibling_actions=actions,
                completion=CoordinatedBarrierCompletion.CONTINUE_SUCCESS,
                trigger_disposition=CoordinatedBarrierTriggerDisposition.KNOWN_TERMINAL,
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
            trigger_guard=trigger_guard,
            source_drain_guard=source_drain_guard,
            requested_target=requested_target,
            requested_reason=requested_reason,
            effective_target=effective_target,
            effective_reason=effective_reason,
            create_drain=True,
            retarget_drain=False,
            sibling_actions=actions,
            completion=completion,
            trigger_disposition=CoordinatedBarrierTriggerDisposition.KNOWN_TERMINAL,
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
        trigger_guard=trigger_guard,
        source_drain_guard=source_drain_guard,
        requested_target=requested_target,
        requested_reason=requested_reason,
        effective_target=effective_target,
        effective_reason=effective_reason,
        create_drain=False,
        retarget_drain=effective_target != active_drain.target,
        sibling_actions=actions,
        completion=completion,
        trigger_disposition=CoordinatedBarrierTriggerDisposition.KNOWN_TERMINAL,
    )


def plan_unknown_outcome(
    aggregate: CoordinatedRuntimeAggregate,
    *,
    triggering_run_id: UUID,
    reason: str,
) -> CoordinatedBarrierPlan:
    """Plan the conservative parking of one crossed managed Runtime outcome.

    This is intentionally a pure projection operation.  The caller owns the
    Runtime/Attempt/Run mutation; this function only freezes the barrier
    identity, drain lattice, and untouched sibling actions.
    """

    _validate_unknown_target(aggregate, triggering_run_id, reason)
    task = aggregate.task
    trigger_guard = _trigger_guard_for(
        aggregate,
        triggering_run_id,
        disposition=CoordinatedBarrierTriggerDisposition.KNOWN_TERMINAL,
    )
    active_drain = aggregate.active_drain
    source_drain_guard = _drain_guard_for(active_drain)
    requested_target = CoordinationRuntimeDrainTarget.RUNNING
    requested_reason = "coordination.runtime_reconciliation_required"
    if active_drain is None:
        effective_target, effective_reason = requested_target, requested_reason
        create_drain = True
        retarget = False
    else:
        effective_target, effective_reason = _retarget_projection(
            active_drain.target,
            active_drain.reason,
            requested_target,
            requested_reason,
        )
        create_drain = False
        retarget = effective_target is not active_drain.target or (
            effective_reason != active_drain.reason
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
    completion = _completion(actions, effective_target, trigger_uncertain=True)
    return CoordinatedBarrierPlan(
        task_id=task.id,
        tenant_id=task.tenant_id,
        triggering_run_id=triggering_run_id,
        trigger_guard=replace(
            trigger_guard,
            disposition=CoordinatedBarrierTriggerDisposition.RECONCILIATION_EVIDENCE,
        ),
        source_drain_guard=source_drain_guard,
        requested_target=requested_target,
        requested_reason=requested_reason,
        effective_target=effective_target,
        effective_reason=effective_reason,
        create_drain=create_drain,
        retarget_drain=retarget,
        sibling_actions=actions,
        completion=completion,
        trigger_disposition=CoordinatedBarrierTriggerDisposition.RECONCILIATION_EVIDENCE,
    )


def plan_reconciled_terminal(
    aggregate: CoordinatedRuntimeAggregate,
    *,
    triggering_run_id: UUID,
    phase: KnownTerminalPhase,
    cancel_intent_present: bool,
    safe_error: str | None = None,
) -> CoordinatedBarrierPlan:
    """Plan convergence of a previously parked coordinated Runtime outcome."""

    _validate_reconciled_target(
        aggregate, triggering_run_id, phase, cancel_intent_present, safe_error
    )
    task = aggregate.task
    trigger_guard = _trigger_guard_for(
        aggregate,
        triggering_run_id,
        disposition=CoordinatedBarrierTriggerDisposition.RECONCILIATION_EVIDENCE,
    )
    _validate_cancel_evidence(
        aggregate,
        trigger_guard.execution_id,
        cancel_intent_present,
        task.tenant_id,
    )
    requested_target, requested_reason = _request_for_phase(
        phase, cancel_intent_present, safe_error
    )
    active_drain = aggregate.active_drain
    if active_drain is None or active_drain.status is not CoordinationRuntimeDrainStatus.DRAINING:
        raise RuntimeExecutionConflict("Reconciled coordinated outcome requires an active drain")
    source_drain_guard = _drain_guard_for(active_drain)
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
    completion = _completion(actions, effective_target, trigger_uncertain=False)
    return CoordinatedBarrierPlan(
        task_id=task.id,
        tenant_id=task.tenant_id,
        triggering_run_id=triggering_run_id,
        trigger_guard=trigger_guard,
        source_drain_guard=source_drain_guard,
        requested_target=requested_target,
        requested_reason=requested_reason,
        effective_target=effective_target,
        effective_reason=effective_reason,
        create_drain=False,
        retarget_drain=effective_target is not active_drain.target
        or effective_reason != active_drain.reason,
        sibling_actions=actions,
        completion=completion,
        trigger_disposition=CoordinatedBarrierTriggerDisposition.RECONCILIATION_EVIDENCE,
    )


def _validate_target(
    aggregate: CoordinatedRuntimeAggregate,
    triggering_run_id: UUID,
    phase: KnownTerminalPhase,
    cancel_intent_present: bool,
    safe_error: str | None,
) -> None:
    if type(aggregate) is not CoordinatedRuntimeAggregate:
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
    active_drain = aggregate.active_drain
    if task.status is not TaskStatus.RUNNING and not (
        task.status is TaskStatus.RECONCILIATION_REQUIRED
        and active_drain is not None
        and active_drain.status is CoordinationRuntimeDrainStatus.DRAINING
        and active_drain.task_id == task.id
        and active_drain.tenant_id == task.tenant_id
    ):
        raise RuntimeExecutionConflict("Known-terminal Task is not pre-observation")
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
        or run.status is not RunStatus.RUNNING
        or run.runtime_execution_id is None
        or run.runtime_execution_intent_id != run.runtime_execution_id
    ):
        raise RuntimeExecutionConflict("Known-terminal target Run is invalid")
    subtask = _subtask_for(aggregate, run.subtask_id)
    if (
        subtask.task_id != task.id
        or subtask.current_run_id != run.id
        or subtask.status is not SubtaskStatus.RUNNING
    ):
        raise RuntimeExecutionConflict("Known-terminal target Subtask is invalid")
    attempt = aggregate.latest_attempts.get(run.id)
    if (
        attempt is None
        or attempt.run_id != run.id
        or attempt.status is not AttemptStatus.RUNNING
        or attempt.fencing_token <= 0
    ):
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
    if (
        aggregate.boundary_classifications.get(run.id)
        is not CoordinationRuntimeBoundary.CROSSED_ACTIVE
    ):
        raise RuntimeExecutionConflict("Known-terminal target is not crossed active")
    if execution.phase not in {
        RuntimeExecutionPhase.DISPATCHING,
        RuntimeExecutionPhase.ACCEPTED,
        RuntimeExecutionPhase.RUNNING,
        RuntimeExecutionPhase.WAITING_INPUT,
        RuntimeExecutionPhase.WAITING_APPROVAL,
        RuntimeExecutionPhase.PAUSE_REQUESTED,
        RuntimeExecutionPhase.PAUSED,
        RuntimeExecutionPhase.CANCEL_REQUESTED,
    }:
        raise RuntimeExecutionConflict("Known-terminal target execution is not active")
    version = aggregate.runtime_versions.get(run.runtime_version_id)
    if type(version) is not RuntimeVersion or version.id != run.runtime_version_id:
        raise RuntimeExecutionConflict("Known-terminal Runtime Version is invalid")
    try:
        AuthorityCohortResolver._validate_builtin_langgraph_v2_version(version)
    except RuntimeVersionNotFound as exc:
        raise RuntimeExecutionConflict("Known-terminal Runtime Version is incompatible") from exc


def _validate_unknown_target(
    aggregate: CoordinatedRuntimeAggregate,
    triggering_run_id: UUID,
    reason: str,
) -> None:
    if type(reason) is not str:
        raise RuntimeExecutionConflict("Unknown-outcome reason is invalid")
    _safe_reason(reason)
    if type(aggregate) is not CoordinatedRuntimeAggregate:
        raise RuntimeExecutionConflict("Unknown-outcome planner requires a locked aggregate")
    _validate_managed_coordinated_aggregate(aggregate)
    task = aggregate.task
    if (
        task.status is not TaskStatus.RUNNING
    ):
        raise RuntimeExecutionConflict("Unknown-outcome Task is not running")
    run = _run_for(aggregate, triggering_run_id)
    if (
        run.runtime_authority != "managed"
        or run.comparison_mode != "off"
        or run.runtime_execution_id is None
        or run.runtime_execution_intent_id != run.runtime_execution_id
        or run.runtime_version_id != aggregate.cohort.runtime_version_id
        or run.task_id != task.id
    ):
        raise RuntimeExecutionConflict("Unknown-outcome target Run is not managed")
    if run.role is RunRole.EXECUTOR:
        if run.subtask_id is None:
            raise RuntimeExecutionConflict("Unknown-outcome Executor is unbound")
        subtask = _subtask_for(aggregate, run.subtask_id)
        if subtask.current_run_id != run.id or subtask.status is not SubtaskStatus.RUNNING:
            raise RuntimeExecutionConflict("Unknown-outcome Executor Subtask is not running")
    elif run.role is RunRole.SUPERVISOR:
        current_runs = [value for value in aggregate.runs if value.id == task.current_run_id]
        if (
            run.subtask_id is not None
            or task.current_run_id != run.id
            or len(current_runs) != 1
            or current_runs[0].role is not RunRole.SUPERVISOR
        ):
            raise RuntimeExecutionConflict("Unknown-outcome Supervisor binding is invalid")
        if any(
            subtask.status
            not in {
                SubtaskStatus.COMPLETED,
                SubtaskStatus.FAILED,
                SubtaskStatus.CANCELED,
            }
            for subtask in aggregate.subtasks
        ):
            raise RuntimeExecutionConflict("Unknown-outcome Supervisor requires terminal Subtasks")
    else:
        raise RuntimeExecutionConflict("Unknown-outcome target Run role is invalid")
    attempt = aggregate.latest_attempts.get(run.id)
    execution = _execution_for(aggregate, run)
    if (
        run.status is not RunStatus.RUNNING
        or attempt is None
        or attempt.status is not AttemptStatus.RUNNING
        or attempt.run_id != run.id
        or type(attempt.fencing_token) is not int
        or attempt.fencing_token <= 0
    ):
        raise RuntimeExecutionConflict("Unknown-outcome target lifecycle is invalid")
    if (
        execution.tenant_id != task.tenant_id
        or execution.run_id != run.id
        or execution.runtime_version_id != run.runtime_version_id
        or execution.current_owner_attempt_id != attempt.id
        or execution.current_fencing_token != attempt.fencing_token
    ):
        raise RuntimeExecutionConflict("Unknown-outcome target ownership is invalid")
    if execution.phase not in {
        RuntimeExecutionPhase.DISPATCHING,
        RuntimeExecutionPhase.ACCEPTED,
        RuntimeExecutionPhase.RUNNING,
        RuntimeExecutionPhase.WAITING_INPUT,
        RuntimeExecutionPhase.WAITING_APPROVAL,
        RuntimeExecutionPhase.PAUSE_REQUESTED,
        RuntimeExecutionPhase.PAUSED,
        RuntimeExecutionPhase.CANCEL_REQUESTED,
    }:
        raise RuntimeExecutionConflict("Unknown-outcome Runtime is not active")
    if run.role is RunRole.EXECUTOR and aggregate.boundary_classifications.get(
        run.id
    ) is not CoordinationRuntimeBoundary.CROSSED_ACTIVE:
        raise RuntimeExecutionConflict("Unknown-outcome target is not crossed active")
    _validate_runtime_version(aggregate, run)


def _validate_reconciled_target(
    aggregate: CoordinatedRuntimeAggregate,
    triggering_run_id: UUID,
    phase: KnownTerminalPhase,
    cancel_intent_present: bool,
    safe_error: str | None,
) -> None:
    if type(phase) is not KnownTerminalPhase:
        raise RuntimeExecutionConflict("Reconciled terminal phase is invalid")
    if type(cancel_intent_present) is not bool:
        raise RuntimeExecutionConflict("Reconciled cancel intent evidence flag is invalid")
    if safe_error is not None and type(safe_error) is not str:
        raise RuntimeExecutionConflict("Reconciled safe error is invalid")
    if type(aggregate) is not CoordinatedRuntimeAggregate:
        raise RuntimeExecutionConflict("Reconciled planner requires a locked aggregate")
    _validate_managed_coordinated_aggregate(aggregate)
    task = aggregate.task
    if (
        task.status is not TaskStatus.RECONCILIATION_REQUIRED
    ):
        raise RuntimeExecutionConflict("Reconciled coordinated Task is not held")
    if aggregate.active_drain is None or (
        aggregate.active_drain.status is not CoordinationRuntimeDrainStatus.DRAINING
    ):
        raise RuntimeExecutionConflict("Reconciled coordinated Task has no active drain")
    run = _run_for(aggregate, triggering_run_id)
    if (
        run.runtime_authority != "managed"
        or run.comparison_mode != "off"
        or run.runtime_execution_id is None
        or run.runtime_execution_intent_id != run.runtime_execution_id
        or run.runtime_version_id != aggregate.cohort.runtime_version_id
        or run.task_id != task.id
    ):
        raise RuntimeExecutionConflict("Reconciled target Run is not managed")
    attempt = aggregate.latest_attempts.get(run.id)
    execution = _execution_for(aggregate, run)
    if (
        run.status is not RunStatus.RECONCILIATION_REQUIRED
        or attempt is None
        or attempt.status is not AttemptStatus.OUTCOME_UNKNOWN
        or attempt.run_id != run.id
        or type(attempt.fencing_token) is not int
        or attempt.fencing_token <= 0
    ):
        raise RuntimeExecutionConflict("Reconciled target lifecycle is invalid")
    if execution.phase not in {RuntimeExecutionPhase.LOST, RuntimeExecutionPhase.OUTCOME_UNKNOWN}:
        raise RuntimeExecutionConflict("Reconciled Runtime phase is invalid")
    if (
        execution.tenant_id != task.tenant_id
        or execution.run_id != run.id
        or execution.runtime_version_id != run.runtime_version_id
        or execution.current_owner_attempt_id != attempt.id
        or execution.current_fencing_token != attempt.fencing_token
    ):
        raise RuntimeExecutionConflict("Reconciled target ownership is invalid")
    if run.role is RunRole.EXECUTOR:
        if run.subtask_id is None:
            raise RuntimeExecutionConflict("Reconciled Executor is unbound")
        subtask = _subtask_for(aggregate, run.subtask_id)
        if (
            subtask.current_run_id != run.id
            or subtask.status is not SubtaskStatus.RECONCILIATION_REQUIRED
        ):
            raise RuntimeExecutionConflict("Reconciled Executor Subtask is invalid")
    elif run.role is RunRole.SUPERVISOR:
        current_runs = [value for value in aggregate.runs if value.id == task.current_run_id]
        if (
            run.subtask_id is not None
            or task.current_run_id != run.id
            or len(current_runs) != 1
            or current_runs[0].role is not RunRole.SUPERVISOR
        ):
            raise RuntimeExecutionConflict("Reconciled Supervisor binding is invalid")
        if any(
            subtask.status
            not in {
                SubtaskStatus.COMPLETED,
                SubtaskStatus.FAILED,
                SubtaskStatus.CANCELED,
            }
            for subtask in aggregate.subtasks
        ):
            raise RuntimeExecutionConflict("Reconciled Supervisor requires terminal Subtasks")
    else:
        raise RuntimeExecutionConflict("Reconciled target Run role is invalid")
    if run.role is RunRole.EXECUTOR and aggregate.boundary_classifications.get(
        run.id
    ) is not CoordinationRuntimeBoundary.RECONCILIATION_EVIDENCE:
        raise RuntimeExecutionConflict("Reconciled target boundary is invalid")
    _validate_runtime_version(aggregate, run)


def _validate_managed_coordinated_aggregate(
    aggregate: CoordinatedRuntimeAggregate,
) -> None:
    task = aggregate.task
    cohort = aggregate.cohort
    if type(task) is not Task or type(cohort) is not AuthorityCohort:
        raise RuntimeExecutionConflict("Coordinated planner aggregate projection is invalid")
    if (
        task.execution_mode is not TaskExecutionMode.COORDINATED
        or task.tenant_id != cohort.tenant_id
        or task.id != cohort.task_id
        or cohort.runtime_authority != "managed"
        or cohort.comparison_mode != "off"
        or not cohort.tenant_id.strip()
    ):
        raise RuntimeExecutionConflict("Coordinated planner cohort identity is invalid")


def _validate_runtime_version(aggregate: CoordinatedRuntimeAggregate, run: TaskRun) -> None:
    version = aggregate.runtime_versions.get(run.runtime_version_id)
    if type(version) is not RuntimeVersion or version.id != run.runtime_version_id:
        raise RuntimeExecutionConflict("Coordinated Runtime Version is invalid")
    try:
        AuthorityCohortResolver._validate_builtin_langgraph_v2_version(version)
    except RuntimeVersionNotFound as exc:
        raise RuntimeExecutionConflict("Coordinated Runtime Version is incompatible") from exc


def _run_for(aggregate: CoordinatedRuntimeAggregate, run_id: UUID) -> TaskRun:
    matches = [run for run in aggregate.runs if run.id == run_id]
    if len(matches) != 1 or type(matches[0]) is not TaskRun:
        raise RuntimeExecutionConflict("Known-terminal Run is not in the locked aggregate")
    return matches[0]


def _trigger_guard_for(
    aggregate: CoordinatedRuntimeAggregate,
    run_id: UUID,
    *,
    disposition: CoordinatedBarrierTriggerDisposition = (
        CoordinatedBarrierTriggerDisposition.KNOWN_TERMINAL
    ),
) -> CoordinatedBarrierTriggerGuard:
    run = _run_for(aggregate, run_id)
    if run.runtime_execution_id is None:
        raise RuntimeExecutionConflict("Coordinated barrier trigger guard binding is incomplete")
    subtask = _subtask_for(aggregate, run.subtask_id) if run.subtask_id is not None else None
    if run.role is RunRole.EXECUTOR and subtask is None:
        raise RuntimeExecutionConflict("Coordinated barrier Executor guard binding is incomplete")
    if run.role is RunRole.SUPERVISOR and subtask is not None:
        raise RuntimeExecutionConflict("Coordinated barrier Supervisor guard is bound to a Subtask")
    if run.role not in {RunRole.EXECUTOR, RunRole.SUPERVISOR}:
        raise RuntimeExecutionConflict("Coordinated barrier trigger Run role is invalid")
    attempt = aggregate.latest_attempts.get(run.id)
    execution = _execution_for(aggregate, run)
    if attempt is None:
        raise RuntimeExecutionConflict("Coordinated barrier trigger guard Attempt is missing")
    boundary = aggregate.boundary_classifications.get(run.id)
    if boundary is None:
        if run.role is not RunRole.SUPERVISOR:
            raise RuntimeExecutionConflict("Coordinated barrier trigger guard boundary is missing")
        if run.status is RunStatus.RECONCILIATION_REQUIRED and execution.phase in {
            RuntimeExecutionPhase.LOST,
            RuntimeExecutionPhase.OUTCOME_UNKNOWN,
        }:
            boundary = CoordinationRuntimeBoundary.RECONCILIATION_EVIDENCE
        elif run.status is RunStatus.RUNNING:
            boundary = CoordinationRuntimeBoundary.CROSSED_ACTIVE
        else:
            raise RuntimeExecutionConflict("Coordinated barrier Supervisor boundary is invalid")
    return CoordinatedBarrierTriggerGuard(
        run_id=run.id,
        subtask_id=subtask.id if subtask is not None else None,
        execution_id=execution.id,
        attempt_id=attempt.id,
        fencing_token=attempt.fencing_token,
        boundary=boundary,
        disposition=disposition,
    )


def _drain_guard_for(
    drain: CoordinationRuntimeDrain | None,
) -> CoordinatedBarrierDrainGuard | None:
    if drain is None:
        return None
    if type(drain) is not CoordinationRuntimeDrain:
        raise RuntimeExecutionConflict("Coordinated barrier source drain is invalid")
    return CoordinatedBarrierDrainGuard(
        id=drain.id,
        version=drain.version,
        status=drain.status,
        target=drain.target,
        reason=drain.reason,
        triggering_run_id=drain.triggering_run_id,
        created_at=drain.created_at,
    )


def _subtask_for(aggregate: CoordinatedRuntimeAggregate, subtask_id: UUID) -> Subtask:
    matches = [subtask for subtask in aggregate.subtasks if subtask.id == subtask_id]
    if len(matches) != 1 or type(matches[0]) is not Subtask:
        raise RuntimeExecutionConflict("Known-terminal Subtask is not in the locked aggregate")
    return matches[0]


def _execution_for(aggregate: CoordinatedRuntimeAggregate, run: TaskRun) -> RuntimeExecution:
    matches = [
        execution for execution in aggregate.executions if execution.id == run.runtime_execution_id
    ]
    if len(matches) != 1 or type(matches[0]) is not RuntimeExecution:
        raise RuntimeExecutionConflict("Known-terminal execution is not in the locked aggregate")
    return matches[0]


def _validate_cancel_evidence(
    aggregate: CoordinatedRuntimeAggregate,
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
    aggregate: CoordinatedRuntimeAggregate,
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
        execution = _execution_for(aggregate, run) if run.runtime_execution_id is not None else None
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
    *,
    trigger_uncertain: bool = False,
) -> CoordinatedBarrierCompletion:
    if any(
        action.kind
        in {
            CoordinatedSiblingActionKind.WAIT_CROSSED,
            CoordinatedSiblingActionKind.REQUEST_CANCEL,
        }
        for action in actions
    ):
        return CoordinatedBarrierCompletion.WAIT_ACTIVE
    if any(action.kind is CoordinatedSiblingActionKind.WAIT_RECONCILIATION for action in actions):
        return CoordinatedBarrierCompletion.WAIT_RECONCILIATION
    if trigger_uncertain:
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


_MAX_CANCEL_DEADLINE_WINDOW = timedelta(days=7)
_DRAIN_ID_PREFIX = "coordination-runtime-drain:"
_LIFECYCLE_OUTBOX_PREFIX = "coordination-runtime-lifecycle-outbox:"
_ABORT_OUTBOX_PREFIX = "coordination-runtime-abort-outbox:"


@dataclass(frozen=True)
class CoordinatedBarrierApplication:
    """Closed result of one transaction-local barrier application."""

    effective_drain: CoordinationRuntimeDrain | None
    changed_ids: tuple[UUID, ...]
    lifecycle_operation_ids: tuple[str, ...]
    completion: CoordinatedBarrierCompletion
    made_progress: bool

    def __post_init__(self) -> None:
        if self.effective_drain is not None and type(
            self.effective_drain
        ) is not CoordinationRuntimeDrain:
            raise RuntimeExecutionConflict("Coordinated barrier application drain is invalid")
        if type(self.completion) is not CoordinatedBarrierCompletion:
            raise RuntimeExecutionConflict("Coordinated barrier application completion is invalid")
        changed = tuple(self.changed_ids)
        if changed != self.changed_ids or any(type(value) is not UUID for value in changed):
            raise RuntimeExecutionConflict("Coordinated barrier changed IDs are invalid")
        if tuple(sorted(changed, key=str)) != changed or len(set(changed)) != len(changed):
            raise RuntimeExecutionConflict("Coordinated barrier changed IDs are not ordered")
        operation_ids = tuple(self.lifecycle_operation_ids)
        if operation_ids != self.lifecycle_operation_ids or any(
            type(value) is not str or not value.strip() for value in operation_ids
        ):
            raise RuntimeExecutionConflict("Coordinated barrier lifecycle IDs are invalid")
        if tuple(sorted(operation_ids)) != operation_ids or len(set(operation_ids)) != len(
            operation_ids
        ):
            raise RuntimeExecutionConflict("Coordinated barrier lifecycle IDs are not ordered")
        if type(self.made_progress) is not bool:
            raise RuntimeExecutionConflict("Coordinated barrier progress flag is invalid")
        if changed and not self.made_progress:
            raise RuntimeExecutionConflict("Coordinated barrier progress flag is inconsistent")

    @property
    def drain(self) -> CoordinationRuntimeDrain | None:
        """Compatibility alias for callers that name the effective drain directly."""
        return self.effective_drain

    @property
    def changed_entity_ids(self) -> tuple[UUID, ...]:
        return self.changed_ids

    @property
    def operation_ids(self) -> tuple[str, ...]:
        return self.lifecycle_operation_ids


class CoordinatedRuntimeBarrierApplier:
    """Apply a validated d1 plan inside the caller's already locked UoW."""

    def apply_in_uow(
        self,
        uow: Any,
        *,
        aggregate: CoordinatedRuntimeAggregate,
        plan: CoordinatedBarrierPlan,
        now: datetime,
        cancel_deadline_window: timedelta,
        defer_task_save: bool = False,
    ) -> CoordinatedBarrierApplication:
        timestamp = _barrier_timestamp(now)
        _validate_cancel_window(cancel_deadline_window)
        _validate_application_plan(aggregate, plan)
        _validate_cancel_deadline_before_writes(
            aggregate, plan, now=timestamp, cancel_deadline_window=cancel_deadline_window
        )
        drain, changed_ids = self._prepare_drain(
            uow, aggregate=aggregate, plan=plan, now=timestamp
        )
        changed: set[UUID] = set(changed_ids)
        lifecycle_ids: set[str] = set()
        task_changed = False
        made_progress = bool(changed)

        for action in plan.sibling_actions:
            action_changed, action_task_changed, operation_id, action_progress = self._apply_action(
                uow,
                aggregate=aggregate,
                action=action,
                drain=drain,
                now=timestamp,
                cancel_deadline_window=cancel_deadline_window,
            )
            changed.update(action_changed)
            task_changed = task_changed or action_task_changed
            made_progress = made_progress or action_progress
            if operation_id is not None:
                lifecycle_ids.add(operation_id)

        if task_changed and not defer_task_save:
            uow.tasks.save(aggregate.task)
        if task_changed:
            changed.add(aggregate.task.id)

        return CoordinatedBarrierApplication(
            effective_drain=drain,
            changed_ids=tuple(sorted(changed, key=str)),
            lifecycle_operation_ids=tuple(sorted(lifecycle_ids)),
            completion=plan.completion,
            made_progress=made_progress,
        )

    @staticmethod
    def _prepare_drain(
        uow: Any,
        *,
        aggregate: CoordinatedRuntimeAggregate,
        plan: CoordinatedBarrierPlan,
        now: datetime,
    ) -> tuple[CoordinationRuntimeDrain | None, set[UUID]]:
        active = aggregate.active_drain
        if plan.create_drain:
            if active is not None or plan.effective_target is None or plan.effective_reason is None:
                raise RuntimeExecutionConflict("Barrier drain creation does not match aggregate")
            drain_id = uuid5(
                NAMESPACE_URL,
                f"{_DRAIN_ID_PREFIX}{aggregate.task.tenant_id}:{aggregate.task.id}",
            )
            existing = uow.coordination_runtime_drains.get(
                drain_id, tenant_id=aggregate.task.tenant_id, for_update=False
            )
            if existing is not None:
                raise RuntimeExecutionConflict("Deterministic barrier drain identity collides")
            drain = CoordinationRuntimeDrain.start(
                drain_id=drain_id,
                tenant_id=aggregate.task.tenant_id,
                task_id=aggregate.task.id,
                triggering_run_id=plan.triggering_run_id,
                target=plan.effective_target,
                reason=plan.effective_reason,
                at=now,
            )
            uow.coordination_runtime_drains.add(drain)
            return drain, {drain.id}

        if active is None:
            if plan.retarget_drain or plan.effective_target is not None or plan.sibling_actions:
                raise RuntimeExecutionConflict("Barrier plan requires an active drain")
            return None, set()
        if (
            active.status is not CoordinationRuntimeDrainStatus.DRAINING
            or active.tenant_id != aggregate.task.tenant_id
            or active.task_id != aggregate.task.id
            or plan.effective_target is None
            or plan.effective_reason is None
        ):
            raise RuntimeExecutionConflict("Locked barrier drain is invalid")
        should_retarget = (
            active.target is not plan.effective_target or active.reason != plan.effective_reason
        )
        if plan.retarget_drain != should_retarget:
            raise RuntimeExecutionConflict("Barrier drain version or target is stale")
        if not should_retarget:
            return active, set()
        updated = active.retarget(
            target=plan.effective_target,
            reason=plan.effective_reason,
            at=now,
        )
        if updated is active or updated.target is not plan.effective_target:
            raise RuntimeExecutionConflict("Barrier drain retarget is not allowed")
        uow.coordination_runtime_drains.save(updated, tenant_id=aggregate.task.tenant_id)
        return updated, {updated.id}

    def _apply_action(
        self,
        uow: Any,
        *,
        aggregate: CoordinatedRuntimeAggregate,
        action: CoordinatedSiblingAction,
        drain: CoordinationRuntimeDrain | None,
        now: datetime,
        cancel_deadline_window: timedelta,
    ) -> tuple[set[UUID], bool, str | None, bool]:
        run = _run_for(aggregate, action.run_id)
        subtask = _subtask_for(aggregate, action.subtask_id)
        if run.subtask_id != subtask.id or subtask.current_run_id != run.id:
            raise RuntimeExecutionConflict("Barrier action binding is stale")
        attempt = aggregate.latest_attempts.get(run.id)
        execution = _execution_for(aggregate, run) if run.runtime_execution_id is not None else None
        _validate_action_projection(action, aggregate, run, subtask, attempt, execution)
        if action.kind in {
            CoordinatedSiblingActionKind.RETAIN_TERMINAL,
            CoordinatedSiblingActionKind.WAIT_CROSSED,
            CoordinatedSiblingActionKind.WAIT_RECONCILIATION,
        }:
            return set(), False, None, False
        if action.kind is CoordinatedSiblingActionKind.REQUEST_CANCEL:
            if drain is None:
                raise RuntimeExecutionConflict("Cancel action requires an active drain")
            assert execution is not None and attempt is not None
            operation_id, cancel_changed, cancel_progress = self._request_cancel(
                uow,
                aggregate=aggregate,
                execution=execution,
                attempt=attempt,
                drain=drain,
                now=now,
                cancel_deadline_window=cancel_deadline_window,
            )
            return cancel_changed, False, operation_id, cancel_progress

        if action.kind is CoordinatedSiblingActionKind.RELEASE_QUEUED:
            _ensure_now(now, _run_latest_timestamp(run), subtask.updated_at)
            run.cancel(at=now)
            subtask.release_never_dispatched_run(run.id, at=now)
            uow.runs.save(run)
            uow.subtasks.save(subtask)
            return {run.id, subtask.id}, False, None, True

        if attempt is None:
            raise RuntimeExecutionConflict("Barrier release action lacks an Attempt")
        _ensure_now(now, _run_latest_timestamp(run), subtask.updated_at, attempt.heartbeat_at)
        task_changed = False
        if action.kind is CoordinatedSiblingActionKind.ABORT_PREPARED:
            if execution is None:
                raise RuntimeExecutionConflict("Prepared abort lacks a Runtime execution")
            aborted = execution.abort_before_dispatch(
                attempt_id=attempt.id,
                fencing_token=attempt.fencing_token,
                now=now,
            )
            uow.runtimes.save_execution(aborted, tenant_id=aggregate.task.tenant_id)
            _outbox_add_if_absent(
                uow.outbox,
                _abort_audit_envelope(
                    tenant_id=aggregate.task.tenant_id,
                    drain_id=drain.id if drain is not None else uuid5(
                        NAMESPACE_URL,
                        f"{_DRAIN_ID_PREFIX}{aggregate.task.tenant_id}:{aggregate.task.id}",
                    ),
                    execution_id=aborted.id,
                    run_id=run.id,
                    attempt_id=attempt.id,
                    at=now,
                )
            )
        task_changed = _release_accounting(uow, aggregate.task, attempt, now=now)
        attempt.cancel(at=now)
        run.cancel(at=now)
        subtask.release_never_dispatched_run(run.id, at=now)
        uow.attempts.save(attempt)
        uow.runs.save(run)
        uow.subtasks.save(subtask)
        changed = {attempt.id, run.id, subtask.id}
        if execution is not None:
            changed.add(execution.id)
        return changed, task_changed, None, True

    @staticmethod
    def _request_cancel(
        uow: Any,
        *,
        aggregate: CoordinatedRuntimeAggregate,
        execution: RuntimeExecution,
        attempt: Any,
        drain: CoordinationRuntimeDrain,
        now: datetime,
        cancel_deadline_window: timedelta,
    ) -> tuple[str, set[UUID], bool]:
        deadline = drain.created_at + cancel_deadline_window
        if deadline <= now:
            raise RuntimeExecutionConflict("Coordinated cancel deadline has expired")
        operation_id = f"runtime-cancel:{execution.id}:v1"
        intent_payload = {
            "tenant_id": aggregate.task.tenant_id,
            "runtime_execution_id": str(execution.id),
            "operation_id": operation_id,
            "operation": RuntimeLifecycleOperation.CANCEL.value,
            "deadline": deadline.astimezone(timezone.utc).isoformat(),
        }
        from agentmesh.runtime_sdk.canonical import canonical_digest, canonical_json_bytes

        try:
            intent_digest = canonical_digest(intent_payload)
            if len(canonical_json_bytes(intent_payload)) > 65_536:
                raise ValueError("intent too large")
        except Exception as exc:
            raise RuntimeExecutionConflict("Runtime cancellation intent is invalid") from exc
        rows = aggregate.lifecycle_operations_by_execution.get(execution.id, ())
        cancel_rows = [
            row for row in rows if row.operation is RuntimeLifecycleOperation.CANCEL
        ]
        stable_rows = [
            row
            for row in cancel_rows
            if row.operation_id == operation_id
            and row.tenant_id == aggregate.task.tenant_id
            and row.runtime_execution_id == execution.id
        ]
        if len(cancel_rows) > 1 or (cancel_rows and len(stable_rows) != 1):
            raise RuntimeExecutionConflict("Runtime cancellation intent is ambiguous")
        lifecycle_created = False
        changed: set[UUID] = set()
        if stable_rows:
            existing = stable_rows[0]
            if (
                existing.intent_digest != intent_digest
                or existing.deadline.astimezone(timezone.utc) != deadline.astimezone(timezone.utc)
            ):
                raise RuntimeExecutionConflict("Runtime cancellation intent conflicts")
        else:
            lifecycle = RuntimeLifecycleIntent(
                id=uuid5(
                    NAMESPACE_URL,
                    f"{_LIFECYCLE_OUTBOX_PREFIX}{aggregate.task.tenant_id}:{operation_id}",
                ),
                tenant_id=aggregate.task.tenant_id,
                runtime_execution_id=execution.id,
                operation_id=operation_id,
                operation=RuntimeLifecycleOperation.CANCEL,
                intent_digest=intent_digest,
                status=RuntimeLifecycleStatus.REQUESTED,
                deadline=deadline.astimezone(timezone.utc),
                receipt_summary=None,
                version=1,
                created_at=now,
                updated_at=now,
                next_attempt_at=now,
            )
            uow.runtimes.add_lifecycle_operation(lifecycle)
            lifecycle_created = True
            changed.add(lifecycle.id)
        execution_changed = False
        if execution.phase is not RuntimeExecutionPhase.CANCEL_REQUESTED:
            updated = execution.apply_observation(
                phase=RuntimeExecutionPhase.CANCEL_REQUESTED,
                provider_sequence=None,
                now=now,
            )
            uow.runtimes.save_execution(updated, tenant_id=aggregate.task.tenant_id)
            execution_changed = True
            changed.add(execution.id)
        outbox_added = _outbox_add_if_absent(
            uow.outbox,
            _lifecycle_outbox_envelope(
                tenant_id=aggregate.task.tenant_id,
                execution_id=execution.id,
                operation_id=operation_id,
                deadline=deadline,
                at=now,
            )
        )
        return operation_id, changed, bool(
            lifecycle_created or execution_changed or outbox_added
        )


def _barrier_timestamp(value: datetime) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise InvalidTaskInput("Coordinated barrier timestamp must be aware UTC")
    normalized = value.astimezone(timezone.utc)
    if normalized.utcoffset() != timedelta(0):
        raise InvalidTaskInput("Coordinated barrier timestamp must be aware UTC")
    return normalized


def _validate_cancel_window(value: timedelta) -> None:
    if type(value) is not timedelta or value <= timedelta(0) or value > _MAX_CANCEL_DEADLINE_WINDOW:
        raise InvalidTaskInput("Coordinated cancel deadline window must be positive and bounded")


def _validate_cancel_deadline_before_writes(
    aggregate: CoordinatedRuntimeAggregate,
    plan: CoordinatedBarrierPlan,
    *,
    now: datetime,
    cancel_deadline_window: timedelta,
) -> None:
    if not any(
        action.kind is CoordinatedSiblingActionKind.REQUEST_CANCEL
        for action in plan.sibling_actions
    ):
        return
    active = aggregate.active_drain
    if active is not None and active.created_at + cancel_deadline_window <= now:
        raise RuntimeExecutionConflict("Coordinated cancel deadline has expired")


def _ensure_now(now: datetime, *rows: datetime) -> None:
    if any(now < value.astimezone(timezone.utc) for value in rows):
        raise RuntimeExecutionConflict("Coordinated barrier clock moved backwards")


def _run_latest_timestamp(run: TaskRun) -> datetime:
    timestamps = [
        value
        for value in (
            run.queued_at,
            run.started_at,
            run.pause_requested_at,
            run.paused_at,
            run.resumed_at,
            run.completed_at,
        )
        if value is not None
    ]
    if not timestamps:
        raise RuntimeExecutionConflict("Coordinated barrier Run has no timestamp")
    return max(timestamps)


def _validate_application_plan(
    aggregate: CoordinatedRuntimeAggregate, plan: CoordinatedBarrierPlan
) -> None:
    if type(aggregate) is not CoordinatedRuntimeAggregate or type(
        plan
    ) is not CoordinatedBarrierPlan:
        raise RuntimeExecutionConflict("Coordinated barrier application requires locked plan")
    task = aggregate.task
    if plan.trigger_disposition is not CoordinatedBarrierTriggerDisposition.KNOWN_TERMINAL:
        raise RuntimeExecutionConflict(
            "Coordinated barrier applier accepts known-terminal plans only"
        )
    if plan.task_id != task.id or plan.tenant_id != task.tenant_id:
        raise RuntimeExecutionConflict("Coordinated barrier plan identity is stale")
    try:
        current_trigger_guard = _trigger_guard_for(
            aggregate,
            plan.triggering_run_id,
            disposition=plan.trigger_disposition,
        )
    except RuntimeExecutionConflict as exc:
        raise RuntimeExecutionConflict("Coordinated barrier trigger guard is stale") from exc
    if plan.trigger_guard != current_trigger_guard:
        raise RuntimeExecutionConflict("Coordinated barrier trigger guard is stale")
    current_source_drain_guard = _drain_guard_for(aggregate.active_drain)
    if plan.source_drain_guard != current_source_drain_guard:
        raise RuntimeExecutionConflict("Coordinated barrier source drain guard is stale")
    active = aggregate.active_drain
    if active is not None and (
        active.status is not CoordinationRuntimeDrainStatus.DRAINING
        or active.task_id != task.id
        or active.tenant_id != task.tenant_id
    ):
        raise RuntimeExecutionConflict("Coordinated barrier drain is stale")
    if plan.create_drain:
        if (
            active is not None
            or plan.retarget_drain
            or plan.effective_target != plan.requested_target
        ):
            raise RuntimeExecutionConflict("Coordinated barrier creation plan is stale")
    elif active is None:
        if plan.retarget_drain or plan.effective_target is not None or plan.sibling_actions:
            raise RuntimeExecutionConflict("Coordinated barrier plan has no drain")
    else:
        expected_retarget = (
            active.target is not plan.effective_target or active.reason != plan.effective_reason
        )
        if plan.retarget_drain != expected_retarget:
            raise RuntimeExecutionConflict("Coordinated barrier drain projection is stale")
    effective = plan.effective_target
    if effective is None:
        expected_actions: tuple[CoordinatedSiblingAction, ...] = ()
    else:
        expected_actions = _sibling_actions(
            aggregate,
            plan.triggering_run_id,
            stopping=effective
            in {
                CoordinationRuntimeDrainTarget.WAITING_APPROVAL,
                CoordinationRuntimeDrainTarget.FAILED,
                CoordinationRuntimeDrainTarget.CANCELED,
            },
        )
    if plan.sibling_actions != expected_actions:
        raise RuntimeExecutionConflict("Coordinated barrier action projection is stale")
    expected_completion = _completion(expected_actions, effective)
    if plan.completion is not expected_completion:
        raise RuntimeExecutionConflict("Coordinated barrier completion is stale")


def _validate_action_projection(
    action: CoordinatedSiblingAction,
    aggregate: CoordinatedRuntimeAggregate,
    run: TaskRun,
    subtask: Subtask,
    attempt: Any | None,
    execution: RuntimeExecution | None,
) -> None:
    boundary = aggregate.boundary_classifications.get(run.id)
    if boundary is None:
        raise RuntimeExecutionConflict("Coordinated barrier action boundary is missing")
    expected_execution = execution.id if execution is not None else None
    expected_attempt = attempt.id if attempt is not None else None
    expected_fence = attempt.fencing_token if attempt is not None else None
    if (
        action.subtask_id != subtask.id
        or action.execution_id != expected_execution
        or action.attempt_id != expected_attempt
        or action.fencing_token != expected_fence
    ):
        raise RuntimeExecutionConflict("Coordinated barrier action ownership is stale")
    if action.kind is CoordinatedSiblingActionKind.RELEASE_QUEUED and boundary is not (
        CoordinationRuntimeBoundary.NOT_CROSSED_QUEUED
    ):
        raise RuntimeExecutionConflict("Queued barrier action boundary is stale")
    if action.kind is CoordinatedSiblingActionKind.ABORT_NO_EXECUTION and boundary is not (
        CoordinationRuntimeBoundary.NOT_CROSSED_NO_EXECUTION
    ):
        raise RuntimeExecutionConflict("No-execution barrier action boundary is stale")
    if action.kind is CoordinatedSiblingActionKind.ABORT_PREPARED and boundary is not (
        CoordinationRuntimeBoundary.NOT_CROSSED_PREPARED
    ):
        raise RuntimeExecutionConflict("Prepared barrier action boundary is stale")
    if action.kind is CoordinatedSiblingActionKind.REQUEST_CANCEL and boundary is not (
        CoordinationRuntimeBoundary.CROSSED_ACTIVE
    ):
        raise RuntimeExecutionConflict("Cancel barrier action boundary is stale")


def _release_accounting(uow: Any, task: Task, attempt: Any, *, now: datetime) -> bool:
    before = task.version
    BudgetController.release_attempt(task, attempt, at=now)
    QuotaController.release_attempt(uow, attempt)
    return task.version != before


def _abort_audit_envelope(
    *,
    tenant_id: str,
    drain_id: UUID,
    execution_id: UUID,
    run_id: UUID,
    attempt_id: UUID,
    at: datetime,
) -> MessageEnvelope:
    message_id = uuid5(NAMESPACE_URL, f"{_ABORT_OUTBOX_PREFIX}{drain_id}:{execution_id}")
    return MessageEnvelope(
        schema_name="agentmesh.runtime.dispatch.aborted",
        schema_version=1,
        message_id=message_id,
        tenant_id=tenant_id,
        occurred_at=at,
        producer="agentmesh-runtime-control-plane-v1",
        correlation_id=execution_id,
        causation_id=None,
        idempotency_key=f"runtime-dispatch-abort:{drain_id}:{execution_id}",
        payload={
            "tenant_id": tenant_id,
            "runtime_execution_id": str(execution_id),
            "run_id": str(run_id),
            "attempt_id": str(attempt_id),
            "reason": "runtime.dispatch_aborted",
        },
    )


def _lifecycle_outbox_envelope(
    *, tenant_id: str, execution_id: UUID, operation_id: str, deadline: datetime, at: datetime
) -> MessageEnvelope:
    message_id = uuid5(NAMESPACE_URL, f"{_LIFECYCLE_OUTBOX_PREFIX}{tenant_id}:{operation_id}")
    return MessageEnvelope(
        schema_name="agentmesh.runtime.lifecycle.requested",
        schema_version=1,
        message_id=message_id,
        tenant_id=tenant_id,
        occurred_at=at,
        producer="agentmesh-runtime-lifecycle-command-v1",
        correlation_id=execution_id,
        causation_id=None,
        idempotency_key=f"runtime-lifecycle:{operation_id}",
        payload={
            "tenant_id": tenant_id,
            "runtime_execution_id": str(execution_id),
            "operation_id": operation_id,
            "operation": RuntimeLifecycleOperation.CANCEL.value,
            "deadline": deadline.astimezone(timezone.utc).isoformat(),
        },
    )


def _outbox_add_if_absent(outbox: Any, envelope: MessageEnvelope) -> bool:
    add_if_absent = getattr(outbox, "add_if_absent", None)
    if callable(add_if_absent):
        return bool(add_if_absent(envelope))
    outbox.add(envelope)
    return True


__all__ = [
    "CoordinatedBarrierCompletion",
    "CoordinatedBarrierDrainGuard",
    "CoordinatedBarrierApplication",
    "CoordinatedRuntimeBarrierApplier",
    "CoordinatedBarrierPlan",
    "CoordinatedBarrierTriggerGuard",
    "CoordinatedBarrierTriggerDisposition",
    "CoordinatedSiblingAction",
    "CoordinatedSiblingActionKind",
    "KnownTerminalPhase",
    "plan_known_terminal",
    "plan_unknown_outcome",
    "plan_reconciled_terminal",
]
