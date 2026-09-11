"""Aggregate-locked convergence of one known Runtime terminal observation.

This command is intentionally a closed control-plane boundary.  It consumes a
fully locked coordinated aggregate, records the provider evidence, applies the
local terminal projection and then delegates sibling work to the transaction
local barrier applier.  There is no adapter, worker, admission, or feature-gate
call in this module.
"""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from agentmesh.application.authority_cohorts import AuthorityCohortResolver
from agentmesh.application.budget_services import BudgetController
from agentmesh.application.business_outcomes import KnownTerminalPhase
from agentmesh.application.coordinated_runtime import (
    CoordinatedRuntimeAggregate,
    CoordinatedRuntimeAggregateLocker,
)
from agentmesh.application.coordinated_runtime_barrier import (
    CoordinatedBarrierCompletion,
    CoordinatedRuntimeBarrierApplier,
    plan_known_terminal,
)
from agentmesh.application.coordination_services import CoordinatedScheduler
from agentmesh.application.quota_services import QuotaController
from agentmesh.application.runtime_contracts import (
    TerminalObservationValidator,
    validate_terminal_observation,
)
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
from agentmesh.domain.runtime_execution import (
    RuntimeExecution,
    RuntimeExecutionPhase,
    RuntimeLifecycleOperation,
    RuntimeObservationEvidence,
    RuntimeObservationOutcome,
    RuntimeVersion,
)
from agentmesh.domain.tasks import (
    AttemptStatus,
    RunRole,
    RunStatus,
    TaskAttempt,
    TaskExecutionMode,
    TaskStatus,
)
from agentmesh.runtime_sdk import RuntimeObservation, RuntimePhase
from agentmesh.runtime_sdk.canonical import thaw_json
from agentmesh.runtime_sdk.descriptor import RuntimeDescriptor


class CoordinatedKnownTerminalKind(str, Enum):
    """Closed result vocabulary for d3 convergence."""

    APPLIED = "APPLIED"
    REPLAY = "REPLAY"
    DRAINING_ACTIVE = "DRAINING_ACTIVE"
    DRAINING_RECONCILIATION = "DRAINING_RECONCILIATION"


@dataclass(frozen=True)
class CoordinatedKnownTerminalResult:
    """Stable projection returned after one convergence transaction."""

    kind: CoordinatedKnownTerminalKind
    tenant_id: str
    task_id: UUID
    run_id: UUID
    attempt_id: UUID
    runtime_execution_id: UUID
    observation_id: str
    observation_digest: str
    task_status: TaskStatus
    run_status: RunStatus
    subtask_status: SubtaskStatus
    drain_id: UUID | None = None
    drain_target: CoordinationRuntimeDrainTarget | None = None
    scheduled_run_ids: tuple[UUID, ...] = ()

    def __post_init__(self) -> None:
        if type(self.kind) is not CoordinatedKnownTerminalKind:
            raise RuntimeExecutionConflict("Known-terminal result kind is invalid")
        if type(self.tenant_id) is not str or not self.tenant_id.strip():
            raise RuntimeExecutionConflict("Known-terminal result tenant is invalid")
        if any(
            type(value) is not UUID
            for value in (
                self.task_id,
                self.run_id,
                self.attempt_id,
                self.runtime_execution_id,
            )
        ):
            raise RuntimeExecutionConflict("Known-terminal result identity is invalid")
        if (
            type(self.observation_id) is not str
            or not self.observation_id.strip()
            or type(self.observation_digest) is not str
            or re.fullmatch(r"[0-9a-f]{64}", self.observation_digest) is None
        ):
            raise RuntimeExecutionConflict("Known-terminal result observation is invalid")
        if type(self.task_status) is not TaskStatus or type(self.run_status) is not RunStatus:
            raise RuntimeExecutionConflict("Known-terminal result status is invalid")
        if type(self.subtask_status) is not SubtaskStatus:
            raise RuntimeExecutionConflict("Known-terminal result Subtask status is invalid")
        if self.drain_id is not None and type(self.drain_id) is not UUID:
            raise RuntimeExecutionConflict("Known-terminal result drain is invalid")
        if self.drain_id is None and self.drain_target is not None:
            raise RuntimeExecutionConflict("Known-terminal result drain target is orphaned")
        if any(type(value) is not UUID for value in self.scheduled_run_ids):
            raise RuntimeExecutionConflict("Known-terminal scheduled Run identity is invalid")
        if tuple(sorted(self.scheduled_run_ids, key=str)) != self.scheduled_run_ids:
            raise RuntimeExecutionConflict("Known-terminal scheduled Runs are not ordered")


