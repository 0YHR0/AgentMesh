from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from agentmesh.application.memory_runtime_services import RuntimeMemoryService
from agentmesh.application.ports import UnitOfWorkFactory
from agentmesh.application.research_materialization_services import (
    ResearchMaterializationService,
)
from agentmesh.domain.errors import (
    AuthorizationDenied,
    IdempotencyConflict,
    InvalidTaskInput,
    InvalidTaskTransition,
    RuntimeExecutionNotFound,
)
from agentmesh.domain.identity import PrincipalContext
from agentmesh.domain.messaging import IdempotencyRecord, MessageEnvelope
from agentmesh.domain.resolutions import TaskResolution, TaskResolutionAction
from agentmesh.domain.runtime_execution import (
    RuntimeExecution,
    RuntimeExecutionPhase,
    RuntimeObservationEvidence,
    RuntimeObservationOutcome,
)
from agentmesh.domain.tasks import AttemptStatus, RunStatus, TaskStatus
from agentmesh.features import Feature, FeatureGateSet
from agentmesh.runtime_sdk import RuntimeObservation, RuntimePhase, canonical_digest

logger = logging.getLogger(__name__)

_KNOWN_TERMINAL_PHASES = {
    RuntimePhase.SUCCEEDED,
    RuntimePhase.FAILED,
    RuntimePhase.CANCELED,
    RuntimePhase.TIMED_OUT,
}


@dataclass(frozen=True)
class RuntimeOutcomeReconciliationResult:
    execution: RuntimeExecution
    resolution: TaskResolution


