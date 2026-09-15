"""Transaction-local application of one Task-wide coordinated cancel plan."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from agentmesh.application.coordinated_runtime import CoordinatedRuntimeAggregate
from agentmesh.application.coordinated_runtime_control import (
    CoordinatedCancelAction,
    CoordinatedCancelActionKind,
    CoordinatedCancelCompletion,
    CoordinatedCancelPlan,
    plan_cancel_request,
)
from agentmesh.application.coordinated_runtime_stop_primitives import (
    abort_prepared_in_uow,
    persisted_cancel_epoch,
    release_attempt_accounting,
    request_cancel_in_uow,
    select_cancel_deadline,
)
from agentmesh.domain.coordination import (
    CoordinationRuntimeDrain,
    CoordinationRuntimeDrainStatus,
)
from agentmesh.domain.errors import InvalidTaskInput, RuntimeExecutionConflict
from agentmesh.domain.runtime_execution import RuntimeExecution
from agentmesh.domain.tasks import RunRole, TaskRun, TaskStatus

_MAX_CANCEL_DEADLINE_WINDOW = timedelta(days=7)


@dataclass(frozen=True)
class CoordinatedCancelApplication:
    """Closed result of applying one cancel plan inside the caller's UoW."""

    effective_drain: CoordinationRuntimeDrain
    task_status: TaskStatus
    changed_ids: tuple[UUID, ...]
    lifecycle_operation_ids: tuple[UUID, ...]
    completion: CoordinatedCancelCompletion
    made_progress: bool

    def __post_init__(self) -> None:
        if type(self.effective_drain) is not CoordinationRuntimeDrain:
            raise RuntimeExecutionConflict("Cancellation application drain is invalid")
        if type(self.task_status) is not TaskStatus:
            raise RuntimeExecutionConflict("Cancellation application Task status is invalid")
        if type(self.completion) is not CoordinatedCancelCompletion:
            raise RuntimeExecutionConflict("Cancellation application completion is invalid")
        for values, label in (
            (self.changed_ids, "changed"),
            (self.lifecycle_operation_ids, "lifecycle"),
        ):
            if (
                tuple(values) != values
                or any(type(value) is not UUID for value in values)
                or tuple(sorted(values, key=str)) != values
                or len(set(values)) != len(values)
            ):
                raise RuntimeExecutionConflict(
                    f"Cancellation application {label} identities are invalid"
                )
        if type(self.made_progress) is not bool or (self.changed_ids and not self.made_progress):
            raise RuntimeExecutionConflict("Cancellation application progress is invalid")


class CoordinatedRuntimeCancelApplier:
    """Apply a validated plan without opening, locking, or committing a UoW."""

    def apply_in_uow(
        self,
        uow: Any,
        *,
        aggregate: CoordinatedRuntimeAggregate,
        plan: CoordinatedCancelPlan,
        now: datetime,
        cancel_deadline_window: timedelta,
    ) -> CoordinatedCancelApplication:
        timestamp = _timestamp(now)
        _cancel_window(cancel_deadline_window)
        _validate_plan(aggregate, plan)
        _validate_clock(aggregate, timestamp)
        deadline_epoch = _deadline_epoch(aggregate, plan, timestamp)
        _preflight_deadlines(
            aggregate,
            plan,
            now=timestamp,
            window=cancel_deadline_window,
            epoch=deadline_epoch,
        )
        drain, drain_changed = _prepare_drain(
            uow,
            aggregate=aggregate,
            plan=plan,
            now=timestamp,
        )
        changed = set(drain_changed)
        lifecycle_ids: set[UUID] = set()
        task_changed = False

        for action in plan.actions:
            action_changed, changed_task, lifecycle_id = _apply_action(
                uow,
                aggregate=aggregate,
                action=action,
                drain=drain,
                now=timestamp,
                cancel_deadline_window=cancel_deadline_window,
                deadline_epoch=deadline_epoch,
            )
            changed.update(action_changed)
            task_changed = task_changed or changed_task
            if lifecycle_id is not None:
                lifecycle_ids.add(lifecycle_id)

        if plan.completion in {
            CoordinatedCancelCompletion.APPLY_CANCELED,
            CoordinatedCancelCompletion.RETAIN_FAILED,
        }:
            completed = drain.complete(at=timestamp)
            if completed is drain:
                raise RuntimeExecutionConflict("Cancellation drain was already complete")
            uow.coordination_runtime_drains.save(
                completed,
                tenant_id=aggregate.task.tenant_id,
            )
            drain = completed
            changed.add(drain.id)
            if plan.completion is CoordinatedCancelCompletion.APPLY_CANCELED:
                aggregate.task.cancel_coordination_from_control(drain, at=timestamp)
            else:
                aggregate.task.fail_coordination_from_control(drain, at=timestamp)
            task_changed = True

        if task_changed:
            uow.tasks.save(aggregate.task)
            changed.add(aggregate.task.id)
        return CoordinatedCancelApplication(
            effective_drain=drain,
            task_status=aggregate.task.status,
            changed_ids=tuple(sorted(changed, key=str)),
            lifecycle_operation_ids=tuple(sorted(lifecycle_ids, key=str)),
            completion=plan.completion,
            made_progress=bool(changed),
        )


