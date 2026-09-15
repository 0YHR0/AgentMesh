"""Transaction-local recovery of an expired coordinated Runtime lifecycle claim.

The recovery command is deliberately separate from the lifecycle adapter
consumer.  It owns one already-claimed deadline pass, reacquires the complete
Task aggregate, and keeps lifecycle, evidence, convergence, and barrier work
in one caller-owned transaction.  It never calls a provider or a public
service that opens another unit of work.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from agentmesh.application.coordinated_runtime import (
    CoordinatedRuntimeAggregate,
    CoordinatedRuntimeAggregateLocker,
)
from agentmesh.application.coordinated_runtime_convergence import (
    CoordinatedKnownTerminalResult,
)
from agentmesh.application.coordinated_runtime_provider_evidence import (
    CoordinatedProviderObservationKind,
    classify_provider_observation,
)
from agentmesh.application.coordinated_runtime_unknown import (
    CoordinatedUnknownOutcomeResult,
)
from agentmesh.application.runtime_contracts import validate_terminal_observation
from agentmesh.domain.coordination import (
    TERMINAL_SUBTASK_STATUSES,
    CoordinationRuntimeBoundary,
    SubtaskStatus,
)
from agentmesh.domain.errors import (
    InvalidTaskInput,
    InvalidTaskTransition,
    RuntimeExecutionConflict,
)
from agentmesh.domain.runtime_execution import (
    RuntimeExecution,
    RuntimeExecutionPhase,
    RuntimeLifecycleIntent,
    RuntimeLifecycleOperation,
    RuntimeLifecycleStatus,
)
from agentmesh.domain.tasks import (
    AttemptStatus,
    RunRole,
    RunStatus,
    Task,
    TaskAttempt,
    TaskExecutionMode,
    TaskStatus,
)
from agentmesh.runtime_sdk import RuntimeObservation, RuntimePhase
from agentmesh.runtime_sdk.canonical import canonical_digest

_ACTIVE_PHASES = frozenset(
    {
        RuntimeExecutionPhase.DISPATCHING,
        RuntimeExecutionPhase.ACCEPTED,
        RuntimeExecutionPhase.RUNNING,
        RuntimeExecutionPhase.WAITING_INPUT,
        RuntimeExecutionPhase.WAITING_APPROVAL,
        RuntimeExecutionPhase.PAUSE_REQUESTED,
        RuntimeExecutionPhase.PAUSED,
        RuntimeExecutionPhase.CANCEL_REQUESTED,
    }
)
_SAFE_UNKNOWN_REASON = "runtime.cancel_outcome_unknown"


class CoordinatedRuntimeDeadlineRecoveryService:
    """Finalize one exact deadline claim under a locked coordinated aggregate."""

    def __init__(
        self,
        *,
        uow_factory: Any,
        convergence_service: Any,
        unknown_service: Any,
        aggregate_locker: CoordinatedRuntimeAggregateLocker | None = None,
    ) -> None:
        if not callable(uow_factory):
            raise InvalidTaskInput("Deadline recovery UoW factory is invalid")
        if not callable(getattr(convergence_service, "apply_known_terminal_in_uow", None)):
            raise InvalidTaskInput("Deadline recovery convergence service is invalid")
        if not callable(getattr(unknown_service, "park_unknown_in_uow", None)):
            raise InvalidTaskInput("Deadline recovery unknown service is invalid")
        if aggregate_locker is not None and not callable(
            getattr(aggregate_locker, "lock", None)
        ):
            raise InvalidTaskInput("Deadline recovery aggregate locker is invalid")
        self._uow_factory = uow_factory
        self._convergence = convergence_service
        self._unknown = unknown_service
        self._aggregate_locker = aggregate_locker or CoordinatedRuntimeAggregateLocker()

    def finalize(
        self,
        tenant_id: str,
        task_id: UUID,
        run_id: UUID,
        attempt_id: UUID,
        fencing_token: int,
        execution_id: UUID,
        operation_id: str,
        claim_token: UUID,
        inspection: RuntimeObservation | None,
        at: datetime,
    ) -> CoordinatedKnownTerminalResult | CoordinatedUnknownOutcomeResult:
        timestamp = _validate_inputs(
            tenant_id=tenant_id,
            task_id=task_id,
            run_id=run_id,
            attempt_id=attempt_id,
            fencing_token=fencing_token,
            execution_id=execution_id,
            operation_id=operation_id,
            claim_token=claim_token,
            at=at,
        )
        with self._uow_factory() as uow:
            # The complete Task-first aggregate lock is the first repository
            # operation.  Every validation below precedes any durable write.
            aggregate = self._aggregate_locker.lock(
                uow, tenant_id=tenant_id, task_id=task_id
            )
            run, attempt, execution, lifecycle = _validate_target(
                aggregate=aggregate,
                tenant_id=tenant_id,
                task_id=task_id,
                run_id=run_id,
                attempt_id=attempt_id,
                fencing_token=fencing_token,
                execution_id=execution_id,
                operation_id=operation_id,
                claim_token=claim_token,
                at=timestamp,
            )
            _validate_target_clock(aggregate, run, attempt, execution, at=timestamp)
            terminal = _valid_terminal_inspection(inspection, execution)
            if terminal is not None:
                received_at = max(
                    timestamp, terminal.observed_at.astimezone(timezone.utc)
                )
                result = self._convergence.apply_known_terminal_in_uow(
                    uow=uow,
                    aggregate=aggregate,
                    tenant_id=tenant_id,
                    task_id=task_id,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    fencing_token=fencing_token,
                    runtime_execution_id=execution_id,
                    observation=terminal,
                    received_at=received_at,
                    causation_id=claim_token,
                )
                if type(result) is not CoordinatedKnownTerminalResult:
                    raise InvalidTaskTransition(
                        "Deadline recovery terminal result type is invalid"
                    )
                _validate_result_identity(
                    result,
                    tenant_id=tenant_id,
                    task_id=task_id,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    execution_id=execution_id,
                )
                cleared = lifecycle.clear_deadline_claim(
                    claim_token=claim_token, now=received_at
                )
                uow.runtimes.save_lifecycle_operation(cleared)
                uow.commit()
                return result

            unknown_observation = _valid_unknown_inspection(inspection, execution)
            if unknown_observation is None:
                unknown_observation = _synthetic_unknown(
                    tenant_id=tenant_id,
                    task_id=task_id,
                    run_id=run_id,
                    execution=execution,
                    observed_at=timestamp,
                )
            received_at = max(
                timestamp, unknown_observation.observed_at.astimezone(timezone.utc)
            )
            expired = lifecycle.expire(
                now=timestamp, error_code=_SAFE_UNKNOWN_REASON
            )
            uow.runtimes.save_lifecycle_operation(expired)
            expired_aggregate = replace(
                aggregate,
                lifecycle_operations=tuple(
                    expired if value.id == lifecycle.id else value
                    for value in aggregate.lifecycle_operations
                ),
            )
            result = self._unknown.park_unknown_in_uow(
                uow=uow,
                aggregate=expired_aggregate,
                tenant_id=tenant_id,
                task_id=task_id,
                run_id=run_id,
                attempt_id=attempt_id,
                fencing_token=fencing_token,
                runtime_execution_id=execution_id,
                observation=unknown_observation,
                received_at=received_at,
                causation_id=claim_token,
            )
            if type(result) is not CoordinatedUnknownOutcomeResult:
                raise InvalidTaskTransition(
                    "Deadline recovery unknown result type is invalid"
                )
            _validate_result_identity(
                result,
                tenant_id=tenant_id,
                task_id=task_id,
                run_id=run_id,
                attempt_id=attempt_id,
                execution_id=execution_id,
            )
            uow.commit()
            return result


def _validate_inputs(**values: Any) -> datetime:
    tenant_id = values["tenant_id"]
    if (
        type(tenant_id) is not str
        or not tenant_id.strip()
        or tenant_id != tenant_id.strip()
        or any(
            type(values[name]) is not UUID
            for name in ("task_id", "run_id", "attempt_id", "execution_id", "claim_token")
        )
        or type(values["fencing_token"]) is not int
        or values["fencing_token"] <= 0
        or type(values["operation_id"]) is not str
        or not values["operation_id"].strip()
        or len(values["operation_id"]) > 512
        or type(values["at"]) is not datetime
        or values["at"].tzinfo is None
        or values["at"].utcoffset() is None
        or values["at"].utcoffset() != timedelta(0)
    ):
        raise InvalidTaskInput("Deadline recovery command input is invalid")
    return values["at"].astimezone(timezone.utc)


def _validate_target(
    *,
    aggregate: CoordinatedRuntimeAggregate,
    tenant_id: str,
    task_id: UUID,
    run_id: UUID,
    attempt_id: UUID,
    fencing_token: int,
    execution_id: UUID,
    operation_id: str,
    claim_token: UUID,
    at: datetime,
) -> tuple[Any, TaskAttempt, RuntimeExecution, RuntimeLifecycleIntent]:
    if type(aggregate) is not CoordinatedRuntimeAggregate:
        raise RuntimeExecutionConflict("Deadline recovery aggregate is invalid")
    task = aggregate.task
    if (
        type(task) is not Task
        or task.id != task_id
        or task.tenant_id != tenant_id
        or type(task.execution_mode) is not TaskExecutionMode
        or task.execution_mode is not TaskExecutionMode.COORDINATED
        or task.status not in {TaskStatus.RUNNING, TaskStatus.RECONCILIATION_REQUIRED}
    ):
        raise RuntimeExecutionConflict("Deadline recovery Task projection is invalid")
    runs = [value for value in aggregate.runs if value.id == run_id]
    if len(runs) != 1:
        raise RuntimeExecutionConflict("Deadline recovery Run identity is ambiguous")
    run = runs[0]
    if (
        run.task_id != task_id
        or run.runtime_authority != "managed"
        or run.comparison_mode != "off"
        or run.runtime_execution_id != execution_id
        or run.runtime_execution_intent_id != execution_id
        or run.status not in {RunStatus.RUNNING, RunStatus.RECONCILIATION_REQUIRED}
        or run.role not in {RunRole.EXECUTOR, RunRole.SUPERVISOR}
    ):
        raise RuntimeExecutionConflict("Deadline recovery Run projection is invalid")
    if run.role is RunRole.EXECUTOR:
        subtasks = [value for value in aggregate.subtasks if value.id == run.subtask_id]
        if (
            len(subtasks) != 1
            or subtasks[0].task_id != task_id
            or subtasks[0].current_run_id != run.id
            or subtasks[0].status
            not in {SubtaskStatus.RUNNING, SubtaskStatus.RECONCILIATION_REQUIRED}
        ):
            raise RuntimeExecutionConflict("Deadline recovery Executor Subtask is invalid")
    elif (
        run.subtask_id is not None
        or task.current_run_id != run.id
        or any(value.status not in TERMINAL_SUBTASK_STATUSES for value in aggregate.subtasks)
    ):
        raise RuntimeExecutionConflict("Deadline recovery Supervisor binding is invalid")
    attempt = aggregate.latest_attempts.get(run.id)
    if (
        type(attempt) is not TaskAttempt
        or attempt.id != attempt_id
        or attempt.run_id != run.id
        or attempt.fencing_token != fencing_token
        or attempt.status not in {AttemptStatus.RUNNING, AttemptStatus.OUTCOME_UNKNOWN}
    ):
        raise RuntimeExecutionConflict("Deadline recovery Attempt projection is invalid")
    executions = [value for value in aggregate.executions if value.id == execution_id]
    if len(executions) != 1:
        raise RuntimeExecutionConflict("Deadline recovery execution identity is ambiguous")
    execution = executions[0]
    if (
        type(execution) is not RuntimeExecution
        or execution.tenant_id != tenant_id
        or execution.run_id != run.id
        or execution.current_owner_attempt_id != attempt.id
        or execution.current_fencing_token != fencing_token
        or execution.phase not in _ACTIVE_PHASES
        or aggregate.boundary_classifications.get(run.id)
        is not CoordinationRuntimeBoundary.CROSSED_ACTIVE
    ):
        raise RuntimeExecutionConflict("Deadline recovery execution projection is invalid")
    lifecycle_values = [
        value
        for value in aggregate.lifecycle_operations
        if value.operation_id == operation_id
    ]
    if len(lifecycle_values) != 1:
        raise RuntimeExecutionConflict("Deadline recovery lifecycle identity is ambiguous")
    lifecycle = lifecycle_values[0]
    if (
        type(lifecycle) is not RuntimeLifecycleIntent
        or lifecycle.tenant_id != tenant_id
        or lifecycle.runtime_execution_id != execution.id
        or lifecycle.operation_id != f"runtime-cancel:{execution.id}:v1"
        or operation_id != f"runtime-cancel:{execution.id}:v1"
        or lifecycle.operation is not RuntimeLifecycleOperation.CANCEL
        or lifecycle.status
        not in {
            RuntimeLifecycleStatus.REQUESTED,
            RuntimeLifecycleStatus.ACCEPTED,
            RuntimeLifecycleStatus.REJECTED,
        }
        or lifecycle.claim_token != claim_token
        or lifecycle.claim_acquired_at is None
        or lifecycle.claim_expires_at is None
        or lifecycle.deadline > at
        or at < lifecycle.updated_at.astimezone(timezone.utc)
    ):
        raise RuntimeExecutionConflict("Deadline recovery lifecycle claim is stale")
    return run, attempt, execution, lifecycle


def _validate_target_clock(
    aggregate: CoordinatedRuntimeAggregate,
    run: Any,
    attempt: TaskAttempt,
    execution: RuntimeExecution,
    *,
    at: datetime,
) -> None:
    changed_at = [
        aggregate.task.updated_at,
        attempt.started_at,
        attempt.heartbeat_at,
        execution.updated_at,
        run.queued_at,
        run.started_at,
        run.pause_requested_at,
        run.paused_at,
        run.resumed_at,
        run.completed_at,
    ]
    if run.role is RunRole.EXECUTOR:
        subtask = next(value for value in aggregate.subtasks if value.id == run.subtask_id)
        changed_at.append(subtask.updated_at)
    if any(at < value.astimezone(timezone.utc) for value in changed_at if value is not None):
        raise InvalidTaskTransition("Deadline recovery command clock moved backwards")


def _valid_terminal_inspection(
    inspection: Any, execution: RuntimeExecution
) -> RuntimeObservation | None:
    if type(inspection) is not RuntimeObservation:
        return None
    if (
        inspection.runtime_execution_id != str(execution.id)
        or inspection.assignment_id != str(execution.assignment_id)
        or inspection.assignment_digest != execution.assignment_digest
    ):
        return None
    try:
        validate_terminal_observation(
            inspection,
            runtime_execution_id=execution.id,
            assignment_id=execution.assignment_id,
            assignment_digest=execution.assignment_digest,
            require_known_terminal=True,
        )
        kind = classify_provider_observation(inspection)
    except Exception:
        return None
    if kind is not CoordinatedProviderObservationKind.KNOWN_TERMINAL:
        return None
    return inspection


def _valid_unknown_inspection(
    inspection: Any, execution: RuntimeExecution
) -> RuntimeObservation | None:
    if type(inspection) is not RuntimeObservation:
        return None
    if (
        inspection.runtime_execution_id != str(execution.id)
        or inspection.assignment_id != str(execution.assignment_id)
        or inspection.assignment_digest != execution.assignment_digest
    ):
        return None
    try:
        validate_terminal_observation(
            inspection,
            runtime_execution_id=execution.id,
            assignment_id=execution.assignment_id,
            assignment_digest=execution.assignment_digest,
        )
        kind = classify_provider_observation(inspection)
    except Exception:
        return None
    if kind not in {
        CoordinatedProviderObservationKind.LOST,
        CoordinatedProviderObservationKind.OUTCOME_UNKNOWN,
    }:
        return None
    return inspection


def _synthetic_unknown(
    *,
    tenant_id: str,
    task_id: UUID,
    run_id: UUID,
    execution: RuntimeExecution,
    observed_at: datetime,
) -> RuntimeObservation:
    timestamp = observed_at.astimezone(timezone.utc)
    authority = ":".join(
        (
            tenant_id,
            str(task_id),
            str(run_id),
            str(execution.id),
            str(execution.assignment_id),
            execution.assignment_digest,
            _SAFE_UNKNOWN_REASON,
        )
    )
    observation_id = uuid5(
        NAMESPACE_URL, f"coordinated-runtime-deadline:{authority}:observation"
    )
    provider_event_id = "control-plane-unknown:" + str(
        uuid5(NAMESPACE_URL, f"coordinated-runtime-deadline:{authority}:event")
    )
    snapshot_digest = canonical_digest(
        {
            "kind": RuntimePhase.OUTCOME_UNKNOWN.value,
            "reason": _SAFE_UNKNOWN_REASON,
            "tenant_id": tenant_id,
            "task_id": str(task_id),
            "run_id": str(run_id),
            "runtime_execution_id": str(execution.id),
            "assignment_id": str(execution.assignment_id),
            "assignment_digest": execution.assignment_digest,
            "observed_at": timestamp.isoformat(),
        }
    )
    return RuntimeObservation(
        observation_id=str(observation_id),
        runtime_execution_id=str(execution.id),
        assignment_id=str(execution.assignment_id),
        assignment_digest=execution.assignment_digest,
        phase=RuntimePhase.OUTCOME_UNKNOWN,
        observed_at=timestamp,
        provider_event_id=provider_event_id,
        snapshot_digest=snapshot_digest,
        extensions={"control_plane_reason": _SAFE_UNKNOWN_REASON},
    )


def _validate_result_identity(
    result: CoordinatedKnownTerminalResult | CoordinatedUnknownOutcomeResult,
    *,
    tenant_id: str,
    task_id: UUID,
    run_id: UUID,
    attempt_id: UUID,
    execution_id: UUID,
) -> None:
    if any(
        getattr(result, name, None) != value
        for name, value in (
            ("tenant_id", tenant_id),
            ("task_id", task_id),
            ("run_id", run_id),
            ("attempt_id", attempt_id),
            ("runtime_execution_id", execution_id),
        )
    ):
        raise RuntimeExecutionConflict("Deadline recovery collaborator identity conflicts")


__all__ = ["CoordinatedRuntimeDeadlineRecoveryService"]
