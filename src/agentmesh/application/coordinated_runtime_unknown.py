"""Aggregate-locked parking of one coordinated Runtime unknown outcome.

This module is deliberately a transaction-local control-plane command.  It
does not dispatch, reconcile, call an adapter, or open another unit of work.
The service acquires the aggregate lock first, and one commit covers evidence,
Runtime, business projections, accounting, the barrier, and the reconciliation event.
"""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from agentmesh.application.authority_cohorts import (
    validate_builtin_managed_runtime_version,
)
from agentmesh.application.budget_services import BudgetController
from agentmesh.application.coordinated_runtime import (
    CoordinatedRuntimeAggregate,
    CoordinatedRuntimeAggregateLocker,
)
from agentmesh.application.coordinated_runtime_barrier import (
    CoordinatedBarrierApplicationMode,
    CoordinatedBarrierCompletion,
    CoordinatedRuntimeBarrierApplier,
    plan_unknown_outcome,
)
from agentmesh.application.quota_services import QuotaController
from agentmesh.application.runtime_contracts import validate_terminal_observation
from agentmesh.application.runtime_services import (
    classify_locked_observation,
    validate_runtime_assignment_chain,
)
from agentmesh.application.runtime_snapshots import parse_assignment_payload
from agentmesh.domain.budgets import BudgetSettlementSource
from agentmesh.domain.coordination import (
    CoordinationRuntimeBoundary,
    CoordinationRuntimeDrain,
    CoordinationRuntimeDrainStatus,
    CoordinationRuntimeDrainTarget,
    SubtaskStatus,
)
from agentmesh.domain.errors import (
    InvalidTaskInput,
    InvalidTaskTransition,
    RuntimeExecutionConflict,
    RuntimeVersionNotFound,
)
from agentmesh.domain.messaging import MessageEnvelope
from agentmesh.domain.runtime_execution import (
    RuntimeExecution,
    RuntimeExecutionPhase,
    RuntimeLifecycleOperation,
    RuntimeLifecycleStatus,
    RuntimeObservationEvidence,
    RuntimeObservationOutcome,
    RuntimeVersion,
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
from agentmesh.runtime_sdk.canonical import thaw_json
from agentmesh.runtime_sdk.descriptor import RuntimeDescriptor


class CoordinatedUnknownOutcomeKind(str, Enum):
    PARKED = "PARKED"
    DRAINING_ACTIVE = "DRAINING_ACTIVE"
    REPLAY = "REPLAY"


@dataclass(frozen=True)
class CoordinatedUnknownOutcomeResult:
    kind: CoordinatedUnknownOutcomeKind
    tenant_id: str
    task_id: UUID
    run_id: UUID
    attempt_id: UUID
    runtime_execution_id: UUID
    observation_id: str
    observation_digest: str
    task_status: TaskStatus
    run_status: RunStatus
    subtask_status: SubtaskStatus | None
    drain_id: UUID
    drain_target: CoordinationRuntimeDrainTarget
    lifecycle_operation_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.kind) is not CoordinatedUnknownOutcomeKind:
            raise RuntimeExecutionConflict("Unknown-outcome result kind is invalid")
        if type(self.tenant_id) is not str or not self.tenant_id.strip():
            raise RuntimeExecutionConflict("Unknown-outcome result tenant is invalid")
        if any(
            type(value) is not UUID
            for value in (
                self.task_id,
                self.run_id,
                self.attempt_id,
                self.runtime_execution_id,
                self.drain_id,
            )
        ):
            raise RuntimeExecutionConflict("Unknown-outcome result identity is invalid")
        if (
            type(self.observation_id) is not str
            or not self.observation_id.strip()
            or re.fullmatch(r"[0-9a-f]{64}", self.observation_digest) is None
        ):
            raise RuntimeExecutionConflict("Unknown-outcome result observation is invalid")
        if type(self.task_status) is not TaskStatus or type(self.run_status) is not RunStatus:
            raise RuntimeExecutionConflict("Unknown-outcome result status is invalid")
        if self.subtask_status is not None and type(self.subtask_status) is not SubtaskStatus:
            raise RuntimeExecutionConflict("Unknown-outcome result Subtask status is invalid")
        if type(self.drain_target) is not CoordinationRuntimeDrainTarget:
            raise RuntimeExecutionConflict("Unknown-outcome result drain is invalid")
        if any(
            type(value) is not str or not value.strip() for value in self.lifecycle_operation_ids
        ):
            raise RuntimeExecutionConflict("Unknown-outcome lifecycle IDs are invalid")
        if tuple(sorted(self.lifecycle_operation_ids)) != self.lifecycle_operation_ids:
            raise RuntimeExecutionConflict("Unknown-outcome lifecycle IDs are not ordered")