class CoordinatedRuntimeConvergenceService:
    """Apply one known terminal observation under the coordinated aggregate lock."""

    def __init__(
        self,
        *,
        uow_factory: Any,
        coordinated_scheduler: CoordinatedScheduler,
        cancel_deadline_window: timedelta,
        aggregate_locker: CoordinatedRuntimeAggregateLocker | None = None,
        barrier_applier: CoordinatedRuntimeBarrierApplier | None = None,
    ) -> None:
        if coordinated_scheduler is None or not hasattr(coordinated_scheduler, "schedule"):
            raise InvalidTaskInput("Coordinated convergence scheduler is invalid")
        if (
            type(cancel_deadline_window) is not timedelta
            or cancel_deadline_window <= timedelta(0)
            or cancel_deadline_window > timedelta(days=7)
        ):
            raise InvalidTaskInput("Coordinated convergence cancel window is invalid")
        self._uow_factory = uow_factory
        self._scheduler = coordinated_scheduler
        self._cancel_deadline_window = cancel_deadline_window
        self._aggregate_locker = aggregate_locker or CoordinatedRuntimeAggregateLocker()
        self._barrier_applier = barrier_applier or CoordinatedRuntimeBarrierApplier()

    def apply_known_terminal(
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
    ) -> CoordinatedKnownTerminalResult:
        timestamp = _validate_command(
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
        observation_digest = TerminalObservationValidator.digest(observation)
        with self._uow_factory() as uow:
            # d3's first repository action is always the complete b2 lock.
            aggregate = self._aggregate_locker.lock(uow, tenant_id=tenant_id, task_id=task_id)
            before_task_status = aggregate.task.status
            target = _select_target(
                aggregate,
                tenant_id=tenant_id,
                task_id=task_id,
                run_id=run_id,
                attempt_id=attempt_id,
                fencing_token=fencing_token,
                runtime_execution_id=runtime_execution_id,
                received_at=timestamp,
            )
            _, run, attempt, execution, snapshot, version = target
            validate_terminal_observation(
                observation,
                runtime_execution_id=execution.id,
                assignment_id=execution.assignment_id,
                assignment_digest=execution.assignment_digest,
                require_known_terminal=True,
            )
            if observation.output_artifact_refs:
                raise InvalidTaskInput(
                    "Known-terminal observations cannot carry Artifact references"
                )
            if timestamp < observation.observed_at.astimezone(timezone.utc):
                raise InvalidTaskTransition("Known-terminal receipt precedes observation")
            _validate_monotonic_target_clock(
                timestamp,
                run=run,
                subtask=next(value for value in aggregate.subtasks if value.id == run.subtask_id),
                attempt=attempt,
                execution=execution,
            )
            cancel_intent_present = _validate_cancel_projection(aggregate, execution.id, tenant_id)
            previous = uow.runtimes.prior_observations(
                execution.id,
                tenant_id=tenant_id,
                observation_id=observation.observation_id,
                digest=observation_digest,
            )
            accepted: list[RuntimeObservationEvidence] = []
            for phase_value in (
                RuntimePhase.SUCCEEDED,
                RuntimePhase.FAILED,
                RuntimePhase.CANCELED,
                RuntimePhase.TIMED_OUT,
            ):
                accepted.extend(
                    uow.runtimes.accepted_terminal_observations(
                        execution.id,
                        tenant_id=tenant_id,
                        phase=_runtime_phase(phase_value),
                    )
                )
            phase = _known_terminal_phase(observation.phase)
            safe_error = _safe_error(observation, cancel_intent_present=cancel_intent_present)
            replay = _classify_replay(
                previous,
                accepted,
                uow=uow,
                aggregate=aggregate,
                run=run,
                attempt=attempt,
                subtask=next(value for value in aggregate.subtasks if value.id == run.subtask_id),
                observation=observation,
                observation_digest=observation_digest,
                execution=execution,
                phase=phase,
                safe_error=safe_error,
                cancel_intent_present=cancel_intent_present,
            )
            if replay:
                return _result(
                    kind=CoordinatedKnownTerminalKind.REPLAY,
                    aggregate=aggregate,
                    run=run,
                    attempt=attempt,
                    execution=execution,
                    observation=observation,
                    observation_digest=observation_digest,
                    drain=_replay_drain_projection(
                        uow,
                        aggregate=aggregate,
                        phase=observation.phase,
                        cancel_intent_present=cancel_intent_present,
                    ),
                )

            classifier_outcome = classify_locked_observation(
                execution,
                prior=previous,
                assignment_id=execution.assignment_id,
                assignment_digest=execution.assignment_digest,
                provider_sequence=observation.provider_sequence,
                attempt_id=attempt.id,
                fencing_token=fencing_token,
                observation_id=observation.observation_id,
                observation_digest=observation_digest,
            )
            if classifier_outcome is not RuntimeObservationOutcome.APPLIED:
                raise RuntimeExecutionConflict(
                    f"Known-terminal evidence is {classifier_outcome.value}"
                )
            plan = plan_known_terminal(
                aggregate,
                triggering_run_id=run.id,
                phase=phase,
                cancel_intent_present=cancel_intent_present,
                safe_error=safe_error,
            )
            if plan.completion in {
                CoordinatedBarrierCompletion.APPLY_WAITING_APPROVAL,
                CoordinatedBarrierCompletion.APPLY_CANCELED,
            }:
                raise RuntimeExecutionConflict(
                    "Known-terminal convergence produced an unsupported barrier completion"
                )
            _preflight_accounting(aggregate, attempt, phase, timestamp)
            task_version_before_target_accounting = aggregate.task.version
            if phase is KnownTerminalPhase.SUCCEEDED:
                budget_reason = BudgetController.settle_attempt(
                    aggregate.task, attempt, (), at=timestamp
                )
                if budget_reason is not None:
                    raise RuntimeExecutionConflict(
                        "Known-terminal accounting would require budget waiting"
                    )
            else:
                BudgetController.release_attempt(aggregate.task, attempt, at=timestamp)
            QuotaController.release_attempt(uow, attempt)

            updated_execution = execution.apply_observation(
                phase=_runtime_phase(observation.phase),
                provider_sequence=observation.provider_sequence,
                provider_execution_ref=execution.provider_execution_ref,
                provider_generation=execution.provider_generation,
                checkpoint_ref=observation.checkpoint_ref,
                workspace_ref=observation.workspace_ref,
                now=timestamp,
            )
            evidence = RuntimeObservationEvidence(
                id=uuid4(),
                tenant_id=tenant_id,
                runtime_execution_id=execution.id,
                observation_id=observation.observation_id,
                observation_digest=observation_digest,
                assignment_id=execution.assignment_id,
                assignment_digest=execution.assignment_digest,
                provider_sequence=observation.provider_sequence,
                phase=_runtime_phase(observation.phase),
                observed_at=observation.observed_at.astimezone(timezone.utc),
                received_at=timestamp,
                safe_summary=safe_error,
                processing_outcome=RuntimeObservationOutcome.APPLIED,
                provider_event_present=observation.provider_event_id is not None,
                evidence=MappingProxyType(_safe_evidence(observation)),
            )
            uow.runtimes.add_observation(evidence)
            uow.runtimes.save_execution(updated_execution, tenant_id=tenant_id)
            _apply_target(
                aggregate,
                run=run,
                attempt=attempt,
                phase=phase,
                observation=observation,
                safe_error=safe_error,
                cancel_intent_present=cancel_intent_present,
                at=timestamp,
            )
            uow.attempts.save(attempt)
            uow.runs.save(run)
            subtask = next(value for value in aggregate.subtasks if value.id == run.subtask_id)
            uow.subtasks.save(subtask)

            barrier = self._barrier_applier.apply_in_uow(
                uow,
                aggregate=aggregate,
                plan=plan,
                now=timestamp,
                cancel_deadline_window=self._cancel_deadline_window,
            )
            _apply_completion(
                uow,
                aggregate,
                barrier,
                before_status=before_task_status,
                at=timestamp,
            )
            target_accounting_changed = (
                aggregate.task.version != task_version_before_target_accounting
            )
            scheduled = ()
            if barrier.completion is CoordinatedBarrierCompletion.CONTINUE_SUCCESS:
                if target_accounting_changed:
                    uow.tasks.save(aggregate.task)
                scheduled = tuple(
                    value.id
                    for value in self._scheduler.schedule(
                        uow,
                        aggregate.task,
                        at=timestamp,
                        causation_id=causation_id,
                    )
                )
            elif barrier.completion is CoordinatedBarrierCompletion.WAIT_ACTIVE:
                if target_accounting_changed:
                    uow.tasks.save(aggregate.task)
            uow.commit()
            final_subtask = next(
                value for value in aggregate.subtasks if value.id == run.subtask_id
            )
            result_kind = {
                CoordinatedBarrierCompletion.WAIT_ACTIVE: (
                    CoordinatedKnownTerminalKind.DRAINING_ACTIVE
                ),
                CoordinatedBarrierCompletion.WAIT_RECONCILIATION: (
                    CoordinatedKnownTerminalKind.DRAINING_RECONCILIATION
                ),
            }.get(barrier.completion, CoordinatedKnownTerminalKind.APPLIED)
            return _result(
                kind=result_kind,
                aggregate=aggregate,
                run=run,
                attempt=attempt,
                execution=updated_execution,
                observation=observation,
                observation_digest=observation_digest,
                drain=barrier.effective_drain,
                subtask=final_subtask,
                scheduled_run_ids=scheduled,
            )


def _validate_command(**values: Any) -> datetime:
    tenant_id = values["tenant_id"]
    if (
        type(tenant_id) is not str
        or not tenant_id.strip()
        or tenant_id != tenant_id.strip()
        or any(
            type(values[name]) is not UUID
            for name in (
                "task_id",
                "run_id",
                "attempt_id",
                "runtime_execution_id",
                "causation_id",
            )
        )
        or type(values["fencing_token"]) is not int
        or values["fencing_token"] <= 0
        or type(values["observation"]) is not RuntimeObservation
        or type(values["received_at"]) is not datetime
        or values["received_at"].tzinfo is None
        or values["received_at"].utcoffset() is None
    ):
        raise InvalidTaskInput("Known-terminal convergence input is invalid")
    received_at = values["received_at"].astimezone(timezone.utc)
    if received_at.utcoffset() != timedelta(0):
        raise InvalidTaskInput("Known-terminal receipt must be UTC")
    return received_at


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
) -> tuple[Any, Any, Any, RuntimeExecution, Any, RuntimeVersion]:
    task = aggregate.task
    if (
        task.id != task_id
        or task.tenant_id != tenant_id
        or task.execution_mode is not TaskExecutionMode.COORDINATED
        or task.status
        not in {
            TaskStatus.RUNNING,
            TaskStatus.RECONCILIATION_REQUIRED,
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELED,
        }
    ):
        raise InvalidTaskTransition("Known-terminal Task is not convergent")
    run = next((value for value in aggregate.runs if value.id == run_id), None)
    if run is None or run.role is not RunRole.EXECUTOR:
        raise RuntimeExecutionConflict("Known-terminal Run is not active")
    subtask = next((value for value in aggregate.subtasks if value.id == run.subtask_id), None)
    attempt = aggregate.latest_attempts.get(run.id)
    execution = next(
        (value for value in aggregate.executions if value.id == runtime_execution_id),
        None,
    )
    snapshot = aggregate.assignment_snapshots_by_execution.get(runtime_execution_id)
    version = aggregate.runtime_versions.get(run.runtime_version_id)
    active_target = (
        run.status is RunStatus.RUNNING
        and subtask is not None
        and subtask.status is SubtaskStatus.RUNNING
        and type(attempt) is TaskAttempt
        and attempt.status is AttemptStatus.RUNNING
    )
    if (
        subtask is None
        or subtask.current_run_id != run.id
        or run.runtime_execution_id != runtime_execution_id
        or run.runtime_execution_intent_id != runtime_execution_id
        or type(attempt) is not TaskAttempt
        or attempt.id != attempt_id
        or attempt.fencing_token != fencing_token
        or type(execution) is not RuntimeExecution
        or execution.run_id != run.id
        or execution.current_owner_attempt_id != attempt.id
        or execution.current_fencing_token != fencing_token
        or snapshot is None
        or type(version) is not RuntimeVersion
        or aggregate.cohort.tenant_id != tenant_id
        or aggregate.cohort.task_id != task_id
        or aggregate.cohort.runtime_authority != "managed"
        or aggregate.cohort.runtime_version_id != version.id
    ):
        raise RuntimeExecutionConflict("Known-terminal target projection is invalid")
    if active_target:
        if (
            task.status not in {TaskStatus.RUNNING, TaskStatus.RECONCILIATION_REQUIRED}
            or attempt.started_at.astimezone(timezone.utc) > received_at
            or attempt.heartbeat_at.astimezone(timezone.utc) > received_at
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
            }
            or aggregate.boundary_classifications.get(run.id)
            is not CoordinationRuntimeBoundary.CROSSED_ACTIVE
        ):
            raise RuntimeExecutionConflict("Known-terminal target is not active")
    elif (
        run.status not in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELED}
        or subtask.status
        not in {SubtaskStatus.COMPLETED, SubtaskStatus.FAILED, SubtaskStatus.CANCELED}
        or attempt.status
        not in {
            AttemptStatus.SUCCEEDED,
            AttemptStatus.FAILED,
            AttemptStatus.CANCELED,
            AttemptStatus.LEASE_EXPIRED,
        }
        or execution.phase
        not in {
            RuntimeExecutionPhase.SUCCEEDED,
            RuntimeExecutionPhase.FAILED,
            RuntimeExecutionPhase.CANCELED,
            RuntimeExecutionPhase.TIMED_OUT,
        }
        or aggregate.boundary_classifications.get(run.id)
        is not CoordinationRuntimeBoundary.KNOWN_TERMINAL
    ):
        raise RuntimeExecutionConflict("Known-terminal target is neither active nor replayable")
    if (
        snapshot.assignment_id != execution.assignment_id
        or snapshot.assignment_digest != execution.assignment_digest
    ):
        raise RuntimeExecutionConflict("Known-terminal Assignment snapshot conflicts")
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
            raise RuntimeExecutionConflict("Known-terminal Assignment is incompatible")
    except RuntimeExecutionConflict:
        raise
    except (InvalidTaskInput, KeyError, TypeError, ValueError) as exc:
        raise RuntimeExecutionConflict("Known-terminal Assignment is invalid") from exc
    try:
        AuthorityCohortResolver._validate_builtin_langgraph_v2_version(version)
    except RuntimeVersionNotFound as exc:
        raise RuntimeExecutionConflict("Known-terminal Runtime Version is incompatible") from exc
    return subtask, run, attempt, execution, snapshot, version


