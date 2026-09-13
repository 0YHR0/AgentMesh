from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from agentmesh.application.business_outcomes import (
    AccountingDisposition,
    BusinessOutcomeApplier,
    KnownTerminalPhase,
    ProgressionContext,
)
from agentmesh.application.memory_runtime_services import RuntimeMemoryService
from agentmesh.application.ports import UnitOfWorkFactory
from agentmesh.application.research_materialization_services import (
    ResearchMaterializationService,
)
from agentmesh.application.runtime_contracts import validate_terminal_observation
from agentmesh.domain.budgets import BudgetSettlementSource
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
from agentmesh.domain.tasks import (
    AttemptStatus,
    RunRole,
    RunStatus,
    TaskExecutionMode,
    TaskStatus,
)
from agentmesh.features import Feature, FeatureGateSet
from agentmesh.runtime_sdk import RuntimeObservation, RuntimePhase, canonical_digest

logger = logging.getLogger(__name__)

_KNOWN_TERMINAL_PHASES = {
    RuntimePhase.SUCCEEDED,
    RuntimePhase.FAILED,
    RuntimePhase.CANCELED,
    RuntimePhase.TIMED_OUT,
}


class _ParkedConvergence(str, Enum):
    ACTIVE_DIRECT = "ACTIVE_DIRECT"
    ACTIVE_REVIEWED_EXECUTOR = "ACTIVE_REVIEWED_EXECUTOR"
    ACTIVE_REVIEWED_REVIEWER = "ACTIVE_REVIEWED_REVIEWER"
    CANCELED_RUNTIME_ONLY = "CANCELED_RUNTIME_ONLY"


@dataclass(frozen=True)
class RuntimeOutcomeReconciliationResult:
    execution: RuntimeExecution
    resolution: TaskResolution