class CoordinatedRuntimeUnknownOutcomeService:
    """Park one canonical LOST/OUTCOME_UNKNOWN observation."""

    def __init__(
        self,
        *,
        uow_factory: Any,
        cancel_deadline_window: timedelta,
        aggregate_locker: CoordinatedRuntimeAggregateLocker | None = None,
        barrier_applier: CoordinatedRuntimeBarrierApplier | None = None,
    ) -> None:
        if type(cancel_deadline_window) is not timedelta or cancel_deadline_window <= timedelta(0):
            raise InvalidTaskInput("Unknown-outcome cancel window is invalid")
        self._uow_factory = uow_factory
        self._cancel_deadline_window = cancel_deadline_window
        self._aggregate_locker = aggregate_locker or CoordinatedRuntimeAggregateLocker()
        self._barrier_applier = barrier_applier or CoordinatedRuntimeBarrierApplier()

    def park_unknown(
        self,
        *,
        tenant_id: str,
        task_id: UUID,
        run_id: UUID,
        attempt_id: UUID,
        fencing_token: int,
        runtime_execution_id: UUID,
        observation: RuntimeObservation,
        received_at: datetime,
        causation_id: UUID,
    ) -> CoordinatedUnknownOutcomeResult:
        received = _validate_command(
            tenant_id=tenant_id,
            task_id=task_id,
            run_id=run_id,
            attempt_id=attempt_id,
            fencing_token=fencing_token,
            runtime_execution_id=runtime_execution_id,
            observation=observation,
            received_at=received_at,
            causation_id=causation_id,
        )
        digest = _observation_digest(observation)
        with self._uow_factory() as uow:
            # This must remain the first repository operation in this command.
            aggregate = self._aggregate_locker.lock(uow, tenant_id=tenant_id, task_id=task_id)
            target = _select_target(
                aggregate,
                tenant_id=tenant_id,
                task_id=task_id,
                run_id=run_id,
                attempt_id=attempt_id,
                fencing_token=fencing_token,
                runtime_execution_id=runtime_execution_id,
                received_at=received,
            )
            run, attempt, execution, snapshot, version, subtask = target
            validate_terminal_observation(
                observation,
                runtime_execution_id=execution.id,
                assignment_id=execution.assignment_id,
                assignment_digest=execution.assignment_digest,
            )
            if observation.phase not in {RuntimePhase.LOST, RuntimePhase.OUTCOME_UNKNOWN}:
                raise InvalidTaskInput("Unknown-outcome observation phase is invalid")
            if received < observation.observed_at.astimezone(timezone.utc):
                raise InvalidTaskTransition("Unknown-outcome receipt precedes observation")
            _validate_target_clock(received, aggregate.task, run, attempt, execution, subtask)

            all_evidence = uow.runtimes.find_observations(
                execution.id, tenant_id=tenant_id, limit=257, offset=0
            )
            prior = uow.runtimes.prior_observations(
                execution.id,
                tenant_id=tenant_id,
                observation_id=observation.observation_id,
                digest=digest,
            )
            exact = [
                value
                for value in prior
                if value.observation_id == observation.observation_id
                and value.observation_digest == digest
            ]
            if any(
                value.observation_id == observation.observation_id
                and value.observation_digest != digest
                for value in prior
            ):
                raise RuntimeExecutionConflict("Unknown-outcome observation ID conflicts")
            if any(
                value.observation_digest == digest
                and value.observation_id != observation.observation_id
                for value in prior
            ):
                raise RuntimeExecutionConflict("Unknown-outcome observation digest conflicts")
            if exact:
                if len(exact) != 1 or len(all_evidence) != 1:
                    raise RuntimeExecutionConflict(
                        "Unknown-outcome replay has contradictory evidence"
                    )
                lifecycle_operation_ids = _validate_parked_projection(
                    aggregate=aggregate,
                    uow=uow,
                    run=run,
                    attempt=attempt,
                    execution=execution,
                    subtask=subtask,
                    evidence=exact[0],
                    observation=observation,
                    digest=digest,
                    cancel_deadline_window=self._cancel_deadline_window,
                )
                return _result(
                    kind=CoordinatedUnknownOutcomeKind.REPLAY,
                    aggregate=aggregate,
                    run=run,
                    attempt=attempt,
                    execution=execution,
                    subtask=subtask,
                    observation=observation,
                    digest=digest,
                    drain=aggregate.active_drain,
                    lifecycle_operation_ids=lifecycle_operation_ids,
                )
            if all_evidence:
                raise RuntimeExecutionConflict("Unknown-outcome execution has prior evidence")
            event_id = uuid5(
                NAMESPACE_URL,
                f"coordinated-runtime-reconciliation:{tenant_id}:{execution.id}:{digest}",
            )
            get_outbox = getattr(uow.outbox, "get", None)
            if callable(get_outbox) and get_outbox(event_id, tenant_id=tenant_id) is not None:
                raise RuntimeExecutionConflict(
                    "Unknown-outcome execution has a partial reconciliation Outbox event"
                )
            if (
                aggregate.task.candidate_output is not None
                or aggregate.task.budget_exhausted_reason is not None
            ):
                raise RuntimeExecutionConflict(
                    "Unknown-outcome active Task carries an incompatible hold projection"
                )
            _require_fresh_projection(
                aggregate,
                run=run,
                attempt=attempt,
                execution=execution,
                subtask=subtask,
            )
            for phase in (
                RuntimeExecutionPhase.SUCCEEDED,
                RuntimeExecutionPhase.FAILED,
                RuntimeExecutionPhase.CANCELED,
                RuntimeExecutionPhase.TIMED_OUT,
            ):
                if uow.runtimes.accepted_terminal_observations(
                    execution.id, tenant_id=tenant_id, phase=phase
                ):
                    raise RuntimeExecutionConflict(
                        "Unknown-outcome execution has a known-terminal anchor"
                    )
            outcome = classify_locked_observation(
                execution,
                prior=prior,
                assignment_id=execution.assignment_id,
                assignment_digest=execution.assignment_digest,
                provider_sequence=observation.provider_sequence,
                attempt_id=attempt.id,
                fencing_token=fencing_token,
                observation_id=observation.observation_id,
                observation_digest=digest,
            )
            if outcome is not RuntimeObservationOutcome.APPLIED:
                raise RuntimeExecutionConflict(f"Unknown-outcome evidence is {outcome.value}")
            plan = plan_unknown_outcome(
                aggregate,
                triggering_run_id=run.id,
                reason=_unknown_reason(observation),
            )
            _preflight_accounting(aggregate, attempt, received)
            BudgetController.settle_attempt(aggregate.task, attempt, (), at=received)
            QuotaController.release_attempt(uow, attempt)
            updated_execution = execution.apply_observation(
                phase=RuntimeExecutionPhase(observation.phase.value),
                provider_sequence=observation.provider_sequence,
                provider_execution_ref=execution.provider_execution_ref,
                provider_generation=execution.provider_generation,
                checkpoint_ref=observation.checkpoint_ref,
                workspace_ref=observation.workspace_ref,
                now=received,
            )
            evidence = RuntimeObservationEvidence(
                id=uuid4(),
                tenant_id=tenant_id,
                runtime_execution_id=execution.id,
                observation_id=observation.observation_id,
                observation_digest=digest,
                assignment_id=execution.assignment_id,
                assignment_digest=execution.assignment_digest,
                provider_sequence=observation.provider_sequence,
                phase=RuntimeExecutionPhase(observation.phase.value),
                observed_at=observation.observed_at.astimezone(timezone.utc),
                received_at=received,
                safe_summary=_unknown_reason(observation),
                processing_outcome=RuntimeObservationOutcome.APPLIED,
                provider_event_present=observation.provider_event_id is not None,
                evidence=MappingProxyType(_safe_unknown_evidence(observation)),
            )
            uow.runtimes.add_observation(evidence)
            uow.runtimes.save_execution(updated_execution, tenant_id=tenant_id)
            attempt.mark_outcome_unknown(_unknown_reason(observation), at=received)
            run.require_runtime_reconciliation(_unknown_reason(observation), at=received)
            if subtask is not None:
                subtask.require_runtime_reconciliation(
                    run.id, _unknown_reason(observation), at=received
                )
            uow.attempts.save(attempt)
            uow.runs.save(run)
            if subtask is not None:
                uow.subtasks.save(subtask)
            barrier = self._barrier_applier.apply_in_uow(
                uow,
                aggregate=replace(
                    aggregate,
                    executions=tuple(
                        updated_execution if value.id == updated_execution.id else value
                        for value in aggregate.executions
                    ),
                ),
                plan=plan,
                now=received,
                cancel_deadline_window=self._cancel_deadline_window,
                defer_task_save=True,
                application_mode=CoordinatedBarrierApplicationMode.UNKNOWN_PARKING,
            )
            drain = barrier.effective_drain
            if drain is None:
                raise RuntimeExecutionConflict("Unknown-outcome parking did not create a drain")
            _apply_task_hold(aggregate.task, run, drain, at=received)
            uow.tasks.save(aggregate.task)
            uow.outbox.add(
                _reconciliation_event(
                    tenant_id=tenant_id,
                    task_id=task_id,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    execution_id=runtime_execution_id,
                    observation_id=observation.observation_id,
                    digest=digest,
                    causation_id=causation_id,
                    at=received,
                    runtime_phase=observation.phase.value,
                    drain=drain,
                    attempt=attempt,
                    task=aggregate.task,
                )
            )
            uow.commit()
            kind = (
                CoordinatedUnknownOutcomeKind.DRAINING_ACTIVE
                if barrier.completion is CoordinatedBarrierCompletion.WAIT_ACTIVE
                else CoordinatedUnknownOutcomeKind.PARKED
            )
            return _result(
                kind=kind,
                aggregate=aggregate,
                run=run,
                attempt=attempt,
                execution=updated_execution,
                subtask=subtask,
                observation=observation,
                digest=digest,
                drain=drain,
                lifecycle_operation_ids=barrier.lifecycle_operation_ids,
            )