def _validate_plan(
    aggregate: CoordinatedRuntimeAggregate,
    plan: CoordinatedCancelPlan,
) -> None:
    if (
        type(aggregate) is not CoordinatedRuntimeAggregate
        or type(plan) is not CoordinatedCancelPlan
    ):
        raise RuntimeExecutionConflict("Cancellation application requires a locked plan")
    expected = plan_cancel_request(aggregate, plan.requested_reason)
    if expected != plan:
        raise RuntimeExecutionConflict("Cancellation plan is stale")


def _timestamp(value: datetime) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise InvalidTaskInput("Cancellation application timestamp must be aware UTC")
    return value.astimezone(timezone.utc)


def _cancel_window(value: timedelta) -> None:
    if type(value) is not timedelta or value <= timedelta(0) or value > _MAX_CANCEL_DEADLINE_WINDOW:
        raise InvalidTaskInput("Cancellation deadline window must be positive and bounded")


def _validate_clock(aggregate: CoordinatedRuntimeAggregate, now: datetime) -> None:
    timestamps = [aggregate.task.updated_at]
    if aggregate.active_drain is not None:
        timestamps.append(aggregate.active_drain.updated_at)
    for run in aggregate.runs:
        timestamps.extend(
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
        )
    timestamps.extend(value.updated_at for value in aggregate.subtasks)
    timestamps.extend(
        attempt.heartbeat_at
        for attempt in aggregate.latest_attempts.values()
        if attempt is not None
    )
    timestamps.extend(execution.updated_at for execution in aggregate.executions)
    if any(now < value.astimezone(timezone.utc) for value in timestamps):
        raise RuntimeExecutionConflict("Cancellation application clock moved backwards")


def _deadline_epoch(
    aggregate: CoordinatedRuntimeAggregate,
    plan: CoordinatedCancelPlan,
    now: datetime,
) -> datetime:
    if aggregate.active_drain is None or plan.retarget_drain:
        return now
    return persisted_cancel_epoch(aggregate.active_drain)


def _preflight_deadlines(
    aggregate: CoordinatedRuntimeAggregate,
    plan: CoordinatedCancelPlan,
    *,
    now: datetime,
    window: timedelta,
    epoch: datetime,
) -> None:
    for action in plan.actions:
        if action.kind is CoordinatedCancelActionKind.REQUEST_CANCEL:
            if action.execution_id is None:
                raise RuntimeExecutionConflict("Cancellation lifecycle action lacks execution")
            select_cancel_deadline(
                aggregate,
                execution_id=action.execution_id,
                now=now,
                cancel_deadline_window=window,
                deadline_epoch=epoch,
            )


def _prepare_drain(
    uow: Any,
    *,
    aggregate: CoordinatedRuntimeAggregate,
    plan: CoordinatedCancelPlan,
    now: datetime,
) -> tuple[CoordinationRuntimeDrain, set[UUID]]:
    active = aggregate.active_drain
    if plan.create_drain:
        if active is not None:
            raise RuntimeExecutionConflict("Cancellation drain creation is stale")
        if (
            uow.coordination_runtime_drains.get(
                plan.drain_id,
                tenant_id=aggregate.task.tenant_id,
                for_update=False,
            )
            is not None
        ):
            raise RuntimeExecutionConflict("Cancellation drain identity collides")
        drain = CoordinationRuntimeDrain.start(
            drain_id=plan.drain_id,
            tenant_id=aggregate.task.tenant_id,
            task_id=aggregate.task.id,
            triggering_run_id=plan.audit_anchor_run_id,
            target=plan.effective_target,
            reason=plan.effective_reason,
            at=now,
        )
        uow.coordination_runtime_drains.add(drain)
        return drain, {drain.id}
    if active is None or active.status is not CoordinationRuntimeDrainStatus.DRAINING:
        raise RuntimeExecutionConflict("Cancellation active drain is unavailable")
    if plan.retarget_drain:
        updated = active.retarget(
            target=plan.effective_target,
            reason=plan.effective_reason,
            at=now,
        )
        if updated is active:
            raise RuntimeExecutionConflict("Cancellation drain retarget is stale")
        uow.coordination_runtime_drains.save(updated, tenant_id=aggregate.task.tenant_id)
        return updated, {updated.id}
    if active.target is not plan.effective_target or active.reason != plan.effective_reason:
        raise RuntimeExecutionConflict("Cancellation drain projection is stale")
    return active, set()


