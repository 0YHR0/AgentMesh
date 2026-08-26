"""Lifecycle intent consumption and immutable Runtime integrity evidence.

The lifecycle worker deliberately splits every operation into short database
transactions.  Claim/receipt state is committed before/after the provider
call; the adapter is never invoked while a control-plane row is locked.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from agentmesh.application.ports import UnitOfWorkFactory
from agentmesh.application.runtime_services import RuntimeRegistryService
from agentmesh.application.runtime_snapshots import handle_from_snapshot
from agentmesh.domain.errors import InvalidTaskInput
from agentmesh.domain.messaging import InboxMessage, MessageEnvelope
from agentmesh.domain.runtime_execution import (
    RuntimeLifecycleIntent,
    RuntimeLifecycleOperation,
    RuntimeLifecycleStatus,
)
from agentmesh.features import Feature, FeatureGateSet
from agentmesh.runtime_sdk import LifecycleReceipt, ManagedAgentRuntime, RuntimePhase

LIFECYCLE_SCHEMA = "agentmesh.runtime.lifecycle.requested"
LIFECYCLE_CONSUMER = "agentmesh-runtime-lifecycle-v1"
_DEFAULT_CLAIM_LEASE = timedelta(seconds=30)
# Leave a small scheduling margin so the provider transport cannot outlive the
# claim or the business deadline by exactly one boundary tick.
_TIMEOUT_MARGIN = timedelta(milliseconds=1)
_CROSSED_PHASES = {
    RuntimePhase.DISPATCHING,
    RuntimePhase.ACCEPTED,
    RuntimePhase.RUNNING,
    RuntimePhase.WAITING_INPUT,
    RuntimePhase.WAITING_APPROVAL,
    RuntimePhase.PAUSE_REQUESTED,
    RuntimePhase.PAUSED,
}


@dataclass(frozen=True)
class LifecycleProcessResult:
    operation: RuntimeLifecycleIntent | None
    provider_called: bool


class RuntimeLifecycleService:
    """Consume stable lifecycle intents for a tenant-scoped Runtime."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        tenant_id: str,
        feature_gates: FeatureGateSet,
        adapter: ManagedAgentRuntime,
        claim_lease: timedelta = _DEFAULT_CLAIM_LEASE,
        adapter_timeout: timedelta | None = timedelta(seconds=20),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._tenant_id = tenant_id
        self._feature_gates = feature_gates
        self._adapter = adapter
        if claim_lease <= timedelta(0):
            raise InvalidTaskInput("Runtime lifecycle claim lease must be positive")
        if adapter_timeout is None or (
            adapter_timeout <= timedelta(0) or adapter_timeout >= claim_lease
        ):
            raise InvalidTaskInput(
                "Runtime lifecycle adapter timeout must be shorter than its claim lease"
            )
        self._claim_lease = claim_lease
        self._adapter_timeout = adapter_timeout
        self._clock = clock

    def request_cancel(
        self, execution_id: UUID, *, deadline: datetime, now: datetime | None = None
    ) -> RuntimeLifecycleStatus:
        registry = RuntimeRegistryService(
            uow_factory=self._uow_factory,
            tenant_id=self._tenant_id,
            feature_gates=self._feature_gates,
        )
        operation_id = f"runtime-cancel:{execution_id}:v1"
        return registry.request_lifecycle_operation(
            execution_id=execution_id,
            operation_id=operation_id,
            operation=RuntimeLifecycleOperation.CANCEL,
            deadline=deadline,
            intent={
                "tenant_id": self._tenant_id,
                "runtime_execution_id": str(execution_id),
                "operation_id": operation_id,
                "operation": RuntimeLifecycleOperation.CANCEL.value,
                "deadline": deadline.astimezone(timezone.utc).isoformat(),
            },
            now=now,
        )

    def process_due(
        self,
        execution_id: UUID,
        *,
        operation_id: str | None = None,
        now: datetime | None = None,
    ) -> LifecycleProcessResult:
        """Claim one due operation, call the adapter, and persist its receipt."""
        self._feature_gates.require(Feature.MANAGED_AGENT_RUNTIME)
        timestamp = (
            now
            if now is not None
            else (self._clock() if self._clock is not None else datetime.now(timezone.utc))
        )
        handle_snapshot = self._read_handle(execution_id)
        handle = None
        if handle_snapshot is not None:
            try:
                handle = handle_from_snapshot(handle_snapshot)
            except InvalidTaskInput:
                # A corrupt immutable row is not provider-callable.  Schedule
                # an ordinary no-call retry so an operator can repair evidence.
                handle = None
        with self._uow_factory() as uow:
            lifecycle = uow.runtimes.claim_due_lifecycle(
                tenant_id=self._tenant_id,
                now=timestamp,
                lease=self._claim_lease,
                execution_id=execution_id,
                operation_id=operation_id,
                has_handle=handle is not None,
            )
            uow.commit()
        if lifecycle is None or handle is None:
            return LifecycleProcessResult(lifecycle, False)

        # The snapshot is read before claim to keep the claim transaction
        # short, then rebound to the persisted execution immediately before
        # provider contact.  A mismatch is never sent to the adapter.
        execution = self._read_execution(execution_id)
        if execution is None or (
            handle.runtime_execution_id != str(execution.id)
            or handle.runtime_version_id != str(execution.runtime_version_id)
            or handle.assignment_id != str(execution.assignment_id)
            or handle.assignment_digest != execution.assignment_digest
        ):
            self._release_without_provider_call(
                lifecycle, now=timestamp, error_code="runtime.handle_contract_invalid"
            )
            return LifecycleProcessResult(lifecycle, False)

        # The claim transaction and handle rebind can consume part of the
        # lease.  Production must use an immediate pre-call clock reading;
        # explicit ``now`` remains deterministic for existing unit fixtures.
        pre_call_timestamp = (
            self._clock()
            if self._clock is not None
            else (datetime.now(timezone.utc) if now is None else timestamp)
        )
        claim_budget = lifecycle.claim_expires_at - pre_call_timestamp
        operation_budget = lifecycle.deadline - pre_call_timestamp
        call_budget = min(self._adapter_timeout, claim_budget, operation_budget)
        call_budget -= _TIMEOUT_MARGIN
        if call_budget <= timedelta(0):
            updated = self._release_without_provider_call(
                lifecycle,
                now=timestamp,
                error_code="runtime.lifecycle_timeout",
            )
            return LifecycleProcessResult(updated or lifecycle, False)
        try:
            receipt = self._call_adapter(lifecycle, handle, timeout=call_budget)
        except TimeoutError:
            updated = self._retry(
                lifecycle,
                now=timestamp,
                error_code="runtime.lifecycle_timeout",
                provider_call=True,
            )
            return LifecycleProcessResult(updated or lifecycle, True)
        except Exception:
            updated = self._retry(
                lifecycle,
                now=timestamp,
                error_code="runtime.lifecycle_transport_error",
                provider_call=True,
            )
            return LifecycleProcessResult(updated or lifecycle, True)
        if not _valid_receipt(receipt, lifecycle):
            updated = self._retry(
                lifecycle,
                now=timestamp,
                error_code="runtime.lifecycle_receipt_invalid",
                provider_call=True,
            )
            return LifecycleProcessResult(updated or lifecycle, True)
        updated = self._persist_receipt(lifecycle, receipt, now=timestamp)
        return LifecycleProcessResult(updated or lifecycle, True)

    def process_next_due(self, *, now: datetime | None = None) -> LifecycleProcessResult | None:
        """Scan and process one due operation for a retry worker."""
        timestamp = now or datetime.now(timezone.utc)
        with self._uow_factory() as uow:
            refs = uow.runtimes.list_due_lifecycle_refs(
                tenant_id=self._tenant_id, now=timestamp, limit=32
            )
            uow.commit()
        if not refs:
            return None
        execution_id, operation_id = refs[0]
        return self.process_due(execution_id, operation_id=operation_id, now=timestamp)

    def claim_deadline(
        self,
        *,
        execution_id: UUID | None = None,
        operation_id: str | None = None,
        now: datetime | None = None,
    ) -> RuntimeLifecycleIntent | None:
        """Claim an expired lifecycle row for a later inspect/parking stage."""
        timestamp = now or datetime.now(timezone.utc)
        with self._uow_factory() as uow:
            value = uow.runtimes.claim_deadline_lifecycle(
                tenant_id=self._tenant_id,
                now=timestamp,
                lease=self._claim_lease,
                execution_id=execution_id,
                operation_id=operation_id,
            )
            uow.commit()
            return value

    def process_envelope(self, envelope: MessageEnvelope) -> LifecycleProcessResult:
        """Deduplicate a lifecycle wake-up; retries are driven by due rows."""
        if envelope.schema_name != LIFECYCLE_SCHEMA:
            raise InvalidTaskInput("Unknown Runtime lifecycle message")
        if envelope.tenant_id != self._tenant_id:
            raise InvalidTaskInput("Runtime lifecycle message tenant mismatch")
        try:
            execution_id = UUID(str(envelope.payload["runtime_execution_id"]))
            operation_id = str(envelope.payload["operation_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise InvalidTaskInput("Runtime lifecycle message payload is invalid") from exc
        with self._uow_factory() as uow:
            if uow.inbox.contains(self._tenant_id, LIFECYCLE_CONSUMER, envelope.message_id):
                return LifecycleProcessResult(None, False)
        result = self.process_due(execution_id, operation_id=operation_id)
        with self._uow_factory() as uow:
            if not uow.inbox.contains(self._tenant_id, LIFECYCLE_CONSUMER, envelope.message_id):
                uow.inbox.add(InboxMessage.processed(LIFECYCLE_CONSUMER, envelope))
            uow.commit()
        return result

    def _read_handle(self, execution_id: UUID) -> Any:
        with self._uow_factory() as uow:
            return uow.runtimes.get_handle_snapshot(execution_id, tenant_id=self._tenant_id)

    def _read_execution(self, execution_id: UUID) -> Any:
        with self._uow_factory() as uow:
            return uow.runtimes.get_execution(execution_id, tenant_id=self._tenant_id)

    def _call_adapter(
        self,
        lifecycle: RuntimeLifecycleIntent,
        handle: Any,
        *,
        timeout: timedelta,
    ) -> LifecycleReceipt:
        if lifecycle.operation is RuntimeLifecycleOperation.CANCEL:
            return self._adapter.request_cancel(
                handle,
                cancellation_id=lifecycle.operation_id,
                deadline=lifecycle.deadline,
                timeout=timeout,
            )
        if lifecycle.operation is RuntimeLifecycleOperation.PAUSE:
            return self._adapter.request_pause(
                handle, operation_id=lifecycle.operation_id, timeout=timeout
            )
        return self._adapter.request_resume(
            handle, operation_id=lifecycle.operation_id, timeout=timeout
        )

    def _retry(
        self,
        lifecycle: RuntimeLifecycleIntent,
        *,
        now: datetime,
        error_code: str,
        provider_call: bool,
    ) -> RuntimeLifecycleIntent | None:
        with self._uow_factory() as uow:
            current = uow.runtimes.find_lifecycle_operation(
                lifecycle.runtime_execution_id,
                tenant_id=self._tenant_id,
                operation_id=lifecycle.operation_id,
                for_update=True,
            )
            if current is None or current.claim_token != lifecycle.claim_token:
                uow.commit()
                return None
            updated = current.schedule_retry(
                now=now, error_code=error_code, provider_call=provider_call
            )
            uow.runtimes.save_lifecycle_operation(updated)
            uow.commit()
            return updated

    def _release_without_provider_call(
        self,
        lifecycle: RuntimeLifecycleIntent,
        *,
        now: datetime,
        error_code: str,
    ) -> RuntimeLifecycleIntent | None:
        with self._uow_factory() as uow:
            current = uow.runtimes.find_lifecycle_operation(
                lifecycle.runtime_execution_id,
                tenant_id=self._tenant_id,
                operation_id=lifecycle.operation_id,
                for_update=True,
            )
            if current is None or current.claim_token != lifecycle.claim_token:
                uow.commit()
                return None
            updated = current.release_claim_without_call(now=now, error_code=error_code)
            uow.runtimes.save_lifecycle_operation(updated)
            uow.commit()
            return updated

    def _persist_receipt(
        self, lifecycle: RuntimeLifecycleIntent, receipt: LifecycleReceipt, *, now: datetime
    ) -> RuntimeLifecycleIntent | None:
        summary = {
            "operation_id": receipt.operation_id,
            "runtime_execution_id": receipt.runtime_execution_id,
            "operation": receipt.operation,
            "accepted": receipt.accepted,
            "observed_phase": receipt.observed_phase.value
            if receipt.observed_phase is not None
            else None,
            "safe_message": receipt.safe_message,
        }
        with self._uow_factory() as uow:
            current = uow.runtimes.find_lifecycle_operation(
                lifecycle.runtime_execution_id,
                tenant_id=self._tenant_id,
                operation_id=lifecycle.operation_id,
                for_update=True,
            )
            if current is None or current.claim_token != lifecycle.claim_token:
                uow.commit()
                return None
            updated = current.finish_receipt(
                accepted=receipt.accepted, receipt_summary=summary, now=now
            )
            uow.runtimes.save_lifecycle_operation(updated)
            uow.commit()
            return updated


def _valid_receipt(receipt: object, lifecycle: RuntimeLifecycleIntent) -> bool:
    if type(receipt) is not LifecycleReceipt:
        return False
    if (
        receipt.operation_id != lifecycle.operation_id
        or receipt.runtime_execution_id != str(lifecycle.runtime_execution_id)
        or receipt.operation != lifecycle.operation.value
        or receipt.observed_phase is None
    ):
        return False
    phase = receipt.observed_phase
    if receipt.accepted:
        return phase is RuntimePhase.CANCEL_REQUESTED or phase.terminal
    return phase in _CROSSED_PHASES or phase.terminal


__all__ = ["LifecycleProcessResult", "RuntimeLifecycleService"]