def _runtime_phase(phase: RuntimePhase) -> RuntimeExecutionPhase:
    try:
        return RuntimeExecutionPhase(phase.value)
    except ValueError as exc:
        raise InvalidTaskInput("Known-terminal Runtime phase is unsupported") from exc


def _known_terminal_phase(phase: RuntimePhase) -> KnownTerminalPhase:
    try:
        return KnownTerminalPhase(phase.value)
    except ValueError as exc:
        raise InvalidTaskInput("Known-terminal Runtime phase is unsupported") from exc


def _safe_error(observation: RuntimeObservation, *, cancel_intent_present: bool) -> str | None:
    if observation.phase is RuntimePhase.SUCCEEDED:
        return None
    if observation.phase is RuntimePhase.CANCELED and not cancel_intent_present:
        return "runtime.unrequested_cancellation"
    if observation.error is None:
        return {
            RuntimePhase.FAILED: "runtime.failed",
            RuntimePhase.TIMED_OUT: "runtime.timed_out",
            RuntimePhase.CANCELED: "runtime.canceled",
        }[observation.phase]
    code = observation.error.code.strip()
    if not code or len(code) > 128 or any(ord(char) < 32 or ord(char) == 127 for char in code):
        raise InvalidTaskInput("Known-terminal provider error code is invalid")
    return code