class RuntimeOutcomeReconciliationService:
    """Privileged evidence-only convergence for parked managed executions."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        tenant_id: str,
        feature_gates: FeatureGateSet,
        runtime_memory_service: RuntimeMemoryService | None = None,
        research_materialization_service: ResearchMaterializationService | None = None,
        business_outcome_applier: BusinessOutcomeApplier | None = None,
        executor_agent_id: str = "demo-agent",
        reviewer_agent_id: str = "demo-reviewer",
    ) -> None:
        self._uow_factory = uow_factory
        self._tenant_id = tenant_id
        self._feature_gates = feature_gates
        self._runtime_memory_service = runtime_memory_service
        self._research_materialization_service = research_materialization_service
        self._business_outcome_applier = business_outcome_applier or BusinessOutcomeApplier(
            executor_agent_id=executor_agent_id,
            reviewer_agent_id=reviewer_agent_id,
        )

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
        # Validate the full terminal contract before calculating request
        # identity or opening a UoW.  This is intentionally shared with
        # managed dispatch finalization so reconciliation cannot accept a
        # shape that ordinary execution would reject.
        # This first pass is deliberately shape-only: the expected
        # Assignment identity is not known until the persisted execution is
        # loaded below.  The second pass binds it to that immutable snapshot.
        validate_terminal_observation(
            observation,
            runtime_execution_id=execution_id,
            assignment_id=UUID(observation.assignment_id),
            assignment_digest=observation.assignment_digest,
            require_known_terminal=True,
        )
        if (
            observation.provider_event_id is not None
            and len(observation.provider_event_id.encode("utf-8")) > 512
        ):
            raise InvalidTaskInput(
                "Runtime reconciliation provider event identity exceeds the persistence limit"
            )
        observation_digest = canonical_digest(observation.to_dict())
        if evidence_digest != observation_digest:
            raise InvalidTaskInput("Evidence digest must equal the canonical observation digest")
        if UUID(observation.runtime_execution_id) != execution_id:
            raise InvalidTaskInput("Observation Runtime execution identity does not match")

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
            cancel_intent = uow.runtimes.find_cancel_intent(
                execution.id, tenant_id=execution.tenant_id
            )
            convergence = self._require_parked(
                task, run, attempt, execution, cancel_intent
            )
            validate_terminal_observation(
                observation,
                runtime_execution_id=execution.id,
                assignment_id=execution.assignment_id,
                assignment_digest=execution.assignment_digest,
                require_known_terminal=True,
            )
            # A reconciliation is one control-plane transition.  Capture its
            # policy clock once and pass it through evidence, execution and
            # business convergence; provider observed_at is evidence only.
            finalized_at = datetime.now(timezone.utc)
            self._reconcile_evidence(
                uow,
                execution=execution,
                observation=observation,
                observation_digest=observation_digest,
                evidence_reference=normalized_reference,
                received_at=finalized_at,
                quarantine_output=convergence is _ParkedConvergence.CANCELED_RUNTIME_ONLY,
            )

            previous_phase = execution.phase
            confirmed_phase = RuntimeExecutionPhase(observation.phase.value)
            reconciled_execution = execution.reconcile_terminal(
                phase=confirmed_phase,
                provider_sequence=observation.provider_sequence,
                now=finalized_at,
            )
            previous_status = task.status
            previous_error = task.error
            budget_rejection = None
            if (
                observation.phase is RuntimePhase.SUCCEEDED
                and task.budget is not None
                and task.budget.deadline is not None
                and finalized_at >= task.budget.deadline.astimezone(timezone.utc)
            ):
                budget_rejection = "budget_deadline_exceeded"
            disposition = (
                AccountingDisposition.NOT_APPLICABLE
                if task.budget is None
                else AccountingDisposition.ALREADY_CONSERVATIVE
            )
            causation_id = uuid5(
                NAMESPACE_URL,
                f"runtime-reconcile:{execution.id}:{normalized_key}",
            )
            if convergence is _ParkedConvergence.CANCELED_RUNTIME_ONLY:
                action = {
                    RuntimePhase.SUCCEEDED: TaskResolutionAction.RECONCILE_RUNTIME_SUCCEEDED,
                    RuntimePhase.FAILED: TaskResolutionAction.RECONCILE_RUNTIME_FAILED,
                    RuntimePhase.CANCELED: TaskResolutionAction.RECONCILE_RUNTIME_CANCELED,
                    RuntimePhase.TIMED_OUT: TaskResolutionAction.RECONCILE_RUNTIME_TIMED_OUT,
                }[observation.phase]
                business_reason = (
                    "runtime.canceled_task_runtime_only."
                    f"{observation.phase.value.lower()}"
                )
                summary = None
            else:
                reconciliation_context = {
                    _ParkedConvergence.ACTIVE_DIRECT: ProgressionContext.DIRECT_RECONCILIATION,
                    _ParkedConvergence.ACTIVE_REVIEWED_EXECUTOR: (
                        ProgressionContext.REVIEWED_EXECUTOR_RECONCILIATION
                    ),
                    _ParkedConvergence.ACTIVE_REVIEWED_REVIEWER: (
                        ProgressionContext.REVIEWED_REVIEWER_RECONCILIATION
                    ),
                }[convergence]
                summary = self._business_outcome_applier.apply_known_terminal_in_uow(
                    uow,
                    task=task,
                    run=run,
                    attempt=attempt,
                    progression_context=reconciliation_context,
                    phase=KnownTerminalPhase(observation.phase.value),
                    output=dict(observation.output) if observation.output is not None else None,
                    safe_error=(observation.error.code if observation.error is not None else None),
                    budget_rejection=budget_rejection,
                    cancel_intent_present=cancel_intent is not None,
                    accounting_disposition=disposition,
                    finalized_at=finalized_at,
                    causation_id=causation_id,
                )
                action = summary.reconciliation_action
                business_reason = summary.reconciliation_reason
                assert action is not None and business_reason is not None
            # The applier locks and saves identity-map copies in the caller
            # UoW.  Resolution metadata must describe those persisted copies,
            # especially the review/candidate fields produced by continuation
            # planning, rather than the stale objects loaded above.
            resolution_task = uow.tasks.get(task.id, for_update=True) or task
            resolution_run = uow.runs.get(run.id, for_update=True) or run
            resolution = TaskResolution.create(
                task_id=task.id,
                action=action,
                actor=principal.principal_id,
                reason=normalized_reason,
                previous_status=previous_status,
                resulting_status=summary.task_status if summary is not None else task.status,
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
                    "mode": resolution_task.execution_mode.value,
                    "role": resolution_run.role.value,
                    "revision": resolution_run.revision_number,
                    "candidate_digest": (
                        canonical_digest(resolution_task.candidate_output)
                        if resolution_task.candidate_output is not None
                        else None
                    ),
                    "decision_digest": (
                        canonical_digest(resolution_task.latest_review)
                        if resolution_task.latest_review is not None
                        else None
                    ),
                    "new_run_id": (
                        str(summary.new_run_ids[0])
                        if summary is not None and summary.new_run_ids
                        else None
                    ),
                },
                at=finalized_at,
            )
            uow.runtimes.save_execution(reconciled_execution, tenant_id=self._tenant_id)
            uow.task_resolutions.add(resolution)
            uow.outbox.add(
                MessageEnvelope.domain_event(
                    schema_name="agentmesh.runtime.outcome-reconciled",
                    tenant_id=self._tenant_id,
                    aggregate_id=task.id,
                    causation_id=causation_id,
                    producer="agentmesh-runtime-reconciler-v1",
                    at=finalized_at,
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
            if (
                summary is not None
                and summary.may_capture_completion_memory
                and self._runtime_memory_service is not None
            ):
                persisted_task = uow.tasks.get(task.id, for_update=True)
                if persisted_task is None or persisted_task.status is not TaskStatus.COMPLETED:
                    raise InvalidTaskTransition("Completed Task disappeared before Memory capture")
                self._runtime_memory_service.capture_completed_task_in_unit_of_work(
                    uow, persisted_task
                )
            uow.commit()
            # The outcome applier owns the locked business entity and may
            # mutate a fresh identity-map copy rather than the caller's stale
            # ``task`` object.  Use its immutable summary for post-commit
            # status/completion decisions so the resolution and research hook
            # describe the state that was actually persisted.
            completed_task_id = (
                task.id
                if summary is not None and summary.task_completed
                else None
            )
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
    def _require_parked(
        task: Any,
        run: Any,
        attempt: Any,
        execution: RuntimeExecution,
        cancel_intent: Any,
    ) -> _ParkedConvergence:
        if (
            task.status is TaskStatus.CANCELED
            or run.status is RunStatus.CANCELED
            or attempt.status is AttemptStatus.CANCELED
        ):
            if (
                task.execution_mode in {TaskExecutionMode.DIRECT, TaskExecutionMode.REVIEWED}
                and task.status is TaskStatus.CANCELED
                and run.status is RunStatus.CANCELED
                and attempt.status is AttemptStatus.CANCELED
                and run.runtime_authority == "managed"
                and run.role in {RunRole.EXECUTOR, RunRole.REVIEWER}
                and (
                    task.execution_mode is TaskExecutionMode.REVIEWED
                    or run.role is RunRole.EXECUTOR
                )
                and (
                    task.execution_mode is TaskExecutionMode.DIRECT
                    or run.role is RunRole.EXECUTOR
                    or task.candidate_output is not None
                )
                and run.subtask_id is None
                and task.current_run_id == run.id
                and execution.run_id == run.id
                and run.runtime_version_id == execution.runtime_version_id
                and run.runtime_execution_intent_id == execution.id
                and run.runtime_execution_id == execution.id
                and run.comparison_mode == "off"
                and run.revision_number == task.revision_count
                and execution.current_owner_attempt_id == attempt.id
                and execution.current_fencing_token == attempt.fencing_token
                and execution.phase
                in {RuntimeExecutionPhase.OUTCOME_UNKNOWN, RuntimeExecutionPhase.LOST}
                and cancel_intent is not None
                and (
                    (
                        task.budget is None
                        and attempt.budget_settlement_source is None
                    )
                    or (
                        task.budget is not None
                        and attempt.budget_settlement_source
                        is BudgetSettlementSource.RELEASED
                        and task.reserved_tokens == 0
                        and task.reserved_cost_micros == 0
                        and attempt.settled_tokens == 0
                        and attempt.settled_cost_micros == 0
                    )
                )
            ):
                return _ParkedConvergence.CANCELED_RUNTIME_ONLY
            raise InvalidTaskTransition("Runtime canceled chain is not strictly consistent")
        if (
            task.status is not TaskStatus.RECONCILIATION_REQUIRED
            or run.status is not RunStatus.RECONCILIATION_REQUIRED
            or attempt.status is not AttemptStatus.OUTCOME_UNKNOWN
            or run.runtime_authority != "managed"
            or task.current_run_id != run.id
            or execution.run_id != run.id
            or run.runtime_version_id != execution.runtime_version_id
            or run.runtime_execution_intent_id != execution.id
            or run.runtime_execution_id != execution.id
            or run.comparison_mode != "off"
            or run.revision_number != task.revision_count
            or execution.current_owner_attempt_id != attempt.id
            or execution.current_fencing_token != attempt.fencing_token
            or execution.phase
            not in {RuntimeExecutionPhase.OUTCOME_UNKNOWN, RuntimeExecutionPhase.LOST}
            or task.execution_mode not in {TaskExecutionMode.DIRECT, TaskExecutionMode.REVIEWED}
            or run.role not in {RunRole.EXECUTOR, RunRole.REVIEWER}
            or (
                task.execution_mode is TaskExecutionMode.DIRECT
                and run.role is not RunRole.EXECUTOR
            )
            or (
                task.execution_mode is TaskExecutionMode.REVIEWED
                and run.role is RunRole.REVIEWER
                and task.candidate_output is None
            )
            or run.subtask_id is not None
        ):
            raise InvalidTaskTransition(
                "Runtime execution is not a strictly consistent parked managed Run"
            )
        if task.execution_mode is TaskExecutionMode.DIRECT:
            return _ParkedConvergence.ACTIVE_DIRECT
        return (
            _ParkedConvergence.ACTIVE_REVIEWED_EXECUTOR
            if run.role is RunRole.EXECUTOR
            else _ParkedConvergence.ACTIVE_REVIEWED_REVIEWER
        )

    @staticmethod
    def _reconcile_evidence(
        uow: Any,
        *,
        execution: RuntimeExecution,
        observation: RuntimeObservation,
        observation_digest: str,
        evidence_reference: str,
        received_at: datetime,
        quarantine_output: bool = False,
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
                if quarantine_output and observation.phase is RuntimePhase.SUCCEEDED:
                    # Runtime evidence rows are immutable apart from their
                    # processing outcome.  Preserve that rule while making a
                    # quarantined terminal output discoverable in a separate,
                    # deterministic reconciliation evidence row.
                    uow.runtimes.add_observation(
                        RuntimeObservationEvidence(
                            id=uuid5(NAMESPACE_URL, f"{exact.id}:quarantined-output"),
                            tenant_id=execution.tenant_id,
                            runtime_execution_id=execution.id,
                            observation_id=f"{observation.observation_id}:quarantined-output",
                            observation_digest=canonical_digest(
                                {
                                    "observation_id": observation.observation_id,
                                    "quarantined_output": observation.output,
                                }
                            ),
                            assignment_id=execution.assignment_id,
                            assignment_digest=execution.assignment_digest,
                            provider_sequence=observation.provider_sequence,
                            phase=RuntimeExecutionPhase.SUCCEEDED,
                            observed_at=observation.observed_at.astimezone(timezone.utc),
                            received_at=received_at,
                            safe_summary="Reconciled Runtime output quarantined from canceled Task",
                            processing_outcome=RuntimeObservationOutcome.RECONCILED,
                            provider_event_present=observation.provider_event_id is not None,
                            evidence={
                                **expected_provider,
                                "evidence_reference": evidence_reference,
                                "quarantined_output": dict(observation.output),
                            },
                        )
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
                received_at=received_at,
                safe_summary="Operator-confirmed Runtime outcome",
                processing_outcome=RuntimeObservationOutcome.RECONCILED,
                provider_event_present=observation.provider_event_id is not None,
                evidence={
                    **expected_provider,
                    "evidence_reference": evidence_reference,
                    **(
                        {"quarantined_output": dict(observation.output)}
                        if observation.phase is RuntimePhase.SUCCEEDED and quarantine_output
                        else {}
                    ),
                },
            )
        )