def _validate_command(**values: Any) -> datetime:
    if (
        type(values["tenant_id"]) is not str
        or not values["tenant_id"].strip()
        or values["tenant_id"] != values["tenant_id"].strip()
        or any(
            type(values[name]) is not UUID
            for name in ("task_id", "run_id", "attempt_id", "runtime_execution_id", "causation_id")
        )
        or type(values["fencing_token"]) is not int
        or values["fencing_token"] <= 0
        or type(values["observation"]) is not RuntimeObservation
        or type(values["received_at"]) is not datetime
        or values["received_at"].tzinfo is None
        or values["received_at"].utcoffset() is None
        or values["received_at"].utcoffset() != timedelta(0)
    ):
        raise InvalidTaskInput("Unknown-outcome command input is invalid")
    observation = values["observation"]
    if observation.phase not in {RuntimePhase.LOST, RuntimePhase.OUTCOME_UNKNOWN}:
        raise InvalidTaskInput("Unknown-outcome observation phase is invalid")
    if (
        observation.output is not None
        or observation.output_artifact_refs
        or observation.usage
        or observation.governed_action_requests
        or observation.wait_refs
    ):
        raise InvalidTaskInput("Unknown-outcome observation carries forbidden effects")
    return values["received_at"].astimezone(timezone.utc)


def _observation_digest(observation: RuntimeObservation) -> str:
    from agentmesh.application.runtime_contracts import TerminalObservationValidator

    return TerminalObservationValidator.digest(observation)


def _unknown_reason(observation: RuntimeObservation) -> str:
    if observation.phase is RuntimePhase.LOST:
        return "runtime.lost"
    return "runtime.outcome_unknown"


