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

from agentmesh.application.ports import (
    ManagedRuntimeExecutionPort,
    RuntimeAssignmentBuilder,
    WorkflowWorkItem,
)
from agentmesh.application.runtime_comparison import RuntimeComparisonSnapshot
from agentmesh.application.runtime_services import RuntimeRegistryService
from agentmesh.domain.errors import InvalidTaskTransition
from agentmesh.domain.runtime_execution import RuntimeExecutionPhase
from agentmesh.domain.tasks import AttemptStatus, Task, TaskAttempt, TaskRun
from agentmesh.runtime_sdk import ManagedAgentRuntime

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
            output=(dict(observation.output) if isinstance(observation.output, dict) else None),
            usage=dict(observation.usage),
            artifact_refs=tuple(ref.to_dict() for ref in observation.output_artifact_refs),
            review=(dict(task.latest_review) if task.latest_review is not None else None),
            revision=run.revision_number,
            audit={"semantic": "task_run_terminal"},
            evidence_id=observation.observation_id,
        )


def _uuid(value: str):
    from uuid import UUID

    return UUID(value)


def _observation_digest(observation: Any) -> str:
    from agentmesh.runtime_sdk import canonical_digest

    return canonical_digest(observation.to_dict())
