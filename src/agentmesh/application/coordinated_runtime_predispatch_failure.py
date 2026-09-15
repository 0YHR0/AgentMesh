"""Atomic provider-free failure handling for coordinated Runtime deliveries."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from agentmesh.application.authority_cohorts import validate_builtin_managed_runtime_version
from agentmesh.application.coordinated_runtime import (
    CoordinatedRuntimeAggregate,
    CoordinatedRuntimeAggregateLocker,
)
from agentmesh.application.coordinated_runtime_barrier import (
    CoordinatedBarrierApplicationMode,
    CoordinatedBarrierCompletion,
    CoordinatedRuntimeBarrierApplier,
    fail_preboundary_in_uow,
    plan_predispatch_failure,
)
from agentmesh.domain.coordination import (
    CoordinationRuntimeBoundary,
    CoordinationRuntimeDrainTarget,
    SubtaskStatus,
    normalize_coordination_reason,
)
from agentmesh.domain.errors import (
    InvalidMessage,
    InvalidTaskInput,
    RuntimeExecutionConflict,
    RuntimeVersionNotFound,
)
from agentmesh.domain.messaging import (
    RUN_REQUESTED_SCHEMA,
    RUN_REQUESTED_VERSION,
    InboxMessage,
    MessageEnvelope,
)
from agentmesh.domain.tasks import (
    AttemptStatus,
    RunRole,
    RunStatus,
    TaskExecutionMode,
    TaskRun,
    TaskStatus,
)

_SAFE_REASON = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")
_FAILURE_EVENT_NAMESPACE = "coordination-runtime-predispatch-failure:"


class CoordinatedPredispatchFailureKind(str, Enum):
    FAILED = "FAILED"
    DRAINING_ACTIVE = "DRAINING_ACTIVE"
    WAIT_RECONCILIATION = "WAIT_RECONCILIATION"
    REPLAY = "REPLAY"


@dataclass(frozen=True)
class CoordinatedPredispatchFailureResult:
    kind: CoordinatedPredispatchFailureKind
    tenant_id: str
    task_id: UUID
    run_id: UUID
    attempt_id: UUID
    task_status: TaskStatus
    run_status: RunStatus
    subtask_status: SubtaskStatus | None
    drain_id: UUID | None
    drain_target: CoordinationRuntimeDrainTarget | None


class CoordinatedRuntimePredispatchFailureService:
    """Turn a local construction/validation failure into one durable conclusion."""

    def __init__(
        self,
        *,
        uow_factory: Any,
        aggregate_locker: CoordinatedRuntimeAggregateLocker | None = None,
        barrier_applier: CoordinatedRuntimeBarrierApplier | None = None,
        cancel_deadline_window: timedelta = timedelta(hours=1),
    ) -> None:
        if (
            type(cancel_deadline_window) is not timedelta
            or cancel_deadline_window <= timedelta(0)
            or cancel_deadline_window > timedelta(days=7)
        ):
            raise InvalidTaskInput("Pre-dispatch cancel deadline window is invalid")
        self._uow_factory = uow_factory
        self._aggregate_locker = aggregate_locker or CoordinatedRuntimeAggregateLocker()
        self._barrier_applier = barrier_applier or CoordinatedRuntimeBarrierApplier()
        self._cancel_deadline_window = cancel_deadline_window

    def fail_delivery(
        self,
        tenant_id: str,
        task_id: UUID,
        run_id: UUID,
        attempt_id: UUID,
        fencing_token: int,
        consumer_name: str,
        envelope: MessageEnvelope,
        reason: str,
        causation_id: UUID,
        at: datetime,
    ) -> CoordinatedPredispatchFailureResult:
        timestamp, safe_reason = _validate_command(
            tenant_id=tenant_id,
            task_id=task_id,
            run_id=run_id,
            attempt_id=attempt_id,
            fencing_token=fencing_token,
            consumer_name=consumer_name,
            envelope=envelope,
            reason=reason,
            causation_id=causation_id,
            at=at,
        )
        with self._uow_factory() as uow:
            # The aggregate locker makes Task FOR UPDATE the first repository operation.
            aggregate = self._aggregate_locker.lock(
                uow, tenant_id=tenant_id, task_id=task_id
            )
            _validate_cohort(aggregate, tenant_id=tenant_id, task_id=task_id)
            if uow.inbox.contains(tenant_id, consumer_name, envelope.message_id):
                _validate_replay_identity(
                    aggregate,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    fencing_token=fencing_token,
                )
                return _result(
                    CoordinatedPredispatchFailureKind.REPLAY,
                    aggregate,
                    run_id=run_id,
                    attempt_id=attempt_id,
                )

            run, attempt = _select_target(
                aggregate,
                task_id=task_id,
                run_id=run_id,
                attempt_id=attempt_id,
                fencing_token=fencing_token,
            )
            plan = plan_predispatch_failure(
                aggregate, triggering_run_id=run.id, reason=safe_reason
            )
            task_changed = fail_preboundary_in_uow(
                uow,
                aggregate=aggregate,
                run=run,
                attempt_id=attempt.id,
                fencing_token=fencing_token,
                reason=safe_reason,
                now=timestamp,
            )
            barrier = self._barrier_applier.apply_in_uow(
                uow,
                aggregate=aggregate,
                plan=plan,
                now=timestamp,
                cancel_deadline_window=self._cancel_deadline_window,
                defer_task_save=True,
                application_mode=CoordinatedBarrierApplicationMode.PREDISPATCH_FAILURE,
            )
            task_changed = _apply_completion(
                uow,
                aggregate=aggregate,
                run=run,
                barrier=barrier,
                reason=safe_reason,
                at=timestamp,
            ) or task_changed or aggregate.task.id in barrier.changed_ids
            if task_changed:
                uow.tasks.save(aggregate.task)
            _add_failure_outbox(
                uow,
                tenant_id=tenant_id,
                task_id=task_id,
                run_id=run_id,
                attempt_id=attempt_id,
                fencing_token=fencing_token,
                reason=safe_reason,
                causation_id=causation_id,
                at=timestamp,
            )
            uow.inbox.add(InboxMessage.processed(consumer_name, envelope, at=timestamp))
            uow.commit()
            kind = {
                CoordinatedBarrierCompletion.WAIT_ACTIVE: (
                    CoordinatedPredispatchFailureKind.DRAINING_ACTIVE
                ),
                CoordinatedBarrierCompletion.WAIT_RECONCILIATION: (
                    CoordinatedPredispatchFailureKind.WAIT_RECONCILIATION
                ),
            }.get(barrier.completion, CoordinatedPredispatchFailureKind.FAILED)
            return _result(kind, aggregate, run_id=run_id, attempt_id=attempt_id, barrier=barrier)


def _validate_command(**values: Any) -> tuple[datetime, str]:
    envelope = values["envelope"]
    if (
        type(values["tenant_id"]) is not str
        or not values["tenant_id"].strip()
        or values["tenant_id"] != values["tenant_id"].strip()
        or len(values["tenant_id"]) > 128
        or any(
            type(values[name]) is not UUID
            for name in ("task_id", "run_id", "attempt_id", "causation_id")
        )
        or type(values["fencing_token"]) is not int
        or values["fencing_token"] <= 0
        or type(values["consumer_name"]) is not str
        or not values["consumer_name"].strip()
        or values["consumer_name"] != values["consumer_name"].strip()
        or len(values["consumer_name"]) > 128
        or type(values["at"]) is not datetime
        or values["at"].tzinfo is None
        or values["at"].utcoffset() != timedelta(0)
    ):
        raise InvalidTaskInput("Pre-dispatch failure input is invalid")
    try:
        safe_reason = normalize_coordination_reason(values["reason"])
    except (InvalidTaskInput, AttributeError, TypeError) as exc:
        raise InvalidTaskInput("Pre-dispatch failure reason is invalid") from exc
    if _SAFE_REASON.fullmatch(safe_reason) is None:
        raise InvalidTaskInput("Pre-dispatch failure reason must be a bounded safe code")
    if type(envelope) is not MessageEnvelope:
        raise InvalidMessage("Pre-dispatch failure envelope is invalid")
    expected_payload = {"task_id": str(values["task_id"]), "run_id": str(values["run_id"])}
    if (
        type(envelope.schema_name) is not str
        or envelope.schema_name != RUN_REQUESTED_SCHEMA
        or type(envelope.schema_version) is not int
        or envelope.schema_version != RUN_REQUESTED_VERSION
        or type(envelope.message_id) is not UUID
        or envelope.tenant_id != values["tenant_id"]
        or type(envelope.occurred_at) is not datetime
        or envelope.occurred_at.tzinfo is None
        or envelope.occurred_at.utcoffset() is None
        or type(envelope.producer) is not str
        or not envelope.producer.strip()
        or envelope.producer != envelope.producer.strip()
        or type(envelope.causation_id) not in {UUID, type(None)}
        or envelope.correlation_id != values["task_id"]
        or type(envelope.idempotency_key) is not str
        or envelope.idempotency_key != f"run:{values['run_id']}"
        or type(envelope.payload) is not dict
        or envelope.payload != expected_payload
    ):
        raise InvalidMessage("Pre-dispatch failure envelope identity is invalid")
    return values["at"].astimezone(timezone.utc), safe_reason


def _validate_cohort(
    aggregate: CoordinatedRuntimeAggregate, *, tenant_id: str, task_id: UUID
) -> None:
    cohort = aggregate.cohort
    if (
        type(aggregate) is not CoordinatedRuntimeAggregate
        or aggregate.task.id != task_id
        or aggregate.task.tenant_id != tenant_id
        or aggregate.task.execution_mode is not TaskExecutionMode.COORDINATED
        or cohort.task_id != task_id
        or cohort.tenant_id != tenant_id
        or cohort.runtime_authority != "managed"
        or cohort.comparison_mode != "off"
        or cohort.runtime_version_id not in aggregate.runtime_versions
    ):
        raise RuntimeExecutionConflict("Pre-dispatch failure cohort is invalid")
    try:
        validate_builtin_managed_runtime_version(
            aggregate.runtime_versions[cohort.runtime_version_id]
        )
    except (RuntimeVersionNotFound, InvalidTaskInput) as exc:
        raise RuntimeExecutionConflict(
            "Pre-dispatch failure Runtime Version is incompatible"
        ) from exc


def _select_target(
    aggregate: CoordinatedRuntimeAggregate,
    *,
    task_id: UUID,
    run_id: UUID,
    attempt_id: UUID,
    fencing_token: int,
) -> tuple[TaskRun, Any]:
    matches = [value for value in aggregate.runs if value.id == run_id]
    if len(matches) != 1:
        raise RuntimeExecutionConflict("Pre-dispatch failure Run is unavailable")
    run = matches[0]
    attempt = aggregate.latest_attempts.get(run.id)
    boundary = aggregate.boundary_classifications.get(run.id)
    if boundary not in {
        CoordinationRuntimeBoundary.NOT_CROSSED_NO_EXECUTION,
        CoordinationRuntimeBoundary.NOT_CROSSED_PREPARED,
    }:
        raise RuntimeExecutionConflict("Pre-dispatch failure crossed the provider boundary")
    if (
        run.task_id != task_id
        or run.runtime_authority != "managed"
        or run.comparison_mode != "off"
        or run.runtime_version_id != aggregate.cohort.runtime_version_id
        or run.status is not RunStatus.RUNNING
        or attempt is None
        or attempt.id != attempt_id
        or attempt.fencing_token != fencing_token
        or attempt.status is not AttemptStatus.RUNNING
    ):
        raise RuntimeExecutionConflict("Pre-dispatch failure ownership is stale")
    if run.role is RunRole.EXECUTOR:
        subtasks = [value for value in aggregate.subtasks if value.id == run.subtask_id]
        if (
            len(subtasks) != 1
            or subtasks[0].current_run_id != run.id
            or subtasks[0].status is not SubtaskStatus.RUNNING
        ):
            raise RuntimeExecutionConflict("Pre-dispatch Executor binding is stale")
    elif (
        run.role is not RunRole.SUPERVISOR
        or run.subtask_id is not None
        or aggregate.task.current_run_id != run.id
    ):
        raise RuntimeExecutionConflict("Pre-dispatch Supervisor binding is stale")
    return run, attempt


def _validate_replay_identity(
    aggregate: CoordinatedRuntimeAggregate,
    *,
    run_id: UUID,
    attempt_id: UUID,
    fencing_token: int,
) -> None:
    runs = [value for value in aggregate.runs if value.id == run_id]
    if len(runs) != 1:
        raise RuntimeExecutionConflict("Pre-dispatch failure replay Run is unavailable")
    attempt = aggregate.latest_attempts.get(run_id)
    if (
        attempt is None
        or attempt.id != attempt_id
        or attempt.fencing_token != fencing_token
        or attempt.status is not AttemptStatus.FAILED
        or runs[0].status is not RunStatus.FAILED
        or runs[0].error != attempt.error
    ):
        raise RuntimeExecutionConflict("Pre-dispatch failure replay ownership differs")


def _apply_completion(uow: Any, *, aggregate, run, barrier, reason: str, at: datetime) -> bool:
    if barrier.completion is CoordinatedBarrierCompletion.WAIT_RECONCILIATION:
        if aggregate.task.status is TaskStatus.RUNNING:
            aggregate.task.require_coordination_runtime_reconciliation(
                barrier.effective_drain, at=at
            )
            return True
        return False
    if barrier.completion is not CoordinatedBarrierCompletion.APPLY_FAILED:
        return False
    drain = barrier.effective_drain
    if drain is None or drain.target is not CoordinationRuntimeDrainTarget.FAILED:
        raise RuntimeExecutionConflict("Pre-dispatch failure completion drain is invalid")
    completed = drain.complete(at=at)
    uow.coordination_runtime_drains.save(completed, tenant_id=aggregate.task.tenant_id)
    if aggregate.task.status is TaskStatus.RECONCILIATION_REQUIRED:
        aggregate.task.fail_coordination_after_runtime_reconciliation(completed, at=at)
    elif run.role is RunRole.SUPERVISOR:
        aggregate.task.fail(run.id, reason, at=at)
    else:
        aggregate.task.fail_coordination(reason, at=at)
    return True


def _add_failure_outbox(uow: Any, **values: Any) -> None:
    message_id = uuid5(
        NAMESPACE_URL,
        f"{_FAILURE_EVENT_NAMESPACE}{values['tenant_id']}:{values['task_id']}:"
        f"{values['run_id']}:{values['attempt_id']}:{values['fencing_token']}",
    )
    envelope = MessageEnvelope(
        schema_name="agentmesh.runtime.predispatch-failed",
        schema_version=1,
        message_id=message_id,
        tenant_id=values["tenant_id"],
        occurred_at=values["at"],
        producer="agentmesh-runtime-control-plane-v1",
        correlation_id=values["task_id"],
        causation_id=values["causation_id"],
        idempotency_key=f"runtime-predispatch-failed:{values['attempt_id']}:{values['fencing_token']}",
        payload={
            "tenant_id": values["tenant_id"],
            "task_id": str(values["task_id"]),
            "run_id": str(values["run_id"]),
            "attempt_id": str(values["attempt_id"]),
            "fencing_token": values["fencing_token"],
            "reason": values["reason"],
        },
    )
    add_if_absent = getattr(uow.outbox, "add_if_absent", None)
    if callable(add_if_absent):
        if not add_if_absent(envelope):
            raise RuntimeExecutionConflict("Pre-dispatch failure Outbox identity collides")
    else:
        uow.outbox.add(envelope)


def _result(kind, aggregate, *, run_id, attempt_id, barrier=None):
    run = next((value for value in aggregate.runs if value.id == run_id), None)
    if run is None:
        raise RuntimeExecutionConflict("Pre-dispatch failure replay Run is unavailable")
    subtask = next(
        (value for value in aggregate.subtasks if value.id == run.subtask_id), None
    )
    drain = barrier.effective_drain if barrier is not None else aggregate.active_drain
    return CoordinatedPredispatchFailureResult(
        kind=kind,
        tenant_id=aggregate.task.tenant_id,
        task_id=aggregate.task.id,
        run_id=run.id,
        attempt_id=attempt_id,
        task_status=aggregate.task.status,
        run_status=run.status,
        subtask_status=subtask.status if subtask is not None else None,
        drain_id=drain.id if drain is not None else None,
        drain_target=drain.target if drain is not None else None,
    )


__all__ = [
    "CoordinatedPredispatchFailureKind",
    "CoordinatedPredispatchFailureResult",
    "CoordinatedRuntimePredispatchFailureService",
]
