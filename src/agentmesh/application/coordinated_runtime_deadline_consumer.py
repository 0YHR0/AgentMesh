"""Deadline lifecycle consumer with provider inspection outside database UoWs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from agentmesh.application.ports import UnitOfWorkFactory
from agentmesh.application.runtime_snapshots import handle_from_snapshot
from agentmesh.domain.errors import InvalidTaskInput
from agentmesh.domain.runtime_execution import (
    RuntimeExecution,
    RuntimeLifecycleIntent,
    RuntimeLifecycleOperation,
    RuntimeLifecycleStatus,
)
from agentmesh.domain.tasks import TaskAttempt, TaskExecutionMode, TaskRun
from agentmesh.features import Feature, FeatureGateSet
from agentmesh.runtime_sdk import ManagedAgentRuntime, RuntimeObservation

_DEFAULT_CLAIM_LEASE = timedelta(seconds=30)
_INVALID_TARGET = "runtime.deadline_target_invalid"


@dataclass(frozen=True)
class CoordinatedDeadlineProcessResult:
    operation: RuntimeLifecycleIntent | None
    inspection_attempted: bool
    finalized: bool
    convergence_result: Any | None = None


@dataclass(frozen=True)
class _DeadlineTarget:
    task_id: UUID
    run_id: UUID
    attempt_id: UUID
    fencing_token: int
    execution_id: UUID
    handle: Any | None


class CoordinatedRuntimeDeadlineConsumer:
    """Claim one expired cancel intent, inspect once, and delegate convergence."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        tenant_id: str,
        feature_gates: FeatureGateSet,
        adapter: ManagedAgentRuntime,
        recovery_service: Any,
        claim_lease: timedelta = _DEFAULT_CLAIM_LEASE,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not callable(uow_factory):
            raise InvalidTaskInput("Deadline consumer UoW factory is invalid")
        if type(tenant_id) is not str or not tenant_id.strip():
            raise InvalidTaskInput("Deadline consumer tenant is invalid")
        if not callable(getattr(adapter, "inspect", None)):
            raise InvalidTaskInput("Deadline consumer adapter is invalid")
        if not callable(getattr(recovery_service, "finalize", None)):
            raise InvalidTaskInput("Deadline consumer recovery service is invalid")
        if type(claim_lease) is not timedelta or claim_lease <= timedelta(0):
            raise InvalidTaskInput("Deadline consumer claim lease is invalid")
        self._uow_factory = uow_factory
        self._tenant_id = tenant_id
        self._feature_gates = feature_gates
        self._adapter = adapter
        self._recovery = recovery_service
        self._claim_lease = claim_lease
        self._clock = clock

    def process_next_deadline(
        self, *, now: datetime | None = None
    ) -> CoordinatedDeadlineProcessResult:
        return self.process_deadline(now=now)

    def process_deadline(
        self,
        execution_id: UUID | None = None,
        *,
        operation_id: str | None = None,
        now: datetime | None = None,
    ) -> CoordinatedDeadlineProcessResult:
        self._feature_gates.require(Feature.MANAGED_AGENT_RUNTIME)
        timestamp = self._timestamp(now)
        with self._uow_factory() as uow:
            lifecycle = uow.runtimes.claim_deadline_lifecycle(
                tenant_id=self._tenant_id,
                now=timestamp,
                lease=self._claim_lease,
                execution_id=execution_id,
                operation_id=operation_id,
            )
            uow.commit()
        if lifecycle is None:
            return CoordinatedDeadlineProcessResult(None, False, False)

        target = self._read_target(lifecycle)
        if target is None:
            self._expire_invalid_claim(lifecycle, at=self._timestamp(now))
            return CoordinatedDeadlineProcessResult(lifecycle, False, False)

        inspection = None
        attempted = target.handle is not None
        if target.handle is not None:
            try:
                candidate = self._adapter.inspect(target.handle)
                if type(candidate) is RuntimeObservation:
                    inspection = candidate
            except Exception:
                inspection = None
        claim_token = lifecycle.claim_token
        if type(claim_token) is not UUID:
            return CoordinatedDeadlineProcessResult(lifecycle, attempted, False)
        result = self._recovery.finalize(
            self._tenant_id,
            target.task_id,
            target.run_id,
            target.attempt_id,
            target.fencing_token,
            target.execution_id,
            lifecycle.operation_id,
            claim_token,
            inspection,
            self._timestamp(now),
        )
        return CoordinatedDeadlineProcessResult(lifecycle, attempted, True, result)

    def _read_target(self, lifecycle: RuntimeLifecycleIntent) -> _DeadlineTarget | None:
        with self._uow_factory() as uow:
            execution = uow.runtimes.get_execution(
                lifecycle.runtime_execution_id,
                tenant_id=self._tenant_id,
            )
            if type(execution) is not RuntimeExecution:
                return None
            run = uow.runs.get(execution.run_id)
            if type(run) is not TaskRun:
                return None
            task = uow.tasks.get(run.task_id)
            attempt = uow.attempts.latest_for_run(run.id)
            snapshot = uow.runtimes.get_handle_snapshot(
                execution.id,
                tenant_id=self._tenant_id,
            )
        if (
            task is None
            or task.tenant_id != self._tenant_id
            or task.execution_mode is not TaskExecutionMode.COORDINATED
            or run.task_id != task.id
            or run.runtime_authority != "managed"
            or run.runtime_execution_id != execution.id
            or run.runtime_execution_intent_id != execution.id
            or type(attempt) is not TaskAttempt
            or attempt.id != execution.current_owner_attempt_id
            or attempt.run_id != run.id
            or attempt.fencing_token != execution.current_fencing_token
            or lifecycle.operation is not RuntimeLifecycleOperation.CANCEL
            or lifecycle.operation_id != f"runtime-cancel:{execution.id}:v1"
            or lifecycle.status
            not in {
                RuntimeLifecycleStatus.REQUESTED,
                RuntimeLifecycleStatus.ACCEPTED,
                RuntimeLifecycleStatus.REJECTED,
            }
        ):
            return None
        handle = None
        if snapshot is not None:
            try:
                candidate = handle_from_snapshot(snapshot)
                if (
                    candidate.runtime_execution_id == str(execution.id)
                    and candidate.runtime_version_id == str(execution.runtime_version_id)
                    and candidate.assignment_id == str(execution.assignment_id)
                    and candidate.assignment_digest == execution.assignment_digest
                ):
                    handle = candidate
            except (InvalidTaskInput, TypeError, ValueError):
                handle = None
        return _DeadlineTarget(
            task_id=task.id,
            run_id=run.id,
            attempt_id=attempt.id,
            fencing_token=attempt.fencing_token,
            execution_id=execution.id,
            handle=handle,
        )

    def _expire_invalid_claim(
        self, lifecycle: RuntimeLifecycleIntent, *, at: datetime
    ) -> None:
        with self._uow_factory() as uow:
            current = uow.runtimes.find_lifecycle_operation(
                lifecycle.runtime_execution_id,
                tenant_id=self._tenant_id,
                operation_id=lifecycle.operation_id,
                for_update=True,
            )
            if current is None or current.claim_token != lifecycle.claim_token:
                return
            expired = current.expire(now=at, error_code=_INVALID_TARGET)
            uow.runtimes.save_lifecycle_operation(expired)
            uow.commit()

    def _timestamp(self, explicit: datetime | None) -> datetime:
        value = explicit
        if value is None:
            value = self._clock() if self._clock is not None else datetime.now(timezone.utc)
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
            raise InvalidTaskInput("Deadline consumer timestamp is invalid")
        return value.astimezone(timezone.utc)


__all__ = [
    "CoordinatedDeadlineProcessResult",
    "CoordinatedRuntimeDeadlineConsumer",
]