def _safe_unknown_evidence(observation: RuntimeObservation) -> dict[str, Any]:
    return {
        key: value
        for key, value in {
            "phase": observation.phase.value,
            "reason": _unknown_reason(observation),
            "provider_event_id": observation.provider_event_id,
            "snapshot_digest": observation.snapshot_digest,
            "provider_sequence": observation.provider_sequence,
        }.items()
        if value is not None
    }


def _select_target(
    aggregate: CoordinatedRuntimeAggregate,
    *,
    tenant_id: str,
    task_id: UUID,
    run_id: UUID,
    attempt_id: UUID,
    fencing_token: int,
    runtime_execution_id: UUID,
    received_at: datetime,
) -> tuple[Any, ...]:
    task = aggregate.task
    if (
        task.id != task_id
        or task.tenant_id != tenant_id
        or task.execution_mode is not TaskExecutionMode.COORDINATED
        or task.status not in {TaskStatus.RUNNING, TaskStatus.RECONCILIATION_REQUIRED}
    ):
        raise InvalidTaskTransition("Unknown-outcome Task is not running")
    runs = [value for value in aggregate.runs if value.id == run_id]
    if len(runs) != 1:
        raise RuntimeExecutionConflict("Unknown-outcome Run identity is ambiguous")
    run = runs[0]
    if (
        run.runtime_authority != "managed"
        or run.comparison_mode != "off"
        or run.task_id != task.id
        or run.runtime_execution_id != runtime_execution_id
        or run.runtime_execution_intent_id != runtime_execution_id
        or run.runtime_version_id != aggregate.cohort.runtime_version_id
        or run.status not in {RunStatus.RUNNING, RunStatus.RECONCILIATION_REQUIRED}
        or run.role not in {RunRole.EXECUTOR, RunRole.SUPERVISOR}
    ):
        raise RuntimeExecutionConflict("Unknown-outcome target Run is invalid")
    subtask = None
    if run.role is RunRole.EXECUTOR:
        matches = [value for value in aggregate.subtasks if value.id == run.subtask_id]
        if (
            len(matches) != 1
            or matches[0].current_run_id != run.id
            or matches[0].status
            not in {SubtaskStatus.RUNNING, SubtaskStatus.RECONCILIATION_REQUIRED}
        ):
            raise RuntimeExecutionConflict("Unknown-outcome Executor Subtask is invalid")
        subtask = matches[0]
    else:
        if (
            run.subtask_id is not None
            or task.current_run_id != run.id
            or any(
                value.status
                not in {SubtaskStatus.COMPLETED, SubtaskStatus.FAILED, SubtaskStatus.CANCELED}
                for value in aggregate.subtasks
            )
        ):
            raise RuntimeExecutionConflict("Unknown-outcome Supervisor binding is invalid")
    attempt = aggregate.latest_attempts.get(run.id)
    if (
        type(attempt) is not TaskAttempt
        or attempt.id != attempt_id
        or attempt.run_id != run.id
        or attempt.status not in {AttemptStatus.RUNNING, AttemptStatus.OUTCOME_UNKNOWN}
        or attempt.fencing_token != fencing_token
    ):
        raise RuntimeExecutionConflict("Unknown-outcome target Attempt is invalid")
    executions = [value for value in aggregate.executions if value.id == runtime_execution_id]
    if len(executions) != 1:
        raise RuntimeExecutionConflict("Unknown-outcome execution identity is ambiguous")
    execution = executions[0]
    if (
        execution.tenant_id != tenant_id
        or execution.run_id != run.id
        or execution.runtime_version_id != run.runtime_version_id
        or execution.current_owner_attempt_id != attempt.id
        or execution.current_fencing_token != fencing_token
        or execution.phase
        not in {
            RuntimeExecutionPhase.DISPATCHING,
            RuntimeExecutionPhase.ACCEPTED,
            RuntimeExecutionPhase.RUNNING,
            RuntimeExecutionPhase.WAITING_INPUT,
            RuntimeExecutionPhase.WAITING_APPROVAL,
            RuntimeExecutionPhase.PAUSE_REQUESTED,
            RuntimeExecutionPhase.PAUSED,
            RuntimeExecutionPhase.CANCEL_REQUESTED,
            RuntimeExecutionPhase.LOST,
            RuntimeExecutionPhase.OUTCOME_UNKNOWN,
        }
    ):
        raise RuntimeExecutionConflict("Unknown-outcome target execution is invalid")
    active_target = run.status is RunStatus.RUNNING and attempt.status is AttemptStatus.RUNNING
    if (
        active_target
        and run.role is RunRole.EXECUTOR
        and aggregate.boundary_classifications.get(run.id)
        is not CoordinationRuntimeBoundary.CROSSED_ACTIVE
    ):
        raise RuntimeExecutionConflict("Unknown-outcome target is not crossed active")
    snapshot = aggregate.assignment_snapshots_by_execution.get(execution.id)
    version = aggregate.runtime_versions.get(run.runtime_version_id)
    if (
        snapshot is None
        or type(version) is not RuntimeVersion
        or snapshot.created_at.astimezone(timezone.utc) > received_at
    ):
        raise RuntimeExecutionConflict("Unknown-outcome Assignment snapshot is invalid")
    if (
        snapshot.assignment_id != execution.assignment_id
        or snapshot.assignment_digest != execution.assignment_digest
    ):
        raise RuntimeExecutionConflict("Unknown-outcome Assignment snapshot conflicts")
    try:
        assignment = parse_assignment_payload(snapshot.canonical_payload)
        validate_runtime_assignment_chain(
            assignment,
            tenant_id=tenant_id,
            task_id=task_id,
            run=run,
            execution_id=runtime_execution_id,
        )
        if (
            assignment.assignment_digest != execution.assignment_digest
            or assignment.runtime_version_id != str(version.id)
            or assignment.runtime_descriptor_digest
            != RuntimeDescriptor.from_dict(thaw_json(version.descriptor)).digest()
        ):
            raise RuntimeExecutionConflict("Unknown-outcome Assignment is incompatible")
        validate_builtin_managed_runtime_version(version)
    except RuntimeExecutionConflict:
        raise
    except (InvalidTaskInput, KeyError, TypeError, ValueError, RuntimeVersionNotFound) as exc:
        raise RuntimeExecutionConflict("Unknown-outcome Assignment is invalid") from exc
    return run, attempt, execution, snapshot, version, subtask