class RuntimeOutcomeReconciliationService:
    """Privileged evidence-only convergence for parked managed DIRECT executions."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        tenant_id: str,
        feature_gates: FeatureGateSet,
        runtime_memory_service: RuntimeMemoryService | None = None,
        research_materialization_service: ResearchMaterializationService | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._tenant_id = tenant_id
        self._feature_gates = feature_gates
        self._runtime_memory_service = runtime_memory_service
        self._research_materialization_service = research_materialization_service

    @property
    def tenant_id(self) -> str:
        return self._tenant_id

    def reconcile_outcome(
        self,
        execution_id: UUID,
        *,
        principal: PrincipalContext,
        observation: RuntimeObservation,
        evidence_digest: str,
        evidence_reference: str,
        reason: str,
        idempotency_key: str,
    ) -> RuntimeOutcomeReconciliationResult:
        self._feature_gates.require(Feature.MANAGED_AGENT_RUNTIME)
        self._feature_gates.require(Feature.OUTCOME_RECONCILIATION)
        self._require_principal(principal)
        normalized_reference = evidence_reference.strip()
        normalized_reason = reason.strip()
        normalized_key = idempotency_key.strip()
        if not normalized_reference or len(normalized_reference.encode("utf-8")) > 2048:
            raise InvalidTaskInput("Evidence reference must contain 1-2048 UTF-8 bytes")
        if not normalized_reason or len(normalized_reason.encode("utf-8")) > 2000:
            raise InvalidTaskInput("Reconciliation reason must contain 1-2000 UTF-8 bytes")
        if not normalized_key:
            raise IdempotencyConflict("Idempotency-Key must not be empty")
        if observation.phase not in _KNOWN_TERMINAL_PHASES:
            raise InvalidTaskInput("Reconciliation requires a known terminal observation")
        observation_digest = canonical_digest(observation.to_dict())
        if evidence_digest != observation_digest:
            raise InvalidTaskInput("Evidence digest must equal the canonical observation digest")
        if UUID(observation.runtime_execution_id) != execution_id:
            raise InvalidTaskInput("Observation Runtime execution identity does not match")
        if observation.phase is RuntimePhase.SUCCEEDED:
            if type(observation.output) is not dict or observation.usage:
                raise InvalidTaskInput(
                    "Managed Runtime success requires mapping output and empty usage"
                )
        elif observation.output is not None or observation.output_artifact_refs:
            raise InvalidTaskInput("Non-success Runtime evidence cannot carry output")

        request_hash = canonical_digest(
            {
                "execution_id": str(execution_id),
                "observation": observation.to_dict(),
                "evidence_digest": evidence_digest,
                "evidence_reference": normalized_reference,
                "reason": normalized_reason,
            }
        )
        scope = (
            f"runtime-outcome-reconciliation:{self._tenant_id}:"
            f"{principal.principal_id}:{execution_id}"
        )
        completed_task_id: UUID | None = None
        with self._uow_factory() as uow:
            replay = self._existing_replay(uow, scope, normalized_key, request_hash)
            if replay is not None:
                return self._replay_result(uow, execution_id, replay)

            located = uow.runtimes.get_execution(execution_id, tenant_id=self._tenant_id)
            if located is None:
                raise RuntimeExecutionNotFound("Runtime execution was not found")
            located_run = uow.runs.get(located.run_id)
            if located_run is None:
                raise InvalidTaskTransition("Runtime execution Run linkage was lost")

            task = uow.tasks.get(located_run.task_id, for_update=True)
            run = uow.runs.get(located.run_id, for_update=True)
            attempt = uow.attempts.latest_for_run(located.run_id, for_update=True)
            execution = uow.runtimes.get_execution(
                execution_id, tenant_id=self._tenant_id, for_update=True
            )
            if task is None or task.tenant_id != self._tenant_id:
                raise AuthorizationDenied("Runtime tenant scope denied")
            if run is None or attempt is None or execution is None:
                raise InvalidTaskTransition("Runtime reconciliation linkage was lost")

            uow.idempotency.lock(scope, normalized_key)
            replay = self._existing_replay(uow, scope, normalized_key, request_hash)
            if replay is not None:
                return self._replay_result(uow, execution_id, replay)
            self._require_parked(task, run, attempt, execution)
            self._require_observation_identity(execution, observation)
            self._reconcile_evidence(
                uow,
                execution=execution,
                observation=observation,
                observation_digest=observation_digest,
                evidence_reference=normalized_reference,
            )

            previous_phase = execution.phase
            confirmed_phase = RuntimeExecutionPhase(observation.phase.value)
            reconciled_execution = execution.reconcile_terminal(
                phase=confirmed_phase,
                provider_sequence=observation.provider_sequence,
            )
            previous_status = task.status
            previous_error = task.error
            action, business_reason = self._converge_business_state(
                uow,
                task=task,
                run=run,
                attempt=attempt,
                execution=execution,
                observation=observation,
            )
            resolution = TaskResolution.create(
                task_id=task.id,
                action=action,
                actor=principal.principal_id,
                reason=normalized_reason,
                previous_status=previous_status,
                resulting_status=task.status,
                previous_error=previous_error,
                details={
                    "target_type": "RUNTIME_EXECUTION",
                    "execution_id": str(execution.id),
                    "run_id": str(run.id),
                    "attempt_id": str(attempt.id),
                    "previous_phase": previous_phase.value,
                    "confirmed_phase": confirmed_phase.value,
                    "business_mapping_reason": business_reason,
                    "assignment_digest": execution.assignment_digest,
                    "observation_id": observation.observation_id,
                    "observation_digest": observation_digest,
                    "provider_event_id": observation.provider_event_id,
                    "snapshot_digest": observation.snapshot_digest,
                    "evidence_reference": normalized_reference,
                },
            )
            uow.runtimes.save_execution(reconciled_execution, tenant_id=self._tenant_id)
            uow.tasks.save(task)
            uow.runs.save(run)
            uow.attempts.save(attempt)
            uow.task_resolutions.add(resolution)
            uow.outbox.add(
                MessageEnvelope.domain_event(
                    schema_name="agentmesh.runtime.outcome-reconciled",
                    tenant_id=self._tenant_id,
                    aggregate_id=task.id,
                    causation_id=resolution.id,
                    producer="agentmesh-runtime-reconciler-v1",
                    payload={
                        "tenant_id": self._tenant_id,
                        "task_id": str(task.id),
                        "run_id": str(run.id),
                        "attempt_id": str(attempt.id),
                        "runtime_execution_id": str(execution.id),
                        "resolution_id": str(resolution.id),
                        "confirmed_phase": confirmed_phase.value,
                    },
                )
            )
            uow.idempotency.add(
                IdempotencyRecord.create(
                    scope=scope,
                    key=normalized_key,
                    request_hash=request_hash,
                    result={"resolution_id": str(resolution.id)},
                )
            )
            if self._runtime_memory_service is not None and task.status is TaskStatus.COMPLETED:
                self._runtime_memory_service.capture_completed_task_in_unit_of_work(uow, task)
            uow.commit()
            completed_task_id = task.id if task.status is TaskStatus.COMPLETED else None
            result = RuntimeOutcomeReconciliationResult(reconciled_execution, resolution)

        if completed_task_id is not None and self._research_materialization_service is not None:
            try:
                self._research_materialization_service.materialize_if_ready(
                    completed_task_id, actor=principal.principal_id
                )
            except Exception:
                logger.warning(
                    "Automatic research materialization failed for reconciled Task %s",
                    completed_task_id,
                    exc_info=True,
                )
        return result

    def _require_principal(self, principal: PrincipalContext) -> None:
        if not principal.authenticated or principal.tenant_id != self._tenant_id:
            raise AuthorizationDenied(
                "Runtime outcome reconciliation requires an authenticated tenant Principal"
            )

    @staticmethod
    def _existing_replay(uow: Any, scope: str, key: str, request_hash: str) -> dict | None:
        record = uow.idempotency.get(scope, key)
        if record is None:
            return None
        if record.request_hash != request_hash:
            raise IdempotencyConflict("Idempotency key was reused with a different request")
        return record.result

    def _replay_result(
        self, uow: Any, execution_id: UUID, replay: dict
    ) -> RuntimeOutcomeReconciliationResult:
        execution = uow.runtimes.get_execution(execution_id, tenant_id=self._tenant_id)
        resolution = uow.task_resolutions.get(UUID(str(replay["resolution_id"])))
        if execution is None or resolution is None:
            raise InvalidTaskTransition("Reconciliation idempotency result was lost")
        return RuntimeOutcomeReconciliationResult(execution, resolution)

    @staticmethod
    def _require_parked(task: Any, run: Any, attempt: Any, execution: RuntimeExecution) -> None:
        if (
            task.status is not TaskStatus.RECONCILIATION_REQUIRED
            or run.status is not RunStatus.RECONCILIATION_REQUIRED
            or attempt.status is not AttemptStatus.OUTCOME_UNKNOWN
            or run.runtime_authority != "managed"
            or task.current_run_id != run.id
            or execution.run_id != run.id
            or execution.current_owner_attempt_id != attempt.id
            or execution.current_fencing_token != attempt.fencing_token
            or execution.phase
            not in {RuntimeExecutionPhase.OUTCOME_UNKNOWN, RuntimeExecutionPhase.LOST}
        ):
            raise InvalidTaskTransition(
                "Runtime execution is not a strictly consistent parked managed Run"
            )

    @staticmethod
    def _require_observation_identity(
        execution: RuntimeExecution, observation: RuntimeObservation
    ) -> None:
        if (
            UUID(observation.assignment_id) != execution.assignment_id
            or observation.assignment_digest != execution.assignment_digest
        ):
            raise InvalidTaskInput("Observation assignment identity does not match")

    @staticmethod
    def _reconcile_evidence(
        uow: Any,
        *,
        execution: RuntimeExecution,
        observation: RuntimeObservation,
        observation_digest: str,
        evidence_reference: str,
    ) -> None:
        prior = uow.runtimes.prior_observations(
            execution.id,
            tenant_id=execution.tenant_id,
            observation_id=observation.observation_id,
            digest=observation_digest,
        )
        if any(
            item.observation_id == observation.observation_id
            and item.observation_digest != observation_digest
            for item in prior
        ):
            raise InvalidTaskTransition("Observation identity conflicts with existing evidence")
        exact = next(
            (
                item
                for item in prior
                if item.observation_id == observation.observation_id
                and item.observation_digest == observation_digest
            ),
            None,
        )
        expected_provider = {
            "provider_event_id": observation.provider_event_id,
            "snapshot_digest": observation.snapshot_digest,
        }
        if exact is not None:
            actual_provider = {
                "provider_event_id": exact.evidence.get("provider_event_id"),
                "snapshot_digest": exact.evidence.get("snapshot_digest"),
            }
            if (
                exact.runtime_execution_id != execution.id
                or exact.assignment_id != execution.assignment_id
                or exact.assignment_digest != execution.assignment_digest
                or exact.phase is not RuntimeExecutionPhase(observation.phase.value)
                or actual_provider != expected_provider
                or exact.processing_outcome
                not in {RuntimeObservationOutcome.CONFLICT, RuntimeObservationOutcome.RECONCILED}
            ):
                raise InvalidTaskTransition("Existing Runtime evidence cannot be reconciled")
            if exact.processing_outcome is RuntimeObservationOutcome.CONFLICT:
                uow.runtimes.update_observation_outcome(
                    exact, outcome=RuntimeObservationOutcome.RECONCILED
                )
            return
        uow.runtimes.add_observation(
            RuntimeObservationEvidence(
                id=uuid4(),
                tenant_id=execution.tenant_id,
                runtime_execution_id=execution.id,
                observation_id=observation.observation_id,
                observation_digest=observation_digest,
                assignment_id=execution.assignment_id,
                assignment_digest=execution.assignment_digest,
                provider_sequence=observation.provider_sequence,
                phase=RuntimeExecutionPhase(observation.phase.value),
                observed_at=observation.observed_at.astimezone(timezone.utc),
                received_at=datetime.now(timezone.utc),
                safe_summary="Operator-confirmed Runtime outcome",
                processing_outcome=RuntimeObservationOutcome.RECONCILED,
                provider_event_present=observation.provider_event_id is not None,
                evidence={
                    **expected_provider,
                    "evidence_reference": evidence_reference,
                },
            )
        )

    @staticmethod
    def _converge_business_state(
        uow: Any,
        *,
        task: Any,
        run: Any,
        attempt: Any,
        execution: RuntimeExecution,
        observation: RuntimeObservation,
    ) -> tuple[TaskResolutionAction, str]:
        if observation.phase is RuntimePhase.SUCCEEDED:
            output = dict(observation.output)
            deadline_exceeded = (
                task.budget is not None
                and task.budget.deadline is not None
                and observation.observed_at.astimezone(timezone.utc)
                >= task.budget.deadline.astimezone(timezone.utc)
            )
            run.reconcile_runtime_succeeded(output)
            attempt.reconcile_runtime_succeeded()
            task.reconcile_runtime_succeeded(
                run.id, output, budget_deadline_exceeded=deadline_exceeded
            )
            return (
                TaskResolutionAction.RECONCILE_RUNTIME_SUCCEEDED,
                "budget_deadline_exceeded" if deadline_exceeded else "runtime.confirmed_success",
            )
        if observation.phase is RuntimePhase.CANCELED:
            cancel_intent = uow.runtimes.find_cancel_intent(
                execution.id, tenant_id=execution.tenant_id
            )
            if cancel_intent is not None:
                run.reconcile_runtime_canceled("runtime.reconciled_canceled")
                attempt.reconcile_runtime_canceled("runtime.reconciled_canceled")
                task.reconcile_runtime_canceled(run.id, "runtime.reconciled_canceled")
                return (
                    TaskResolutionAction.RECONCILE_RUNTIME_CANCELED,
                    "runtime.reconciled_canceled",
                )
            reason = "runtime.unrequested_cancellation"
            run.reconcile_runtime_failed(reason)
            attempt.reconcile_runtime_failed(reason)
            task.reconcile_runtime_failed(run.id, reason)
            return TaskResolutionAction.RECONCILE_RUNTIME_CANCELED, reason
        if observation.phase is RuntimePhase.TIMED_OUT:
            reason = "runtime.reconciled_timed_out"
            action = TaskResolutionAction.RECONCILE_RUNTIME_TIMED_OUT
        else:
            reason = "runtime.reconciled_failed"
            action = TaskResolutionAction.RECONCILE_RUNTIME_FAILED
        run.reconcile_runtime_failed(reason)
        attempt.reconcile_runtime_failed(reason)
        task.reconcile_runtime_failed(run.id, reason)
        return action, reason
