"""Pure planning contracts for Task-scoped coordinated cancellation.

This module deliberately stops at an immutable plan.  It does not open a
transaction, read a clock, acquire a lock, call a provider, or write an Outbox
event.  The control service added by the later c2f6 slices owns those effects.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from agentmesh.application.authority_cohorts import AuthorityCohort
from agentmesh.application.coordinated_runtime import CoordinatedRuntimeAggregate
from agentmesh.domain.coordination import (
    CoordinationRuntimeBoundary,
    CoordinationRuntimeDrain,
    CoordinationRuntimeDrainStatus,
    CoordinationRuntimeDrainTarget,
    Subtask,
    SubtaskStatus,
    normalize_coordination_reason,
)
from agentmesh.domain.errors import InvalidTaskInput, RuntimeExecutionConflict
from agentmesh.domain.runtime_execution import (
    RuntimeExecution,
    RuntimeExecutionPhase,
)
from agentmesh.domain.tasks import (
    AttemptStatus,
    RunRole,
    RunStatus,
    Task,
    TaskAttempt,
    TaskExecutionMode,
    TaskRun,
    TaskStatus,
)

_DRAIN_ID_PREFIX = "coordination-runtime-drain:"
_MAX_REASON_BYTES = 512
_SECRET_MARKERS = (
    "secret",
    "password",
    "bearer ",
    "api_key",
    "access_token",
    "client_secret",
    "private_key",
    "authorization",
)
_KNOWN_TERMINAL_PHASES = {
    RuntimeExecutionPhase.SUCCEEDED,
    RuntimeExecutionPhase.FAILED,
    RuntimeExecutionPhase.CANCELED,
    RuntimeExecutionPhase.TIMED_OUT,
}
_PARKED_PHASES = {
    RuntimeExecutionPhase.LOST,
    RuntimeExecutionPhase.OUTCOME_UNKNOWN,
}
_ACTIVE_PHASES = {
    RuntimeExecutionPhase.DISPATCHING,
    RuntimeExecutionPhase.ACCEPTED,
    RuntimeExecutionPhase.RUNNING,
    RuntimeExecutionPhase.WAITING_INPUT,
    RuntimeExecutionPhase.WAITING_APPROVAL,
    RuntimeExecutionPhase.PAUSE_REQUESTED,
    RuntimeExecutionPhase.PAUSED,
    RuntimeExecutionPhase.CANCEL_REQUESTED,
}
_TERMINAL_RUN_STATUSES = {
    RunStatus.SUCCEEDED,
    RunStatus.FAILED,
    RunStatus.CANCELED,
}
_TERMINAL_SUBTASK_STATUSES = {
    SubtaskStatus.COMPLETED,
    SubtaskStatus.FAILED,
    SubtaskStatus.CANCELED,
}
_ACTIVE_TASK_STATUSES = {
    value
    for value in TaskStatus
    if value not in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELED}
}


class CoordinatedCancelKind(str, Enum):
    """Closed public outcomes of a Task-scoped cancellation command."""

    APPLIED = "APPLIED"
    DRAINING_ACTIVE = "DRAINING_ACTIVE"
    WAIT_RECONCILIATION = "WAIT_RECONCILIATION"
    ALREADY_TERMINAL = "ALREADY_TERMINAL"
    REPLAY = "REPLAY"


class CoordinatedCancelActionKind(str, Enum):
    """Closed provider-boundary action vocabulary for every locked Run.

    ``CANCEL_QUEUED`` and ``CANCEL_NO_EXECUTION`` are distinct wire values.
    Both actions cancel the member Run/Subtask and must never reuse the barrier
    helper that releases a Run back to ``READY``.
    """

    RETAIN_TERMINAL = "RETAIN_TERMINAL"
    CANCEL_QUEUED = "CANCEL_QUEUED"
    CANCEL_NO_EXECUTION = "CANCEL_NO_EXECUTION"
    ABORT_PREPARED = "ABORT_PREPARED"
    REQUEST_CANCEL = "REQUEST_CANCEL"
    WAIT_RECONCILIATION = "WAIT_RECONCILIATION"


class CoordinatedCancelCompletion(str, Enum):
    """How a pure cancellation plan may finish after its actions apply."""

    WAIT_ACTIVE = "WAIT_ACTIVE"
    WAIT_RECONCILIATION = "WAIT_RECONCILIATION"
    APPLY_CANCELED = "APPLY_CANCELED"
    RETAIN_FAILED = "RETAIN_FAILED"


@dataclass(frozen=True)
class CoordinatedCancelDrainGuard:
    """Immutable source drain/version/epoch proof captured by the planner."""

    id: UUID
    version: int
    status: CoordinationRuntimeDrainStatus
    target: CoordinationRuntimeDrainTarget
    reason: str
    triggering_run_id: UUID
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if type(self.id) is not UUID or type(self.triggering_run_id) is not UUID:
            raise RuntimeExecutionConflict("Cancellation drain guard identity is invalid")
        if type(self.version) is not int or self.version <= 0:
            raise RuntimeExecutionConflict("Cancellation drain guard version is invalid")
        if type(self.status) is not CoordinationRuntimeDrainStatus or (
            self.status is not CoordinationRuntimeDrainStatus.DRAINING
        ):
            raise RuntimeExecutionConflict("Cancellation drain guard status is invalid")
        if type(self.target) is not CoordinationRuntimeDrainTarget:
            raise RuntimeExecutionConflict("Cancellation drain guard target is invalid")
        if type(self.reason) is not str:
            raise RuntimeExecutionConflict("Cancellation drain guard reason is invalid")
        try:
            normalized = _normalized_reason(self.reason)
        except Exception as exc:
            raise RuntimeExecutionConflict("Cancellation drain guard reason is invalid") from exc
        if normalized != self.reason:
            raise RuntimeExecutionConflict("Cancellation drain guard reason is not normalized")
        if (
            type(self.created_at) is not datetime
            or self.created_at.tzinfo is None
            or self.created_at.utcoffset() is None
            or self.created_at.utcoffset() != timedelta(0)
            or type(self.updated_at) is not datetime
            or self.updated_at.tzinfo is None
            or self.updated_at.utcoffset() is None
            or self.updated_at.utcoffset() != timedelta(0)
            or self.updated_at < self.created_at
        ):
            raise RuntimeExecutionConflict("Cancellation drain guard epoch is invalid")


def _invalid(message: str) -> InvalidTaskInput:
    return InvalidTaskInput(message)


def _uuid(value: Any, name: str, *, optional: bool = False) -> UUID | None:
    if optional and value is None:
        return None
    if type(value) is not UUID:
        raise _invalid(f"{name} identity is invalid")
    return value


def _tenant(value: Any) -> str:
    if type(value) is not str or not value or value != value.strip() or len(value) > 128:
        raise _invalid("Cancellation tenant is invalid")
    return value


def _normalized_reason(value: Any) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise _invalid("Cancellation reason is invalid")
    if len(value.encode("utf-8")) > _MAX_REASON_BYTES or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise _invalid("Cancellation reason is invalid")
    lowered = value.casefold()
    if any(marker in lowered for marker in _SECRET_MARKERS):
        raise _invalid("Cancellation reason contains secret material")
    return normalize_coordination_reason(value)


def _drain_id(tenant_id: str, task_id: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"{_DRAIN_ID_PREFIX}{tenant_id}:{task_id}")


@dataclass(frozen=True)
class CoordinatedCancelAction:
    """Immutable identity and boundary proof for one Run action."""

    run_id: UUID
    subtask_id: UUID | None
    execution_id: UUID | None
    attempt_id: UUID | None
    fencing_token: int | None
    boundary: CoordinationRuntimeBoundary
    kind: CoordinatedCancelActionKind

    def __post_init__(self) -> None:
        _uuid(self.run_id, "Cancel action Run")
        _uuid(self.subtask_id, "Cancel action Subtask", optional=True)
        _uuid(self.execution_id, "Cancel action Runtime execution", optional=True)
        _uuid(self.attempt_id, "Cancel action Attempt", optional=True)
        if type(self.fencing_token) not in (int, type(None)) or (
            self.fencing_token is not None and self.fencing_token <= 0
        ):
            raise _invalid("Cancel action fencing token is invalid")
        if type(self.boundary) is not CoordinationRuntimeBoundary:
            raise _invalid("Cancel action boundary is invalid")
        if type(self.kind) is not CoordinatedCancelActionKind:
            raise _invalid("Cancel action kind is invalid")
        expected = {
            CoordinationRuntimeBoundary.KNOWN_TERMINAL: (
                CoordinatedCancelActionKind.RETAIN_TERMINAL,
            ),
            CoordinationRuntimeBoundary.NOT_CROSSED_QUEUED: (
                CoordinatedCancelActionKind.CANCEL_QUEUED,
            ),
            CoordinationRuntimeBoundary.NOT_CROSSED_NO_EXECUTION: (
                CoordinatedCancelActionKind.CANCEL_NO_EXECUTION,
            ),
            CoordinationRuntimeBoundary.NOT_CROSSED_PREPARED: (
                CoordinatedCancelActionKind.ABORT_PREPARED,
            ),
            CoordinationRuntimeBoundary.CROSSED_ACTIVE: (
                CoordinatedCancelActionKind.REQUEST_CANCEL,
            ),
            CoordinationRuntimeBoundary.RECONCILIATION_EVIDENCE: (
                CoordinatedCancelActionKind.WAIT_RECONCILIATION,
            ),
        }[self.boundary]
        if self.kind not in expected:
            raise _invalid("Cancel action kind does not match boundary")
        if self.kind is CoordinatedCancelActionKind.CANCEL_QUEUED:
            if any(
                value is not None
                for value in (
                    self.execution_id,
                    self.attempt_id,
                    self.fencing_token,
                )
            ):
                raise _invalid("Queued cancellation action carries ownership")
        elif self.kind is CoordinatedCancelActionKind.CANCEL_NO_EXECUTION:
            if (
                self.attempt_id is None
                or self.fencing_token is None
                or self.execution_id is not None
            ):
                raise _invalid("No-execution cancellation action ownership is invalid")
        elif self.kind is CoordinatedCancelActionKind.RETAIN_TERMINAL:
            if (
                self.execution_id is None
                and self.attempt_id is None
                and self.fencing_token is not None
            ):
                raise _invalid("Terminal cancellation action fence is orphaned")
        else:
            if self.execution_id is None or self.attempt_id is None or self.fencing_token is None:
                raise _invalid("Cancellation action ownership is incomplete")


@dataclass(frozen=True)
class CoordinatedCancelPlan:
    """Pure, deterministic cancellation plan for one locked aggregate."""

    tenant_id: str
    task_id: UUID
    drain_id: UUID
    audit_anchor_run_id: UUID
    requested_target: CoordinationRuntimeDrainTarget
    requested_reason: str
    effective_target: CoordinationRuntimeDrainTarget
    effective_reason: str
    create_drain: bool
    retarget_drain: bool
    actions: tuple[CoordinatedCancelAction, ...]
    completion: CoordinatedCancelCompletion
    source_drain_guard: CoordinatedCancelDrainGuard | None = None

    @property
    def sibling_actions(self) -> tuple[CoordinatedCancelAction, ...]:
        """Compatibility name for appliers consuming ordered member actions."""

        return self.actions

    def __post_init__(self) -> None:
        _tenant(self.tenant_id)
        _uuid(self.task_id, "Cancel plan Task")
        _uuid(self.drain_id, "Cancel plan drain")
        _uuid(self.audit_anchor_run_id, "Cancel plan audit anchor Run")
        if type(self.requested_target) is not CoordinationRuntimeDrainTarget:
            raise _invalid("Cancel plan requested target is invalid")
        if type(self.effective_target) is not CoordinationRuntimeDrainTarget:
            raise _invalid("Cancel plan effective target is invalid")
        _normalized_reason(self.requested_reason)
        _normalized_reason(self.effective_reason)
        if type(self.create_drain) is not bool or type(self.retarget_drain) is not bool:
            raise _invalid("Cancel plan drain flags are invalid")
        if self.requested_target is not CoordinationRuntimeDrainTarget.CANCELED:
            raise _invalid("Cancel plan requested target must be CANCELED")
        if self.create_drain and self.retarget_drain:
            raise _invalid("Cancel plan cannot create and retarget a drain")
        if self.create_drain and self.effective_target is not self.requested_target:
            raise _invalid("New cancel drain has an invalid effective target")
        if type(self.completion) is not CoordinatedCancelCompletion:
            raise _invalid("Cancel plan completion is invalid")
        if (
            self.source_drain_guard is not None
            and type(self.source_drain_guard) is not CoordinatedCancelDrainGuard
        ):
            raise _invalid("Cancel plan drain guard is invalid")
        if self.source_drain_guard is not None and (
            self.source_drain_guard.id != self.drain_id
            or self.source_drain_guard.status is not CoordinationRuntimeDrainStatus.DRAINING
        ):
            raise _invalid("Cancel plan drain guard is stale")
        if self.create_drain and self.source_drain_guard is not None:
            raise _invalid("New cancel drain cannot carry a source guard")
        if self.retarget_drain and self.source_drain_guard is None:
            raise _invalid("Retargeted cancel drain requires a source guard")
        if not self.create_drain and self.source_drain_guard is None:
            raise _invalid("Existing cancel drain requires a source guard")
        actions = tuple(self.actions)
        if actions != self.actions or any(
            type(value) is not CoordinatedCancelAction for value in actions
        ):
            raise _invalid("Cancel plan actions are invalid")
        if tuple(sorted(actions, key=lambda value: value.run_id)) != actions:
            raise _invalid("Cancel plan actions are not UUID ordered")
        if len({value.run_id for value in actions}) != len(actions):
            raise _invalid("Cancel plan actions are duplicated")
        if not actions:
            raise _invalid("Cancel plan must cover at least one Run")
        if self.source_drain_guard is None:
            if self.drain_id != _drain_id(self.tenant_id, self.task_id):
                raise _invalid("Cancel plan drain identity is not deterministic")
        elif self.source_drain_guard.id != self.drain_id:
            raise _invalid("Cancel plan drain identity is stale")
        action_by_run = {value.run_id: value for value in actions}
        anchor = action_by_run.get(self.audit_anchor_run_id)
        if anchor is None or anchor.kind is CoordinatedCancelActionKind.RETAIN_TERMINAL:
            raise _invalid("Cancel plan audit anchor is not cancellable")
        if (
            any(value.kind is CoordinatedCancelActionKind.REQUEST_CANCEL for value in actions)
            and self.completion is not CoordinatedCancelCompletion.WAIT_ACTIVE
        ):
            raise _invalid("Open cancellation requires active wait")
        if (
            not any(value.kind is CoordinatedCancelActionKind.REQUEST_CANCEL for value in actions)
            and any(
                value.kind is CoordinatedCancelActionKind.WAIT_RECONCILIATION for value in actions
            )
            and self.completion is not CoordinatedCancelCompletion.WAIT_RECONCILIATION
        ):
            raise _invalid("Reconciliation cancellation requires reconciliation wait")
        if not any(
            value.kind
            in {
                CoordinatedCancelActionKind.REQUEST_CANCEL,
                CoordinatedCancelActionKind.WAIT_RECONCILIATION,
            }
            for value in actions
        ) and self.completion not in {
            CoordinatedCancelCompletion.APPLY_CANCELED,
            CoordinatedCancelCompletion.RETAIN_FAILED,
        }:
            raise _invalid("Local cancellation actions require a terminal completion")
        if (
            self.effective_target is not CoordinationRuntimeDrainTarget.FAILED
            and self.completion is CoordinatedCancelCompletion.RETAIN_FAILED
        ):
            raise _invalid("Only FAILED drain may retain a failed target")


@dataclass(frozen=True)
class CoordinatedCancelResult:
    """Safe public cancellation result; it carries no provider payload."""

    kind: CoordinatedCancelKind
    tenant_id: str
    task_id: UUID
    task_status: TaskStatus | None = None
    effective_target: CoordinationRuntimeDrainTarget | None = None
    drain_id: UUID | None = None
    audit_event_id: UUID | None = None
    audit_anchor_run_id: UUID | None = None
    reason: str | None = None
    lifecycle_operation_ids: tuple[UUID, ...] = ()

    @property
    def effective_reason(self) -> str | None:
        """Compatibility alias for the effective bounded control reason."""

        return self.reason

    def __post_init__(self) -> None:
        if type(self.kind) is not CoordinatedCancelKind:
            raise _invalid("Cancel result kind is invalid")
        _tenant(self.tenant_id)
        _uuid(self.task_id, "Cancel result Task")
        if self.task_status is not None and type(self.task_status) is not TaskStatus:
            raise _invalid("Cancel result Task status is invalid")
        if (
            self.effective_target is not None
            and type(self.effective_target) is not CoordinationRuntimeDrainTarget
        ):
            raise _invalid("Cancel result effective target is invalid")
        if self.effective_target not in {
            None,
            CoordinationRuntimeDrainTarget.CANCELED,
            CoordinationRuntimeDrainTarget.FAILED,
        }:
            raise _invalid("Cancel result effective target is not a stopping target")
        if self.reason is not None:
            _normalized_reason(self.reason)
        _uuid(self.drain_id, "Cancel result drain", optional=True)
        _uuid(self.audit_anchor_run_id, "Cancel result audit anchor", optional=True)
        _uuid(self.audit_event_id, "Cancel result audit event", optional=True)
        lifecycle_ids = tuple(self.lifecycle_operation_ids)
        if lifecycle_ids != self.lifecycle_operation_ids or any(
            type(value) is not UUID for value in lifecycle_ids
        ):
            raise _invalid("Cancel result lifecycle identities are invalid")
        if tuple(sorted(lifecycle_ids, key=str)) != lifecycle_ids or len(set(lifecycle_ids)) != len(
            lifecycle_ids
        ):
            raise _invalid("Cancel result lifecycle identities are not canonical")
        if (self.effective_target is None) != (self.reason is None):
            raise _invalid("Cancel result effective target/reason must be paired")
        if self.kind is CoordinatedCancelKind.ALREADY_TERMINAL:
            if (
                self.task_status
                not in {
                    TaskStatus.COMPLETED,
                    TaskStatus.FAILED,
                    TaskStatus.CANCELED,
                }
                or any(
                    value is not None
                    for value in (
                        self.effective_target,
                        self.drain_id,
                        self.audit_anchor_run_id,
                        self.audit_event_id,
                    )
                )
                or lifecycle_ids
            ):
                raise _invalid("Terminal cancel result projection is invalid")
        else:
            if (
                self.task_status is None
                or self.drain_id is None
                or self.effective_target is None
                or self.reason is None
                or self.audit_event_id is None
                or self.audit_anchor_run_id is None
            ):
                raise _invalid("Cancel result projection is incomplete")


def plan_cancel_request(
    aggregate: CoordinatedRuntimeAggregate,
    normalized_reason: str,
) -> CoordinatedCancelPlan:
    """Build a deterministic cancellation plan from an already locked aggregate.

    Every Run is represented, including a Supervisor without a Subtask.  The
    smallest cancellable Run is only an audit anchor; it is never treated as a
    synthetic triggering Run and does not alter the sibling action set.
    """

    _validate_aggregate(aggregate)
    reason = _normalized_reason(normalized_reason)
    task = aggregate.task
    runs = tuple(sorted(aggregate.runs, key=lambda value: value.id))
    actions: list[CoordinatedCancelAction] = []
    for run in runs:
        boundary = aggregate.boundary_classifications.get(run.id)
        if boundary is None:
            _validate_terminal_projection(aggregate, run)
            boundary = CoordinationRuntimeBoundary.KNOWN_TERMINAL
        action = _action_for_run(aggregate, run, boundary)
        actions.append(action)

    ordered_actions = tuple(actions)
    anchor_candidates = tuple(
        action
        for action in ordered_actions
        if action.kind is not CoordinatedCancelActionKind.RETAIN_TERMINAL
    )
    if not anchor_candidates:
        raise RuntimeExecutionConflict("Cancellation aggregate is already terminal")
    anchor = anchor_candidates[0].run_id
    active_drain = aggregate.active_drain
    source_drain_guard = _drain_guard_for(active_drain)
    if active_drain is None:
        effective_target = CoordinationRuntimeDrainTarget.CANCELED
        effective_reason = reason
        create_drain = True
        retarget_drain = False
    else:
        if active_drain.status is not CoordinationRuntimeDrainStatus.DRAINING:
            raise RuntimeExecutionConflict("Cancellation drain is not active")
        if active_drain.target in {
            CoordinationRuntimeDrainTarget.RUNNING,
            CoordinationRuntimeDrainTarget.WAITING_APPROVAL,
        }:
            effective_target = CoordinationRuntimeDrainTarget.CANCELED
            effective_reason = reason
            retarget_drain = True
        elif active_drain.target is CoordinationRuntimeDrainTarget.CANCELED:
            effective_target = active_drain.target
            effective_reason = active_drain.reason
            retarget_drain = False
        elif active_drain.target is CoordinationRuntimeDrainTarget.FAILED:
            effective_target = active_drain.target
            effective_reason = active_drain.reason
            retarget_drain = False
        else:  # pragma: no cover - closed enum defense
            raise RuntimeExecutionConflict("Cancellation drain target is invalid")
        create_drain = False

    if any(action.kind is CoordinatedCancelActionKind.REQUEST_CANCEL for action in ordered_actions):
        completion = CoordinatedCancelCompletion.WAIT_ACTIVE
    elif any(
        action.kind is CoordinatedCancelActionKind.WAIT_RECONCILIATION for action in ordered_actions
    ):
        completion = CoordinatedCancelCompletion.WAIT_RECONCILIATION
    elif effective_target is CoordinationRuntimeDrainTarget.FAILED:
        completion = CoordinatedCancelCompletion.RETAIN_FAILED
    else:
        completion = CoordinatedCancelCompletion.APPLY_CANCELED

    return CoordinatedCancelPlan(
        tenant_id=task.tenant_id,
        task_id=task.id,
        drain_id=(
            active_drain.id if active_drain is not None else _drain_id(task.tenant_id, task.id)
        ),
        audit_anchor_run_id=anchor,
        requested_target=CoordinationRuntimeDrainTarget.CANCELED,
        requested_reason=reason,
        effective_target=effective_target,
        effective_reason=effective_reason,
        create_drain=create_drain,
        retarget_drain=retarget_drain,
        actions=ordered_actions,
        completion=completion,
        source_drain_guard=source_drain_guard,
    )


def _validate_aggregate(aggregate: CoordinatedRuntimeAggregate) -> None:
    if type(aggregate) is not CoordinatedRuntimeAggregate:
        raise RuntimeExecutionConflict("Cancellation requires a locked aggregate")
    task = aggregate.task
    cohort = aggregate.cohort
    if type(task) is not Task or type(cohort) is not AuthorityCohort:
        raise RuntimeExecutionConflict("Cancellation aggregate projection is invalid")
    if (
        task.execution_mode is not TaskExecutionMode.COORDINATED
        or task.status not in _ACTIVE_TASK_STATUSES
        or cohort.task_id != task.id
        or cohort.tenant_id != task.tenant_id
        or cohort.runtime_authority != "managed"
        or cohort.comparison_mode != "off"
        or type(cohort.runtime_version_id) is not UUID
    ):
        raise RuntimeExecutionConflict("Cancellation cohort is invalid")
    if aggregate.active_drain is not None:
        drain = aggregate.active_drain
        if (
            type(drain) is not CoordinationRuntimeDrain
            or drain.tenant_id != task.tenant_id
            or drain.task_id != task.id
            or drain.status is not CoordinationRuntimeDrainStatus.DRAINING
            or drain.id != _drain_id(task.tenant_id, task.id)
        ):
            raise RuntimeExecutionConflict("Cancellation active drain projection is invalid")
    runs = tuple(aggregate.runs)
    subtasks = tuple(aggregate.subtasks)
    if runs != aggregate.runs or subtasks != aggregate.subtasks:
        raise RuntimeExecutionConflict("Cancellation aggregate collections are invalid")
    if any(type(run) is not TaskRun for run in runs) or any(
        type(subtask) is not Subtask for subtask in subtasks
    ):
        raise RuntimeExecutionConflict("Cancellation aggregate entities are invalid")
    if len({run.id for run in runs}) != len(runs) or len({value.id for value in subtasks}) != len(
        subtasks
    ):
        raise RuntimeExecutionConflict("Cancellation aggregate identities are duplicated")
    if tuple(sorted(runs, key=lambda value: value.id)) != runs:
        raise RuntimeExecutionConflict("Cancellation Runs are not UUID ordered")
    if tuple(sorted(subtasks, key=lambda value: value.id)) != subtasks:
        raise RuntimeExecutionConflict("Cancellation Subtasks are not UUID ordered")
    subtask_ids = {value.id for value in subtasks}
    run_ids = {value.id for value in runs}
    if task.current_run_id is not None and (
        type(task.current_run_id) is not UUID or task.current_run_id not in run_ids
    ):
        raise RuntimeExecutionConflict("Cancellation Task current Run pointer is invalid")
    active_supervisors = tuple(
        run
        for run in runs
        if run.role is RunRole.SUPERVISOR and run.status not in _TERMINAL_RUN_STATUSES
    )
    if len(active_supervisors) > 1:
        raise RuntimeExecutionConflict("Cancellation has multiple active Supervisor Runs")
    if active_supervisors:
        if task.current_run_id != active_supervisors[0].id:
            raise RuntimeExecutionConflict("Cancellation active Supervisor lacks current pointer")
    elif task.current_run_id is not None:
        current = next(run for run in runs if run.id == task.current_run_id)
        if current.role is not RunRole.SUPERVISOR:
            raise RuntimeExecutionConflict("Cancellation Task pointer must target Supervisor")
        raise RuntimeExecutionConflict("Cancellation Task pointer targets a terminal Supervisor")
    if set(aggregate.latest_attempts) != run_ids:
        raise RuntimeExecutionConflict("Cancellation Attempt projection is incomplete")
    if any(
        type(run.id) is not UUID
        or run.task_id != task.id
        or run.runtime_authority != "managed"
        or run.comparison_mode != "off"
        or run.runtime_version_id != cohort.runtime_version_id
        or run.role not in {RunRole.EXECUTOR, RunRole.SUPERVISOR}
        or (
            run.role is RunRole.EXECUTOR
            and (type(run.subtask_id) is not UUID or run.subtask_id not in subtask_ids)
        )
        or (run.role is RunRole.SUPERVISOR and run.subtask_id is not None)
        for run in runs
    ):
        raise RuntimeExecutionConflict("Cancellation Run cohort projection is invalid")
    subtasks_by_id = {subtask.id: subtask for subtask in subtasks}
    for run in runs:
        if (
            run.role is RunRole.EXECUTOR
            and run.status not in _TERMINAL_RUN_STATUSES
            and (subtasks_by_id[run.subtask_id].current_run_id != run.id)
        ):
            raise RuntimeExecutionConflict("Cancellation Subtask current Run binding is stale")
    if task.current_run_id is not None:
        current = next(run for run in runs if run.id == task.current_run_id)
        if current.status in _TERMINAL_RUN_STATUSES:
            raise RuntimeExecutionConflict("Cancellation Task current Run is terminal")
    if (
        aggregate.active_drain is not None
        and aggregate.active_drain.triggering_run_id not in run_ids
    ):
        raise RuntimeExecutionConflict("Cancellation drain trigger is outside the aggregate")
    for run_id, attempt in aggregate.latest_attempts.items():
        if type(run_id) is not UUID or (
            attempt is not None and (type(attempt) is not TaskAttempt or attempt.run_id != run_id)
        ):
            raise RuntimeExecutionConflict("Cancellation Attempt ownership is invalid")
    if any(type(value) is not UUID for value in aggregate.boundary_classifications):
        raise RuntimeExecutionConflict("Cancellation boundary identity is invalid")
    if not set(aggregate.boundary_classifications) <= run_ids:
        raise RuntimeExecutionConflict("Cancellation boundary has an unknown Run")
    if any(
        type(value) is not CoordinationRuntimeBoundary
        for value in aggregate.boundary_classifications.values()
    ):
        raise RuntimeExecutionConflict("Cancellation boundary value is invalid")
    executions_by_run = aggregate.executions_by_run
    for run_id, executions in executions_by_run.items():
        if run_id not in run_ids or any(
            type(value) is not RuntimeExecution for value in executions
        ):
            raise RuntimeExecutionConflict("Cancellation Runtime execution projection is invalid")
        if len({value.id for value in executions}) != len(executions):
            raise RuntimeExecutionConflict(
                "Cancellation Runtime execution identities are duplicated"
            )
        for execution in executions:
            if (
                execution.tenant_id != task.tenant_id
                or execution.run_id != run_id
                or execution.runtime_version_id != cohort.runtime_version_id
            ):
                raise RuntimeExecutionConflict("Cancellation Runtime execution binding is invalid")


def _action_for_run(
    aggregate: CoordinatedRuntimeAggregate,
    run: TaskRun,
    boundary: CoordinationRuntimeBoundary,
) -> CoordinatedCancelAction:
    if type(boundary) is not CoordinationRuntimeBoundary:
        raise RuntimeExecutionConflict("Cancellation boundary is invalid")
    attempt = aggregate.latest_attempts.get(run.id)
    executions = aggregate.executions_by_run.get(run.id, ())
    execution = None
    if run.runtime_execution_id is not None:
        bound = [value for value in executions if value.id == run.runtime_execution_id]
        if len(bound) != 1:
            raise RuntimeExecutionConflict("Cancellation Runtime execution binding is ambiguous")
        execution = bound[0]
    elif executions:
        raise RuntimeExecutionConflict("Unbound cancellation Run carries Runtime execution")
    if (
        run.runtime_execution_id is not None
        and run.runtime_execution_intent_id != run.runtime_execution_id
    ):
        raise RuntimeExecutionConflict("Cancellation Runtime intent binding is invalid")
    if execution is not None:
        if attempt is None or (
            execution.current_owner_attempt_id != attempt.id
            or execution.current_fencing_token != attempt.fencing_token
        ):
            raise RuntimeExecutionConflict("Cancellation Runtime owner/fence is stale")
    subtask_id = run.subtask_id
    subtask = (
        next(value for value in aggregate.subtasks if value.id == subtask_id)
        if subtask_id is not None
        else None
    )
    action_kind = {
        CoordinationRuntimeBoundary.KNOWN_TERMINAL: (CoordinatedCancelActionKind.RETAIN_TERMINAL),
        CoordinationRuntimeBoundary.NOT_CROSSED_QUEUED: (CoordinatedCancelActionKind.CANCEL_QUEUED),
        CoordinationRuntimeBoundary.NOT_CROSSED_NO_EXECUTION: (
            CoordinatedCancelActionKind.CANCEL_NO_EXECUTION
        ),
        CoordinationRuntimeBoundary.NOT_CROSSED_PREPARED: (
            CoordinatedCancelActionKind.ABORT_PREPARED
        ),
        CoordinationRuntimeBoundary.CROSSED_ACTIVE: (CoordinatedCancelActionKind.REQUEST_CANCEL),
        CoordinationRuntimeBoundary.RECONCILIATION_EVIDENCE: (
            CoordinatedCancelActionKind.WAIT_RECONCILIATION
        ),
    }[boundary]
    _validate_boundary_projection(
        run,
        attempt,
        execution,
        boundary,
        subtask,
        validate_subtask=boundary is not CoordinationRuntimeBoundary.KNOWN_TERMINAL
        or subtask is None
        or subtask.current_run_id == run.id,
    )
    return CoordinatedCancelAction(
        run_id=run.id,
        subtask_id=subtask_id,
        execution_id=execution.id if execution is not None else None,
        attempt_id=attempt.id if attempt is not None else None,
        fencing_token=attempt.fencing_token if attempt is not None else None,
        boundary=boundary,
        kind=action_kind,
    )


def _validate_boundary_projection(
    run: TaskRun,
    attempt: TaskAttempt | None,
    execution: RuntimeExecution | None,
    boundary: CoordinationRuntimeBoundary,
    subtask: Subtask | None,
    *,
    validate_subtask: bool = True,
) -> None:
    if run.role is RunRole.EXECUTOR and type(subtask) is not Subtask:
        raise RuntimeExecutionConflict("Cancellation Executor Subtask is missing")
    if boundary is CoordinationRuntimeBoundary.NOT_CROSSED_QUEUED:
        if (
            run.status is not RunStatus.QUEUED
            or attempt is not None
            or execution is not None
            or (
                validate_subtask
                and subtask is not None
                and subtask.status is not SubtaskStatus.READY
            )
        ):
            raise RuntimeExecutionConflict("Queued cancellation projection is invalid")
        return
    if boundary is CoordinationRuntimeBoundary.NOT_CROSSED_NO_EXECUTION:
        if (
            run.status is not RunStatus.RUNNING
            or type(attempt) is not TaskAttempt
            or attempt.status is not AttemptStatus.RUNNING
            or execution is not None
            or (
                validate_subtask
                and subtask is not None
                and subtask.status is not SubtaskStatus.RUNNING
            )
        ):
            raise RuntimeExecutionConflict("No-execution cancellation projection is invalid")
        return
    if boundary is CoordinationRuntimeBoundary.NOT_CROSSED_PREPARED:
        if (
            run.status is not RunStatus.RUNNING
            or type(attempt) is not TaskAttempt
            or attempt.status is not AttemptStatus.RUNNING
            or execution is None
            or execution.phase is not RuntimeExecutionPhase.PREPARED
            or (
                validate_subtask
                and subtask is not None
                and subtask.status is not SubtaskStatus.RUNNING
            )
        ):
            raise RuntimeExecutionConflict("Prepared cancellation projection is invalid")
        return
    if boundary is CoordinationRuntimeBoundary.CROSSED_ACTIVE:
        if (
            run.status is not RunStatus.RUNNING
            or type(attempt) is not TaskAttempt
            or attempt.status is not AttemptStatus.RUNNING
            or execution is None
            or execution.phase not in _ACTIVE_PHASES
            or (
                validate_subtask
                and subtask is not None
                and subtask.status is not SubtaskStatus.RUNNING
            )
        ):
            raise RuntimeExecutionConflict("Active cancellation projection is invalid")
        return
    if boundary is CoordinationRuntimeBoundary.RECONCILIATION_EVIDENCE:
        if (
            type(attempt) is not TaskAttempt
            or attempt.status is not AttemptStatus.OUTCOME_UNKNOWN
            or execution is None
            or execution.phase not in _PARKED_PHASES
            or run.status is not RunStatus.RECONCILIATION_REQUIRED
            or (
                validate_subtask
                and subtask is not None
                and subtask.status is not SubtaskStatus.RECONCILIATION_REQUIRED
            )
        ):
            raise RuntimeExecutionConflict("Reconciliation cancellation projection is invalid")
        return
    if boundary is CoordinationRuntimeBoundary.KNOWN_TERMINAL:
        _validate_known_terminal_conclusion(
            run,
            attempt,
            execution,
            subtask if validate_subtask else None,
        )
        return
    raise RuntimeExecutionConflict("Unknown cancellation boundary")


def _validate_terminal_projection(
    aggregate: CoordinatedRuntimeAggregate,
    run: TaskRun,
) -> None:
    """Validate a historical/provider-free terminal Run omitted by the map."""
    if run.status not in _TERMINAL_RUN_STATUSES:
        raise RuntimeExecutionConflict("Cancellation Run has no boundary classification")
    attempt = aggregate.latest_attempts.get(run.id)
    executions = aggregate.executions_by_run.get(run.id, ())
    if len(executions) > 1:
        raise RuntimeExecutionConflict("Terminal cancellation Run has ambiguous executions")
    execution = executions[0] if executions else None
    if execution is not None:
        if execution.phase not in _KNOWN_TERMINAL_PHASES or attempt is None:
            raise RuntimeExecutionConflict("Terminal cancellation ownership is incomplete")
        if (
            execution.current_owner_attempt_id != attempt.id
            or execution.current_fencing_token != attempt.fencing_token
        ):
            raise RuntimeExecutionConflict("Terminal cancellation owner/fence is stale")
    if run.subtask_id is not None:
        subtask = next(value for value in aggregate.subtasks if value.id == run.subtask_id)
        if subtask.current_run_id == run.id and subtask.status not in _TERMINAL_SUBTASK_STATUSES:
            raise RuntimeExecutionConflict("Terminal cancellation Subtask projection is invalid")
        _validate_boundary_projection(
            run,
            attempt,
            execution,
            CoordinationRuntimeBoundary.KNOWN_TERMINAL,
            subtask,
            validate_subtask=subtask.current_run_id == run.id,
        )
    else:
        _validate_boundary_projection(
            run,
            attempt,
            execution,
            CoordinationRuntimeBoundary.KNOWN_TERMINAL,
            None,
        )


def _validate_known_terminal_conclusion(
    run: TaskRun,
    attempt: TaskAttempt | None,
    execution: RuntimeExecution | None,
    subtask: Subtask | None,
) -> None:
    if run.status not in _TERMINAL_RUN_STATUSES:
        raise RuntimeExecutionConflict("Known-terminal Run projection is invalid")
    if attempt is not None and attempt.status not in {
        AttemptStatus.SUCCEEDED,
        AttemptStatus.FAILED,
        AttemptStatus.CANCELED,
    }:
        raise RuntimeExecutionConflict("Known-terminal Attempt projection is invalid")
    expected = {
        RunStatus.SUCCEEDED: (
            RuntimeExecutionPhase.SUCCEEDED,
            AttemptStatus.SUCCEEDED,
            SubtaskStatus.COMPLETED,
        ),
        RunStatus.FAILED: (
            frozenset({RuntimeExecutionPhase.FAILED, RuntimeExecutionPhase.TIMED_OUT}),
            AttemptStatus.FAILED,
            SubtaskStatus.FAILED,
        ),
        RunStatus.CANCELED: (
            RuntimeExecutionPhase.CANCELED,
            AttemptStatus.CANCELED,
            SubtaskStatus.CANCELED,
        ),
    }[run.status]
    expected_phase, expected_attempt, expected_subtask = expected
    allowed_phases = (
        {expected_phase} if type(expected_phase) is RuntimeExecutionPhase else expected_phase
    )
    if execution is not None and execution.phase not in allowed_phases:
        raise RuntimeExecutionConflict("Known-terminal Runtime conclusion is invalid")
    if attempt is not None and attempt.status is not expected_attempt:
        raise RuntimeExecutionConflict("Known-terminal Attempt conclusion is invalid")
    if subtask is not None:
        if subtask.status not in _TERMINAL_SUBTASK_STATUSES:
            raise RuntimeExecutionConflict("Known-terminal Subtask projection is invalid")
        if subtask.status is not expected_subtask:
            raise RuntimeExecutionConflict("Known-terminal Subtask conclusion is invalid")


def _drain_guard_for(
    drain: CoordinationRuntimeDrain | None,
) -> CoordinatedCancelDrainGuard | None:
    if drain is None:
        return None
    if type(drain) is not CoordinationRuntimeDrain:
        raise RuntimeExecutionConflict("Cancellation source drain is invalid")
    return CoordinatedCancelDrainGuard(
        id=drain.id,
        version=drain.version,
        status=drain.status,
        target=drain.target,
        reason=drain.reason,
        triggering_run_id=drain.triggering_run_id,
        created_at=drain.created_at,
        updated_at=drain.updated_at,
    )


__all__ = [
    "CoordinatedCancelAction",
    "CoordinatedCancelActionKind",
    "CoordinatedCancelCompletion",
    "CoordinatedCancelDrainGuard",
    "CoordinatedCancelKind",
    "CoordinatedCancelPlan",
    "CoordinatedCancelResult",
    "plan_cancel_request",
]