def _safe_evidence(observation: RuntimeObservation) -> dict[str, Any]:
    return {
        key: value
        for key, value in {
            "provider_event_id": observation.provider_event_id,
            "snapshot_digest": observation.snapshot_digest,
            "provider_sequence": observation.provider_sequence,
        }.items()
        if value is not None
    }


def _validate_monotonic_target_clock(
    received_at: datetime,
    *,
    run: Any,
    subtask: Any,
    attempt: TaskAttempt,
    execution: RuntimeExecution,
) -> None:
    changed_at = [
        subtask.updated_at,
        attempt.heartbeat_at,
        attempt.started_at,
        execution.updated_at,
        run.queued_at,
        run.started_at,
        run.pause_requested_at,
        run.paused_at,
        run.resumed_at,
        run.completed_at,
    ]
    if any(
        value is not None and received_at < value.astimezone(timezone.utc) for value in changed_at
    ):
        raise InvalidTaskTransition("Known-terminal receipt precedes target state")


def _validate_cancel_projection(
    aggregate: CoordinatedRuntimeAggregate, execution_id: UUID, tenant_id: str
) -> bool:
    rows = [
        row
        for row in aggregate.lifecycle_operations_by_execution.get(execution_id, ())
        if row.operation is RuntimeLifecycleOperation.CANCEL
    ]
    if len(rows) > 1 or any(
        row.operation_id != f"runtime-cancel:{execution_id}:v1"
        or row.tenant_id != tenant_id
        or row.runtime_execution_id != execution_id
        for row in rows
    ):
        raise RuntimeExecutionConflict("Known-terminal cancel evidence is ambiguous")
    return bool(rows)


