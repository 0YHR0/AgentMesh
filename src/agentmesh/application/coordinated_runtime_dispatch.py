"""Closed, aggregate-locked preparation for coordinated managed dispatch.

This module records a control-plane RuntimeExecution and its immutable
Assignment bytes.  It deliberately has no adapter, worker, admission, or
provider dependency: a successful result is only a durable preparation fact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID

from agentmesh.application.authority_cohorts import AuthorityCohortResolver
from agentmesh.application.coordinated_runtime import (
    CoordinatedRuntimeAggregateLocker,
)
from agentmesh.application.runtime_services import (
    validate_runtime_assignment_chain,
)
from agentmesh.application.runtime_snapshots import (
    assignment_snapshot_for,
    parse_assignment_payload,
)
from agentmesh.domain.coordination import CoordinationRuntimeBoundary, SubtaskStatus
from agentmesh.domain.errors import (
    InvalidTaskInput,
    InvalidTaskTransition,
    RuntimeExecutionConflict,
)
from agentmesh.domain.runtime_execution import (
    RuntimeExecution,
    RuntimeExecutionPhase,
    RuntimeLifecycleOperation,
    RuntimeVersion,
)
from agentmesh.domain.tasks import (
    AttemptStatus,
    RunRole,
    RunStatus,
    TaskExecutionMode,
    TaskStatus,
)
from agentmesh.runtime_sdk import RuntimeAssignment, canonical_digest
from agentmesh.runtime_sdk.canonical import thaw_json
from agentmesh.runtime_sdk.descriptor import RuntimeDescriptor


class CoordinatedRuntimePrepareKind(str, Enum):
    """Closed result vocabulary for aggregate preparation."""

    PREPARED = "PREPARED"
    REPLAY = "REPLAY"
    BLOCKED_BY_DRAIN = "BLOCKED_BY_DRAIN"


class CoordinatedRuntimeDispatchKind(str, Enum):
    """Closed result vocabulary for the provider dispatch boundary."""

    DISPATCH_AUTHORIZED = "DISPATCH_AUTHORIZED"
    ALREADY_CROSSED = "ALREADY_CROSSED"
    BLOCKED_BY_DRAIN = "BLOCKED_BY_DRAIN"


@dataclass(frozen=True)
class CoordinatedRuntimePrepareResult:
    kind: CoordinatedRuntimePrepareKind
    execution_id: UUID | None = None
    drain_id: UUID | None = None
    drain_version: int | None = None

    @property
    def runtime_execution_id(self) -> UUID | None:
        """Compatibility spelling for callers that name the persisted entity."""
        return self.execution_id


@dataclass(frozen=True)
class CoordinatedRuntimeDispatchResult:
    kind: CoordinatedRuntimeDispatchKind
    execution_id: UUID | None = None
    drain_id: UUID | None = None
    drain_version: int | None = None

    @property
    def runtime_execution_id(self) -> UUID | None:
        return self.execution_id


class CoordinatedRuntimeDispatchService:
    """Prepare one coordinated managed RuntimeExecution under the b2 lock."""

    def __init__(
        self,
        *,
        uow_factory: Any,
        aggregate_locker: CoordinatedRuntimeAggregateLocker | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._aggregate_locker = aggregate_locker or CoordinatedRuntimeAggregateLocker()

    def prepare_runtime_assignment(
        self,
        *,
        tenant_id: str,
        task_id: UUID,
        run_id: UUID,
        attempt_id: UUID,
        fencing_token: int,
        assignment: RuntimeAssignment,
        now: datetime,
    ) -> CoordinatedRuntimePrepareResult:
        """Prepare, replay, or report a drain without authorizing a provider call."""
        timestamp = _validate_inputs(
            tenant_id=tenant_id,
            task_id=task_id,
            run_id=run_id,
            attempt_id=attempt_id,
            fencing_token=fencing_token,
            assignment=assignment,
            now=now,
        )
        with self._uow_factory() as uow:
            # This is intentionally the first database operation in c2c2.
            aggregate = self._aggregate_locker.lock(
                uow, tenant_id=tenant_id, task_id=task_id
            )
            if aggregate.active_drain is not None:
                return CoordinatedRuntimePrepareResult(
                    kind=CoordinatedRuntimePrepareKind.BLOCKED_BY_DRAIN,
                    drain_id=aggregate.active_drain.id,
                    drain_version=aggregate.active_drain.version,
                )
            if any(
                operation.operation is RuntimeLifecycleOperation.CANCEL
                for operation in aggregate.lifecycle_operations
            ):
                raise RuntimeExecutionConflict(
                    "Coordinated Runtime preparation is blocked by a CANCEL intent"
                )

            subtask, run, attempt, version = _select_and_validate_target(
                aggregate,
                tenant_id=tenant_id,
                task_id=task_id,
                run_id=run_id,
                attempt_id=attempt_id,
                fencing_token=fencing_token,
                assignment=assignment,
                now=timestamp,
            )
            del subtask  # Selection validates the current Subtask binding.
            execution_id = run.runtime_execution_intent_id
            assert execution_id is not None
            candidate_snapshot = assignment_snapshot_for(
                assignment,
                tenant_id=tenant_id,
                runtime_execution_id=execution_id,
                created_at=timestamp,
            )
            executions = aggregate.executions_by_run.get(run.id, ())
            if len(executions) > 1:
                raise RuntimeExecutionConflict(
                    "Coordinated Run has multiple Runtime executions"
                )
            existing = executions[0] if executions else None
            existing_snapshot = aggregate.assignment_snapshots_by_execution.get(execution_id)

            if existing is not None:
                _validate_replay(
                    existing,
                    existing_snapshot,
                    candidate_snapshot,
                    run_id=run.id,
                    tenant_id=tenant_id,
                    runtime_version_id=version.id,
                    attempt_id=attempt.id,
                    fencing_token=fencing_token,
                )
                return CoordinatedRuntimePrepareResult(
                    kind=CoordinatedRuntimePrepareKind.REPLAY,
                    execution_id=existing.id,
                )

            dispatch_key, dispatch_digest = _stable_dispatch_identity(
                tenant_id,
                execution_id,
                assignment.assignment_digest or "",
            )
            prepared = RuntimeExecution.prepare(
                tenant_id=tenant_id,
                run_id=run.id,
                runtime_version_id=version.id,
                assignment_id=UUID(assignment.assignment_id),
                assignment_digest=assignment.assignment_digest or "",
                dispatch_key=dispatch_key,
                dispatch_digest=dispatch_digest,
                execution_id=execution_id,
                now=timestamp,
            )
            claimed = prepared.claim(
                attempt_id=attempt.id,
                fencing_token=fencing_token,
                expected_owner_attempt_id=None,
                expected_fencing_token=None,
                expected_version=prepared.version,
                now=timestamp,
            )
            uow.runtimes.add_execution(claimed)
            run.bind_runtime_execution(execution_id)
            uow.runs.save(run)
            uow.runtimes.add_assignment_snapshot(candidate_snapshot)
            uow.commit()
            return CoordinatedRuntimePrepareResult(
                kind=CoordinatedRuntimePrepareKind.PREPARED,
                execution_id=execution_id,
            )

    def cross_runtime_dispatch_boundary(
        self,
        *,
        tenant_id: str,
        task_id: UUID,
        run_id: UUID,
        attempt_id: UUID,
        fencing_token: int,
        runtime_execution_id: UUID,
        assignment_digest: str,
        now: datetime,
    ) -> CoordinatedRuntimeDispatchResult:
        """Atomically cross PREPARED -> DISPATCHING under the b2 lock.

        The returned authorization is only a durable control-plane boundary;
        this command never validates or invokes an adapter.
        """
        timestamp = _validate_boundary_inputs(
            tenant_id=tenant_id,
            task_id=task_id,
            run_id=run_id,
            attempt_id=attempt_id,
            fencing_token=fencing_token,
            runtime_execution_id=runtime_execution_id,
            assignment_digest=assignment_digest,
            now=now,
        )
        with self._uow_factory() as uow:
            # c2c3 must reacquire the complete aggregate, never a Run-first row.
            aggregate = self._aggregate_locker.lock(
                uow, tenant_id=tenant_id, task_id=task_id
            )
            run, attempt, version, execution, snapshot, boundary = (
                _select_dispatch_target(
                    aggregate,
                    tenant_id=tenant_id,
                    task_id=task_id,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    fencing_token=fencing_token,
                    runtime_execution_id=runtime_execution_id,
                    assignment_digest=assignment_digest,
                    now=timestamp,
                )
            )
            del run, version, snapshot
            # A response-loss replay must converge even if a drain or CANCEL
            # intent was recorded after the successful boundary commit.
            if boundary is CoordinationRuntimeBoundary.CROSSED_ACTIVE:
                return CoordinatedRuntimeDispatchResult(
                    kind=CoordinatedRuntimeDispatchKind.ALREADY_CROSSED,
                    execution_id=execution.id,
                )
            if aggregate.active_drain is not None:
                return CoordinatedRuntimeDispatchResult(
                    kind=CoordinatedRuntimeDispatchKind.BLOCKED_BY_DRAIN,
                    drain_id=aggregate.active_drain.id,
                    drain_version=aggregate.active_drain.version,
                )
            if any(
                operation.operation is RuntimeLifecycleOperation.CANCEL
                for operation in aggregate.lifecycle_operations
            ):
                raise RuntimeExecutionConflict(
                    "Coordinated Runtime dispatch is blocked by a CANCEL intent"
                )
            if boundary is not CoordinationRuntimeBoundary.NOT_CROSSED_PREPARED:
                raise RuntimeExecutionConflict(
                    "Coordinated Runtime dispatch boundary is not PREPARED"
                )
            if (
                execution.phase is not RuntimeExecutionPhase.PREPARED
                or execution.current_owner_attempt_id != attempt.id
                or execution.current_fencing_token != fencing_token
            ):
                raise RuntimeExecutionConflict(
                    "Coordinated Runtime execution owner or phase is stale"
                )
            crossed = execution.apply_observation(
                phase=RuntimeExecutionPhase.DISPATCHING,
                provider_sequence=None,
                now=timestamp,
            )
            uow.runtimes.save_execution(crossed, tenant_id=tenant_id)
            uow.commit()
            return CoordinatedRuntimeDispatchResult(
                kind=CoordinatedRuntimeDispatchKind.DISPATCH_AUTHORIZED,
                execution_id=execution.id,
            )


def _validate_inputs(
    *,
    tenant_id: str,
    task_id: UUID,
    run_id: UUID,
    attempt_id: UUID,
    fencing_token: int,
    assignment: RuntimeAssignment,
    now: datetime,
) -> datetime:
    if (
        type(tenant_id) is not str
        or not tenant_id.strip()
        or tenant_id != tenant_id.strip()
        or any(type(value) is not UUID for value in (task_id, run_id, attempt_id))
        or type(fencing_token) is not int
        or fencing_token <= 0
        or type(assignment) is not RuntimeAssignment
        or type(now) is not datetime
        or now.tzinfo is None
        or now.utcoffset() is None
    ):
        raise InvalidTaskInput("Coordinated Runtime preparation input is invalid")
    return now.astimezone(timezone.utc)


def _validate_boundary_inputs(
    *,
    tenant_id: str,
    task_id: UUID,
    run_id: UUID,
    attempt_id: UUID,
    fencing_token: int,
    runtime_execution_id: UUID,
    assignment_digest: str,
    now: datetime,
) -> datetime:
    if (
        type(tenant_id) is not str
        or not tenant_id.strip()
        or tenant_id != tenant_id.strip()
        or any(
            type(value) is not UUID
            for value in (task_id, run_id, attempt_id, runtime_execution_id)
        )
        or type(fencing_token) is not int
        or fencing_token <= 0
        or type(assignment_digest) is not str
        or re.fullmatch(r"[0-9a-f]{64}", assignment_digest) is None
        or type(now) is not datetime
        or now.tzinfo is None
        or now.utcoffset() is None
    ):
        raise InvalidTaskInput("Coordinated Runtime dispatch input is invalid")
    return now.astimezone(timezone.utc)


def _select_dispatch_target(
    aggregate: Any,
    *,
    tenant_id: str,
    task_id: UUID,
    run_id: UUID,
    attempt_id: UUID,
    fencing_token: int,
    runtime_execution_id: UUID,
    assignment_digest: str,
    now: datetime,
) -> tuple[Any, Any, RuntimeVersion, RuntimeExecution, Any, CoordinationRuntimeBoundary]:
    task = aggregate.task
    if (
        task.id != task_id
        or task.tenant_id != tenant_id
        or task.execution_mode is not TaskExecutionMode.COORDINATED
        or task.status is not TaskStatus.RUNNING
    ):
        raise InvalidTaskTransition("Coordinated Runtime dispatch requires a running Task")
    cohort = aggregate.cohort
    if (
        cohort.runtime_authority != "managed"
        or cohort.task_id != task_id
        or cohort.tenant_id != tenant_id
        or cohort.runtime_version_id is None
    ):
        raise RuntimeExecutionConflict("Task is not bound to a managed Runtime cohort")
    run = next((value for value in aggregate.runs if value.id == run_id), None)
    if run is None:
        raise RuntimeExecutionConflict("Coordinated Runtime Run is unavailable")
    subtask = next(
        (
            value
            for value in aggregate.subtasks
            if value.current_run_id == run_id and value.id == run.subtask_id
        ),
        None,
    )
    if (
        subtask is None
        or subtask.status is not SubtaskStatus.RUNNING
        or run.role is not RunRole.EXECUTOR
        or run.runtime_authority != "managed"
        or run.status is not RunStatus.RUNNING
        or run.runtime_version_id != cohort.runtime_version_id
        or run.runtime_execution_intent_id != runtime_execution_id
        or run.runtime_execution_id != runtime_execution_id
    ):
        raise RuntimeExecutionConflict("Coordinated Runtime Run binding is not dispatchable")
    attempt = aggregate.latest_attempts.get(run.id)
    if (
        attempt is None
        or attempt.id != attempt_id
        or attempt.run_id != run.id
        or attempt.status is not AttemptStatus.RUNNING
        or attempt.fencing_token != fencing_token
        or attempt.lease_expires_at.astimezone(timezone.utc) <= now
        or attempt.started_at.astimezone(timezone.utc) > now
        or attempt.heartbeat_at.astimezone(timezone.utc) > now
    ):
        raise InvalidTaskTransition("Coordinated Runtime Attempt is not dispatchable")
    version = aggregate.runtime_versions.get(run.runtime_version_id)
    if type(version) is not RuntimeVersion:
        raise RuntimeExecutionConflict("Coordinated Runtime Version is unavailable")
    AuthorityCohortResolver._validate_builtin_langgraph_v2_version(version)
    executions = aggregate.executions_by_run.get(run.id, ())
    if len(executions) != 1 or executions[0].id != runtime_execution_id:
        raise RuntimeExecutionConflict("Coordinated Runtime execution identity conflicts")
    execution = executions[0]
    snapshot = aggregate.assignment_snapshots_by_execution.get(execution.id)
    if snapshot is None or snapshot.assignment_digest != assignment_digest:
        raise RuntimeExecutionConflict("Coordinated Runtime Assignment snapshot conflicts")
    assignment = parse_assignment_payload(snapshot.canonical_payload)
    validate_runtime_assignment_chain(
        assignment,
        tenant_id=tenant_id,
        task_id=task_id,
        run=run,
        execution_id=runtime_execution_id,
    )
    if (
        assignment.assignment_digest != assignment_digest
        or assignment.run_role != run.role.value
        or assignment.revision != run.revision_number
        or assignment.runtime_descriptor_digest
        != RuntimeDescriptor.from_dict(thaw_json(version.descriptor)).digest()
        or execution.assignment_id != UUID(assignment.assignment_id)
        or execution.assignment_digest != assignment_digest
        or execution.runtime_version_id != version.id
    ):
        raise RuntimeExecutionConflict("Coordinated Runtime dispatch identity conflicts")
    if (
        execution.dispatch_key,
        execution.dispatch_digest,
    ) != _stable_dispatch_identity(tenant_id, execution.id, assignment_digest):
        raise RuntimeExecutionConflict("Coordinated Runtime dispatch key conflicts")
    boundary = aggregate.boundary_classifications.get(run.id)
    if boundary not in {
        CoordinationRuntimeBoundary.NOT_CROSSED_PREPARED,
        CoordinationRuntimeBoundary.CROSSED_ACTIVE,
    }:
        raise RuntimeExecutionConflict("Coordinated Runtime evidence blocks dispatch")
    return run, attempt, version, execution, snapshot, boundary


def _select_and_validate_target(
    aggregate: Any,
    *,
    tenant_id: str,
    task_id: UUID,
    run_id: UUID,
    attempt_id: UUID,
    fencing_token: int,
    assignment: RuntimeAssignment,
    now: datetime,
) -> tuple[Any, Any, Any, RuntimeVersion]:
    task = aggregate.task
    if (
        task.id != task_id
        or task.tenant_id != tenant_id
        or task.execution_mode is not TaskExecutionMode.COORDINATED
        or task.status is not TaskStatus.RUNNING
    ):
        raise InvalidTaskTransition("Coordinated Runtime preparation requires a running Task")
    if (
        aggregate.cohort.runtime_authority != "managed"
        or aggregate.cohort.task_id != task_id
        or aggregate.cohort.tenant_id != tenant_id
        or aggregate.cohort.runtime_version_id is None
    ):
        raise RuntimeExecutionConflict("Task is not bound to a managed Runtime cohort")
    run = next((value for value in aggregate.runs if value.id == run_id), None)
    if run is None:
        raise RuntimeExecutionConflict("Coordinated Runtime Run is unavailable")
    subtask = next(
        (
            value
            for value in aggregate.subtasks
            if value.current_run_id == run_id and value.id == run.subtask_id
        ),
        None,
    )
    if (
        subtask is None
        or subtask.status is not SubtaskStatus.RUNNING
        or run.role is not RunRole.EXECUTOR
        or run.runtime_authority != "managed"
        or run.status is not RunStatus.RUNNING
        or run.runtime_version_id != aggregate.cohort.runtime_version_id
        or run.runtime_execution_intent_id is None
    ):
        raise InvalidTaskTransition("Coordinated Runtime Run is not an active managed Executor")
    if aggregate.boundary_classifications.get(run.id) not in {
        CoordinationRuntimeBoundary.NOT_CROSSED_NO_EXECUTION,
        CoordinationRuntimeBoundary.NOT_CROSSED_PREPARED,
    }:
        raise RuntimeExecutionConflict("Coordinated Runtime boundary cannot be prepared")
    version = aggregate.runtime_versions.get(run.runtime_version_id)
    if type(version) is not RuntimeVersion:
        raise RuntimeExecutionConflict("Coordinated Runtime Version is unavailable")
    AuthorityCohortResolver._validate_builtin_langgraph_v2_version(version)
    if version.id != aggregate.cohort.runtime_version_id:
        raise RuntimeExecutionConflict("Coordinated Runtime cohort Version changed")
    validate_runtime_assignment_chain(
        assignment,
        tenant_id=tenant_id,
        task_id=task_id,
        run=run,
        execution_id=run.runtime_execution_intent_id,
    )
    if (
        assignment.run_role != run.role.value
        or assignment.revision != run.revision_number
        or assignment.runtime_descriptor_digest
        != RuntimeDescriptor.from_dict(thaw_json(version.descriptor)).digest()
    ):
        raise RuntimeExecutionConflict("Runtime Assignment does not match the managed Version")
    attempt = aggregate.latest_attempts.get(run.id)
    if (
        attempt is None
        or attempt.id != attempt_id
        or attempt.run_id != run.id
        or attempt.status is not AttemptStatus.RUNNING
        or attempt.fencing_token != fencing_token
        or attempt.lease_expires_at.astimezone(timezone.utc) <= now
        or attempt.started_at.astimezone(timezone.utc) > now
        or attempt.heartbeat_at.astimezone(timezone.utc) > now
    ):
        raise InvalidTaskTransition("Coordinated Runtime Attempt is not claimable")
    return subtask, run, attempt, version


def _stable_dispatch_identity(
    tenant_id: str, execution_id: UUID, assignment_digest: str
) -> tuple[str, str]:
    dispatch_key = f"runtime-dispatch:{tenant_id}:{execution_id}"
    return dispatch_key, canonical_digest(
        {
            "execution_id": str(execution_id),
            "dispatch_key": dispatch_key,
            "assignment_digest": assignment_digest,
        }
    )


def _validate_replay(
    existing: RuntimeExecution,
    existing_snapshot: Any,
    candidate_snapshot: Any,
    *,
    run_id: UUID,
    tenant_id: str,
    runtime_version_id: UUID,
    attempt_id: UUID,
    fencing_token: int,
) -> None:
    if (
        existing.id != candidate_snapshot.runtime_execution_id
        or existing.tenant_id != candidate_snapshot.tenant_id
        or existing.run_id != run_id
        or existing.runtime_version_id != runtime_version_id
        or (
            existing.dispatch_key,
            existing.dispatch_digest,
        )
        != _stable_dispatch_identity(
            tenant_id,
            existing.id,
            candidate_snapshot.assignment_digest,
        )
        or existing.assignment_id != candidate_snapshot.assignment_id
        or existing.assignment_digest != candidate_snapshot.assignment_digest
        or existing.phase is not RuntimeExecutionPhase.PREPARED
        or existing.version != 2
        or existing.current_owner_attempt_id != attempt_id
        or existing.current_fencing_token != fencing_token
        or existing_snapshot is None
        or existing_snapshot.tenant_id != candidate_snapshot.tenant_id
        or existing_snapshot.runtime_execution_id != candidate_snapshot.runtime_execution_id
        or existing_snapshot.assignment_id != candidate_snapshot.assignment_id
        or existing_snapshot.assignment_digest != candidate_snapshot.assignment_digest
        or existing_snapshot.canonical_payload != candidate_snapshot.canonical_payload
    ):
        raise RuntimeExecutionConflict("Coordinated Runtime preparation replay conflicts")


__all__ = [
    "CoordinatedRuntimeDispatchService",
    "CoordinatedRuntimeDispatchKind",
    "CoordinatedRuntimeDispatchResult",
    "CoordinatedRuntimePrepareKind",
    "CoordinatedRuntimePrepareResult",
]