def _apply_action(
    uow: Any,
    *,
    aggregate: CoordinatedRuntimeAggregate,
    action: CoordinatedCancelAction,
    drain: CoordinationRuntimeDrain,
    now: datetime,
    cancel_deadline_window: timedelta,
    deadline_epoch: datetime,
) -> tuple[set[UUID], bool, UUID | None]:
    run = _run(aggregate, action.run_id)
    attempt = aggregate.latest_attempts[run.id]
    execution = _execution(aggregate, action.execution_id)
    subtask = _subtask(aggregate, run)
    if action.kind in {
        CoordinatedCancelActionKind.RETAIN_TERMINAL,
        CoordinatedCancelActionKind.WAIT_RECONCILIATION,
    }:
        return set(), False, None
    if action.kind is CoordinatedCancelActionKind.REQUEST_CANCEL:
        if attempt is None or execution is None:
            raise RuntimeExecutionConflict("Cancellation lifecycle ownership is incomplete")
        result = request_cancel_in_uow(
            uow,
            aggregate=aggregate,
            execution=execution,
            attempt=attempt,
            drain=drain,
            now=now,
            cancel_deadline_window=cancel_deadline_window,
            deadline_epoch=deadline_epoch,
        )
        return set(result.changed_ids), False, result.lifecycle_id

    changed: set[UUID] = {run.id}
    task_changed = False
    if action.kind is CoordinatedCancelActionKind.CANCEL_QUEUED:
        run.cancel_before_managed_dispatch(at=now)
    else:
        if attempt is None:
            raise RuntimeExecutionConflict("Cancellation local action lacks Attempt")
        if action.kind is CoordinatedCancelActionKind.ABORT_PREPARED:
            if execution is None:
                raise RuntimeExecutionConflict("Cancellation prepared action lacks execution")
            abort_prepared_in_uow(
                uow,
                tenant_id=aggregate.task.tenant_id,
                drain_id=drain.id,
                execution=execution,
                run_id=run.id,
                attempt_id=attempt.id,
                fencing_token=attempt.fencing_token,
                now=now,
            )
            changed.add(execution.id)
        task_changed = release_attempt_accounting(uow, aggregate.task, attempt, now=now)
        attempt.cancel_before_managed_dispatch(at=now)
        uow.attempts.save(attempt)
        changed.add(attempt.id)
        run.cancel_before_managed_dispatch(at=now)
    uow.runs.save(run)
    if run.role is RunRole.SUPERVISOR:
        aggregate.task.release_never_dispatched_supervisor_run(run.id, at=now)
        task_changed = True
    else:
        if subtask is None:
            raise RuntimeExecutionConflict("Cancellation Executor action lacks Subtask")
        subtask.cancel_before_managed_dispatch(run.id, at=now)
        uow.subtasks.save(subtask)
        changed.add(subtask.id)
    return changed, task_changed, None


def _run(aggregate: CoordinatedRuntimeAggregate, run_id: UUID) -> TaskRun:
    values = [value for value in aggregate.runs if value.id == run_id]
    if len(values) != 1:
        raise RuntimeExecutionConflict("Cancellation Run is unavailable")
    return values[0]


def _subtask(aggregate: CoordinatedRuntimeAggregate, run: TaskRun):
    if run.subtask_id is None:
        return None
    values = [value for value in aggregate.subtasks if value.id == run.subtask_id]
    if len(values) != 1:
        raise RuntimeExecutionConflict("Cancellation Subtask is unavailable")
    return values[0]


def _execution(
    aggregate: CoordinatedRuntimeAggregate,
    execution_id: UUID | None,
) -> RuntimeExecution | None:
    if execution_id is None:
        return None
    values = [value for value in aggregate.executions if value.id == execution_id]
    if len(values) != 1:
        raise RuntimeExecutionConflict("Cancellation execution is unavailable")
    return values[0]


__all__ = ["CoordinatedCancelApplication", "CoordinatedRuntimeCancelApplier"]
