"""Task-scoped coordinated managed Runtime delivery orchestration.

This service is intentionally a protocol coordinator, not a transaction
owner.  Every injected control-plane command opens its own short UoW; adapter
calls happen between those commands with no database lock held here.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from agentmesh.application.coordinated_runtime_convergence import (
    CoordinatedKnownTerminalKind,
    CoordinatedKnownTerminalResult,
)
from agentmesh.application.coordinated_runtime_delivery import (
    CoordinatedDeliveryLeaseV1,
    CoordinatedDeliveryResult,
    CoordinatedDeliveryResultKind,
    CoordinatedRuntimeDeliveryResult,
    DeliveryInProgress,
    RecoveryCrossedProof,
    stable_dispatch_identity,
)
from agentmesh.application.coordinated_runtime_dispatch import (
    CoordinatedRuntimeBindReceiptKind,
    CoordinatedRuntimeBindReceiptResult,
    CoordinatedRuntimeDispatchKind,
    CoordinatedRuntimeDispatchResult,
    CoordinatedRuntimePrepareKind,
    CoordinatedRuntimePrepareResult,
)
from agentmesh.application.coordinated_runtime_predispatch_failure import (
    CoordinatedPredispatchFailureKind,
    CoordinatedPredispatchFailureResult,
)
from agentmesh.application.coordinated_runtime_provider_evidence import (
    CoordinatedProviderObservationKind,
    classify_provider_observation,
    normalize_dispatch_receipt,
    synthetic_unknown_observation,
)
from agentmesh.application.coordinated_runtime_unknown import (
    CoordinatedUnknownOutcomeKind,
    CoordinatedUnknownOutcomeResult,
)
from agentmesh.application.ports import ManagedRuntimeConflictObservation
from agentmesh.application.runtime_conflicts import build_managed_runtime_conflict_observation
from agentmesh.application.runtime_contracts import validate_terminal_observation
from agentmesh.application.runtime_snapshots import handle_from_snapshot
from agentmesh.domain.errors import InvalidMessage, InvalidTaskInput, InvalidTaskTransition
from agentmesh.domain.messaging import (
    RUN_REQUESTED_SCHEMA,
    RUN_REQUESTED_VERSION,
    MessageEnvelope,
)
from agentmesh.runtime_sdk import (
    RuntimeAssignment,
    RuntimeExecutionHandle,
    RuntimeObservation,
    RuntimePhase,
    ValidationReport,
)

_SAFE_REASON = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")


class CoordinatedRuntimeDeliveryService:
    """Coordinate one detached lease through provider and control-plane stages."""

    def __init__(
        self,
        *,
        acquisition_service: Any,
        managed_execution_port: Any,
        adapter: Any,
        dispatch_service: Any,
        predispatch_failure_service: Any,
        convergence_service: Any,
        unknown_service: Any,
        handle_snapshot_reader: Any,
        consumer_name: str,
        utc_clock: Callable[[], datetime],
    ) -> None:
        _require_method(acquisition_service, "classify_and_acquire")
        _require_methods(managed_execution_port, "assignment_for_delivery", "bind_delivery_context")
        _require_methods(adapter, "validate", "dispatch", "inspect")
        _require_methods(
            dispatch_service,
            "prepare_runtime_assignment",
            "cross_runtime_dispatch_boundary",
            "bind_dispatch_receipt",
        )
        _require_method(predispatch_failure_service, "fail_delivery")
        _require_method(convergence_service, "apply_delivery_terminal")
        _require_method(unknown_service, "park_delivery_unknown")
        if not (
            callable(handle_snapshot_reader)
            or callable(getattr(handle_snapshot_reader, "get_handle_snapshot", None))
        ):
            raise InvalidTaskInput("Coordinated delivery handle reader is invalid")
        if (
            type(consumer_name) is not str
            or not consumer_name.strip()
            or consumer_name != consumer_name.strip()
        ):
            raise InvalidTaskInput("Coordinated delivery consumer name is invalid")
        if not callable(utc_clock):
            raise InvalidTaskInput("Coordinated delivery UTC clock is invalid")
        self._acquisition = acquisition_service
        self._managed_execution = managed_execution_port
        self._adapter = adapter
        self._dispatch = dispatch_service
        self._predispatch_failure_service = predispatch_failure_service
        self._convergence = convergence_service
        self._unknown = unknown_service
        self._handle_reader = handle_snapshot_reader
        self._consumer_name = consumer_name
        self._clock = utc_clock

    def process(self, envelope: MessageEnvelope) -> CoordinatedRuntimeDeliveryResult:
        """Process one RunRequested envelope through the c2f4 state machine."""
        task_id, run_id = _validate_envelope(envelope)
        acquired = self._acquisition.classify_and_acquire(envelope, now=self._now())
        return self._route_acquisition(envelope, task_id, run_id, acquired)

    def _route_acquisition(
        self,
        envelope: MessageEnvelope,
        task_id: UUID,
        run_id: UUID,
        result: CoordinatedDeliveryResult,
    ) -> CoordinatedRuntimeDeliveryResult:
        if type(result) is not CoordinatedDeliveryResult:
            raise InvalidTaskTransition("Coordinated acquisition result type is invalid")
        kind = result.kind
        if type(kind) is not CoordinatedDeliveryResultKind:
            raise InvalidTaskTransition("Coordinated acquisition result kind is invalid")
        if kind is CoordinatedDeliveryResultKind.NOT_APPLICABLE:
            return CoordinatedRuntimeDeliveryResult.not_applicable(
                tenant_id=envelope.tenant_id, task_id=task_id, run_id=run_id
            )
        if kind is CoordinatedDeliveryResultKind.REPLAY_PROCESSED:
            return CoordinatedRuntimeDeliveryResult.replay(
                tenant_id=envelope.tenant_id, task_id=task_id, run_id=run_id
            )
        if kind in {
            CoordinatedDeliveryResultKind.BLOCKED_BY_DRAIN,
            CoordinatedDeliveryResultKind.WAITING_APPROVAL,
        }:
            return CoordinatedRuntimeDeliveryResult.blocked_by_drain(
                tenant_id=envelope.tenant_id,
                task_id=task_id,
                run_id=run_id,
                reason=_safe_reason(result.reason, "coordination.blocked_by_drain"),
            )
        if kind is CoordinatedDeliveryResultKind.IN_PROGRESS:
            raise DeliveryInProgress(task_id, run_id)
        if kind in {
            CoordinatedDeliveryResultKind.ACQUIRED,
            CoordinatedDeliveryResultKind.RECOVERED_PRE_BOUNDARY,
        }:
            if result.lease is None:
                raise InvalidTaskTransition("Coordinated delivery lease is missing")
            if (
                result.lease.tenant_id != envelope.tenant_id
                or result.lease.task_id != task_id
                or result.lease.run_id != run_id
            ):
                raise InvalidTaskTransition("Coordinated delivery lease identity conflicts")
            return self._deliver_lease(envelope, result.lease)
        if kind is CoordinatedDeliveryResultKind.RECOVER_CROSSED:
            if result.recovery_crossed_proof is None:
                raise InvalidTaskTransition("Coordinated recovery proof is missing")
            _validate_recovery_proof(result.recovery_crossed_proof)
            return self._recover_crossed(envelope, result.recovery_crossed_proof)
        raise InvalidTaskInput("Coordinated delivery acquisition result is invalid")

    def _deliver_lease(
        self, envelope: MessageEnvelope, lease: CoordinatedDeliveryLeaseV1
    ) -> CoordinatedRuntimeDeliveryResult:
        assignment: RuntimeAssignment | None = None
        failure_reason: str | None = None
        try:
            assignment = self._managed_execution.assignment_for_delivery(lease, lease.work_item)
            if type(assignment) is not RuntimeAssignment:
                raise InvalidTaskInput("Managed delivery Assignment is invalid")
            report = self._adapter.validate(assignment)
            if type(report) is not ValidationReport:
                raise InvalidTaskInput("Managed Runtime validation report is invalid")
        except Exception:
            failure_reason = "runtime.assignment_validation_failed"
            report = None
        if failure_reason is None and getattr(report, "valid", None) is not True:
            failure_reason = "runtime.assignment_invalid"
        if failure_reason is not None:
            return self._apply_predispatch_failure(
                envelope, lease, failure_reason, execution_id=None
            )
        if assignment is None:
            raise InvalidTaskTransition("Managed delivery Assignment is missing")

        prepared = _method(self._dispatch, "prepare_runtime_assignment")(
            tenant_id=lease.tenant_id,
            task_id=lease.task_id,
            run_id=lease.run_id,
            attempt_id=lease.attempt_id,
            fencing_token=lease.fencing_token,
            assignment=assignment,
            now=self._now(),
        )
        if type(prepared) is not CoordinatedRuntimePrepareResult:
            raise InvalidTaskTransition("Coordinated preparation result type is invalid")
        prepared_kind = prepared.kind
        if type(prepared_kind) is not CoordinatedRuntimePrepareKind:
            raise InvalidTaskTransition("Coordinated preparation result kind is invalid")
        _validate_execution_identity(
            prepared,
            lease,
            required=prepared_kind
            in {CoordinatedRuntimePrepareKind.PREPARED, CoordinatedRuntimePrepareKind.REPLAY},
        )
        if prepared_kind is CoordinatedRuntimePrepareKind.BLOCKED_BY_DRAIN:
            return self._reacquire_after_drain(envelope, lease.task_id, lease.run_id)
        if prepared_kind not in {
            CoordinatedRuntimePrepareKind.PREPARED,
            CoordinatedRuntimePrepareKind.REPLAY,
        }:
            raise InvalidTaskTransition("Coordinated preparation result is invalid")

        try:
            self._managed_execution.bind_delivery_context(assignment, lease, lease.work_item)
        except Exception:
            return self._apply_predispatch_failure(
                envelope,
                lease,
                "runtime.context_bind_failed",
                execution_id=lease.runtime_execution_intent_id,
            )

        crossed = _method(self._dispatch, "cross_runtime_dispatch_boundary")(
            tenant_id=lease.tenant_id,
            task_id=lease.task_id,
            run_id=lease.run_id,
            attempt_id=lease.attempt_id,
            fencing_token=lease.fencing_token,
            runtime_execution_id=lease.runtime_execution_intent_id,
            assignment_digest=assignment.assignment_digest or "",
            now=self._now(),
        )
        if type(crossed) is not CoordinatedRuntimeDispatchResult:
            raise InvalidTaskTransition("Coordinated boundary result type is invalid")
        crossed_kind = crossed.kind
        if type(crossed_kind) is not CoordinatedRuntimeDispatchKind:
            raise InvalidTaskTransition("Coordinated boundary result kind is invalid")
        _validate_execution_identity(
            crossed,
            lease,
            required=crossed_kind
            in {
                CoordinatedRuntimeDispatchKind.DISPATCH_AUTHORIZED,
                CoordinatedRuntimeDispatchKind.ALREADY_CROSSED,
            },
        )
        if crossed_kind is CoordinatedRuntimeDispatchKind.BLOCKED_BY_DRAIN:
            return self._reacquire_after_drain(envelope, lease.task_id, lease.run_id)
        if crossed_kind is CoordinatedRuntimeDispatchKind.ALREADY_CROSSED:
            raise DeliveryInProgress(lease.task_id, lease.run_id)
        if crossed_kind is not CoordinatedRuntimeDispatchKind.DISPATCH_AUTHORIZED:
            raise InvalidTaskTransition("Coordinated dispatch boundary result is invalid")

        dispatch_key, _ = stable_dispatch_identity(
            lease.tenant_id, lease.runtime_execution_intent_id, assignment.assignment_digest or ""
        )
        try:
            sdk_receipt = self._adapter.dispatch(assignment, dispatch_key=dispatch_key)
        except Exception:
            return self._park_synthetic_unknown(
                envelope,
                lease,
                assignment,
                "runtime.dispatch_response_unknown",
            )
        try:
            receipt = normalize_dispatch_receipt(lease, assignment, sdk_receipt)
        except InvalidTaskInput:
            conflict = _dispatch_receipt_conflict(
                sdk_receipt,
                execution_id=lease.runtime_execution_intent_id,
                assignment=assignment,
                fallback_observed_at=self._now(),
            )
            return self._park_synthetic_unknown(
                envelope,
                lease,
                assignment,
                "runtime.terminal_contract_invalid",
                conflict=conflict,
            )

        if receipt.handle is not None:
            # Handle binding is a separate short transaction and precedes any
            # terminal finalization, preserving reattach evidence.
            bound = self._dispatch.bind_dispatch_receipt(
                lease=lease, receipt=receipt, now=self._now()
            )
            if type(bound) is not CoordinatedRuntimeBindReceiptResult:
                raise InvalidTaskTransition("Coordinated receipt binding result type is invalid")
            bound_kind = bound.kind
            if type(bound_kind) is not CoordinatedRuntimeBindReceiptKind:
                raise InvalidTaskTransition("Coordinated receipt binding result kind is invalid")
            _validate_execution_identity(bound, lease, required=True)
            if bound_kind not in {
                CoordinatedRuntimeBindReceiptKind.BOUND,
                CoordinatedRuntimeBindReceiptKind.REPLAY,
            }:
                raise InvalidTaskTransition("Coordinated receipt binding result is invalid")
        observation = receipt.observation
        if observation is None:
            try:
                observation = self._adapter.inspect(receipt.handle)
            except Exception:
                return self._park_synthetic_unknown(
                    envelope,
                    lease,
                    assignment,
                    "runtime.inspect_response_unknown",
                )
        return self._finalize_observation(envelope, lease, assignment, observation)

    def _reacquire_after_drain(
        self, envelope: MessageEnvelope, task_id: UUID, run_id: UUID
    ) -> CoordinatedRuntimeDeliveryResult:
        result = self._acquisition.classify_and_acquire(envelope, now=self._now())
        if result.kind not in {
            CoordinatedDeliveryResultKind.BLOCKED_BY_DRAIN,
            CoordinatedDeliveryResultKind.WAITING_APPROVAL,
            CoordinatedDeliveryResultKind.REPLAY_PROCESSED,
        }:
            raise InvalidTaskTransition("Drain race did not produce a terminal acquisition result")
        return self._route_acquisition(envelope, task_id, run_id, result)

    def _apply_predispatch_failure(
        self,
        envelope: MessageEnvelope,
        lease: CoordinatedDeliveryLeaseV1,
        reason: str,
        *,
        execution_id: UUID | None,
    ) -> CoordinatedRuntimeDeliveryResult:
        result = self._predispatch_failure_service.fail_delivery(
            lease.tenant_id,
            lease.task_id,
            lease.run_id,
            lease.attempt_id,
            lease.fencing_token,
            self._consumer_name,
            envelope,
            reason,
            envelope.causation_id or envelope.message_id,
            self._now(),
        )
        if type(result) is not CoordinatedPredispatchFailureResult:
            raise InvalidTaskTransition("Predispatch failure result type is invalid")
        _validate_delivery_result_identity(result, lease, include_runtime=False)
        kind = result.kind
        if type(kind) is not CoordinatedPredispatchFailureKind:
            raise InvalidTaskTransition("Predispatch failure result kind is invalid")
        if kind is CoordinatedPredispatchFailureKind.REPLAY:
            return CoordinatedRuntimeDeliveryResult.replay(
                tenant_id=lease.tenant_id, task_id=lease.task_id, run_id=lease.run_id
            )
        if kind not in {
            CoordinatedPredispatchFailureKind.FAILED,
            CoordinatedPredispatchFailureKind.DRAINING_ACTIVE,
            CoordinatedPredispatchFailureKind.WAIT_RECONCILIATION,
        }:
            raise InvalidTaskTransition("Predispatch failure result is invalid")
        return CoordinatedRuntimeDeliveryResult.processed(
            tenant_id=lease.tenant_id,
            task_id=lease.task_id,
            run_id=lease.run_id,
            attempt_id=lease.attempt_id,
            execution_id=execution_id,
        )

    def _finalize_observation(
        self,
        envelope: MessageEnvelope,
        lease: CoordinatedDeliveryLeaseV1,
        assignment: RuntimeAssignment,
        observation: object,
        *,
        conflict: ManagedRuntimeConflictObservation | None = None,
    ) -> CoordinatedRuntimeDeliveryResult:
        if conflict is None:
            conflict = _terminal_conflict(
                observation,
                execution_id=lease.runtime_execution_intent_id,
                assignment_id=UUID(assignment.assignment_id),
                assignment_digest=assignment.assignment_digest or "",
                fallback_observed_at=self._now(),
            )
            if conflict is not None:
                return self._park_synthetic_unknown(
                    envelope,
                    lease,
                    assignment,
                    "runtime.terminal_contract_invalid",
                    conflict=conflict,
                )
        observation = _validate_inspected_observation(observation, lease, assignment)
        received_at = max(self._now(), observation.observed_at.astimezone(timezone.utc))
        kind = classify_provider_observation(observation)
        if kind is CoordinatedProviderObservationKind.KNOWN_TERMINAL:
            result = _method(self._convergence, "apply_delivery_terminal")(
                tenant_id=lease.tenant_id,
                task_id=lease.task_id,
                run_id=lease.run_id,
                attempt_id=lease.attempt_id,
                fencing_token=lease.fencing_token,
                runtime_execution_id=lease.runtime_execution_intent_id,
                observation=observation,
                received_at=received_at,
                causation_id=envelope.causation_id or envelope.message_id,
                consumer_name=self._consumer_name,
                envelope=envelope,
            )
            if type(result) is not CoordinatedKnownTerminalResult:
                raise InvalidTaskTransition("Known-terminal result type is invalid")
            _validate_delivery_result_identity(result, lease)
            result_kind = result.kind
            if type(result_kind) is not CoordinatedKnownTerminalKind:
                raise InvalidTaskTransition("Known-terminal result kind is invalid")
            if result_kind is CoordinatedKnownTerminalKind.REPLAY:
                return CoordinatedRuntimeDeliveryResult.replay(
                    tenant_id=lease.tenant_id, task_id=lease.task_id, run_id=lease.run_id
                )
            if result_kind not in {
                CoordinatedKnownTerminalKind.APPLIED,
                CoordinatedKnownTerminalKind.DRAINING_ACTIVE,
                CoordinatedKnownTerminalKind.DRAINING_RECONCILIATION,
            }:
                raise InvalidTaskTransition("Known-terminal result is invalid")
            return CoordinatedRuntimeDeliveryResult.processed(
                tenant_id=lease.tenant_id,
                task_id=lease.task_id,
                run_id=lease.run_id,
                attempt_id=lease.attempt_id,
                execution_id=lease.runtime_execution_intent_id,
            )
        result = _method(self._unknown, "park_delivery_unknown")(
            tenant_id=lease.tenant_id,
            task_id=lease.task_id,
            run_id=lease.run_id,
            attempt_id=lease.attempt_id,
            fencing_token=lease.fencing_token,
            runtime_execution_id=lease.runtime_execution_intent_id,
            observation=observation,
            received_at=received_at,
            causation_id=envelope.causation_id or envelope.message_id,
            consumer_name=self._consumer_name,
            envelope=envelope,
            conflict=conflict,
        )
        if type(result) is not CoordinatedUnknownOutcomeResult:
            raise InvalidTaskTransition("Unknown-outcome result type is invalid")
        _validate_delivery_result_identity(result, lease)
        result_kind = result.kind
        if type(result_kind) is not CoordinatedUnknownOutcomeKind:
            raise InvalidTaskTransition("Unknown-outcome result kind is invalid")
        if result_kind is CoordinatedUnknownOutcomeKind.REPLAY:
            return CoordinatedRuntimeDeliveryResult.replay(
                tenant_id=lease.tenant_id, task_id=lease.task_id, run_id=lease.run_id
            )
        if result_kind not in {
            CoordinatedUnknownOutcomeKind.PARKED,
            CoordinatedUnknownOutcomeKind.DRAINING_ACTIVE,
        }:
            raise InvalidTaskTransition("Unknown-outcome result is invalid")
        return CoordinatedRuntimeDeliveryResult.parked_unknown(
            tenant_id=lease.tenant_id,
            task_id=lease.task_id,
            run_id=lease.run_id,
            attempt_id=lease.attempt_id,
            execution_id=lease.runtime_execution_intent_id,
            reason="runtime.outcome_unknown",
        )

    def _park_synthetic_unknown(
        self,
        envelope: MessageEnvelope,
        lease: CoordinatedDeliveryLeaseV1,
        assignment: RuntimeAssignment,
        reason: str,
        *,
        conflict: ManagedRuntimeConflictObservation | None = None,
    ) -> CoordinatedRuntimeDeliveryResult:
        observation = synthetic_unknown_observation(
            lease, assignment, reason=reason, observed_at=self._now()
        )
        return self._finalize_observation(
            envelope, lease, assignment, observation, conflict=conflict
        )

    def _recover_crossed(
        self, envelope: MessageEnvelope, proof: RecoveryCrossedProof
    ) -> CoordinatedRuntimeDeliveryResult:
        handle = self._load_recovery_handle(envelope, proof)
        if handle is None:
            observation = _synthetic_recovery_unknown(
                envelope, proof, reason="runtime.recovery_handle_missing", observed_at=self._now()
            )
            return self._finalize_recovery_observation(envelope, proof, observation)
        try:
            observation = self._adapter.inspect(handle)
        except Exception:
            observation = _synthetic_recovery_unknown(
                envelope, proof, reason="runtime.recovery_inspect_unknown", observed_at=self._now()
            )
        return self._finalize_recovery_observation(envelope, proof, observation)

    def _load_recovery_handle(
        self, envelope: MessageEnvelope, proof: RecoveryCrossedProof
    ) -> RuntimeExecutionHandle | None:
        if (
            proof.persisted_handle_snapshot_id is None
            or proof.persisted_handle_snapshot_digest is None
        ):
            return None
        try:
            reader = getattr(self._handle_reader, "get_handle_snapshot", None)
            if callable(reader):
                snapshot = reader(proof.execution_id)
            else:
                snapshot = self._handle_reader(proof.execution_id)
            if snapshot is None:
                return None
            if (
                getattr(snapshot, "id", None) != proof.persisted_handle_snapshot_id
                or getattr(snapshot, "handle_digest", None)
                != proof.persisted_handle_snapshot_digest
                or getattr(snapshot, "tenant_id", envelope.tenant_id) != envelope.tenant_id
            ):
                return None
            handle = (
                snapshot
                if type(snapshot) is RuntimeExecutionHandle
                else handle_from_snapshot(snapshot)
            )
            if (
                handle.runtime_execution_id != str(proof.execution_id)
                or handle.assignment_id != str(proof.assignment_id)
                or handle.assignment_digest != proof.assignment_digest
            ):
                return None
            return handle
        except Exception:
            return None

    def _finalize_recovery_observation(
        self,
        envelope: MessageEnvelope,
        proof: RecoveryCrossedProof,
        observation: object,
        *,
        conflict: ManagedRuntimeConflictObservation | None = None,
    ) -> CoordinatedRuntimeDeliveryResult:
        if conflict is None:
            conflict = _terminal_conflict(
                observation,
                execution_id=proof.execution_id,
                assignment_id=proof.assignment_id,
                assignment_digest=proof.assignment_digest,
                fallback_observed_at=self._now(),
            )
            if conflict is not None:
                observation = _synthetic_recovery_unknown(
                    envelope,
                    proof,
                    reason="runtime.terminal_contract_invalid",
                    observed_at=self._now(),
                )
        _validate_recovery_observation(observation, proof)
        received_at = max(self._now(), observation.observed_at.astimezone(timezone.utc))
        kind = classify_provider_observation(observation)
        kwargs = {
            "tenant_id": envelope.tenant_id,
            "task_id": envelope.correlation_id,
            "run_id": _payload_uuid(envelope, "run_id"),
            "attempt_id": proof.expired_owner_attempt_id,
            "fencing_token": proof.expired_owner_fencing_token,
            "runtime_execution_id": proof.execution_id,
            "observation": observation,
            "received_at": received_at,
            "causation_id": envelope.causation_id or envelope.message_id,
            "consumer_name": self._consumer_name,
            "envelope": envelope,
        }
        if kind is CoordinatedProviderObservationKind.KNOWN_TERMINAL:
            result = _method(self._convergence, "apply_delivery_terminal")(**kwargs)
            if type(result) is not CoordinatedKnownTerminalResult:
                raise InvalidTaskTransition("Known-terminal result type is invalid")
            _validate_recovery_result_identity(result, envelope, proof)
            result_kind = result.kind
            if type(result_kind) is not CoordinatedKnownTerminalKind:
                raise InvalidTaskTransition("Known-terminal result kind is invalid")
            if result_kind is CoordinatedKnownTerminalKind.REPLAY:
                return CoordinatedRuntimeDeliveryResult.replay(
                    tenant_id=envelope.tenant_id,
                    task_id=envelope.correlation_id,
                    run_id=kwargs["run_id"],
                )
            if result_kind not in {
                CoordinatedKnownTerminalKind.APPLIED,
                CoordinatedKnownTerminalKind.DRAINING_ACTIVE,
                CoordinatedKnownTerminalKind.DRAINING_RECONCILIATION,
            }:
                raise InvalidTaskTransition("Known-terminal result is invalid")
            return CoordinatedRuntimeDeliveryResult.processed(
                tenant_id=envelope.tenant_id,
                task_id=envelope.correlation_id,
                run_id=kwargs["run_id"],
                attempt_id=proof.expired_owner_attempt_id,
                execution_id=proof.execution_id,
            )
        result = _method(self._unknown, "park_delivery_unknown")(
            **kwargs, conflict=conflict
        )
        if type(result) is not CoordinatedUnknownOutcomeResult:
            raise InvalidTaskTransition("Unknown-outcome result type is invalid")
        _validate_recovery_result_identity(result, envelope, proof)
        result_kind = result.kind
        if type(result_kind) is not CoordinatedUnknownOutcomeKind:
            raise InvalidTaskTransition("Unknown-outcome result kind is invalid")
        if result_kind is CoordinatedUnknownOutcomeKind.REPLAY:
            return CoordinatedRuntimeDeliveryResult.replay(
                tenant_id=envelope.tenant_id,
                task_id=envelope.correlation_id,
                run_id=kwargs["run_id"],
            )
        if result_kind not in {
            CoordinatedUnknownOutcomeKind.PARKED,
            CoordinatedUnknownOutcomeKind.DRAINING_ACTIVE,
        }:
            raise InvalidTaskTransition("Unknown-outcome result is invalid")
        return CoordinatedRuntimeDeliveryResult.parked_unknown(
            tenant_id=envelope.tenant_id,
            task_id=envelope.correlation_id,
            run_id=kwargs["run_id"],
            attempt_id=proof.expired_owner_attempt_id,
            execution_id=proof.execution_id,
            reason="runtime.outcome_unknown",
        )

    def _now(self) -> datetime:
        value = self._clock()
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
            raise InvalidTaskInput("Coordinated delivery UTC clock returned an invalid time")
        return value.astimezone(timezone.utc)


def _require_method(value: Any, name: str) -> None:
    if not callable(getattr(value, name, None)):
        raise InvalidTaskInput(f"Coordinated delivery dependency {name} is invalid")


def _method(value: Any, name: str) -> Callable[..., Any]:
    result = getattr(value, name, None)
    if not callable(result):
        raise InvalidTaskInput(f"Coordinated delivery dependency {name} is invalid")
    return result


def _require_methods(value: Any, *names: str) -> None:
    for name in names:
        _require_method(value, name)


def _validate_envelope(envelope: MessageEnvelope) -> tuple[UUID, UUID]:
    if type(envelope) is not MessageEnvelope:
        raise InvalidMessage("RunRequested envelope is invalid")
    if (
        envelope.schema_name != RUN_REQUESTED_SCHEMA
        or envelope.schema_version != RUN_REQUESTED_VERSION
        or type(envelope.tenant_id) is not str
        or not envelope.tenant_id.strip()
        or envelope.tenant_id != envelope.tenant_id.strip()
        or type(envelope.correlation_id) is not UUID
        or type(envelope.payload) is not dict
        or set(envelope.payload) != {"task_id", "run_id"}
    ):
        raise InvalidMessage("RunRequested envelope is invalid")
    task_id = _payload_uuid(envelope, "task_id")
    run_id = _payload_uuid(envelope, "run_id")
    if envelope.correlation_id != task_id:
        raise InvalidMessage("RunRequested correlation identity is invalid")
    return task_id, run_id


def _payload_uuid(envelope: MessageEnvelope, name: str) -> UUID:
    value = envelope.payload.get(name)
    if type(value) is not str:
        raise InvalidMessage("RunRequested identity is invalid")
    try:
        parsed = UUID(value)
    except (TypeError, ValueError) as exc:
        raise InvalidMessage("RunRequested identity is invalid") from exc
    if str(parsed) != value:
        raise InvalidMessage("RunRequested identity is invalid")
    return parsed


def _safe_reason(value: str | None, fallback: str) -> str:
    markers = ("secret", "password", "bearer ", "api_key", "access_token")
    if (
        type(value) is str
        and _SAFE_REASON.fullmatch(value)
        and len(value.encode()) <= 256
        and not any(marker in value.casefold() for marker in markers)
    ):
        return value
    if (
        type(fallback) is not str
        or not _SAFE_REASON.fullmatch(fallback)
        or len(fallback.encode()) > 256
        or any(marker in fallback.casefold() for marker in markers)
    ):
        raise InvalidTaskInput("Coordinated delivery reason fallback is unsafe")
    return fallback


def _validate_execution_identity(
    value: Any, lease: CoordinatedDeliveryLeaseV1, *, required: bool
) -> None:
    """Validate the execution identity exposed by prepare/boundary commands."""
    execution = getattr(value, "execution_id", None)
    if required and execution != lease.runtime_execution_intent_id:
        raise InvalidTaskTransition("Collaborator execution identity conflicts")
    if execution is not None and execution != lease.runtime_execution_intent_id:
        raise InvalidTaskTransition("Collaborator execution identity conflicts")


def _validate_delivery_result_identity(
    value: Any, lease: CoordinatedDeliveryLeaseV1, *, include_runtime: bool = True
) -> None:
    expected = {
        "tenant_id": lease.tenant_id,
        "task_id": lease.task_id,
        "run_id": lease.run_id,
        "attempt_id": lease.attempt_id,
        "runtime_execution_id": lease.runtime_execution_intent_id,
    }
    if not include_runtime:
        expected.pop("runtime_execution_id")
    for name, identity in expected.items():
        if getattr(value, name, None) != identity:
            raise InvalidTaskTransition(f"Collaborator {name} identity conflicts")


def _validate_recovery_result_identity(
    value: Any, envelope: MessageEnvelope, proof: RecoveryCrossedProof
) -> None:
    expected = {
        "tenant_id": envelope.tenant_id,
        "task_id": envelope.correlation_id,
        "run_id": _payload_uuid(envelope, "run_id"),
        "attempt_id": proof.expired_owner_attempt_id,
        "runtime_execution_id": proof.execution_id,
    }
    for name, identity in expected.items():
        if getattr(value, name, None) != identity:
            raise InvalidTaskTransition(f"Recovery collaborator {name} identity conflicts")


def _validate_recovery_proof(proof: RecoveryCrossedProof) -> None:
    if type(proof) is not RecoveryCrossedProof:
        raise InvalidTaskTransition("Recovery proof type is invalid")
    if proof.assignment_id is None or proof.assignment_digest is None:
        raise InvalidTaskTransition("Recovery proof lacks Assignment authority")


def _validate_inspected_observation(
    observation: Any,
    lease: CoordinatedDeliveryLeaseV1,
    assignment: RuntimeAssignment | None,
) -> RuntimeObservation:
    if type(observation) is not RuntimeObservation:
        raise InvalidTaskInput("Runtime inspection observation is invalid")
    if observation.runtime_execution_id != str(lease.runtime_execution_intent_id):
        raise InvalidTaskInput("Runtime inspection execution identity is invalid")
    if assignment is not None and (
        observation.assignment_id != assignment.assignment_id
        or observation.assignment_digest != assignment.assignment_digest
    ):
        raise InvalidTaskInput("Runtime inspection Assignment identity is invalid")
    classify_provider_observation(observation)
    return observation


def _terminal_conflict(
    candidate: object,
    *,
    execution_id: UUID,
    assignment_id: UUID,
    assignment_digest: str,
    fallback_observed_at: datetime,
) -> ManagedRuntimeConflictObservation | None:
    """Return bounded conflict evidence, or None for a valid terminal observation."""
    try:
        observation = validate_terminal_observation(
            candidate,
            runtime_execution_id=execution_id,
            assignment_id=assignment_id,
            assignment_digest=assignment_digest,
        )
        if observation.error is not None and observation.error.code == "runtime.protocol_error":
            raise InvalidTaskInput("Runtime provider returned a protocol-conflict observation")
    except InvalidTaskInput:
        return build_managed_runtime_conflict_observation(
            candidate,
            expected_execution_id=execution_id,
            expected_assignment_id=assignment_id,
            expected_assignment_digest=assignment_digest,
            fallback_observed_at=fallback_observed_at,
        )
    return None


def _dispatch_receipt_conflict(
    receipt: object,
    *,
    execution_id: UUID,
    assignment: RuntimeAssignment,
    fallback_observed_at: datetime,
) -> ManagedRuntimeConflictObservation:
    """Reduce a returned but invalid dispatch response to bounded conflict evidence."""
    candidate: object = receipt
    try:
        candidate = getattr(receipt, "observation", receipt)
    except Exception:
        # Accessor failures are provider-boundary behavior; the receipt itself
        # becomes a deterministic structural-invalid marker without raw data.
        candidate = receipt
    assignment_id = UUID(assignment.assignment_id)
    semantic = _terminal_conflict(
        candidate,
        execution_id=execution_id,
        assignment_id=assignment_id,
        assignment_digest=assignment.assignment_digest or "",
        fallback_observed_at=fallback_observed_at,
    )
    if semantic is not None:
        return semantic
    # The nested observation is valid, so the contradiction belongs to the
    # surrounding receipt identity/shape. Persist only the static marker.
    return build_managed_runtime_conflict_observation(
        receipt,
        expected_execution_id=execution_id,
        expected_assignment_id=assignment_id,
        expected_assignment_digest=assignment.assignment_digest or "",
        fallback_observed_at=fallback_observed_at,
    )


def _synthetic_recovery_unknown(
    envelope: MessageEnvelope,
    proof: RecoveryCrossedProof,
    *,
    reason: str,
    observed_at: datetime,
) -> RuntimeObservation:
    at = observed_at.astimezone(timezone.utc)
    run_id = _payload_uuid(envelope, "run_id")
    identity = ":".join(
        (
            envelope.tenant_id,
            str(envelope.correlation_id),
            str(run_id),
            str(proof.execution_id),
            reason,
        )
    )
    from agentmesh.runtime_sdk import canonical_digest

    observation_id = UUID(canonical_digest({"identity": identity})[:32])
    provider_event_id = f"control-plane-recovery-unknown:{observation_id}"
    return RuntimeObservation(
        observation_id=str(observation_id),
        runtime_execution_id=str(proof.execution_id),
        assignment_id=str(proof.assignment_id),
        assignment_digest=proof.assignment_digest,
        phase=RuntimePhase.OUTCOME_UNKNOWN,
        observed_at=at,
        provider_event_id=provider_event_id,
        snapshot_digest=canonical_digest({"identity": identity, "observed_at": at.isoformat()}),
        extensions={"control_plane_reason": reason},
    )


def _validate_recovery_observation(
    observation: Any, proof: RecoveryCrossedProof
) -> RuntimeObservation:
    """Ensure recovery evidence remains bound to the crossed proof authority."""
    if type(observation) is not RuntimeObservation:
        raise InvalidTaskInput("Recovery observation is invalid")
    if (
        observation.runtime_execution_id != str(proof.execution_id)
        or observation.assignment_id != str(proof.assignment_id)
        or observation.assignment_digest != proof.assignment_digest
    ):
        raise InvalidTaskInput("Recovery observation identity conflicts")
    classify_provider_observation(observation)
    return observation


__all__ = ["CoordinatedRuntimeDeliveryService"]
