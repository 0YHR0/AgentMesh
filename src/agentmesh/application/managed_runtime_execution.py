"""Application coordinator for the framework-neutral managed-runtime port.

The coordinator deliberately has no framework imports.  It records the A1
execution and ownership facts in short transactions, closes the UoW, and only
then invokes the provider adapter.  Provider evidence is recorded in a fresh
transaction after the call returns.  This is the only A2 path used for a
comparison shadow; it never changes the authoritative legacy Run result.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import NAMESPACE_URL, UUID, uuid5

from agentmesh.application.ports import (
    ManagedRuntimeAuthoritativeResult,
    ManagedRuntimeControlPlaneFailure,
    ManagedRuntimeExecutionPort,
    ManagedRuntimePreDispatchFailure,
    RuntimeAssignmentBuilder,
    WorkflowWorkItem,
)
from agentmesh.application.runtime_comparison import RuntimeComparisonSnapshot
from agentmesh.application.runtime_services import RuntimeRegistryService
from agentmesh.domain.errors import InvalidTaskTransition
from agentmesh.domain.runtime_execution import RuntimeExecutionPhase
from agentmesh.domain.tasks import AttemptStatus, Task, TaskAttempt, TaskRun
from agentmesh.runtime_sdk import (
    ErrorCategory,
    ManagedAgentRuntime,
    RetryDisposition,
    RuntimeAssignment,
    RuntimeError,
    RuntimeObservation,
    RuntimePhase,
    canonical_digest,
)

_PHASES = {phase.value: phase for phase in RuntimeExecutionPhase}


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class ManagedRuntimeExecutionService(ManagedRuntimeExecutionPort):
    """Prepare, claim, dispatch, and persist evidence for a pinned Run."""

    def __init__(
        self,
        *,
        registry: RuntimeRegistryService,
        adapter: ManagedAgentRuntime,
        assignment_builder: RuntimeAssignmentBuilder,
    ) -> None:
        self._registry = registry
        self._adapter = adapter
        self._assignment_builder = assignment_builder

    def execute_shadow(
        self,
        task: Task,
        run: TaskRun,
        attempt: TaskAttempt,
        *,
        work_item: WorkflowWorkItem | None = None,
    ) -> RuntimeComparisonSnapshot:
        """Run one explicitly pinned managed shadow and retain safe evidence.

        Admission must have pinned the Runtime Version.  We intentionally do
        not manufacture a RuntimeExecution here: a missing binding is an
        admission/configuration error and must never produce a dispatch key
        containing ``None``.
        """

        if run.runtime_version_id is None or run.runtime_execution_id is None:
            raise ValueError("Managed shadow requires an admitted Runtime execution")
        now = datetime.now(timezone.utc)
        # An idempotent claim replay must never turn an expired worker lease
        # into another provider invocation.  The repository may safely return
        # the already-installed owner/token, but this application boundary is
        # the final dispatch gate and rejects stale Attempts before calling the
        # adapter.
        if (
            attempt.status is not AttemptStatus.RUNNING
            or _utc(attempt.lease_expires_at) <= now
        ):
            raise InvalidTaskTransition("Managed shadow Attempt lease is not active")
        assignment = self._assignment_builder.assignment_for(
            task, run, attempt, work_item=work_item
        )
        expected_key = f"runtime-dispatch:{task.tenant_id}:{run.runtime_execution_id}"
        execution = self._registry.prepare_execution(
            run_id=run.id,
            assignment_id=_uuid(assignment.assignment_id),
            assignment_digest=assignment.assignment_digest or "",
            dispatch_key=expected_key,
            execution_id=run.runtime_execution_id,
        )
        if execution.id != run.runtime_execution_id:
            raise ValueError("Runtime execution binding is inconsistent")

        # Claim CAS is committed before any provider call.  A concurrent or
        # stale attempt therefore fails closed instead of racing the adapter.
        self._registry.claim_execution_owner(
            execution_id=execution.id,
            attempt_id=attempt.id,
            fencing_token=attempt.fencing_token,
            expected_owner_attempt_id=execution.current_owner_attempt_id,
            expected_fencing_token=execution.current_fencing_token,
            expected_version=execution.version,
            claim_reason="initial",
            now=now,
        )

        report = self._adapter.validate(assignment)
        if not report.valid:
            raise ValueError("Managed Runtime assignment validation failed")
        binder = getattr(self._adapter, "bind_context", None)
        if binder is None:
            raise ValueError("Managed Runtime adapter has no assignment backend")
        binder(assignment, task, run, attempt, work_item)
        receipt = self._adapter.dispatch(assignment, dispatch_key=expected_key)
        observation = receipt.observation
        if observation is None:
            observation = self._adapter.inspect(receipt.handle)
        if observation.runtime_execution_id != str(execution.id):
            raise ValueError("Runtime observation identity is inconsistent")
        if observation.assignment_id != assignment.assignment_id:
            raise ValueError("Runtime observation assignment is inconsistent")
        if observation.assignment_digest != assignment.assignment_digest:
            raise ValueError("Runtime observation digest is inconsistent")

        self._registry.record_observation(
            execution_id=execution.id,
            observation_id=observation.observation_id,
            observation_digest=_observation_digest(observation),
            assignment_id=_uuid(assignment.assignment_id),
            assignment_digest=assignment.assignment_digest or "",
            phase=_PHASES[observation.phase.value],
            provider_sequence=observation.provider_sequence,
            observed_at=_utc(observation.observed_at),
            evidence={
                "provider_event_id": observation.provider_event_id,
                "snapshot_digest": observation.snapshot_digest,
                "progress": dict(observation.progress),
            },
            safe_summary="Managed Runtime shadow observation",
            attempt_id=attempt.id,
            fencing_token=attempt.fencing_token,
        )
        return RuntimeComparisonSnapshot(
            terminal_state=observation.phase.value,
            output=observation.output,
            usage=dict(observation.usage),
            artifact_refs=tuple(ref.to_dict() for ref in observation.output_artifact_refs),
            review=(dict(task.latest_review) if task.latest_review is not None else None),
            revision=run.revision_number,
            audit={"semantic": "task_run_terminal"},
            evidence_id=observation.observation_id,
        )

    def execute_authoritative(
        self,
        task: Task,
        run: TaskRun,
        attempt: TaskAttempt,
        *,
        work_item: WorkflowWorkItem | None = None,
    ) -> ManagedRuntimeAuthoritativeResult:
        """Dispatch one managed-authoritative execution without committing its outcome."""
        execution_identity = run.runtime_execution_id or run.runtime_execution_intent_id
        if run.runtime_authority != "managed" or run.runtime_version_id is None:
            raise ValueError("Managed authority requires a pinned Runtime Version")
        if execution_identity is None:
            raise ValueError("Managed authority requires a Runtime execution intent")
        now = datetime.now(timezone.utc)
        if attempt.status is not AttemptStatus.RUNNING or _utc(attempt.lease_expires_at) <= now:
            raise InvalidTaskTransition("Managed Runtime Attempt lease is not active")
        try:
            assignment = self._assignment_builder.assignment_for(
                task, run, attempt, work_item=work_item
            )
            report = self._adapter.validate(assignment)
            if not report.valid:
                raise ValueError("Managed Runtime assignment validation failed")
            binder = getattr(self._adapter, "bind_context", None)
            if binder is None:
                raise ValueError("Managed Runtime adapter has no assignment backend")
            binder(assignment, task, run, attempt, work_item)
        except Exception as exc:
            raise ManagedRuntimePreDispatchFailure(
                "Managed Runtime assignment preparation failed"
            ) from exc
        expected_key = f"runtime-dispatch:{task.tenant_id}:{execution_identity}"
        try:
            execution = self._registry.prepare_execution(
                run_id=run.id,
                assignment_id=_uuid(assignment.assignment_id),
                assignment_digest=assignment.assignment_digest or "",
                dispatch_key=expected_key,
                execution_id=execution_identity,
            )
        except Exception as exc:
            raise ManagedRuntimeControlPlaneFailure(
                "Managed Runtime execution preparation did not commit"
            ) from exc
        if execution.phase is not RuntimeExecutionPhase.PREPARED:
            return self._unknown_result(
                execution.id,
                assignment,
                "runtime.reattach_unavailable",
                observed_at=execution.updated_at,
                dispatch_crossed=True,
            )
        try:
            execution = self._registry.claim_execution_owner(
                execution_id=execution.id,
                attempt_id=attempt.id,
                fencing_token=attempt.fencing_token,
                expected_owner_attempt_id=execution.current_owner_attempt_id,
                expected_fencing_token=execution.current_fencing_token,
                expected_version=execution.version,
                claim_reason=(
                    "replacement"
                    if execution.current_owner_attempt_id is not None
                    else "initial"
                ),
                now=now,
            )
            execution = self._registry.mark_execution_dispatching(
                execution_id=execution.id,
                attempt_id=attempt.id,
                fencing_token=attempt.fencing_token,
            )
        except Exception as exc:
            raise ManagedRuntimeControlPlaneFailure(
                "Managed Runtime dispatch boundary did not commit"
            ) from exc
        try:
            receipt = self._adapter.dispatch(assignment, dispatch_key=expected_key)
            observation = receipt.observation
            if observation is None:
                observation = self._adapter.inspect(receipt.handle)
            self._validate_identity(execution.id, assignment, observation)
        except Exception:
            return self._unknown_result(
                execution.id,
                assignment,
                "runtime.provider_outcome_unknown",
                observed_at=execution.updated_at,
                dispatch_crossed=True,
            )
        return ManagedRuntimeAuthoritativeResult(
            execution_id=execution.id,
            assignment_id=_uuid(assignment.assignment_id),
            assignment_digest=assignment.assignment_digest or "",
            observation=observation,
            dispatch_crossed=True,
        )

    @staticmethod
    def _validate_identity(
        execution_id: UUID, assignment: RuntimeAssignment, observation: object
    ) -> None:
        if type(observation) is not RuntimeObservation:
            raise ValueError("Runtime observation type is inconsistent")
        assignment_id = assignment.assignment_id
        assignment_digest = assignment.assignment_digest
        if (
            observation.runtime_execution_id != str(execution_id)
            or observation.assignment_id != assignment_id
            or observation.assignment_digest != assignment_digest
            or not observation.phase.terminal
        ):
            raise ValueError("Runtime observation identity is inconsistent")

    @staticmethod
    def _unknown_result(
        execution_id: UUID,
        assignment: RuntimeAssignment,
        code: str,
        *,
        observed_at: datetime,
        dispatch_crossed: bool,
    ) -> ManagedRuntimeAuthoritativeResult:
        assignment_id = assignment.assignment_id
        assignment_digest = assignment.assignment_digest
        observation = RuntimeObservation(
            observation_id=str(uuid5(NAMESPACE_URL, f"{execution_id}:{code}")),
            runtime_execution_id=str(execution_id),
            assignment_id=assignment_id,
            assignment_digest=assignment_digest,
            phase=RuntimePhase.OUTCOME_UNKNOWN,
            observed_at=_utc(observed_at),
            provider_event_id=code,
            error=RuntimeError(
                code=code,
                category=ErrorCategory.UNKNOWN,
                message="Runtime provider outcome requires reconciliation",
                retry_disposition=RetryDisposition.RECONCILE,
            ),
        )
        return ManagedRuntimeAuthoritativeResult(
            execution_id=execution_id,
            assignment_id=_uuid(assignment_id),
            assignment_digest=assignment_digest,
            observation=observation,
            dispatch_crossed=dispatch_crossed,
        )


def _uuid(value: str):
    return UUID(value)


def _observation_digest(observation: RuntimeObservation) -> str:
    return canonical_digest(observation.to_dict())