def _classify_replay(
    previous: list[RuntimeObservationEvidence],
    accepted: list[RuntimeObservationEvidence],
    *,
    uow: Any,
    aggregate: CoordinatedRuntimeAggregate,
    run: Any,
    attempt: TaskAttempt,
    subtask: Any,
    observation: RuntimeObservation,
    observation_digest: str,
    execution: RuntimeExecution,
    phase: KnownTerminalPhase,
    safe_error: str | None,
    cancel_intent_present: bool,
) -> bool:
    matching = [
        value
        for value in previous
        if value.observation_id == observation.observation_id
        and value.observation_digest == observation_digest
    ]
    if any(
        value.observation_id == observation.observation_id
        and value.observation_digest != observation_digest
        for value in previous
    ):
        raise RuntimeExecutionConflict("Known-terminal observation identity conflicts")
    if matching:
        if (
            len(matching) != 1
            or matching[0].processing_outcome is not RuntimeObservationOutcome.APPLIED
        ):
            raise RuntimeExecutionConflict("Known-terminal replay evidence is ambiguous")
        if accepted and all(value.id != matching[0].id for value in accepted):
            raise RuntimeExecutionConflict("Known-terminal replay projection is incomplete")
        _validate_replay_projection(
            uow,
            aggregate,
            run=run,
            attempt=attempt,
            subtask=subtask,
            execution=execution,
            observation=observation,
            phase=phase,
            safe_error=safe_error,
            cancel_intent_present=cancel_intent_present,
        )
        return True
    if accepted:
        raise RuntimeExecutionConflict("A different known-terminal observation is already applied")
    return False


