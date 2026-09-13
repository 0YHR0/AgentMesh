"""Application coordinator for the framework-neutral managed-runtime port.

The coordinator deliberately has no framework imports.  It records the A1
execution and ownership facts in short transactions, closes the UoW, and only
then invokes the provider adapter.  Provider evidence is recorded in a fresh
transaction after the call returns.  This is the only A2 path used for a
comparison shadow; it never changes the authoritative legacy Run result.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from agentmesh.application.ports import (
    ManagedRuntimeAuthoritativeResult,
    ManagedRuntimeConflictObservation,
    ManagedRuntimeControlPlaneFailure,
    ManagedRuntimeExecutionPort,
    ManagedRuntimePreDispatchFailure,
    RuntimeAssignmentBuilder,
    WorkflowWorkItem,
)
from agentmesh.application.runtime_comparison import RuntimeComparisonSnapshot
from agentmesh.application.runtime_conflicts import (
    build_managed_runtime_conflict_observation,
)
from agentmesh.application.runtime_contracts import validate_terminal_observation
from agentmesh.application.runtime_services import RuntimeRegistryService
from agentmesh.application.runtime_snapshots import parse_assignment_payload
from agentmesh.domain.errors import InvalidTaskInput, InvalidTaskTransition
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
        assignment, bound_work_item = self._load_or_build_assignment(
            task, run, attempt, work_item=work_item
        )
        expected_key = f"runtime-dispatch:{task.tenant_id}:{run.runtime_execution_id}"
        execution = self._prepare_execution(
            run_id=run.id,
            assignment=assignment,
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
        binder(assignment, task, run, attempt, bound_work_item)
        execution = self._registry.mark_execution_dispatching(
            execution_id=execution.id,
            attempt_id=attempt.id,
            fencing_token=attempt.fencing_token,
        )
        receipt = self._adapter.dispatch(assignment, dispatch_key=expected_key)
        if (
            receipt.dispatch_key != expected_key
            or receipt.runtime_execution_id != str(execution.id)
            or receipt.assignment_digest != assignment.assignment_digest
        ):
            raise ValueError("Runtime dispatch receipt identity is inconsistent")
        if receipt.handle is not None:
            self._bind_handle(
                receipt.handle,
                execution_id=execution.id,
                assignment=assignment,
                attempt=attempt,
            )
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
            assignment, bound_work_item = self._load_or_build_assignment(
                task, run, attempt, work_item=work_item
            )
            report = self._adapter.validate(assignment)
            if not report.valid:
                raise ValueError("Managed Runtime assignment validation failed")
            binder = getattr(self._adapter, "bind_context", None)
            if binder is None:
                raise ValueError("Managed Runtime adapter has no assignment backend")
            binder(assignment, task, run, attempt, bound_work_item)
        except Exception as exc:
            raise ManagedRuntimePreDispatchFailure(
                "Managed Runtime assignment preparation failed"
            ) from exc
        expected_key = f"runtime-dispatch:{task.tenant_id}:{execution_identity}"
        try:
            execution = self._prepare_execution(
                run_id=run.id,
                assignment=assignment,
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
        except Exception:
            return self._unknown_result(
                execution.id,
                assignment,
                "runtime.provider_outcome_unknown",
                observed_at=execution.updated_at,
                dispatch_crossed=True,
            )
        try:
            if (
                receipt.dispatch_key != expected_key
                or receipt.runtime_execution_id != str(execution.id)
                or receipt.assignment_digest != assignment.assignment_digest
            ):
                raise InvalidTaskInput("Runtime dispatch receipt identity is inconsistent")
            if receipt.handle is not None:
                self._bind_handle(
                    receipt.handle,
                    execution_id=execution.id,
                    assignment=assignment,
                    attempt=attempt,
                )
            observation = receipt.observation
            if observation is None:
                if receipt.handle is None:
                    raise InvalidTaskInput("Runtime dispatch returned no handle or observation")
                observation = self._adapter.inspect(receipt.handle)
        except Exception:
            return self._unknown_result(
                execution.id,
                assignment,
                "runtime.handle_contract_invalid",
                observed_at=execution.updated_at,
                dispatch_crossed=True,
            )
        try:
            self._validate_identity(execution.id, assignment, observation)
        except (InvalidTaskInput, ValueError):
            conflict = build_managed_runtime_conflict_observation(
                observation,
                expected_execution_id=execution.id,
                expected_assignment_id=_uuid(assignment.assignment_id),
                expected_assignment_digest=assignment.assignment_digest or "",
                fallback_observed_at=(
                    _utc(observation.observed_at)
                    if type(observation) is RuntimeObservation
                    else _utc(execution.updated_at)
                ),
            )
            return self._unknown_result(
                execution.id,
                assignment,
                "runtime.terminal_contract_invalid",
                observed_at=conflict.observed_at,
                dispatch_crossed=True,
                conflicting_observation=conflict,
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
        validate_terminal_observation(
            observation,
            runtime_execution_id=execution_id,
            assignment_id=_uuid(assignment.assignment_id),
            assignment_digest=assignment.assignment_digest or "",
        )
        if observation.error is not None and observation.error.code == "runtime.protocol_error":
            raise InvalidTaskInput("Runtime provider returned a protocol-conflict observation")

    @staticmethod
    def _unknown_result(
        execution_id: UUID,
        assignment: RuntimeAssignment,
        code: str,
        *,
        observed_at: datetime,
        dispatch_crossed: bool,
        conflicting_observation: ManagedRuntimeConflictObservation | None = None,
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
            conflicting_observation=conflicting_observation,
        )

    def _load_or_build_assignment(
        self,
        task: Task,
        run: TaskRun,
        attempt: TaskAttempt,
        *,
        work_item: WorkflowWorkItem | None,
    ) -> tuple[RuntimeAssignment, WorkflowWorkItem | None]:
        execution_id = run.runtime_execution_id or run.runtime_execution_intent_id
        snapshot = (
            self._registry.get_assignment_snapshot(execution_id)
            if execution_id is not None
            else None
        )
        if snapshot is None:
            return (
                self._assignment_builder.assignment_for(
                    task, run, attempt, work_item=work_item
                ),
                work_item,
            )
        assignment = parse_assignment_payload(snapshot.canonical_payload)
        _validate_loaded_assignment(assignment, task, run, execution_id)
        return assignment, _work_item_from_assignment(assignment)

    def _prepare_execution(
        self,
        *,
        run_id: UUID,
        assignment: RuntimeAssignment,
        dispatch_key: str,
        execution_id: UUID,
    ) -> Any:
        return self._registry.prepare_execution_with_assignment_snapshot(
            run_id=run_id,
            assignment=assignment,
            dispatch_key=dispatch_key,
            execution_id=execution_id,
        )

    def _bind_handle(
        self,
        handle: Any,
        *,
        execution_id: UUID,
        assignment: RuntimeAssignment,
        attempt: TaskAttempt,
    ) -> None:
        if (
            handle.runtime_execution_id != str(execution_id)
            or handle.runtime_version_id != assignment.runtime_version_id
            or handle.assignment_id != assignment.assignment_id
            or handle.assignment_digest != assignment.assignment_digest
        ):
            raise InvalidTaskInput("Runtime handle identity is inconsistent")
        self._registry.bind_handle_snapshot(
            handle=handle,
            attempt_id=attempt.id,
            fencing_token=attempt.fencing_token,
        )


def _uuid(value: str):
    return UUID(value)


def _canonical_digest_identity(value: str | None) -> str | None:
    """Compare SDK-normalized digests with legacy ``sha256:`` projections."""
    if value is None:
        return None
    return value.strip().lower().removeprefix("sha256:")


def _validate_loaded_assignment(
    assignment: RuntimeAssignment,
    task: Task,
    run: TaskRun,
    execution_id: UUID | None,
) -> None:
    if execution_id is None:
        raise InvalidTaskInput("Runtime Assignment snapshot has no execution identity")
    try:
        identity = UUID(assignment.correlation_ids.get("runtime_execution_id", ""))
        if (
            UUID(assignment.task_id) != task.id
            or UUID(assignment.run_id) != run.id
            or identity != execution_id
            or assignment.tenant_id != task.tenant_id
            or UUID(assignment.runtime_version_id) != run.runtime_version_id
            or UUID(assignment.agent_version_id) != run.agent_version_id
            or _canonical_digest_identity(assignment.agent_version_digest)
            != _canonical_digest_identity(run.agent_version_digest)
        ):
            raise InvalidTaskInput("Runtime Assignment snapshot chain conflicts")
    except (TypeError, ValueError) as exc:
        raise InvalidTaskInput("Runtime Assignment snapshot chain is invalid") from exc


def _work_item_from_assignment(assignment: RuntimeAssignment) -> WorkflowWorkItem:
    """Reconstruct only the exact bytes already persisted in the Assignment."""
    return WorkflowWorkItem(
        objective=assignment.objective or "",
        input=dict(assignment.structured_input or {}),
    )


def _observation_digest(observation: RuntimeObservation) -> str:
    return canonical_digest(observation.to_dict())
