"""Task-scoped coordinated Runtime cancellation command."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from agentmesh.application.coordinated_runtime import CoordinatedRuntimeAggregateLocker
from agentmesh.application.coordinated_runtime_cancel_applier import (
    CoordinatedCancelApplication,
    CoordinatedRuntimeCancelApplier,
)
from agentmesh.application.coordinated_runtime_control import (
    CoordinatedCancelCompletion,
    CoordinatedCancelKind,
    CoordinatedCancelResult,
    normalize_cancel_reason,
    plan_cancel_request,
)
from agentmesh.application.coordinated_runtime_stop_primitives import (
    lifecycle_outbox_envelope,
)
from agentmesh.domain.coordination import (
    CoordinationRuntimeBoundary,
    CoordinationRuntimeDrainStatus,
    CoordinationRuntimeDrainTarget,
)
from agentmesh.domain.errors import (
    AuthorizationDenied,
    IdempotencyConflict,
    InvalidTaskInput,
    InvalidTaskTransition,
    RuntimeExecutionConflict,
)
from agentmesh.domain.identity import Permission, PrincipalContext
from agentmesh.domain.messaging import IdempotencyRecord, MessageEnvelope
from agentmesh.domain.runtime_execution import RuntimeLifecycleOperation
from agentmesh.domain.tasks import TERMINAL_TASK_STATUSES, TaskStatus
from agentmesh.runtime_sdk import canonical_digest

_COMMAND_VERSION = "coordinated-runtime-cancel-v1"
_AUDIT_SCHEMA = "agentmesh.runtime.coordinated-cancel-requested"
_AUDIT_PRODUCER = "agentmesh-runtime-control-plane-v1"
_TERMINAL_RESULT_KEYS = frozenset(
    {
        "kind",
        "tenant_id",
        "task_id",
        "task_status",
        "audit_event_id",
        "audit_occurred_at",
    }
)
_ACTIVE_RESULT_KEYS = frozenset(
    {
        "kind",
        "tenant_id",
        "task_id",
        "task_status",
        "effective_target",
        "effective_reason",
        "drain_id",
        "audit_event_id",
        "audit_occurred_at",
        "audit_anchor_run_id",
        "lifecycle_operation_ids",
        "made_progress",
    }
)
_ACTIVE_KINDS = {
    CoordinatedCancelKind.APPLIED,
    CoordinatedCancelKind.DRAINING_ACTIVE,
    CoordinatedCancelKind.WAIT_RECONCILIATION,
}


class CoordinatedRuntimeControlService:
    """Apply a privileged Task-wide coordinated cancellation command."""

    def __init__(
        self,
        *,
        uow_factory: Any,
        aggregate_locker: CoordinatedRuntimeAggregateLocker | None = None,
        cancel_applier: CoordinatedRuntimeCancelApplier | None = None,
        cancel_deadline_window: timedelta = timedelta(minutes=5),
    ) -> None:
        if not callable(uow_factory):
            raise InvalidTaskInput("Coordinated cancellation UoW factory is invalid")
        self._uow_factory = uow_factory
        self._aggregate_locker = aggregate_locker or CoordinatedRuntimeAggregateLocker()
        if (
            type(cancel_deadline_window) is not timedelta
            or cancel_deadline_window <= timedelta(0)
            or cancel_deadline_window > timedelta(days=7)
        ):
            raise InvalidTaskInput("Coordinated cancellation deadline window is invalid")
        self._cancel_applier = cancel_applier or CoordinatedRuntimeCancelApplier()
        self._cancel_deadline_window = cancel_deadline_window

    def request_cancel(
        self,
        *,
        tenant_id: str,
        task_id: UUID,
        principal: PrincipalContext,
        reason: str,
        idempotency_key: str,
        causation_id: UUID,
        at: datetime,
    ) -> CoordinatedCancelResult:
        normalized_reason, key, timestamp = _validate_command(
            tenant_id=tenant_id,
            task_id=task_id,
            principal=principal,
            reason=reason,
            idempotency_key=idempotency_key,
            causation_id=causation_id,
            at=at,
        )
        scope = _task_scope(tenant_id=tenant_id, task_id=task_id)
        request_hash = canonical_digest(
            {
                "command": _COMMAND_VERSION,
                "tenant_id": tenant_id,
                "task_id": str(task_id),
                "principal_id": principal.principal_id,
                "reason": normalized_reason,
                "causation_id": str(causation_id),
            }
        )
        audit_event_id = uuid5(NAMESPACE_URL, f"{scope}:{key}:{request_hash}")

        with self._uow_factory() as uow:
            # Contractual first repository operation: Task-first full aggregate lock.
            aggregate = self._aggregate_locker.lock(
                uow,
                tenant_id=tenant_id,
                task_id=task_id,
            )
            uow.idempotency.lock(scope, key)
            existing = uow.idempotency.get(scope, key)
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise IdempotencyConflict(
                        "Idempotency key was reused with a different cancellation request"
                    )
                if type(existing.result) is not dict:
                    raise RuntimeExecutionConflict(
                        "Cancellation idempotency projection is invalid"
                    )
                replay_values = dict(
                    uow=uow,
                    aggregate=aggregate,
                    tenant_id=tenant_id,
                    task_id=task_id,
                    audit_event_id=audit_event_id,
                    existing_result=existing.result,
                    causation_id=causation_id,
                    principal_id=principal.principal_id,
                    requested_reason=normalized_reason,
                    request_hash=request_hash,
                )
                if existing.result.get("kind") == CoordinatedCancelKind.ALREADY_TERMINAL.value:
                    return _terminal_replay(**replay_values)
                return _active_replay(**replay_values)

            if aggregate.task.status not in TERMINAL_TASK_STATUSES:
                plan = plan_cancel_request(aggregate, normalized_reason)
                application = self._cancel_applier.apply_in_uow(
                    uow,
                    aggregate=aggregate,
                    plan=plan,
                    now=timestamp,
                    cancel_deadline_window=self._cancel_deadline_window,
                )
                result_kind = _application_kind(application.completion)
                audit = _audit_event(
                    tenant_id=tenant_id,
                    task_id=task_id,
                    task_status=application.task_status,
                    principal_id=principal.principal_id,
                    requested_reason=normalized_reason,
                    request_hash=request_hash,
                    audit_event_id=audit_event_id,
                    causation_id=causation_id,
                    at=timestamp,
                    kind=result_kind,
                    effective_target=application.effective_drain.target,
                    effective_reason=application.effective_drain.reason,
                    drain_id=application.effective_drain.id,
                    audit_anchor_run_id=plan.audit_anchor_run_id,
                    lifecycle_operation_ids=application.lifecycle_operation_ids,
                    business_state_changed=application.made_progress,
                )
                result_payload = _active_result_payload(
                    kind=result_kind,
                    tenant_id=tenant_id,
                    task_id=task_id,
                    application=application,
                    audit_event_id=audit_event_id,
                    audit_occurred_at=timestamp,
                    audit_anchor_run_id=plan.audit_anchor_run_id,
                )
                uow.outbox.add(audit)
                uow.idempotency.add(
                    IdempotencyRecord.create(
                        scope=scope,
                        key=key,
                        request_hash=request_hash,
                        result=result_payload,
                    )
                )
                uow.commit()
                return _active_public_result(
                    kind=result_kind,
                    tenant_id=tenant_id,
                    task_id=task_id,
                    application=application,
                    audit_event_id=audit_event_id,
                    audit_anchor_run_id=plan.audit_anchor_run_id,
                )

            audit = _audit_event(
                tenant_id=tenant_id,
                task_id=task_id,
                task_status=aggregate.task.status,
                principal_id=principal.principal_id,
                requested_reason=normalized_reason,
                request_hash=request_hash,
                audit_event_id=audit_event_id,
                causation_id=causation_id,
                at=timestamp,
                kind=CoordinatedCancelKind.ALREADY_TERMINAL,
            )
            result_payload = _terminal_result_payload(
                tenant_id=tenant_id,
                task_id=task_id,
                task_status=aggregate.task.status,
                audit_event_id=audit_event_id,
                audit_occurred_at=timestamp,
            )
            uow.outbox.add(audit)
            uow.idempotency.add(
                IdempotencyRecord.create(
                    scope=scope,
                    key=key,
                    request_hash=request_hash,
                    result=result_payload,
                )
            )
            uow.commit()
            return CoordinatedCancelResult(
                kind=CoordinatedCancelKind.ALREADY_TERMINAL,
                tenant_id=tenant_id,
                task_id=task_id,
                task_status=aggregate.task.status,
                audit_event_id=audit_event_id,
            )


def _validate_command(**values: Any) -> tuple[str, str, datetime]:
    tenant_id = values["tenant_id"]
    task_id = values["task_id"]
    principal = values["principal"]
    if (
        type(tenant_id) is not str
        or not tenant_id
        or tenant_id != tenant_id.strip()
        or len(tenant_id.encode("utf-8")) > 128
    ):
        raise InvalidTaskInput("Coordinated cancellation tenant is invalid")
    if type(task_id) is not UUID or type(values["causation_id"]) is not UUID:
        raise InvalidTaskInput("Coordinated cancellation identity is invalid")
    if (
        type(principal) is not PrincipalContext
        or not principal.authenticated
        or principal.tenant_id != tenant_id
    ):
        raise AuthorizationDenied(
            "Coordinated cancellation requires an authenticated same-tenant Principal"
        )
    if Permission.TASK_OPERATE not in principal.permissions:
        raise AuthorizationDenied("Principal lacks Task operation permission")
    if (
        type(principal.principal_id) is not str
        or not principal.principal_id
        or principal.principal_id != principal.principal_id.strip()
        or len(principal.principal_id.encode("utf-8")) > 255
    ):
        raise AuthorizationDenied("Coordinated cancellation Principal identity is invalid")
    reason = normalize_cancel_reason(values["reason"])
    key = values["idempotency_key"]
    if (
        type(key) is not str
        or not key
        or key != key.strip()
        or len(key.encode("utf-8")) > 255
    ):
        raise InvalidTaskInput("Idempotency-Key must contain 1-255 UTF-8 bytes")
    timestamp = values["at"]
    if (
        type(timestamp) is not datetime
        or timestamp.tzinfo is None
        or timestamp.utcoffset() is None
    ):
        raise InvalidTaskInput("Coordinated cancellation timestamp must be timezone-aware")
    return reason, key, timestamp.astimezone(timezone.utc)


def _task_scope(*, tenant_id: str, task_id: UUID) -> str:
    binding = canonical_digest({"tenant_id": tenant_id, "task_id": str(task_id)})
    return f"coordinated-runtime-cancel:{binding}"


def _terminal_result_payload(
    *,
    tenant_id: str,
    task_id: UUID,
    task_status: TaskStatus,
    audit_event_id: UUID,
    audit_occurred_at: datetime,
) -> dict[str, Any]:
    return {
        "kind": CoordinatedCancelKind.ALREADY_TERMINAL.value,
        "tenant_id": tenant_id,
        "task_id": str(task_id),
        "task_status": task_status.value,
        "audit_event_id": str(audit_event_id),
        "audit_occurred_at": audit_occurred_at.isoformat(),
    }


def _active_result_payload(
    *,
    kind: CoordinatedCancelKind,
    tenant_id: str,
    task_id: UUID,
    application: CoordinatedCancelApplication,
    audit_event_id: UUID,
    audit_occurred_at: datetime,
    audit_anchor_run_id: UUID,
) -> dict[str, Any]:
    return {
        "kind": kind.value,
        "tenant_id": tenant_id,
        "task_id": str(task_id),
        "task_status": application.task_status.value,
        "effective_target": application.effective_drain.target.value,
        "effective_reason": application.effective_drain.reason,
        "drain_id": str(application.effective_drain.id),
        "audit_event_id": str(audit_event_id),
        "audit_occurred_at": audit_occurred_at.isoformat(),
        "audit_anchor_run_id": str(audit_anchor_run_id),
        "lifecycle_operation_ids": [
            str(value) for value in application.lifecycle_operation_ids
        ],
        "made_progress": application.made_progress,
    }


def _audit_event(
    *,
    tenant_id: str,
    task_id: UUID,
    task_status: TaskStatus,
    principal_id: str,
    requested_reason: str,
    request_hash: str,
    audit_event_id: UUID,
    causation_id: UUID,
    at: datetime,
    kind: CoordinatedCancelKind,
    effective_target: CoordinationRuntimeDrainTarget | None = None,
    effective_reason: str | None = None,
    drain_id: UUID | None = None,
    audit_anchor_run_id: UUID | None = None,
    lifecycle_operation_ids: tuple[UUID, ...] = (),
    business_state_changed: bool = False,
) -> MessageEnvelope:
    return MessageEnvelope(
        schema_name=_AUDIT_SCHEMA,
        schema_version=1,
        message_id=audit_event_id,
        tenant_id=tenant_id,
        occurred_at=at,
        producer=_AUDIT_PRODUCER,
        correlation_id=task_id,
        causation_id=causation_id,
        idempotency_key=f"event:{audit_event_id}",
        payload={
            "tenant_id": tenant_id,
            "task_id": str(task_id),
            "task_status": task_status.value,
            "principal_id": principal_id,
            "requested_reason": requested_reason,
            "request_hash": request_hash,
            "business_state_changed": business_state_changed,
            "result_kind": kind.value,
            "effective_target": effective_target.value if effective_target is not None else None,
            "effective_reason": effective_reason,
            "drain_id": str(drain_id) if drain_id is not None else None,
            "audit_anchor_run_id": (
                str(audit_anchor_run_id) if audit_anchor_run_id is not None else None
            ),
            "lifecycle_operation_ids": [str(value) for value in lifecycle_operation_ids],
        },
    )


def _terminal_replay(
    uow: Any,
    *,
    aggregate: Any,
    tenant_id: str,
    task_id: UUID,
    audit_event_id: UUID,
    existing_result: dict[str, Any],
    causation_id: UUID,
    principal_id: str,
    requested_reason: str,
    request_hash: str,
) -> CoordinatedCancelResult:
    if type(existing_result) is not dict or set(existing_result) != _TERMINAL_RESULT_KEYS:
        raise RuntimeExecutionConflict("Cancellation idempotency projection is invalid")
    try:
        stored_status = TaskStatus(existing_result["task_status"])
        stored_task_id = UUID(existing_result["task_id"])
        stored_audit_id = UUID(existing_result["audit_event_id"])
        occurred_at = datetime.fromisoformat(existing_result["audit_occurred_at"])
    except (TypeError, ValueError) as exc:
        raise RuntimeExecutionConflict("Cancellation idempotency projection is invalid") from exc
    if (
        existing_result["kind"] != CoordinatedCancelKind.ALREADY_TERMINAL.value
        or existing_result["tenant_id"] != tenant_id
        or stored_task_id != task_id
        or stored_status is not aggregate.task.status
        or stored_status not in TERMINAL_TASK_STATUSES
        or stored_audit_id != audit_event_id
        or occurred_at.tzinfo is None
        or occurred_at.utcoffset() != timedelta(0)
    ):
        raise RuntimeExecutionConflict("Cancellation idempotency projection is inconsistent")
    audit = uow.outbox.get(audit_event_id, tenant_id=tenant_id)
    expected = _audit_event(
        tenant_id=tenant_id,
        task_id=task_id,
        task_status=stored_status,
        principal_id=principal_id,
        requested_reason=requested_reason,
        request_hash=request_hash,
        audit_event_id=audit_event_id,
        causation_id=causation_id,
        at=occurred_at,
        kind=CoordinatedCancelKind.ALREADY_TERMINAL,
    )
    if audit is None or audit.to_dict() != expected.to_dict():
        raise RuntimeExecutionConflict("Cancellation audit projection is inconsistent")
    return CoordinatedCancelResult(
        kind=CoordinatedCancelKind.REPLAY,
        tenant_id=tenant_id,
        task_id=task_id,
        task_status=stored_status,
        audit_event_id=audit_event_id,
    )


def _active_replay(
    uow: Any,
    *,
    aggregate: Any,
    tenant_id: str,
    task_id: UUID,
    audit_event_id: UUID,
    existing_result: dict[str, Any],
    causation_id: UUID,
    principal_id: str,
    requested_reason: str,
    request_hash: str,
) -> CoordinatedCancelResult:
    if type(existing_result) is not dict or set(existing_result) != _ACTIVE_RESULT_KEYS:
        raise RuntimeExecutionConflict("Cancellation active replay projection is invalid")
    try:
        kind = CoordinatedCancelKind(existing_result["kind"])
        task_status = TaskStatus(existing_result["task_status"])
        effective_target = CoordinationRuntimeDrainTarget(existing_result["effective_target"])
        task_identity = UUID(existing_result["task_id"])
        drain_id = UUID(existing_result["drain_id"])
        stored_audit_id = UUID(existing_result["audit_event_id"])
        anchor_id = UUID(existing_result["audit_anchor_run_id"])
        occurred_at = datetime.fromisoformat(existing_result["audit_occurred_at"])
        lifecycle_ids = tuple(UUID(value) for value in existing_result["lifecycle_operation_ids"])
    except (TypeError, ValueError) as exc:
        raise RuntimeExecutionConflict("Cancellation active replay projection is invalid") from exc
    effective_reason = existing_result["effective_reason"]
    made_progress = existing_result["made_progress"]
    if (
        kind not in _ACTIVE_KINDS
        or existing_result["tenant_id"] != tenant_id
        or task_identity != task_id
        or stored_audit_id != audit_event_id
        or type(effective_reason) is not str
        or type(made_progress) is not bool
        or normalize_cancel_reason(effective_reason) != effective_reason
        or tuple(sorted(lifecycle_ids, key=str)) != lifecycle_ids
        or len(set(lifecycle_ids)) != len(lifecycle_ids)
        or occurred_at.tzinfo is None
        or occurred_at.utcoffset() != timedelta(0)
        or anchor_id not in {run.id for run in aggregate.runs}
    ):
        raise RuntimeExecutionConflict("Cancellation active replay projection is inconsistent")
    drain = uow.coordination_runtime_drains.get(
        drain_id,
        tenant_id=tenant_id,
        for_update=False,
    )
    expected_status = (
        CoordinationRuntimeDrainStatus.COMPLETE
        if kind is CoordinatedCancelKind.APPLIED
        else CoordinationRuntimeDrainStatus.DRAINING
    )
    if (
        drain is None
        or drain.task_id != task_id
        or drain.target is not effective_target
        or drain.reason != effective_reason
    ):
        raise RuntimeExecutionConflict("Cancellation replay drain projection is inconsistent")
    if aggregate.task.status is task_status:
        if drain.status is not expected_status:
            raise RuntimeExecutionConflict(
                "Cancellation replay drain projection is inconsistent"
            )
    elif (
        kind
        in {
            CoordinatedCancelKind.DRAINING_ACTIVE,
            CoordinatedCancelKind.WAIT_RECONCILIATION,
        }
        and aggregate.task.status in TERMINAL_TASK_STATUSES
        and drain.status is CoordinationRuntimeDrainStatus.COMPLETE
    ):
        run_ids = {run.id for run in aggregate.runs}
        if set(aggregate.boundary_classifications) != run_ids or any(
            boundary is not CoordinationRuntimeBoundary.KNOWN_TERMINAL
            for boundary in aggregate.boundary_classifications.values()
        ):
            raise RuntimeExecutionConflict(
                "Cancellation replay aggregate has unfinished members"
            )
        _validate_converged_task(aggregate.task, drain)
    else:
        raise RuntimeExecutionConflict("Cancellation replay Task projection is inconsistent")
    lifecycle_rows = {value.id: value for value in aggregate.lifecycle_operations}
    for lifecycle_id in lifecycle_ids:
        row = lifecycle_rows.get(lifecycle_id)
        if (
            row is None
            or row.operation is not RuntimeLifecycleOperation.CANCEL
            or type(row.created_at) is not datetime
            or row.created_at.tzinfo is None
            or row.created_at.utcoffset() != timedelta(0)
        ):
            raise RuntimeExecutionConflict(
                "Cancellation replay lifecycle projection is inconsistent"
            )
        lifecycle_event = lifecycle_outbox_envelope(
            tenant_id=tenant_id,
            execution_id=row.runtime_execution_id,
            operation_id=row.operation_id,
            deadline=row.deadline,
            at=row.created_at,
        )
        persisted_event = uow.outbox.get(lifecycle_event.message_id, tenant_id=tenant_id)
        if persisted_event is None or persisted_event.to_dict() != lifecycle_event.to_dict():
            raise RuntimeExecutionConflict(
                "Cancellation lifecycle Outbox projection is inconsistent"
            )
    expected_audit = _audit_event(
        tenant_id=tenant_id,
        task_id=task_id,
        task_status=task_status,
        principal_id=principal_id,
        requested_reason=requested_reason,
        request_hash=request_hash,
        audit_event_id=audit_event_id,
        causation_id=causation_id,
        at=occurred_at,
        kind=kind,
        effective_target=effective_target,
        effective_reason=effective_reason,
        drain_id=drain_id,
        audit_anchor_run_id=anchor_id,
        lifecycle_operation_ids=lifecycle_ids,
        business_state_changed=made_progress,
    )
    persisted_audit = uow.outbox.get(audit_event_id, tenant_id=tenant_id)
    if persisted_audit is None or persisted_audit.to_dict() != expected_audit.to_dict():
        raise RuntimeExecutionConflict("Cancellation audit projection is inconsistent")
    return CoordinatedCancelResult(
        kind=CoordinatedCancelKind.REPLAY,
        tenant_id=tenant_id,
        task_id=task_id,
        task_status=aggregate.task.status,
        effective_target=effective_target,
        drain_id=drain_id,
        audit_event_id=audit_event_id,
        audit_anchor_run_id=anchor_id,
        reason=effective_reason,
        lifecycle_operation_ids=lifecycle_ids,
    )


def _application_kind(completion: CoordinatedCancelCompletion) -> CoordinatedCancelKind:
    if completion is CoordinatedCancelCompletion.WAIT_ACTIVE:
        return CoordinatedCancelKind.DRAINING_ACTIVE
    if completion is CoordinatedCancelCompletion.WAIT_RECONCILIATION:
        return CoordinatedCancelKind.WAIT_RECONCILIATION
    return CoordinatedCancelKind.APPLIED


def _validate_converged_task(task: Any, drain: Any) -> None:
    try:
        if drain.target is CoordinationRuntimeDrainTarget.CANCELED:
            task.cancel_coordination_from_control(drain, at=task.updated_at)
        elif drain.target is CoordinationRuntimeDrainTarget.FAILED:
            task.fail_coordination_from_control(drain, at=task.updated_at)
        else:
            raise RuntimeExecutionConflict(
                "Cancellation replay drain target is inconsistent"
            )
    except InvalidTaskTransition as exc:
        raise RuntimeExecutionConflict(
            "Cancellation replay Task projection is inconsistent"
        ) from exc


def _active_public_result(
    *,
    kind: CoordinatedCancelKind,
    tenant_id: str,
    task_id: UUID,
    application: CoordinatedCancelApplication,
    audit_event_id: UUID,
    audit_anchor_run_id: UUID,
) -> CoordinatedCancelResult:
    return CoordinatedCancelResult(
        kind=kind,
        tenant_id=tenant_id,
        task_id=task_id,
        task_status=application.task_status,
        effective_target=application.effective_drain.target,
        drain_id=application.effective_drain.id,
        audit_event_id=audit_event_id,
        audit_anchor_run_id=audit_anchor_run_id,
        reason=application.effective_drain.reason,
        lifecycle_operation_ids=application.lifecycle_operation_ids,
    )
__all__ = ["CoordinatedRuntimeControlService"]