def _validate_target_clock(
    received: datetime,
    task: Task,
    run: Any,
    attempt: TaskAttempt,
    execution: RuntimeExecution,
    subtask: Any | None,
) -> None:
    times = [
        task.updated_at,
        attempt.heartbeat_at,
        execution.updated_at,
        *(
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
        ),
    ]
    if subtask is not None:
        times.append(subtask.updated_at)
    if any(received < value.astimezone(timezone.utc) for value in times):
        raise InvalidTaskTransition("Unknown-outcome command clock moved backwards")


def _require_fresh_projection(
    aggregate: CoordinatedRuntimeAggregate,
    *,
    run: Any,
    attempt: TaskAttempt,
    execution: RuntimeExecution,
    subtask: Any | None,
) -> None:
    active_phases = {
        RuntimeExecutionPhase.DISPATCHING,
        RuntimeExecutionPhase.ACCEPTED,
        RuntimeExecutionPhase.RUNNING,
        RuntimeExecutionPhase.WAITING_INPUT,
        RuntimeExecutionPhase.WAITING_APPROVAL,
        RuntimeExecutionPhase.PAUSE_REQUESTED,
        RuntimeExecutionPhase.PAUSED,
        RuntimeExecutionPhase.CANCEL_REQUESTED,
    }
    if (
        aggregate.task.status is not TaskStatus.RUNNING
        or run.status is not RunStatus.RUNNING
        or attempt.status is not AttemptStatus.RUNNING
        or execution.phase not in active_phases
        or aggregate.task.output is not None
        or aggregate.task.error is not None
        or aggregate.task.candidate_output is not None
        or aggregate.task.budget_exhausted_reason is not None
        or run.output is not None
        or run.error is not None
        or run.completed_at is not None
        or attempt.error is not None
        or attempt.completed_at is not None
        or attempt.settled_tokens is not None
        or attempt.settled_cost_micros is not None
        or attempt.budget_settlement_source is not None
    ):
        raise RuntimeExecutionConflict("Unknown-outcome active projection is inconsistent")
    if run.role is RunRole.EXECUTOR:
        if (
            subtask is None
            or subtask.status is not SubtaskStatus.RUNNING
            or subtask.current_run_id != run.id
            or subtask.output is not None
            or subtask.error is not None
            or aggregate.task.current_run_id is not None
            or aggregate.boundary_classifications.get(run.id)
            is not CoordinationRuntimeBoundary.CROSSED_ACTIVE
        ):
            raise RuntimeExecutionConflict(
                "Unknown-outcome active Executor projection is inconsistent"
            )
    elif run.role is RunRole.SUPERVISOR:
        if (
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
            raise RuntimeExecutionConflict(
                "Unknown-outcome active Supervisor projection is inconsistent"
            )
    else:
        raise RuntimeExecutionConflict("Unknown-outcome active Run role is invalid")


def _preflight_accounting(
    aggregate: CoordinatedRuntimeAggregate, attempt: TaskAttempt, at: datetime
) -> None:
    task_copy = deepcopy(aggregate.task)
    attempt_copy = deepcopy(attempt)
    BudgetController.settle_attempt(task_copy, attempt_copy, (), at=at)


def _validate_parked_projection(
    *,
    aggregate: CoordinatedRuntimeAggregate,
    uow: Any,
    run: Any,
    attempt: TaskAttempt,
    execution: RuntimeExecution,
    subtask: Any | None,
    evidence: RuntimeObservationEvidence,
    observation: RuntimeObservation,
    digest: str,
    cancel_deadline_window: timedelta,
) -> tuple[str, ...]:
    if (
        evidence.processing_outcome is not RuntimeObservationOutcome.APPLIED
        or evidence.observation_id != observation.observation_id
        or evidence.observation_digest != digest
        or evidence.phase is not RuntimeExecutionPhase(observation.phase.value)
        or evidence.assignment_id != execution.assignment_id
        or evidence.assignment_digest != execution.assignment_digest
        or evidence.provider_sequence != execution.provider_sequence
        or evidence.tenant_id != aggregate.task.tenant_id
        or evidence.runtime_execution_id != execution.id
        or evidence.observed_at.tzinfo is None
        or evidence.observed_at.utcoffset() != timedelta(0)
        or observation.observed_at.tzinfo is None
        or observation.observed_at.utcoffset() != timedelta(0)
        or evidence.observed_at.astimezone(timezone.utc)
        != observation.observed_at.astimezone(timezone.utc)
        or evidence.received_at.tzinfo is None
        or evidence.received_at.utcoffset() != timedelta(0)
        or evidence.received_at.astimezone(timezone.utc)
        < evidence.observed_at.astimezone(timezone.utc)
        or evidence.provider_event_present
        != (observation.provider_event_id is not None)
        or evidence.safe_summary != _unknown_reason(observation)
        or dict(evidence.evidence) != _safe_unknown_evidence(observation)
        or execution.checkpoint_ref != observation.checkpoint_ref
        or execution.workspace_ref != observation.workspace_ref
    ):
        raise RuntimeExecutionConflict("Unknown-outcome replay evidence differs")
    if (
        execution.phase is not evidence.phase
        or attempt.status is not AttemptStatus.OUTCOME_UNKNOWN
        or run.status is not RunStatus.RECONCILIATION_REQUIRED
        or aggregate.task.status is not TaskStatus.RECONCILIATION_REQUIRED
    ):
        raise RuntimeExecutionConflict("Unknown-outcome replay local projection differs")
    if (
        execution.current_owner_attempt_id != attempt.id
        or execution.current_fencing_token != attempt.fencing_token
    ):
        raise RuntimeExecutionConflict("Unknown-outcome replay ownership differs")
    if run.role is RunRole.EXECUTOR:
        if (
            subtask is None
            or subtask.status is not SubtaskStatus.RECONCILIATION_REQUIRED
            or subtask.current_run_id != run.id
            or aggregate.task.current_run_id is not None
        ):
            raise RuntimeExecutionConflict("Unknown-outcome replay Subtask differs")
    elif aggregate.task.current_run_id != run.id:
        raise RuntimeExecutionConflict("Unknown-outcome replay Supervisor differs")
    if (
        attempt.error != _unknown_reason(observation)
        or attempt.completed_at is None
        or attempt.completed_at.astimezone(timezone.utc)
        != evidence.received_at.astimezone(timezone.utc)
        or run.output is not None
        or run.error != _unknown_reason(observation)
        or run.completed_at is not None
    ):
        raise RuntimeExecutionConflict("Unknown-outcome replay business projection differs")
    if subtask is not None and (
        subtask.output is not None
        or subtask.error != _unknown_reason(observation)
        or subtask.updated_at.astimezone(timezone.utc)
        != evidence.received_at.astimezone(timezone.utc)
    ):
        raise RuntimeExecutionConflict("Unknown-outcome replay Subtask projection differs")
    drain = aggregate.active_drain
    expected_drain_id = uuid5(
        NAMESPACE_URL,
        f"coordination-runtime-drain:{aggregate.task.tenant_id}:{aggregate.task.id}",
    )
    created_by_unknown = (
        drain is not None
        and drain.triggering_run_id == run.id
        and drain.target is CoordinationRuntimeDrainTarget.RUNNING
        and drain.reason == "coordination.runtime_reconciliation_required"
        and drain.created_at.astimezone(timezone.utc)
        == evidence.received_at.astimezone(timezone.utc)
    )
    if (
        drain is None
        or (created_by_unknown and drain.id != expected_drain_id)
        or drain.tenant_id != aggregate.task.tenant_id
        or drain.task_id != aggregate.task.id
        or drain.status is not CoordinationRuntimeDrainStatus.DRAINING
        or drain.target
        not in {
            CoordinationRuntimeDrainTarget.RUNNING,
            CoordinationRuntimeDrainTarget.WAITING_APPROVAL,
            CoordinationRuntimeDrainTarget.FAILED,
            CoordinationRuntimeDrainTarget.CANCELED,
        }
        or not drain.reason.strip()
        or not any(value.id == drain.triggering_run_id for value in aggregate.runs)
        or drain.version <= 0
        or drain.created_at.tzinfo is None
        or drain.created_at.utcoffset() != timedelta(0)
        or drain.updated_at.tzinfo is None
        or drain.updated_at.utcoffset() != timedelta(0)
        or drain.updated_at < drain.created_at
        or drain.completed_at is not None
    ):
        raise RuntimeExecutionConflict("Unknown-outcome replay drain differs")
    if aggregate.task.error != "coordination.runtime_reconciliation_required":
        raise RuntimeExecutionConflict("Unknown-outcome replay Task hold differs")
    if aggregate.task.output is not None or aggregate.task.candidate_output is not None:
        raise RuntimeExecutionConflict("Unknown-outcome replay Task output differs")
    if aggregate.task.budget_exhausted_reason is not None:
        raise RuntimeExecutionConflict("Unknown-outcome replay Task budget hold differs")
    if aggregate.task.updated_at.astimezone(timezone.utc) != evidence.received_at.astimezone(
        timezone.utc
    ):
        raise RuntimeExecutionConflict("Unknown-outcome replay Task clock differs")
    if (
        aggregate.task.budget is not None
        and attempt.budget_settlement_source is not BudgetSettlementSource.CONSERVATIVE_ESTIMATE
    ):
        raise RuntimeExecutionConflict("Unknown-outcome replay accounting differs")
    if aggregate.task.budget is not None and (
        attempt.settled_tokens != attempt.reserved_tokens
        or attempt.settled_cost_micros != attempt.reserved_cost_micros
        or aggregate.task.reserved_tokens < 0
        or aggregate.task.reserved_cost_micros < 0
    ):
        raise RuntimeExecutionConflict("Unknown-outcome replay accounting differs")
    if aggregate.task.budget is None and (
        attempt.settled_tokens not in {None, 0} or attempt.settled_cost_micros not in {None, 0}
    ):
        raise RuntimeExecutionConflict("Unknown-outcome replay accounting differs")
    reservations = uow.quotas.list_reservations_for_attempt(attempt.id, for_update=False)
    if any(value.released_at is None for value in reservations):
        raise RuntimeExecutionConflict("Unknown-outcome replay quota remains reserved")
    for phase in (
        RuntimeExecutionPhase.SUCCEEDED,
        RuntimeExecutionPhase.FAILED,
        RuntimeExecutionPhase.CANCELED,
        RuntimeExecutionPhase.TIMED_OUT,
    ):
        if uow.runtimes.accepted_terminal_observations(
            execution.id, tenant_id=aggregate.task.tenant_id, phase=phase
        ):
            raise RuntimeExecutionConflict(
                "Unknown-outcome replay has contradictory terminal evidence"
            )
    get_outbox = getattr(uow.outbox, "get", None)
    if not callable(get_outbox):
        raise RuntimeExecutionConflict("Unknown-outcome replay cannot verify its Outbox event")
    event_id = uuid5(
        NAMESPACE_URL,
        f"coordinated-runtime-reconciliation:{aggregate.task.tenant_id}:{execution.id}:{digest}",
    )
    event = get_outbox(event_id, tenant_id=aggregate.task.tenant_id)
    if event is None or event.schema_name != "agentmesh.runtime.reconciliation.required":
        raise RuntimeExecutionConflict("Unknown-outcome replay Outbox event is missing")
    expected_payload = {
        "tenant_id": aggregate.task.tenant_id,
        "task_id": str(aggregate.task.id),
        "run_id": str(run.id),
        "attempt_id": str(attempt.id),
        "runtime_execution_id": str(execution.id),
        "observation_id": observation.observation_id,
        "observation_digest": digest,
        "runtime_phase": observation.phase.value,
        "reason_code": "coordination.runtime_reconciliation_required",
        "drain_id": str(drain.id),
        "drain_target": drain.target.value,
        "drain_reason": drain.reason,
        "drain_status": drain.status.value,
        "drain_tenant_id": drain.tenant_id,
        "drain_task_id": str(drain.task_id),
        "drain_triggering_run_id": str(drain.triggering_run_id),
        "drain_created_at": drain.created_at.astimezone(timezone.utc).isoformat(),
        "drain_updated_at": drain.updated_at.astimezone(timezone.utc).isoformat(),
        "drain_version": drain.version,
        "drain_completed_at": (
            drain.completed_at.astimezone(timezone.utc).isoformat()
            if drain.completed_at is not None
            else None
        ),
        "attempt_reserved_tokens": attempt.reserved_tokens,
        "attempt_reserved_cost_micros": attempt.reserved_cost_micros,
        "attempt_settled_tokens": attempt.settled_tokens,
        "attempt_settled_cost_micros": attempt.settled_cost_micros,
        "attempt_budget_settlement_source": (
            attempt.budget_settlement_source.value
            if attempt.budget_settlement_source is not None
            else None
        ),
        "task_reserved_tokens": aggregate.task.reserved_tokens,
        "task_reserved_cost_micros": aggregate.task.reserved_cost_micros,
        "task_settled_tokens": aggregate.task.settled_tokens,
        "task_settled_cost_micros": aggregate.task.settled_cost_micros,
    }
    if (
        event.message_id != event_id
        or event.tenant_id != aggregate.task.tenant_id
        or event.correlation_id != aggregate.task.id
        or event.schema_version != 1
        or event.producer != "agentmesh-coordinated-runtime-v1"
        or event.idempotency_key != f"event:{event_id}"
        or event.occurred_at.astimezone(timezone.utc)
        != evidence.received_at.astimezone(timezone.utc)
        or event.payload != expected_payload
    ):
        raise RuntimeExecutionConflict("Unknown-outcome replay Outbox event differs")
    lifecycle_operation_ids: list[str] = []
    expected_lifecycle_ids: set[str] = set()
    stopping_targets = {
        CoordinationRuntimeDrainTarget.WAITING_APPROVAL,
        CoordinationRuntimeDrainTarget.FAILED,
        CoordinationRuntimeDrainTarget.CANCELED,
    }
    for sibling in aggregate.runs:
        if sibling.id == run.id or sibling.subtask_id is None:
            continue
        subtask_matches = [
            value for value in aggregate.subtasks if value.id == sibling.subtask_id
        ]
        if len(subtask_matches) != 1 or subtask_matches[0].current_run_id != sibling.id:
            continue
        boundary = aggregate.boundary_classifications.get(sibling.id)
        if drain.target not in stopping_targets or boundary is not (
            CoordinationRuntimeBoundary.CROSSED_ACTIVE
        ):
            continue
        executions = aggregate.executions_by_run.get(sibling.id, ())
        sibling_attempt = aggregate.latest_attempts.get(sibling.id)
        if (
            len(executions) != 1
            or sibling.role is not RunRole.EXECUTOR
            or sibling.runtime_authority != "managed"
            or sibling.status is not RunStatus.RUNNING
            or sibling_attempt is None
            or sibling_attempt.status is not AttemptStatus.RUNNING
            or subtask_matches[0].status is not SubtaskStatus.RUNNING
            or executions[0].phase is not RuntimeExecutionPhase.CANCEL_REQUESTED
            or executions[0].current_owner_attempt_id != sibling_attempt.id
            or executions[0].current_fencing_token != sibling_attempt.fencing_token
        ):
            raise RuntimeExecutionConflict("Unknown-outcome replay sibling execution differs")
        execution_id = executions[0].id
        operation_id = f"runtime-cancel:{execution_id}:v1"
        rows = aggregate.lifecycle_operations_by_execution.get(execution_id, ())
        cancel_rows = [
            value
            for value in rows
            if value.operation is RuntimeLifecycleOperation.CANCEL
        ]
        if len(rows) != 1 or len(cancel_rows) != 1:
            raise RuntimeExecutionConflict("Unknown-outcome replay lifecycle differs")
        lifecycle = cancel_rows[0]
        expected_deadline = (
            drain.created_at.astimezone(timezone.utc) + cancel_deadline_window
        )
        if (
            lifecycle.operation_id != operation_id
            or lifecycle.tenant_id != aggregate.task.tenant_id
            or lifecycle.runtime_execution_id != execution_id
            or lifecycle.status is not RuntimeLifecycleStatus.REQUESTED
            or lifecycle.deadline.astimezone(timezone.utc) != expected_deadline
            or type(lifecycle.intent_digest) is not str
            or re.fullmatch(r"[0-9a-f]{64}", lifecycle.intent_digest) is None
        ):
            raise RuntimeExecutionConflict("Unknown-outcome replay lifecycle differs")
        expected_lifecycle_ids.add(operation_id)
        lifecycle_operation_ids.append(operation_id)
    if {value.operation_id for value in aggregate.lifecycle_operations} != expected_lifecycle_ids:
        raise RuntimeExecutionConflict("Unknown-outcome replay lifecycle differs")
    return tuple(sorted(lifecycle_operation_ids))


def _apply_task_hold(
    task: Task, run: Any, drain: CoordinationRuntimeDrain, *, at: datetime
) -> None:
    if run.role is RunRole.SUPERVISOR:
        task.require_coordination_supervisor_runtime_reconciliation(run.id, drain, at=at)
    else:
        task.require_coordination_runtime_reconciliation(drain, at=at)


def _reconciliation_event(
    *,
    tenant_id: str,
    task_id: UUID,
    run_id: UUID,
    attempt_id: UUID,
    execution_id: UUID,
    observation_id: str,
    digest: str,
    causation_id: UUID,
    at: datetime,
    runtime_phase: str,
    drain: CoordinationRuntimeDrain,
    attempt: TaskAttempt,
    task: Task,
) -> MessageEnvelope:
    message_id = uuid5(
        NAMESPACE_URL, f"coordinated-runtime-reconciliation:{tenant_id}:{execution_id}:{digest}"
    )
    return MessageEnvelope(
        schema_name="agentmesh.runtime.reconciliation.required",
        schema_version=1,
        message_id=message_id,
        tenant_id=tenant_id,
        occurred_at=at,
        producer="agentmesh-coordinated-runtime-v1",
        correlation_id=task_id,
        causation_id=causation_id,
        idempotency_key=f"event:{message_id}",
        payload={
            "tenant_id": tenant_id,
            "task_id": str(task_id),
            "run_id": str(run_id),
            "attempt_id": str(attempt_id),
            "runtime_execution_id": str(execution_id),
            "observation_id": observation_id,
            "observation_digest": digest,
            "runtime_phase": runtime_phase,
            "reason_code": "coordination.runtime_reconciliation_required",
            "drain_id": str(drain.id),
            "drain_target": drain.target.value,
            "drain_reason": drain.reason,
            "drain_status": drain.status.value,
            "drain_tenant_id": drain.tenant_id,
            "drain_task_id": str(drain.task_id),
            "drain_triggering_run_id": str(drain.triggering_run_id),
            "drain_created_at": drain.created_at.astimezone(timezone.utc).isoformat(),
            "drain_updated_at": drain.updated_at.astimezone(timezone.utc).isoformat(),
            "drain_version": drain.version,
            "drain_completed_at": (
                drain.completed_at.astimezone(timezone.utc).isoformat()
                if drain.completed_at is not None
                else None
            ),
            "attempt_reserved_tokens": attempt.reserved_tokens,
            "attempt_reserved_cost_micros": attempt.reserved_cost_micros,
            "attempt_settled_tokens": attempt.settled_tokens,
            "attempt_settled_cost_micros": attempt.settled_cost_micros,
            "attempt_budget_settlement_source": (
                attempt.budget_settlement_source.value
                if attempt.budget_settlement_source is not None
                else None
            ),
            "task_reserved_tokens": task.reserved_tokens,
            "task_reserved_cost_micros": task.reserved_cost_micros,
            "task_settled_tokens": task.settled_tokens,
            "task_settled_cost_micros": task.settled_cost_micros,
        },
    )


def _result(
    *,
    kind: CoordinatedUnknownOutcomeKind,
    aggregate: CoordinatedRuntimeAggregate,
    run: Any,
    attempt: TaskAttempt,
    execution: RuntimeExecution,
    subtask: Any | None,
    observation: RuntimeObservation,
    digest: str,
    drain: CoordinationRuntimeDrain | None,
    lifecycle_operation_ids: tuple[str, ...] = (),
) -> CoordinatedUnknownOutcomeResult:
    if drain is None:
        raise RuntimeExecutionConflict("Unknown-outcome result has no drain")
    return CoordinatedUnknownOutcomeResult(
        kind=kind,
        tenant_id=aggregate.task.tenant_id,
        task_id=aggregate.task.id,
        run_id=run.id,
        attempt_id=attempt.id,
        runtime_execution_id=execution.id,
        observation_id=observation.observation_id,
        observation_digest=digest,
        task_status=aggregate.task.status,
        run_status=run.status,
        subtask_status=subtask.status if subtask is not None else None,
        drain_id=drain.id,
        drain_target=drain.target,
        lifecycle_operation_ids=tuple(sorted(lifecycle_operation_ids)),
    )


__all__ = [
    "CoordinatedRuntimeUnknownOutcomeService",
    "CoordinatedUnknownOutcomeKind",
    "CoordinatedUnknownOutcomeResult",
]