def _validate_replay_projection(
    uow: Any,
    aggregate: CoordinatedRuntimeAggregate,
    *,
    run: Any,
    attempt: TaskAttempt,
    subtask: Any,
    execution: RuntimeExecution,
    observation: RuntimeObservation,
    phase: KnownTerminalPhase,
    safe_error: str | None,
    cancel_intent_present: bool,
) -> None:
    expected_execution_phase = _runtime_phase(observation.phase)
    if (
        execution.phase is not expected_execution_phase
        or execution.provider_sequence != observation.provider_sequence
    ):
        raise RuntimeExecutionConflict("Known-terminal replay Runtime projection differs")
    expected_cancel = phase is KnownTerminalPhase.CANCELED and cancel_intent_present
    if phase is KnownTerminalPhase.SUCCEEDED:
        if (
            attempt.status is not AttemptStatus.SUCCEEDED
            or run.status is not RunStatus.SUCCEEDED
            or subtask.status is not SubtaskStatus.COMPLETED
            or run.output != observation.output
            or subtask.output != observation.output
            or run.error is not None
            or subtask.error is not None
        ):
            raise RuntimeExecutionConflict("Known-terminal replay success projection differs")
        expected_budget = BudgetSettlementSource.ACTUAL
    elif expected_cancel:
        if (
            attempt.status is not AttemptStatus.CANCELED
            or run.status is not RunStatus.CANCELED
            or subtask.status is not SubtaskStatus.CANCELED
        ):
            raise RuntimeExecutionConflict("Known-terminal replay cancel projection differs")
        expected_budget = BudgetSettlementSource.RELEASED
    else:
        expected_error = safe_error or "runtime.failed"
        if (
            attempt.status is not AttemptStatus.FAILED
            or run.status is not RunStatus.FAILED
            or subtask.status is not SubtaskStatus.FAILED
            or run.error != expected_error
            or subtask.error != expected_error
            or run.output is not None
            or subtask.output is not None
        ):
            raise RuntimeExecutionConflict("Known-terminal replay failure projection differs")
        expected_budget = BudgetSettlementSource.RELEASED
    if aggregate.task.budget is not None:
        if attempt.budget_settlement_source is not expected_budget:
            raise RuntimeExecutionConflict("Known-terminal replay accounting differs")
        if aggregate.task.reserved_tokens != 0 or aggregate.task.reserved_cost_micros != 0:
            raise RuntimeExecutionConflict("Known-terminal replay budget remains reserved")
    reservations = uow.quotas.list_reservations_for_attempt(attempt.id, for_update=False)
    if any(value.released_at is None for value in reservations):
        raise RuntimeExecutionConflict("Known-terminal replay quota remains reserved")


