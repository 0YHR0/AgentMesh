"""Transaction-local primitives for coordinated runtime stopping.

These helpers deliberately operate only on the caller-owned UoW and locked
aggregate.  They never acquire locks, open/commit a UoW, or call a provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from agentmesh.application.budget_services import BudgetController
from agentmesh.application.coordinated_runtime import CoordinatedRuntimeAggregate
from agentmesh.application.quota_services import QuotaController
from agentmesh.domain.coordination import (
    CoordinationRuntimeDrain,
    CoordinationRuntimeDrainTarget,
)
from agentmesh.domain.errors import RuntimeExecutionConflict
from agentmesh.domain.messaging import MessageEnvelope
from agentmesh.domain.runtime_execution import (
    RuntimeExecution,
    RuntimeExecutionPhase,
    RuntimeLifecycleIntent,
    RuntimeLifecycleOperation,
    RuntimeLifecycleStatus,
)
from agentmesh.domain.tasks import Task
from agentmesh.runtime_sdk.canonical import canonical_digest, canonical_json_bytes

_LIFECYCLE_OUTBOX_PREFIX = "coordination-runtime-lifecycle-outbox:"
_ABORT_OUTBOX_PREFIX = "coordination-runtime-abort-outbox:"


@dataclass(frozen=True)
class CoordinatedCancelRequestResult:
    """Safe result of staging one crossed-execution cancellation intent."""

    lifecycle_id: UUID
    operation_id: str
    operation: str
    changed_ids: tuple[UUID, ...]
    made_progress: bool

    def __post_init__(self) -> None:
        if type(self.lifecycle_id) is not UUID:
            raise RuntimeExecutionConflict("Coordinated cancel lifecycle identity is invalid")
        if type(self.operation_id) is not str or not self.operation_id.strip():
            raise RuntimeExecutionConflict("Coordinated cancel operation identity is invalid")
        if self.operation != RuntimeLifecycleOperation.CANCEL.value:
            raise RuntimeExecutionConflict("Coordinated cancel operation is invalid")
        if tuple(self.changed_ids) != self.changed_ids or any(
            type(value) is not UUID for value in self.changed_ids
        ):
            raise RuntimeExecutionConflict("Coordinated cancel changed IDs are invalid")
        if tuple(sorted(self.changed_ids, key=str)) != self.changed_ids:
            raise RuntimeExecutionConflict("Coordinated cancel changed IDs are not ordered")
        if type(self.made_progress) is not bool:
            raise RuntimeExecutionConflict("Coordinated cancel progress flag is invalid")

    def __iter__(self):
        """Keep the old private barrier tuple unpacking source-compatible."""
        yield self.operation_id
        yield set(self.changed_ids)
        yield self.made_progress


def persisted_cancel_epoch(drain: CoordinationRuntimeDrain) -> datetime:
    """Recover the stable cancellation epoch from the persisted drain guard."""
    if drain.target not in {
        CoordinationRuntimeDrainTarget.CANCELED,
        CoordinationRuntimeDrainTarget.FAILED,
    }:
        return drain.created_at
    return drain.updated_at if drain.version > 1 else drain.created_at


def select_cancel_deadline(
    aggregate: CoordinatedRuntimeAggregate,
    *,
    execution_id: UUID,
    now: datetime,
    cancel_deadline_window: timedelta,
    deadline_epoch: datetime | None,
) -> tuple[str, datetime, RuntimeLifecycleIntent | None]:
    """Select one immutable cancellation deadline without mutating state."""
    if deadline_epoch is None:
        raise RuntimeExecutionConflict("Coordinated cancel deadline epoch is missing")
    operation_id = f"runtime-cancel:{execution_id}:v1"
    rows = aggregate.lifecycle_operations_by_execution.get(execution_id, ())
    cancel_rows = [row for row in rows if row.operation is RuntimeLifecycleOperation.CANCEL]
    stable_rows = [
        row
        for row in cancel_rows
        if row.operation_id == operation_id
        and row.tenant_id == aggregate.task.tenant_id
        and row.runtime_execution_id == execution_id
    ]
    if len(cancel_rows) > 1 or (cancel_rows and len(stable_rows) != 1):
        raise RuntimeExecutionConflict("Runtime cancellation intent is ambiguous")
    stable_row = stable_rows[0] if stable_rows else None
    if stable_row is None:
        deadline = deadline_epoch + cancel_deadline_window
    else:
        if (
            type(stable_row.deadline) is not datetime
            or stable_row.deadline.tzinfo is None
            or stable_row.deadline.utcoffset() is None
        ):
            raise RuntimeExecutionConflict("Runtime cancellation deadline is invalid")
        deadline = stable_row.deadline.astimezone(timezone.utc)
    if deadline <= now:
        raise RuntimeExecutionConflict("Coordinated cancel deadline has expired")
    return operation_id, deadline, stable_row


def request_cancel_in_uow(
    uow: Any,
    *,
    aggregate: CoordinatedRuntimeAggregate,
    execution: RuntimeExecution,
    attempt: Any,
    drain: CoordinationRuntimeDrain,
    now: datetime,
    cancel_deadline_window: timedelta,
    deadline_epoch: datetime | None,
) -> CoordinatedCancelRequestResult:
    """Stage a crossed execution CANCEL lifecycle intent and outbox command."""
    operation_id, deadline, stable_row = select_cancel_deadline(
        aggregate,
        execution_id=execution.id,
        now=now,
        cancel_deadline_window=cancel_deadline_window,
        deadline_epoch=deadline_epoch,
    )
    intent_payload = {
        "tenant_id": aggregate.task.tenant_id,
        "runtime_execution_id": str(execution.id),
        "operation_id": operation_id,
        "operation": RuntimeLifecycleOperation.CANCEL.value,
        "deadline": deadline.astimezone(timezone.utc).isoformat(),
    }
    try:
        intent_digest = canonical_digest(intent_payload)
        if len(canonical_json_bytes(intent_payload)) > 65_536:
            raise ValueError("intent too large")
    except Exception as exc:
        raise RuntimeExecutionConflict("Runtime cancellation intent is invalid") from exc
    lifecycle_created = False
    changed: set[UUID] = set()
    if stable_row is not None:
        if stable_row.intent_digest != intent_digest or stable_row.deadline.astimezone(
            timezone.utc
        ) != deadline.astimezone(timezone.utc):
            raise RuntimeExecutionConflict("Runtime cancellation intent conflicts")
        lifecycle_id = stable_row.id
    else:
        lifecycle = RuntimeLifecycleIntent(
            id=uuid5(
                NAMESPACE_URL,
                f"{_LIFECYCLE_OUTBOX_PREFIX}{aggregate.task.tenant_id}:{operation_id}",
            ),
            tenant_id=aggregate.task.tenant_id,
            runtime_execution_id=execution.id,
            operation_id=operation_id,
            operation=RuntimeLifecycleOperation.CANCEL,
            intent_digest=intent_digest,
            status=RuntimeLifecycleStatus.REQUESTED,
            deadline=deadline.astimezone(timezone.utc),
            receipt_summary=None,
            version=1,
            created_at=now,
            updated_at=now,
            next_attempt_at=now,
        )
        uow.runtimes.add_lifecycle_operation(lifecycle)
        lifecycle_id = lifecycle.id
        lifecycle_created = True
        changed.add(lifecycle.id)
    execution_changed = False
    if execution.phase is not RuntimeExecutionPhase.CANCEL_REQUESTED:
        updated = execution.apply_observation(
            phase=RuntimeExecutionPhase.CANCEL_REQUESTED,
            provider_sequence=None,
            now=now,
        )
        uow.runtimes.save_execution(updated, tenant_id=aggregate.task.tenant_id)
        execution_changed = True
        changed.add(execution.id)
    outbox_added = outbox_add_if_absent(
        uow.outbox,
        lifecycle_outbox_envelope(
            tenant_id=aggregate.task.tenant_id,
            execution_id=execution.id,
            operation_id=operation_id,
            deadline=deadline,
            at=now,
        ),
    )
    return CoordinatedCancelRequestResult(
        lifecycle_id=lifecycle_id,
        operation_id=operation_id,
        operation=RuntimeLifecycleOperation.CANCEL.value,
        changed_ids=tuple(sorted(changed, key=str)),
        made_progress=bool(lifecycle_created or execution_changed or outbox_added),
    )


def release_attempt_accounting(uow: Any, task: Task, attempt: Any, *, now: datetime) -> bool:
    """Release budget/quota accounting exactly once in the caller UoW."""
    before = task.version
    BudgetController.release_attempt(task, attempt, at=now)
    QuotaController.release_attempt(uow, attempt)
    return task.version != before


def abort_prepared_in_uow(
    uow: Any,
    *,
    tenant_id: str,
    drain_id: UUID,
    execution: RuntimeExecution,
    run_id: UUID,
    attempt_id: UUID,
    fencing_token: int,
    now: datetime,
) -> RuntimeExecution:
    """Abort one PREPARED execution and stage its deterministic audit message."""
    aborted = execution.abort_before_dispatch(
        attempt_id=attempt_id,
        fencing_token=fencing_token,
        now=now,
    )
    uow.runtimes.save_execution(aborted, tenant_id=tenant_id)
    outbox_add_if_absent(
        uow.outbox,
        abort_audit_envelope(
            tenant_id=tenant_id,
            drain_id=drain_id,
            execution_id=aborted.id,
            run_id=run_id,
            attempt_id=attempt_id,
            at=now,
        ),
    )
    return aborted


def abort_audit_envelope(
    *,
    tenant_id: str,
    drain_id: UUID,
    execution_id: UUID,
    run_id: UUID,
    attempt_id: UUID,
    at: datetime,
) -> MessageEnvelope:
    message_id = uuid5(NAMESPACE_URL, f"{_ABORT_OUTBOX_PREFIX}{drain_id}:{execution_id}")
    return MessageEnvelope(
        schema_name="agentmesh.runtime.dispatch.aborted",
        schema_version=1,
        message_id=message_id,
        tenant_id=tenant_id,
        occurred_at=at,
        producer="agentmesh-runtime-control-plane-v1",
        correlation_id=execution_id,
        causation_id=None,
        idempotency_key=f"runtime-dispatch-abort:{drain_id}:{execution_id}",
        payload={
            "tenant_id": tenant_id,
            "runtime_execution_id": str(execution_id),
            "run_id": str(run_id),
            "attempt_id": str(attempt_id),
            "reason": "runtime.dispatch_aborted",
        },
    )


def lifecycle_outbox_envelope(
    *, tenant_id: str, execution_id: UUID, operation_id: str, deadline: datetime, at: datetime
) -> MessageEnvelope:
    message_id = uuid5(NAMESPACE_URL, f"{_LIFECYCLE_OUTBOX_PREFIX}{tenant_id}:{operation_id}")
    return MessageEnvelope(
        schema_name="agentmesh.runtime.lifecycle.requested",
        schema_version=1,
        message_id=message_id,
        tenant_id=tenant_id,
        occurred_at=at,
        producer="agentmesh-runtime-lifecycle-command-v1",
        correlation_id=execution_id,
        causation_id=None,
        idempotency_key=f"runtime-lifecycle:{operation_id}",
        payload={
            "tenant_id": tenant_id,
            "runtime_execution_id": str(execution_id),
            "operation_id": operation_id,
            "operation": RuntimeLifecycleOperation.CANCEL.value,
            "deadline": deadline.astimezone(timezone.utc).isoformat(),
        },
    )


def outbox_add_if_absent(outbox: Any, envelope: MessageEnvelope) -> bool:
    """Stage deterministic outbox data without hiding repository failures."""
    add_if_absent = getattr(outbox, "add_if_absent", None)
    if callable(add_if_absent):
        return bool(add_if_absent(envelope))
    outbox.add(envelope)
    return True


__all__ = [
    "CoordinatedCancelRequestResult",
    "abort_audit_envelope",
    "abort_prepared_in_uow",
    "lifecycle_outbox_envelope",
    "outbox_add_if_absent",
    "persisted_cancel_epoch",
    "release_attempt_accounting",
    "request_cancel_in_uow",
    "select_cancel_deadline",
]
