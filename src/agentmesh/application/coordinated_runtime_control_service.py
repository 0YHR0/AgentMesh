"""Task-scoped coordinated Runtime cancellation command shell.

This c.2f6b3a slice deliberately implements only the already-terminal branch.
Active aggregate application remains fail-closed until the shared cancellation
applier is connected by a later slice.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from agentmesh.application.coordinated_runtime import CoordinatedRuntimeAggregateLocker
from agentmesh.application.coordinated_runtime_control import (
    CoordinatedCancelKind,
    CoordinatedCancelResult,
    normalize_cancel_reason,
)
from agentmesh.domain.errors import (
    AuthorizationDenied,
    IdempotencyConflict,
    InvalidTaskInput,
    RuntimeExecutionConflict,
)
from agentmesh.domain.identity import Permission, PrincipalContext
from agentmesh.domain.messaging import IdempotencyRecord, MessageEnvelope
from agentmesh.domain.tasks import TERMINAL_TASK_STATUSES, TaskStatus
from agentmesh.runtime_sdk import canonical_digest

_COMMAND_VERSION = "coordinated-runtime-cancel-v1"
_AUDIT_SCHEMA = "agentmesh.runtime.coordinated-cancel-requested"
_AUDIT_PRODUCER = "agentmesh-runtime-control-plane-v1"
_RESULT_KEYS = frozenset(
    {
        "kind",
        "tenant_id",
        "task_id",
        "task_status",
        "audit_event_id",
        "audit_occurred_at",
    }
)


class CoordinatedRuntimeControlService:
    """Apply a privileged Task-wide coordinated cancellation command."""

    def __init__(
        self,
        *,
        uow_factory: Any,
        aggregate_locker: CoordinatedRuntimeAggregateLocker | None = None,
    ) -> None:
        if not callable(uow_factory):
            raise InvalidTaskInput("Coordinated cancellation UoW factory is invalid")
        self._uow_factory = uow_factory
        self._aggregate_locker = aggregate_locker or CoordinatedRuntimeAggregateLocker()

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
            if aggregate.task.status not in TERMINAL_TASK_STATUSES:
                raise RuntimeExecutionConflict(
                    "Active coordinated cancellation is not implemented by this slice"
                )

            uow.idempotency.lock(scope, key)
            existing = uow.idempotency.get(scope, key)
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise IdempotencyConflict(
                        "Idempotency key was reused with a different cancellation request"
                    )
                return _terminal_replay(
                    uow,
                    aggregate_task_status=aggregate.task.status,
                    tenant_id=tenant_id,
                    task_id=task_id,
                    audit_event_id=audit_event_id,
                    existing_result=existing.result,
                    causation_id=causation_id,
                    principal_id=principal.principal_id,
                    reason=normalized_reason,
                    request_hash=request_hash,
                )

            audit = _audit_event(
                tenant_id=tenant_id,
                task_id=task_id,
                task_status=aggregate.task.status,
                principal_id=principal.principal_id,
                reason=normalized_reason,
                request_hash=request_hash,
                audit_event_id=audit_event_id,
                causation_id=causation_id,
                at=timestamp,
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


def _audit_event(
    *,
    tenant_id: str,
    task_id: UUID,
    task_status: TaskStatus,
    principal_id: str,
    reason: str,
    request_hash: str,
    audit_event_id: UUID,
    causation_id: UUID,
    at: datetime,
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
            "reason": reason,
            "request_hash": request_hash,
            "business_state_changed": False,
        },
    )


def _terminal_replay(
    uow: Any,
    *,
    aggregate_task_status: TaskStatus,
    tenant_id: str,
    task_id: UUID,
    audit_event_id: UUID,
    existing_result: dict[str, Any],
    causation_id: UUID,
    principal_id: str,
    reason: str,
    request_hash: str,
) -> CoordinatedCancelResult:
    if type(existing_result) is not dict or set(existing_result) != _RESULT_KEYS:
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
        or stored_status is not aggregate_task_status
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
        reason=reason,
        request_hash=request_hash,
        audit_event_id=audit_event_id,
        causation_id=causation_id,
        at=occurred_at,
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


__all__ = ["CoordinatedRuntimeControlService"]