def _replay_drain_projection(
    uow: Any,
    *,
    aggregate: CoordinatedRuntimeAggregate,
    phase: RuntimePhase,
    cancel_intent_present: bool,
) -> CoordinationRuntimeDrain | None:
    drain_id = uuid5(
        NAMESPACE_URL,
        f"coordination-runtime-drain:{aggregate.task.tenant_id}:{aggregate.task.id}",
    )
    drain = aggregate.active_drain
    if drain is None:
        drain = uow.coordination_runtime_drains.get(
            drain_id, tenant_id=aggregate.task.tenant_id, for_update=False
        )
    if drain is None and phase is RuntimePhase.SUCCEEDED:
        if aggregate.task.status is not TaskStatus.RUNNING:
            raise RuntimeExecutionConflict("Known-terminal replay Task status differs")
        return None
    if drain is None:
        raise RuntimeExecutionConflict("Known-terminal replay drain projection is missing")
    expected_target = (
        CoordinationRuntimeDrainTarget.CANCELED
        if phase is RuntimePhase.CANCELED and cancel_intent_present
        else CoordinationRuntimeDrainTarget.RUNNING
        if phase is RuntimePhase.SUCCEEDED
        else CoordinationRuntimeDrainTarget.FAILED
    )
    if drain.target is not expected_target:
        raise RuntimeExecutionConflict("Known-terminal replay drain target differs")
    if drain.status is CoordinationRuntimeDrainStatus.DRAINING:
        if aggregate.task.status not in {
            TaskStatus.RUNNING,
            TaskStatus.RECONCILIATION_REQUIRED,
        }:
            raise RuntimeExecutionConflict("Known-terminal replay Task hold differs")
    elif drain.target is CoordinationRuntimeDrainTarget.FAILED:
        if aggregate.task.status is not TaskStatus.FAILED:
            raise RuntimeExecutionConflict("Known-terminal replay Task failure differs")
    return drain


def _preflight_accounting(
    aggregate: CoordinatedRuntimeAggregate,
    attempt: Any,
    phase: KnownTerminalPhase,
    at: datetime,
) -> None:
    task_copy = deepcopy(aggregate.task)
    attempt_copy = deepcopy(attempt)
    if phase is KnownTerminalPhase.SUCCEEDED:
        budget_reason = BudgetController.settle_attempt(task_copy, attempt_copy, (), at=at)
        if budget_reason is not None:
            raise RuntimeExecutionConflict("Known-terminal accounting would require budget waiting")
    else:
        BudgetController.release_attempt(task_copy, attempt_copy, at=at)


