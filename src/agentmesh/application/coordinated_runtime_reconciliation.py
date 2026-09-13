"""Privileged convergence of one parked coordinated Runtime execution.

The command is intentionally caller-free until c.2f.  It owns one UoW, takes
the complete coordinated aggregate lock before every repository operation,
and never invokes a Runtime adapter or research materializer.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from agentmesh.application.authority_cohorts import validate_builtin_managed_runtime_version
from agentmesh.application.budget_services import BudgetController
from agentmesh.application.business_outcomes import KnownTerminalPhase
from agentmesh.application.coordinated_runtime import (
    CoordinatedRuntimeAggregate,
    CoordinatedRuntimeAggregateLocker,
)
from agentmesh.application.coordinated_runtime_barrier import (
    CoordinatedBarrierApplicationMode,
    CoordinatedBarrierCompletion,
    CoordinatedRuntimeBarrierApplier,
    plan_reconciled_terminal,
)
from agentmesh.application.coordination_services import CoordinatedScheduler
from agentmesh.application.memory_runtime_services import RuntimeMemoryService
from agentmesh.application.runtime_contracts import (
    TerminalObservationValidator,
    validate_terminal_observation,
)
from agentmesh.application.runtime_services import validate_runtime_assignment_chain
from agentmesh.application.runtime_snapshots import parse_assignment_payload
from agentmesh.domain.budgets import BudgetSettlementSource
from agentmesh.domain.coordination import (
    CoordinationRuntimeBoundary,
    CoordinationRuntimeDrain,
    CoordinationRuntimeDrainStatus,
    CoordinationRuntimeDrainTarget,
    Subtask,
    SubtaskStatus,
)
from agentmesh.domain.errors import (
    AuthorizationDenied,
    IdempotencyConflict,
    InvalidTaskInput,
    InvalidTaskTransition,
    RuntimeExecutionConflict,
    RuntimeVersionNotFound,
)
from agentmesh.domain.identity import Permission, PrincipalContext
from agentmesh.domain.messaging import IdempotencyRecord, MessageEnvelope
from agentmesh.domain.resolutions import TaskResolution, TaskResolutionAction
from agentmesh.domain.runtime_execution import (
    RuntimeExecution,
    RuntimeExecutionPhase,
    RuntimeObservationEvidence,
    RuntimeObservationOutcome,
)
from agentmesh.domain.tasks import (
    AttemptStatus,
    RunRole,
    RunStatus,
    TaskAttempt,
    TaskExecutionMode,
    TaskRun,
    TaskStatus,
)
from agentmesh.features import Feature, FeatureGateSet
from agentmesh.runtime_sdk import RuntimeObservation, RuntimePhase, canonical_digest
from agentmesh.runtime_sdk.canonical import thaw_json
from agentmesh.runtime_sdk.descriptor import RuntimeDescriptor


@dataclass(frozen=True)
class CoordinatedRuntimeReconciliationResult:
    execution: RuntimeExecution
    resolution: TaskResolution
    scheduled_run_ids: tuple[UUID, ...] = ()

    def __post_init__(self) -> None:
        if (
            type(self.execution) is not RuntimeExecution
            or type(self.resolution) is not TaskResolution
        ):
            raise RuntimeExecutionConflict("Coordinated reconciliation result is invalid")
        if any(type(value) is not UUID for value in self.scheduled_run_ids):
            raise RuntimeExecutionConflict("Coordinated reconciliation scheduled Runs are invalid")
        if tuple(sorted(self.scheduled_run_ids, key=str)) != self.scheduled_run_ids:
            raise RuntimeExecutionConflict(
                "Coordinated reconciliation scheduled Runs are unordered"
            )


class CoordinatedRuntimeReconciliationService:
    """Resolve one exact parked Executor or Supervisor from accepted evidence."""

    def __init__(
        self,
        *,
        uow_factory: Any,
        feature_gates: FeatureGateSet,
        coordinated_scheduler: CoordinatedScheduler,
        cancel_deadline_window: timedelta,
        aggregate_locker: CoordinatedRuntimeAggregateLocker | None = None,
        barrier_applier: CoordinatedRuntimeBarrierApplier | None = None,
        runtime_memory_service: RuntimeMemoryService | None = None,
    ) -> None:
        if coordinated_scheduler is None or not hasattr(coordinated_scheduler, "schedule"):
            raise InvalidTaskInput("Coordinated reconciliation scheduler is invalid")
        if (
            type(cancel_deadline_window) is not timedelta
            or cancel_deadline_window <= timedelta(0)
            or cancel_deadline_window > timedelta(days=7)
        ):
            raise InvalidTaskInput("Coordinated reconciliation cancel window is invalid")
        self._uow_factory = uow_factory
        self._feature_gates = feature_gates
        self._scheduler = coordinated_scheduler
        self._cancel_deadline_window = cancel_deadline_window
        self._aggregate_locker = aggregate_locker or CoordinatedRuntimeAggregateLocker()
        self._barrier_applier = barrier_applier or CoordinatedRuntimeBarrierApplier()
        self._runtime_memory_service = runtime_memory_service

    def reconcile_known_terminal(
        self,
        *,
        tenant_id: str,
        task_id: UUID,
        run_id: UUID,
        attempt_id: UUID,
        fencing_token: int,
        runtime_execution_id: UUID,
        principal: PrincipalContext,
        observation: RuntimeObservation,
        evidence_digest: str,
        evidence_reference: str,
        reason: str,
        idempotency_key: str,
        received_at: datetime,
    ) -> CoordinatedRuntimeReconciliationResult:
        values = _validate_command(
            tenant_id=tenant_id,
            task_id=task_id,
            run_id=run_id,
            attempt_id=attempt_id,
            fencing_token=fencing_token,
            runtime_execution_id=runtime_execution_id,
            principal=principal,
            observation=observation,
            evidence_digest=evidence_digest,
            evidence_reference=evidence_reference,
            reason=reason,
            idempotency_key=idempotency_key,
            received_at=received_at,
        )
        self._feature_gates.require(Feature.MANAGED_AGENT_RUNTIME)
        self._feature_gates.require(Feature.OUTCOME_RECONCILIATION)
        reference, normalized_reason, key, timestamp, digest = values
        request_hash = canonical_digest(
            {
                "tenant_id": tenant_id,
                "task_id": str(task_id),
                "run_id": str(run_id),
                "attempt_id": str(attempt_id),
                "fencing_token": fencing_token,
                "runtime_execution_id": str(runtime_execution_id),
                "principal_id": principal.principal_id,
                "observation": observation.to_dict(),
                "evidence_digest": digest,
                "evidence_reference": reference,
                "reason": normalized_reason,
            }
        )
        scope = (
            f"coordinated-runtime-reconciliation:{tenant_id}:"
            f"{task_id}:{principal.principal_id}:{runtime_execution_id}"
        )
        with self._uow_factory() as uow:
            # This is the first repository operation by contract.
            aggregate = self._aggregate_locker.lock(uow, tenant_id=tenant_id, task_id=task_id)
            run, attempt, execution, subtask = _locate_exact_target(
                aggregate,
                run_id=run_id,
                attempt_id=attempt_id,
                fencing_token=fencing_token,
                runtime_execution_id=runtime_execution_id,
            )
            _validate_assignment_chain(
                aggregate,
                run=run,
                execution=execution,
                received_at=timestamp,
            )
            uow.idempotency.lock(scope, key)
            replay = _existing_replay(uow, scope, key, request_hash)
            if replay is not None:
                return _replay_result(
                    uow,
                    aggregate=aggregate,
                    run=run,
                    attempt=attempt,
                    execution=execution,
                    subtask=subtask,
                    principal=principal,
                    observation=observation,
                    observation_digest=digest,
                    evidence_reference=reference,
                    reason=normalized_reason,
                    request_hash=request_hash,
                    replay=replay,
                )

            # The stricter selector binds the immutable Assignment and cohort
            # only after idempotency has excluded a completed replay.
            uncertain_evidence = _require_complete_parked_projection(
                uow,
                aggregate=aggregate,
                run=run,
                attempt=attempt,
                execution=execution,
                subtask=subtask,
            )
            validate_terminal_observation(
                observation,
                runtime_execution_id=execution.id,
                assignment_id=execution.assignment_id,
                assignment_digest=execution.assignment_digest,
                require_known_terminal=True,
            )
            if timestamp < observation.observed_at.astimezone(timezone.utc):
                raise InvalidTaskTransition("Reconciliation receipt precedes observation")
            _require_independent_conclusion(
                uow,
                execution=execution,
                observation=observation,
                observation_digest=digest,
            )
            cancel_intent_present = _cancel_intent_present(aggregate, execution.id)
            phase = KnownTerminalPhase(observation.phase.value)
            safe_error = _safe_error(observation, cancel_intent_present=cancel_intent_present)
            budget_rejection = (
                BudgetController.exhausted_reason(aggregate.task, now=timestamp)
                if observation.phase is RuntimePhase.SUCCEEDED
                else None
            )
            plan = plan_reconciled_terminal(
                aggregate,
                triggering_run_id=run.id,
                phase=phase,
                cancel_intent_present=cancel_intent_present,
                safe_error=safe_error,
                budget_rejection=budget_rejection,
            )
            previous_task_status = aggregate.task.status
            previous_task_error = aggregate.task.error
            previous_phase = execution.phase
            previous_provider_sequence = execution.provider_sequence
            previous_attempt_status = attempt.status
            previous_run_status = run.status
            previous_subtask_status = subtask.status if subtask is not None else None
            quarantine_output = (
                observation.phase is RuntimePhase.SUCCEEDED
                and plan.requested_target is CoordinationRuntimeDrainTarget.RUNNING
                and plan.effective_target is not CoordinationRuntimeDrainTarget.RUNNING
            )
            evidence = _reconciliation_evidence(
                execution=execution,
                observation=observation,
                digest=digest,
                reference=reference,
                uncertain_evidence=uncertain_evidence,
                received_at=timestamp,
                quarantine_output=quarantine_output,
            )
            reconciled_execution = execution.reconcile_terminal(
                phase=RuntimeExecutionPhase(observation.phase.value),
                provider_sequence=observation.provider_sequence,
                now=timestamp,
            )
            uow.runtimes.add_observation(evidence)
            uow.runtimes.save_execution(reconciled_execution, tenant_id=tenant_id)
            _apply_local_chain(
                run=run,
                attempt=attempt,
                subtask=subtask,
                observation=observation,
                safe_error=safe_error,
                cancel_intent_present=cancel_intent_present,
                quarantine_output=quarantine_output,
                at=timestamp,
            )
            uow.attempts.save(attempt)
            uow.runs.save(run)
            if subtask is not None:
                uow.subtasks.save(subtask)

            # Keep the immutable execution tuple coherent for d2's post-write
            # trigger validation without changing the locked membership.
            applied_aggregate = replace(
                aggregate,
                executions=tuple(
                    reconciled_execution if value.id == execution.id else value
                    for value in aggregate.executions
                ),
            )
            barrier = self._barrier_applier.apply_in_uow(
                uow,
                aggregate=applied_aggregate,
                plan=plan,
                now=timestamp,
                cancel_deadline_window=self._cancel_deadline_window,
                defer_task_save=True,
                application_mode=CoordinatedBarrierApplicationMode.RECONCILED_TERMINAL,
            )
            scheduled = _apply_completion_and_schedule(
                uow,
                aggregate=applied_aggregate,
                run=run,
                observation=observation,
                safe_error=safe_error,
                barrier=barrier,
                scheduler=self._scheduler,
                budget_rejection=budget_rejection,
                quarantine_output=quarantine_output,
                causation_id=uuid5(NAMESPACE_URL, f"{scope}:{key}"),
                at=timestamp,
            )
            resulting_task = applied_aggregate.task
            resolution = TaskResolution.create(
                task_id=task_id,
                action=_resolution_action(observation.phase),
                actor=principal.principal_id,
                reason=normalized_reason,
                previous_status=previous_task_status,
                resulting_status=resulting_task.status,
                previous_error=previous_task_error,
                details={
                    "target_type": "COORDINATED_RUNTIME_EXECUTION",
                    "execution_id": str(execution.id),
                    "run_id": str(run.id),
                    "attempt_id": str(attempt.id),
                    "role": run.role.value,
                    "previous_phase": previous_phase.value,
                    "previous_provider_sequence": previous_provider_sequence,
                    "confirmed_phase": observation.phase.value,
                    "previous_attempt_status": previous_attempt_status.value,
                    "previous_run_status": previous_run_status.value,
                    "previous_subtask_status": (
                        previous_subtask_status.value
                        if previous_subtask_status is not None
                        else None
                    ),
                    "resulting_task_status": resulting_task.status.value,
                    "resulting_run_status": run.status.value,
                    "resulting_attempt_status": attempt.status.value,
                    "resulting_subtask_status": (
                        subtask.status.value if subtask is not None else None
                    ),
                    "effective_drain_target": (
                        barrier.effective_drain.target.value
                        if barrier.effective_drain is not None
                        else None
                    ),
                    "effective_drain_id": (
                        str(barrier.effective_drain.id)
                        if barrier.effective_drain is not None
                        else None
                    ),
                    "effective_drain_reason": (
                        barrier.effective_drain.reason
                        if barrier.effective_drain is not None
                        else None
                    ),
                    "assignment_digest": execution.assignment_digest,
                    "observation_id": observation.observation_id,
                    "observation_digest": digest,
                    "unknown_observation_digest": uncertain_evidence.observation_digest,
                    "scheduled_run_ids": [str(value) for value in scheduled],
                    "output_quarantined": quarantine_output,
                    "cancel_intent_present": cancel_intent_present,
                    "provider_sequence": observation.provider_sequence,
                },
                at=timestamp,
            )
            uow.task_resolutions.add(resolution)
            uow.outbox.add(
                _outcome_event(
                    tenant_id=tenant_id,
                    task_id=task_id,
                    run_id=run.id,
                    attempt_id=attempt.id,
                    execution_id=execution.id,
                    resolution_id=resolution.id,
                    confirmed_phase=observation.phase,
                    previous_phase=previous_phase,
                    effective_drain=barrier.effective_drain,
                    scheduled_run_ids=scheduled,
                    request_hash=request_hash,
                    causation_id=uuid5(NAMESPACE_URL, f"{scope}:{key}"),
                    at=timestamp,
                )
            )
            uow.idempotency.add(
                IdempotencyRecord.create(
                    scope=scope,
                    key=key,
                    request_hash=request_hash,
                    result={
                        "resolution_id": str(resolution.id),
                        "scheduled_run_ids": [str(value) for value in scheduled],
                    },
                )
            )
            if (
                run.role is RunRole.SUPERVISOR
                and resulting_task.status is TaskStatus.COMPLETED
                and self._runtime_memory_service is not None
            ):
                self._runtime_memory_service.capture_completed_task_in_unit_of_work(
                    uow, resulting_task
                )
            uow.commit()
            return CoordinatedRuntimeReconciliationResult(
                reconciled_execution,
                resolution,
                scheduled,
            )


def _validate_command(**values: Any) -> tuple[str, str, str, datetime, str]:
    tenant_id = values["tenant_id"]
    principal = values["principal"]
    observation = values["observation"]
    if type(tenant_id) is not str or not tenant_id.strip() or tenant_id != tenant_id.strip():
        raise InvalidTaskInput("Coordinated reconciliation tenant is invalid")
    for name in ("task_id", "run_id", "attempt_id", "runtime_execution_id"):
        if type(values[name]) is not UUID:
            raise InvalidTaskInput(f"Coordinated reconciliation {name} is invalid")
    if type(values["fencing_token"]) is not int or values["fencing_token"] <= 0:
        raise InvalidTaskInput("Coordinated reconciliation fence is invalid")
    if type(principal) is not PrincipalContext or (
        not principal.authenticated or principal.tenant_id != tenant_id
    ):
        raise AuthorizationDenied(
            "Coordinated reconciliation requires an authenticated same-tenant Principal"
        )
    if Permission.OUTCOME_RECONCILE not in principal.permissions:
        raise AuthorizationDenied("Principal lacks outcome reconciliation permission")
    if type(observation) is not RuntimeObservation:
        raise InvalidTaskInput("Coordinated reconciliation observation is invalid")
    try:
        validate_terminal_observation(
            observation,
            runtime_execution_id=values["runtime_execution_id"],
            assignment_id=UUID(observation.assignment_id),
            assignment_digest=observation.assignment_digest,
            require_known_terminal=True,
        )
    except ValueError as exc:
        raise InvalidTaskInput(
            "Coordinated reconciliation observation identity is invalid"
        ) from exc
    if (
        observation.provider_event_id is not None
        and len(observation.provider_event_id.encode("utf-8")) > 512
    ):
        raise InvalidTaskInput("Reconciliation provider event identity is too long")
    reference = _bounded(values["evidence_reference"], "Evidence reference", 2048)
    reason = _bounded(values["reason"], "Reconciliation reason", 2000)
    key = _bounded(values["idempotency_key"], "Idempotency-Key", 512)
    timestamp = values["received_at"]
    if type(timestamp) is not datetime or timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise InvalidTaskInput("Coordinated reconciliation timestamp must be aware UTC")
    timestamp = timestamp.astimezone(timezone.utc)
    digest = TerminalObservationValidator.digest(observation)
    if values["evidence_digest"] != digest or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise InvalidTaskInput("Evidence digest must equal the canonical observation digest")
    if UUID(observation.runtime_execution_id) != values["runtime_execution_id"]:
        raise InvalidTaskInput("Observation Runtime execution identity does not match")
    return reference, reason, key, timestamp, digest


def _bounded(value: Any, label: str, maximum: int) -> str:
    if type(value) is not str:
        raise InvalidTaskInput(f"{label} is invalid")
    normalized = value.strip()
    if not normalized or len(normalized.encode("utf-8")) > maximum:
        raise InvalidTaskInput(f"{label} must contain 1-{maximum} UTF-8 bytes")
    return normalized


def _locate_exact_target(
    aggregate: CoordinatedRuntimeAggregate,
    *,
    run_id: UUID,
    attempt_id: UUID,
    fencing_token: int,
    runtime_execution_id: UUID,
) -> tuple[TaskRun, TaskAttempt, RuntimeExecution, Subtask | None]:
    runs = [value for value in aggregate.runs if value.id == run_id]
    if len(runs) != 1:
        raise RuntimeExecutionConflict("Coordinated reconciliation Run is ambiguous")
    run = runs[0]
    attempt = aggregate.latest_attempts.get(run.id)
    executions = [value for value in aggregate.executions if value.id == runtime_execution_id]
    if (
        type(attempt) is not TaskAttempt
        or attempt.id != attempt_id
        or attempt.fencing_token != fencing_token
        or len(executions) != 1
    ):
        raise RuntimeExecutionConflict("Coordinated reconciliation ownership is stale")
    execution = executions[0]
    if (
        run.task_id != aggregate.task.id
        or run.runtime_execution_id != execution.id
        or run.runtime_execution_intent_id != execution.id
        or execution.run_id != run.id
        or execution.current_owner_attempt_id != attempt.id
        or execution.current_fencing_token != fencing_token
    ):
        raise RuntimeExecutionConflict("Coordinated reconciliation chain is inconsistent")
    subtask = None
    if run.role is RunRole.EXECUTOR:
        matches = [value for value in aggregate.subtasks if value.id == run.subtask_id]
        if len(matches) != 1:
            raise RuntimeExecutionConflict("Coordinated reconciliation Subtask is ambiguous")
        subtask = matches[0]
    elif run.role is not RunRole.SUPERVISOR or run.subtask_id is not None:
        raise RuntimeExecutionConflict("Coordinated reconciliation role is invalid")
    return run, attempt, execution, subtask


def _validate_assignment_chain(
    aggregate: CoordinatedRuntimeAggregate,
    *,
    run: TaskRun,
    execution: RuntimeExecution,
    received_at: datetime,
) -> None:
    snapshot = aggregate.assignment_snapshots_by_execution.get(execution.id)
    version = aggregate.runtime_versions.get(run.runtime_version_id)
    if (
        snapshot is None
        or version is None
        or snapshot.created_at.astimezone(timezone.utc) > received_at
        or snapshot.assignment_id != execution.assignment_id
        or snapshot.assignment_digest != execution.assignment_digest
        or execution.tenant_id != aggregate.task.tenant_id
        or execution.run_id != run.id
        or execution.runtime_version_id != run.runtime_version_id
        or run.runtime_authority != "managed"
        or run.comparison_mode != "off"
        or run.runtime_version_id != aggregate.cohort.runtime_version_id
    ):
        raise RuntimeExecutionConflict("Coordinated reconciliation Assignment is unavailable")
    try:
        assignment = parse_assignment_payload(snapshot.canonical_payload)
        validate_runtime_assignment_chain(
            assignment,
            tenant_id=aggregate.task.tenant_id,
            task_id=aggregate.task.id,
            run=run,
            execution_id=execution.id,
        )
        if (
            assignment.assignment_digest != execution.assignment_digest
            or assignment.runtime_version_id != str(version.id)
            or assignment.runtime_descriptor_digest
            != RuntimeDescriptor.from_dict(thaw_json(version.descriptor)).digest()
        ):
            raise RuntimeExecutionConflict(
                "Coordinated reconciliation Assignment is incompatible"
            )
        validate_builtin_managed_runtime_version(version)
    except RuntimeExecutionConflict:
        raise
    except (InvalidTaskInput, KeyError, TypeError, ValueError, RuntimeVersionNotFound) as exc:
        raise RuntimeExecutionConflict(
            "Coordinated reconciliation Assignment is invalid"
        ) from exc


def _existing_replay(uow: Any, scope: str, key: str, request_hash: str) -> dict[str, Any] | None:
    record = uow.idempotency.get(scope, key)
    if record is None:
        return None
    if record.request_hash != request_hash:
        raise IdempotencyConflict("Idempotency key was reused with a different request")
    if not isinstance(record.result, Mapping):
        raise RuntimeExecutionConflict(
            "Coordinated reconciliation replay result is invalid"
        )
    try:
        return dict(record.result)
    except (TypeError, ValueError) as exc:
        raise RuntimeExecutionConflict(
            "Coordinated reconciliation replay result is invalid"
        ) from exc


def _replay_result(
    uow: Any,
    *,
    aggregate: CoordinatedRuntimeAggregate,
    run: TaskRun,
    attempt: TaskAttempt,
    execution: RuntimeExecution,
    subtask: Subtask | None,
    principal: PrincipalContext,
    observation: RuntimeObservation,
    observation_digest: str,
    evidence_reference: str,
    reason: str,
    request_hash: str,
    replay: dict[str, Any],
) -> CoordinatedRuntimeReconciliationResult:
    if set(replay) != {"resolution_id", "scheduled_run_ids"}:
        raise RuntimeExecutionConflict("Coordinated reconciliation replay result is invalid")
    resolution_id = _replay_uuid(replay.get("resolution_id"), "resolution")
    resolution = uow.task_resolutions.get(resolution_id)
    expected_detail_keys = {
        "target_type",
        "execution_id",
        "run_id",
        "attempt_id",
        "role",
        "previous_phase",
        "previous_provider_sequence",
        "confirmed_phase",
        "previous_attempt_status",
        "previous_run_status",
        "previous_subtask_status",
        "resulting_task_status",
        "resulting_run_status",
        "resulting_attempt_status",
        "resulting_subtask_status",
        "effective_drain_target",
        "effective_drain_id",
        "effective_drain_reason",
        "assignment_digest",
        "observation_id",
        "observation_digest",
        "unknown_observation_digest",
        "scheduled_run_ids",
        "output_quarantined",
        "cancel_intent_present",
        "provider_sequence",
    }
    if resolution is None or (
        set(resolution.details) != expected_detail_keys
        or resolution.id != resolution_id
        or resolution.task_id != aggregate.task.id
        or resolution.actor != principal.principal_id
        or resolution.reason != reason
        or resolution.action is not _resolution_action(observation.phase)
        or resolution.previous_status is not TaskStatus.RECONCILIATION_REQUIRED
        or resolution.previous_error != "coordination.runtime_reconciliation_required"
        or resolution.resulting_status is not aggregate.task.status
        or resolution.details.get("execution_id") != str(execution.id)
        or resolution.details.get("run_id") != str(run.id)
        or resolution.details.get("attempt_id") != str(attempt.id)
        or resolution.details.get("observation_id") != observation.observation_id
        or resolution.details.get("observation_digest") != observation_digest
        or resolution.details.get("target_type") != "COORDINATED_RUNTIME_EXECUTION"
        or resolution.details.get("role") != run.role.value
        or resolution.details.get("confirmed_phase") != observation.phase.value
        or resolution.details.get("previous_phase")
        not in {
            RuntimeExecutionPhase.LOST.value,
            RuntimeExecutionPhase.OUTCOME_UNKNOWN.value,
        }
        or resolution.details.get("previous_attempt_status")
        != AttemptStatus.OUTCOME_UNKNOWN.value
        or resolution.details.get("previous_run_status")
        != RunStatus.RECONCILIATION_REQUIRED.value
        or resolution.details.get("previous_subtask_status")
        != (
            SubtaskStatus.RECONCILIATION_REQUIRED.value
            if subtask is not None
            else None
        )
        or resolution.details.get("assignment_digest") != execution.assignment_digest
        or resolution.details.get("provider_sequence") != observation.provider_sequence
        or resolution.details.get("resulting_task_status") != aggregate.task.status.value
        or type(resolution.details.get("output_quarantined")) is not bool
        or type(resolution.details.get("cancel_intent_present")) is not bool
        or re.fullmatch(
            r"[0-9a-f]{64}", str(resolution.details.get("unknown_observation_digest"))
        )
        is None
    ):
        raise RuntimeExecutionConflict("Coordinated reconciliation replay resolution differs")
    quarantined = resolution.details.get("output_quarantined") is True
    _validate_replay_evidence(
        uow,
        execution=execution,
        observation=observation,
        observation_digest=observation_digest,
        evidence_reference=evidence_reference,
        unknown_observation_digest=resolution.details.get("unknown_observation_digest"),
        previous_phase=resolution.details.get("previous_phase"),
        previous_provider_sequence=resolution.details.get("previous_provider_sequence"),
        quarantined=quarantined,
        resolution_created_at=resolution.created_at,
    )
    expected_attempt = (
        AttemptStatus.SUCCEEDED
        if observation.phase is RuntimePhase.SUCCEEDED
        else AttemptStatus.CANCELED
        if observation.phase is RuntimePhase.CANCELED
        and resolution.details.get("cancel_intent_present") is True
        else AttemptStatus.FAILED
    )
    expected_run = {
        AttemptStatus.SUCCEEDED: RunStatus.SUCCEEDED,
        AttemptStatus.CANCELED: RunStatus.CANCELED,
        AttemptStatus.FAILED: RunStatus.FAILED,
    }[expected_attempt]
    expected_subtask = {
        AttemptStatus.SUCCEEDED: SubtaskStatus.COMPLETED,
        AttemptStatus.CANCELED: SubtaskStatus.CANCELED,
        AttemptStatus.FAILED: SubtaskStatus.FAILED,
    }[expected_attempt]
    expected_error = (
        None
        if expected_attempt is AttemptStatus.SUCCEEDED
        else _safe_error(
            observation,
            cancel_intent_present=resolution.details.get("cancel_intent_present") is True,
        )
    )
    if (
        execution.phase is not RuntimeExecutionPhase(observation.phase.value)
        or execution.provider_sequence != observation.provider_sequence
        or attempt.status is not expected_attempt
        or run.status is not expected_run
        or (subtask is not None and subtask.status is not expected_subtask)
        or (
            observation.phase is RuntimePhase.SUCCEEDED
            and (
                run.output != (None if quarantined else dict(observation.output or {}))
                or (
                    subtask is not None
                    and subtask.output
                    != (None if quarantined else dict(observation.output or {}))
                )
            )
        )
        or (
            observation.phase is not RuntimePhase.SUCCEEDED
            and (
                run.output is not None
                or (subtask is not None and subtask.output is not None)
            )
        )
        or attempt.error != expected_error
        or run.error != expected_error
        or (subtask is not None and subtask.error != run.error)
        or resolution.details.get("resulting_attempt_status") != attempt.status.value
        or resolution.details.get("resulting_run_status") != run.status.value
        or resolution.details.get("resulting_subtask_status")
        != (subtask.status.value if subtask is not None else None)
    ):
        raise RuntimeExecutionConflict("Coordinated reconciliation replay terminal chain differs")
    _validate_replay_task_projection(
        aggregate.task,
        run=run,
        observation=observation,
        resolution=resolution,
        quarantined=quarantined,
    )
    _validate_accounting_and_quota(uow, aggregate.task, attempt)
    scheduled = _replay_scheduled_run_ids(replay.get("scheduled_run_ids"))
    if resolution.details.get("scheduled_run_ids") != [str(value) for value in scheduled]:
        raise RuntimeExecutionConflict("Coordinated reconciliation replay scheduling differs")
    event_id = uuid5(
        NAMESPACE_URL,
        f"coordinated-runtime-outcome:{aggregate.task.tenant_id}:{request_hash}",
    )
    event = uow.outbox.get(event_id, tenant_id=aggregate.task.tenant_id)
    expected_event = {
        "tenant_id": aggregate.task.tenant_id,
        "task_id": str(aggregate.task.id),
        "run_id": str(run.id),
        "attempt_id": str(attempt.id),
        "runtime_execution_id": str(execution.id),
        "resolution_id": str(resolution.id),
        "previous_phase": resolution.details.get("previous_phase"),
        "confirmed_phase": observation.phase.value,
        "effective_drain_id": resolution.details.get("effective_drain_id"),
        "effective_drain_target": resolution.details.get("effective_drain_target"),
        "effective_drain_reason": resolution.details.get("effective_drain_reason"),
        "scheduled_run_ids": [str(value) for value in scheduled],
    }
    if (
        event is None
        or event.schema_name != "agentmesh.runtime.outcome-reconciled"
        or event.message_id != event_id
        or dict(event.payload) != expected_event
    ):
        raise RuntimeExecutionConflict("Coordinated reconciliation replay event differs")
    drain_id = _replay_uuid(
        resolution.details.get("effective_drain_id"), "effective drain"
    )
    drain = uow.coordination_runtime_drains.get(
        drain_id, tenant_id=aggregate.task.tenant_id, for_update=False
    )
    expected_drain_status = (
        CoordinationRuntimeDrainStatus.DRAINING
        if aggregate.task.status is TaskStatus.RECONCILIATION_REQUIRED
        else CoordinationRuntimeDrainStatus.COMPLETE
    )
    if (
        drain is None
        or drain.id != drain_id
        or drain.status is not expected_drain_status
        or drain.target.value != resolution.details.get("effective_drain_target")
        or drain.reason != resolution.details.get("effective_drain_reason")
    ):
        raise RuntimeExecutionConflict("Coordinated reconciliation replay drain differs")
    if (
        execution.phase
        not in {
            RuntimeExecutionPhase.SUCCEEDED,
            RuntimeExecutionPhase.FAILED,
            RuntimeExecutionPhase.CANCELED,
            RuntimeExecutionPhase.TIMED_OUT,
        }
        or resolution.id != resolution_id
    ):
        raise RuntimeExecutionConflict("Coordinated reconciliation replay projection was lost")
    return CoordinatedRuntimeReconciliationResult(execution, resolution, scheduled)


def _replay_uuid(value: Any, label: str) -> UUID:
    if type(value) is not str:
        raise RuntimeExecutionConflict(
            f"Coordinated reconciliation replay {label} identity is invalid"
        )
    try:
        parsed = UUID(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeExecutionConflict(
            f"Coordinated reconciliation replay {label} identity is invalid"
        ) from exc
    if str(parsed) != value:
        raise RuntimeExecutionConflict(
            f"Coordinated reconciliation replay {label} identity is not canonical"
        )
    return parsed


def _replay_scheduled_run_ids(value: Any) -> tuple[UUID, ...]:
    if type(value) is not list:
        raise RuntimeExecutionConflict(
            "Coordinated reconciliation replay scheduled Runs are invalid"
        )
    parsed = tuple(_replay_uuid(item, "scheduled Run") for item in value)
    if parsed != tuple(sorted(parsed, key=str)) or len(set(parsed)) != len(parsed):
        raise RuntimeExecutionConflict(
            "Coordinated reconciliation replay scheduled Runs are unordered"
        )
    return parsed


def _validate_replay_task_projection(
    task: Any,
    *,
    run: TaskRun,
    observation: RuntimeObservation,
    resolution: TaskResolution,
    quarantined: bool,
) -> None:
    target = resolution.details.get("effective_drain_target")
    reason = resolution.details.get("effective_drain_reason")
    common_empty = task.output is None and task.candidate_output is None
    if task.status is TaskStatus.RECONCILIATION_REQUIRED:
        valid = (
            common_empty
            and task.current_run_id == (run.id if run.role is RunRole.SUPERVISOR else None)
            and task.error == "coordination.runtime_reconciliation_required"
            and task.budget_exhausted_reason is None
        )
    elif task.status is TaskStatus.RUNNING:
        valid = (
            run.role is RunRole.EXECUTOR
            and common_empty
            and task.current_run_id is None
            and task.error is None
            and task.budget_exhausted_reason is None
            and target == CoordinationRuntimeDrainTarget.RUNNING.value
        )
    elif task.status is TaskStatus.COMPLETED:
        valid = (
            run.role is RunRole.SUPERVISOR
            and not quarantined
            and task.current_run_id == run.id
            and task.output == dict(observation.output or {})
            and task.candidate_output is None
            and task.error is None
            and task.budget_exhausted_reason is None
        )
    elif task.status is TaskStatus.FAILED:
        valid = (
            common_empty
            and task.current_run_id == (run.id if run.role is RunRole.SUPERVISOR else None)
            and task.error == reason
            and task.budget_exhausted_reason is None
            and target == CoordinationRuntimeDrainTarget.FAILED.value
        )
    elif task.status is TaskStatus.CANCELED:
        valid = (
            common_empty
            and task.current_run_id == (run.id if run.role is RunRole.SUPERVISOR else None)
            and task.error is None
            and task.budget_exhausted_reason is None
            and target == CoordinationRuntimeDrainTarget.CANCELED.value
        )
    elif task.status is TaskStatus.WAITING_APPROVAL:
        expected_candidate = (
            dict(observation.output or {})
            if run.role is RunRole.SUPERVISOR and not quarantined
            else None
        )
        valid = (
            task.current_run_id is None
            and task.output is None
            and task.candidate_output == expected_candidate
            and task.error == reason
            and task.budget_exhausted_reason == reason
            and target == CoordinationRuntimeDrainTarget.WAITING_APPROVAL.value
        )
    else:
        valid = False
    if not valid:
        raise RuntimeExecutionConflict("Coordinated reconciliation replay Task differs")


def _require_complete_parked_projection(
    uow: Any,
    *,
    aggregate: CoordinatedRuntimeAggregate,
    run: TaskRun,
    attempt: TaskAttempt,
    execution: RuntimeExecution,
    subtask: Subtask | None,
) -> RuntimeObservationEvidence:
    if (
        aggregate.task.execution_mode is not TaskExecutionMode.COORDINATED
        or aggregate.task.status is not TaskStatus.RECONCILIATION_REQUIRED
        or aggregate.task.error != "coordination.runtime_reconciliation_required"
        or aggregate.task.output is not None
        or aggregate.task.candidate_output is not None
        or run.status is not RunStatus.RECONCILIATION_REQUIRED
        or attempt.status is not AttemptStatus.OUTCOME_UNKNOWN
        or execution.phase
        not in {RuntimeExecutionPhase.LOST, RuntimeExecutionPhase.OUTCOME_UNKNOWN}
        or aggregate.boundary_classifications.get(run.id)
        is not CoordinationRuntimeBoundary.RECONCILIATION_EVIDENCE
    ):
        raise RuntimeExecutionConflict("Coordinated reconciliation parked projection is incomplete")
    if run.role is RunRole.EXECUTOR:
        if (
            subtask is None
            or subtask.status is not SubtaskStatus.RECONCILIATION_REQUIRED
            or subtask.current_run_id != run.id
            or aggregate.task.current_run_id is not None
        ):
            raise RuntimeExecutionConflict("Parked Executor projection is incomplete")
    elif (
        subtask is not None
        or aggregate.task.current_run_id != run.id
        or any(
            value.status
            not in {
                SubtaskStatus.COMPLETED,
                SubtaskStatus.FAILED,
                SubtaskStatus.CANCELED,
            }
            for value in aggregate.subtasks
        )
    ):
        raise RuntimeExecutionConflict("Parked Supervisor projection is incomplete")
    drain = aggregate.active_drain
    if (
        type(drain) is not CoordinationRuntimeDrain
        or drain.status is not CoordinationRuntimeDrainStatus.DRAINING
        or drain.tenant_id != aggregate.task.tenant_id
        or drain.task_id != aggregate.task.id
        or drain.target
        not in {
            CoordinationRuntimeDrainTarget.RUNNING,
            CoordinationRuntimeDrainTarget.WAITING_APPROVAL,
            CoordinationRuntimeDrainTarget.FAILED,
            CoordinationRuntimeDrainTarget.CANCELED,
        }
        or not drain.reason.strip()
    ):
        raise RuntimeExecutionConflict("Coordinated reconciliation drain is incomplete")
    _validate_accounting_and_quota(uow, aggregate.task, attempt)
    observations = uow.runtimes.find_observations(
        execution.id, tenant_id=aggregate.task.tenant_id, limit=257, offset=0
    )
    uncertain = [
        value
        for value in observations
        if value.phase in {RuntimeExecutionPhase.LOST, RuntimeExecutionPhase.OUTCOME_UNKNOWN}
        and value.processing_outcome is RuntimeObservationOutcome.APPLIED
        and value.runtime_execution_id == execution.id
        and value.assignment_id == execution.assignment_id
        and value.assignment_digest == execution.assignment_digest
    ]
    if len(uncertain) != 1 or len(observations) >= 257:
        raise RuntimeExecutionConflict("Coordinated reconciliation evidence is ambiguous")
    anchor = uncertain[0]
    unknown_reason = (
        "runtime.lost"
        if execution.phase is RuntimeExecutionPhase.LOST
        else "runtime.outcome_unknown"
    )
    if (
        anchor.phase is not execution.phase
            or anchor.provider_sequence != execution.provider_sequence
        or anchor.safe_summary != unknown_reason
        or anchor.evidence.get("phase") != execution.phase.value
        or anchor.evidence.get("reason") != unknown_reason
        or attempt.error != unknown_reason
        or run.error != unknown_reason
        or run.output is not None
        or (subtask is not None and subtask.error != unknown_reason)
        or (subtask is not None and subtask.output is not None)
    ):
        raise RuntimeExecutionConflict("Coordinated reconciliation parked evidence differs")
    return anchor


def _validate_accounting_and_quota(uow: Any, task: Any, attempt: TaskAttempt) -> None:
    if task.budget is not None:
        if (
            attempt.budget_settlement_source is not BudgetSettlementSource.CONSERVATIVE_ESTIMATE
            or attempt.settled_tokens != attempt.reserved_tokens
            or attempt.settled_cost_micros != attempt.reserved_cost_micros
        ):
            raise RuntimeExecutionConflict("Coordinated reconciliation accounting is incomplete")
    elif (
        attempt.budget_settlement_source is not None
        or attempt.settled_tokens not in {None, 0}
        or attempt.settled_cost_micros not in {None, 0}
    ):
        raise RuntimeExecutionConflict("No-budget reconciliation accounting is inconsistent")
    reservations = uow.quotas.list_reservations_for_attempt(attempt.id, for_update=False)
    if reservations:
        raise RuntimeExecutionConflict("Coordinated reconciliation quota remains reserved")


def _validate_replay_evidence(
    uow: Any,
    *,
    execution: RuntimeExecution,
    observation: RuntimeObservation,
    observation_digest: str,
    evidence_reference: str,
    unknown_observation_digest: Any,
    previous_phase: Any,
    previous_provider_sequence: Any,
    quarantined: bool,
    resolution_created_at: datetime,
) -> None:
    rows = uow.runtimes.find_observations(
        execution.id, tenant_id=execution.tenant_id, limit=257, offset=0
    )
    if len(rows) >= 257:
        raise RuntimeExecutionConflict("Coordinated reconciliation replay evidence is unbounded")
    conclusions = [
        value
        for value in rows
        if value.phase
        in {
            RuntimeExecutionPhase.SUCCEEDED,
            RuntimeExecutionPhase.FAILED,
            RuntimeExecutionPhase.CANCELED,
            RuntimeExecutionPhase.TIMED_OUT,
        }
        and value.processing_outcome
        in {RuntimeObservationOutcome.APPLIED, RuntimeObservationOutcome.RECONCILED}
    ]
    uncertain = [
        value
        for value in rows
        if value.phase in {RuntimeExecutionPhase.LOST, RuntimeExecutionPhase.OUTCOME_UNKNOWN}
        and value.processing_outcome is RuntimeObservationOutcome.APPLIED
    ]
    if len(conclusions) != 1 or len(uncertain) != 1:
        raise RuntimeExecutionConflict("Coordinated reconciliation replay evidence is ambiguous")
    conclusion = conclusions[0]
    anchor = uncertain[0]
    try:
        anchor_phase = RuntimeExecutionPhase(previous_phase)
    except (TypeError, ValueError) as exc:
        raise RuntimeExecutionConflict(
            "Coordinated reconciliation replay unknown phase is invalid"
        ) from exc
    unknown_reason = (
        "runtime.lost"
        if anchor_phase is RuntimeExecutionPhase.LOST
        else "runtime.outcome_unknown"
    )
    expected_anchor_evidence = {
        key: value
        for key, value in {
            "phase": anchor_phase.value,
            "reason": unknown_reason,
            "provider_event_id": anchor.evidence.get("provider_event_id"),
            "snapshot_digest": anchor.evidence.get("snapshot_digest"),
            "provider_sequence": anchor.provider_sequence,
        }.items()
        if value is not None
    }
    expected_evidence = {
        "provider_event_id": observation.provider_event_id,
        "snapshot_digest": observation.snapshot_digest,
        "evidence_reference": evidence_reference,
        "unknown_observation_id": anchor.observation_id,
        "unknown_observation_digest": unknown_observation_digest,
        **(
            {"quarantined_output": dict(observation.output or {})}
            if quarantined
            else {}
        ),
    }
    if (
        conclusion.tenant_id != execution.tenant_id
        or conclusion.runtime_execution_id != execution.id
        or conclusion.observation_id != observation.observation_id
        or conclusion.observation_digest != observation_digest
        or conclusion.phase is not execution.phase
        or conclusion.processing_outcome is not RuntimeObservationOutcome.RECONCILED
        or conclusion.assignment_id != execution.assignment_id
        or conclusion.assignment_digest != execution.assignment_digest
        or conclusion.provider_sequence != observation.provider_sequence
        or conclusion.observed_at.astimezone(timezone.utc)
        != observation.observed_at.astimezone(timezone.utc)
        or conclusion.received_at.astimezone(timezone.utc)
        != resolution_created_at.astimezone(timezone.utc)
        or conclusion.safe_summary != "Operator-confirmed coordinated Runtime outcome"
        or conclusion.provider_event_present
        is not (observation.provider_event_id is not None)
        or dict(conclusion.evidence) != expected_evidence
        or anchor.observation_digest != unknown_observation_digest
        or anchor.tenant_id != execution.tenant_id
        or anchor.runtime_execution_id != execution.id
        or anchor.assignment_id != execution.assignment_id
        or anchor.assignment_digest != execution.assignment_digest
        or anchor.phase is not anchor_phase
        or anchor.processing_outcome is not RuntimeObservationOutcome.APPLIED
        or anchor.provider_sequence != previous_provider_sequence
        or anchor.safe_summary != unknown_reason
        or anchor.provider_event_present
        is not (anchor.evidence.get("provider_event_id") is not None)
        or anchor.observed_at.tzinfo is None
        or anchor.observed_at.utcoffset() != timedelta(0)
        or anchor.received_at.tzinfo is None
        or anchor.received_at.utcoffset() != timedelta(0)
        or anchor.received_at < anchor.observed_at
        or dict(anchor.evidence) != expected_anchor_evidence
    ):
        raise RuntimeExecutionConflict("Coordinated reconciliation replay evidence differs")


def _require_independent_conclusion(
    uow: Any,
    *,
    execution: RuntimeExecution,
    observation: RuntimeObservation,
    observation_digest: str,
) -> None:
    prior = uow.runtimes.prior_observations(
        execution.id,
        tenant_id=execution.tenant_id,
        observation_id=observation.observation_id,
        digest=observation_digest,
    )
    if prior:
        raise RuntimeExecutionConflict("Reconciliation conclusion evidence already exists")
    accepted = []
    for phase in (
        RuntimeExecutionPhase.SUCCEEDED,
        RuntimeExecutionPhase.FAILED,
        RuntimeExecutionPhase.CANCELED,
        RuntimeExecutionPhase.TIMED_OUT,
    ):
        accepted.extend(
            uow.runtimes.accepted_terminal_observations(
                execution.id, tenant_id=execution.tenant_id, phase=phase
            )
        )
    if accepted:
        raise RuntimeExecutionConflict("Runtime already has an accepted terminal conclusion")


def _cancel_intent_present(aggregate: CoordinatedRuntimeAggregate, execution_id: UUID) -> bool:
    return any(
        value.operation_id == f"runtime-cancel:{execution_id}:v1"
        for value in aggregate.lifecycle_operations_by_execution.get(execution_id, ())
    )


def _safe_error(observation: RuntimeObservation, *, cancel_intent_present: bool) -> str | None:
    if observation.phase is RuntimePhase.SUCCEEDED:
        return None
    if observation.phase is RuntimePhase.CANCELED and not cancel_intent_present:
        return "runtime.unrequested_cancellation"
    if observation.error is not None:
        return observation.error.code
    return {
        RuntimePhase.FAILED: "runtime.failed",
        RuntimePhase.CANCELED: "runtime.canceled",
        RuntimePhase.TIMED_OUT: "runtime.timed_out",
    }[observation.phase]


def _reconciliation_evidence(
    *,
    execution: RuntimeExecution,
    observation: RuntimeObservation,
    digest: str,
    reference: str,
    uncertain_evidence: RuntimeObservationEvidence,
    received_at: datetime,
    quarantine_output: bool,
) -> RuntimeObservationEvidence:
    evidence: dict[str, Any] = {
        "provider_event_id": observation.provider_event_id,
        "snapshot_digest": observation.snapshot_digest,
        "evidence_reference": reference,
        "unknown_observation_id": uncertain_evidence.observation_id,
        "unknown_observation_digest": uncertain_evidence.observation_digest,
    }
    if quarantine_output:
        evidence["quarantined_output"] = dict(observation.output or {})
    return RuntimeObservationEvidence(
        id=uuid4(),
        tenant_id=execution.tenant_id,
        runtime_execution_id=execution.id,
        observation_id=observation.observation_id,
        observation_digest=digest,
        assignment_id=execution.assignment_id,
        assignment_digest=execution.assignment_digest,
        provider_sequence=observation.provider_sequence,
        phase=RuntimeExecutionPhase(observation.phase.value),
        observed_at=observation.observed_at.astimezone(timezone.utc),
        received_at=received_at,
        safe_summary="Operator-confirmed coordinated Runtime outcome",
        processing_outcome=RuntimeObservationOutcome.RECONCILED,
        provider_event_present=observation.provider_event_id is not None,
        evidence=MappingProxyType(evidence),
    )


def _apply_local_chain(
    *,
    run: TaskRun,
    attempt: TaskAttempt,
    subtask: Subtask | None,
    observation: RuntimeObservation,
    safe_error: str | None,
    cancel_intent_present: bool,
    quarantine_output: bool,
    at: datetime,
) -> None:
    output = dict(observation.output or {})
    if observation.phase is RuntimePhase.SUCCEEDED:
        attempt.reconcile_runtime_succeeded(at=at)
        if quarantine_output:
            run.reconcile_runtime_succeeded_quarantined(at=at)
            if subtask is not None:
                subtask.reconcile_runtime_succeeded_quarantined(run.id, at=at)
        else:
            run.reconcile_runtime_succeeded(output, at=at)
            if subtask is not None:
                subtask.reconcile_runtime_succeeded(run.id, output, at=at)
    elif observation.phase is RuntimePhase.CANCELED and cancel_intent_present:
        reason = safe_error or "runtime.canceled"
        attempt.reconcile_runtime_canceled(reason, at=at)
        run.reconcile_runtime_canceled(reason, at=at)
        if subtask is not None:
            subtask.reconcile_runtime_canceled(run.id, reason, at=at)
    else:
        reason = safe_error or "runtime.failed"
        attempt.reconcile_runtime_failed(reason, at=at)
        run.reconcile_runtime_failed(reason, at=at)
        if subtask is not None:
            subtask.reconcile_runtime_failed(run.id, reason, at=at)


def _apply_completion_and_schedule(
    uow: Any,
    *,
    aggregate: CoordinatedRuntimeAggregate,
    run: TaskRun,
    observation: RuntimeObservation,
    safe_error: str | None,
    barrier: Any,
    scheduler: CoordinatedScheduler,
    budget_rejection: str | None,
    quarantine_output: bool,
    causation_id: UUID,
    at: datetime,
) -> tuple[UUID, ...]:
    drain = barrier.effective_drain
    if drain is None:
        raise RuntimeExecutionConflict("Reconciled coordinated outcome lost its drain")
    completion = barrier.completion
    if completion in {
        CoordinatedBarrierCompletion.WAIT_ACTIVE,
        CoordinatedBarrierCompletion.WAIT_RECONCILIATION,
    }:
        uow.tasks.save(aggregate.task)
        return ()
    completed = drain.complete(at=at)
    uow.coordination_runtime_drains.save(completed, tenant_id=aggregate.task.tenant_id)
    if run.role is RunRole.SUPERVISOR:
        if (
            observation.phase is RuntimePhase.SUCCEEDED
            and quarantine_output
            and completion is CoordinatedBarrierCompletion.APPLY_WAITING_APPROVAL
        ):
            aggregate.task.reconcile_coordination_supervisor_waiting_quarantined(
                run.id, completed, at=at
            )
            uow.tasks.save(aggregate.task)
            return ()
        if observation.phase is RuntimePhase.SUCCEEDED and completion in {
            CoordinatedBarrierCompletion.APPLY_RUNNING,
            CoordinatedBarrierCompletion.APPLY_WAITING_APPROVAL,
        }:
            aggregate.task.reconcile_coordination_supervisor_succeeded(
                run.id, completed, dict(observation.output or {}), budget_rejection, at=at
            )
        elif completion is CoordinatedBarrierCompletion.APPLY_CANCELED:
            aggregate.task.reconcile_coordination_supervisor_canceled(
                run.id, completed, safe_error or "runtime.canceled", at=at
            )
        else:
            aggregate.task.reconcile_coordination_supervisor_failed(
                run.id, completed, completed.reason, at=at
            )
        uow.tasks.save(aggregate.task)
        return ()
    if completion is CoordinatedBarrierCompletion.APPLY_RUNNING:
        aggregate.task.resume_coordination_after_runtime_reconciliation(completed, at=at)
        uow.tasks.save(aggregate.task)
        return tuple(
            sorted(
                (
                    value.id
                    for value in scheduler.schedule(
                        uow, aggregate.task, at=at, causation_id=causation_id
                    )
                ),
                key=str,
            )
        )
    if completion is CoordinatedBarrierCompletion.APPLY_FAILED:
        aggregate.task.fail_coordination_after_runtime_reconciliation(completed, at=at)
        uow.tasks.save(aggregate.task)
        return ()
    if completion is CoordinatedBarrierCompletion.APPLY_CANCELED:
        aggregate.task.cancel_coordination_after_runtime_reconciliation(completed, at=at)
        uow.tasks.save(aggregate.task)
        return ()
    if completion is CoordinatedBarrierCompletion.APPLY_WAITING_APPROVAL:
        aggregate.task.wait_coordination_after_runtime_reconciliation(completed, at=at)
        uow.tasks.save(aggregate.task)
        return ()
    raise RuntimeExecutionConflict("Coordinated reconciliation completion is invalid")


def _resolution_action(phase: RuntimePhase) -> TaskResolutionAction:
    return {
        RuntimePhase.SUCCEEDED: TaskResolutionAction.RECONCILE_RUNTIME_SUCCEEDED,
        RuntimePhase.FAILED: TaskResolutionAction.RECONCILE_RUNTIME_FAILED,
        RuntimePhase.CANCELED: TaskResolutionAction.RECONCILE_RUNTIME_CANCELED,
        RuntimePhase.TIMED_OUT: TaskResolutionAction.RECONCILE_RUNTIME_TIMED_OUT,
    }[phase]


def _outcome_event(
    *,
    tenant_id: str,
    task_id: UUID,
    run_id: UUID,
    attempt_id: UUID,
    execution_id: UUID,
    resolution_id: UUID,
    confirmed_phase: RuntimePhase,
    previous_phase: RuntimeExecutionPhase,
    effective_drain: CoordinationRuntimeDrain,
    scheduled_run_ids: tuple[UUID, ...],
    request_hash: str,
    causation_id: UUID,
    at: datetime,
) -> MessageEnvelope:
    message_id = uuid5(NAMESPACE_URL, f"coordinated-runtime-outcome:{tenant_id}:{request_hash}")
    return MessageEnvelope(
        schema_name="agentmesh.runtime.outcome-reconciled",
        schema_version=1,
        message_id=message_id,
        tenant_id=tenant_id,
        occurred_at=at,
        producer="agentmesh-coordinated-runtime-reconciler-v1",
        correlation_id=task_id,
        causation_id=causation_id,
        idempotency_key=f"event:{message_id}",
        payload={
            "tenant_id": tenant_id,
            "task_id": str(task_id),
            "run_id": str(run_id),
            "attempt_id": str(attempt_id),
            "runtime_execution_id": str(execution_id),
            "resolution_id": str(resolution_id),
            "previous_phase": previous_phase.value,
            "confirmed_phase": confirmed_phase.value,
            "effective_drain_id": str(effective_drain.id),
            "effective_drain_target": effective_drain.target.value,
            "effective_drain_reason": effective_drain.reason,
            "scheduled_run_ids": [str(value) for value in scheduled_run_ids],
        },
    )