def _apply_target(
    aggregate: CoordinatedRuntimeAggregate,
    *,
    run: Any,
    attempt: Any,
    phase: KnownTerminalPhase,
    observation: RuntimeObservation,
    safe_error: str | None,
    cancel_intent_present: bool,
    at: datetime,
) -> None:
    output = observation.output if phase is KnownTerminalPhase.SUCCEEDED else None
    if phase is KnownTerminalPhase.SUCCEEDED:
        attempt.succeed(at=at)
        run.succeed(output, at=at)
        subtask = next(value for value in aggregate.subtasks if value.id == run.subtask_id)
        subtask.complete(run.id, output, at=at)
    elif phase is KnownTerminalPhase.CANCELED and cancel_intent_present:
        attempt.cancel(at=at)
        run.cancel(at=at)
        subtask = next(value for value in aggregate.subtasks if value.id == run.subtask_id)
        subtask.cancel(at=at)
    else:
        attempt.fail(safe_error or "runtime.failed", at=at)
        run.fail(safe_error or "runtime.failed", at=at)
        subtask = next(value for value in aggregate.subtasks if value.id == run.subtask_id)
        subtask.fail(run.id, safe_error or "runtime.failed", at=at)


def _apply_completion(
    uow: Any,
    aggregate: CoordinatedRuntimeAggregate,
    barrier: Any,
    *,
    before_status: TaskStatus,
    at: datetime,
) -> None:
    drain = barrier.effective_drain
    if barrier.completion is CoordinatedBarrierCompletion.WAIT_RECONCILIATION and drain is not None:
        if aggregate.task.status is TaskStatus.RUNNING:
            aggregate.task.require_coordination_runtime_reconciliation(drain, at=at)
            uow.tasks.save(aggregate.task)
    elif barrier.completion is CoordinatedBarrierCompletion.APPLY_FAILED and drain is not None:
        completed = drain.complete(at=at)
        uow.coordination_runtime_drains.save(completed, tenant_id=aggregate.task.tenant_id)
        if before_status is TaskStatus.RECONCILIATION_REQUIRED:
            aggregate.task.fail_coordination_after_runtime_reconciliation(completed, at=at)
        else:
            aggregate.task.fail_coordination(completed.reason, at=at)
        uow.tasks.save(aggregate.task)
    elif barrier.completion is CoordinatedBarrierCompletion.APPLY_RUNNING and drain is not None:
        completed = drain.complete(at=at)
        uow.coordination_runtime_drains.save(completed, tenant_id=aggregate.task.tenant_id)
        if before_status is TaskStatus.RECONCILIATION_REQUIRED:
            aggregate.task.resume_coordination_after_runtime_reconciliation(completed, at=at)
        elif before_status is not TaskStatus.RUNNING:
            raise RuntimeExecutionConflict(
                "Running drain completion requires a running or reconciliation Task"
            )
        uow.tasks.save(aggregate.task)


def _result(
    *,
    kind: CoordinatedKnownTerminalKind,
    aggregate: CoordinatedRuntimeAggregate,
    run: Any,
    attempt: Any,
    execution: RuntimeExecution,
    observation: RuntimeObservation,
    observation_digest: str,
    drain: CoordinationRuntimeDrain | None = None,
    subtask: Any | None = None,
    scheduled_run_ids: tuple[UUID, ...] = (),
) -> CoordinatedKnownTerminalResult:
    if subtask is None:
        subtask = next(value for value in aggregate.subtasks if value.id == run.subtask_id)
    return CoordinatedKnownTerminalResult(
        kind=kind,
        tenant_id=aggregate.task.tenant_id,
        task_id=aggregate.task.id,
        run_id=run.id,
        attempt_id=attempt.id,
        runtime_execution_id=execution.id,
        observation_id=observation.observation_id,
        observation_digest=observation_digest,
        task_status=aggregate.task.status,
        run_status=run.status,
        subtask_status=subtask.status,
        drain_id=drain.id if drain is not None else None,
        drain_target=drain.target if drain is not None else None,
        scheduled_run_ids=tuple(sorted(scheduled_run_ids, key=str)),
    )


__all__ = [
    "CoordinatedKnownTerminalKind",
    "CoordinatedKnownTerminalResult",
    "CoordinatedRuntimeConvergenceService",
]
