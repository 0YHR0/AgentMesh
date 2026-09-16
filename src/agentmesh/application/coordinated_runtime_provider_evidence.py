"""Pure normalization of managed provider dispatch evidence.

The normalizer is deliberately independent of adapters, repositories, and
unit-of-work objects.  It validates the detached delivery authority and turns
the SDK receipt into the bounded control-plane receipt used by the next
transaction.  Provider exceptions and response payloads never enter this
module's public values.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from agentmesh.application.coordinated_runtime_delivery import (
    CoordinatedDeliveryLeaseV1,
    CoordinatedDispatchReceiptV1,
    canonical_work_item_bytes,
    stable_dispatch_identity,
)
from agentmesh.domain.errors import InvalidTaskInput
from agentmesh.runtime_sdk import (
    DispatchReceipt,
    RuntimeAssignment,
    RuntimeExecutionHandle,
    RuntimeObservation,
    RuntimePhase,
    canonical_digest,
    thaw_json,
)

_SAFE_REASON = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")
_MAX_REASON_BYTES = 256
_KNOWN_TERMINAL = frozenset(
    {
        RuntimePhase.SUCCEEDED,
        RuntimePhase.FAILED,
        RuntimePhase.CANCELED,
        RuntimePhase.TIMED_OUT,
    }
)
_UNKNOWN = frozenset({RuntimePhase.LOST, RuntimePhase.OUTCOME_UNKNOWN})
_UNKNOWN_NAMESPACE = "agentmesh:coordinated-runtime:synthetic-unknown:v1"


class CoordinatedProviderObservationKind(str, Enum):
    """Closed classification accepted by delivery finalization."""

    KNOWN_TERMINAL = "KNOWN_TERMINAL"
    LOST = "LOST"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"


def _invalid(detail: str) -> InvalidTaskInput:
    return InvalidTaskInput(f"Coordinated provider evidence {detail}")


def _utc(value: Any) -> datetime:
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise _invalid("timestamp is invalid")
    return value.astimezone(timezone.utc)


def _safe_reason(reason: Any) -> str:
    if (
        type(reason) is not str
        or not _SAFE_REASON.fullmatch(reason)
        or len(reason.encode("utf-8")) > _MAX_REASON_BYTES
    ):
        raise _invalid("reason is not a bounded safe code")
    if any(
        marker in reason.casefold()
        for marker in ("secret", "password", "bearer ", "api_key", "access_token")
    ):
        raise _invalid("reason contains secret material")
    return reason


def _assignment_execution_id(assignment: RuntimeAssignment) -> UUID:
    value = assignment.correlation_ids.get("runtime_execution_id")
    if type(value) is not str:
        raise _invalid("Assignment execution identity is missing")
    try:
        execution_id = UUID(value)
    except (TypeError, ValueError) as exc:
        raise _invalid("Assignment execution identity is invalid") from exc
    if str(execution_id) != value:
        raise _invalid("Assignment execution identity is not canonical")
    return execution_id


def _validate_assignment(
    lease: CoordinatedDeliveryLeaseV1, assignment: RuntimeAssignment
) -> UUID:
    if type(lease) is not CoordinatedDeliveryLeaseV1:
        raise _invalid("lease is invalid")
    if type(assignment) is not RuntimeAssignment:
        raise _invalid("Assignment is invalid")
    execution_id = _assignment_execution_id(assignment)
    if (
        assignment.tenant_id != lease.tenant_id
        or assignment.task_id != str(lease.task_id)
        or assignment.run_id != str(lease.run_id)
        or assignment.runtime_version_id != str(lease.runtime_version_id)
        or assignment.agent_version_id != str(lease.agent_version_id)
        or assignment.agent_version_digest != lease.agent_version_digest
        or assignment.run_role != lease.role.value
        or assignment.revision != lease.run_revision
        or execution_id != lease.runtime_execution_intent_id
    ):
        raise _invalid("Assignment identity conflicts with lease")
    extension = assignment.extensions.get("coordinated_delivery")
    if not isinstance(extension, dict) or (
        extension.get("assignment_projection_digest")
        != lease.assignment_projection_digest
    ):
        raise _invalid("Assignment projection authority is missing or conflicts")
    # Force the canonical assignment bytes before any provider evidence is
    # accepted; this also rejects mutable/non-JSON test doubles.
    try:
        # Persistence freezes lease JSON as mappingproxy/tuple, whereas the
        # SDK assignment contract uses dict/list.  Compare their canonical
        # JSON meaning rather than Python container types.  The digest keeps
        # nested values and keys covered, so a real payload change remains a
        # fail-closed authority conflict.
        assignment_work_item_digest = canonical_digest(
            {
                "objective": assignment.objective,
                "input": thaw_json(assignment.structured_input),
            }
        )
        lease_work_item_digest = canonical_digest(
            {
                "objective": lease.work_item.objective,
                "input": thaw_json(lease.work_item.input),
            }
        )
        assignment.to_dict()
        canonical_work_item_bytes(lease.work_item)
    except Exception as exc:
        raise _invalid("Assignment canonicalization failed") from exc
    if assignment_work_item_digest != lease_work_item_digest:
        raise _invalid("Assignment work item conflicts with lease")
    return execution_id


def _receipt_value(receipt: Any, name: str, *, required: bool = True) -> Any:
    value = getattr(receipt, name, None)
    if required and value is None:
        raise _invalid(f"receipt {name} is missing")
    return value


def _validate_observation(
    observation: RuntimeObservation,
    *,
    lease: CoordinatedDeliveryLeaseV1,
    assignment: RuntimeAssignment,
) -> RuntimeObservation:
    if type(observation) is not RuntimeObservation:
        raise _invalid("receipt observation is invalid")
    if (
        observation.runtime_execution_id != str(lease.runtime_execution_intent_id)
        or observation.assignment_id != assignment.assignment_id
        or observation.assignment_digest != assignment.assignment_digest
    ):
        raise _invalid("receipt observation identity conflicts")
    classify_provider_observation(observation)
    return observation


def normalize_dispatch_receipt(
    lease: CoordinatedDeliveryLeaseV1,
    assignment: RuntimeAssignment,
    receipt: DispatchReceipt,
) -> CoordinatedDispatchReceiptV1:
    """Validate and convert one SDK dispatch receipt without side effects."""
    execution_id = _validate_assignment(lease, assignment)
    if type(receipt) is not DispatchReceipt and not all(
        hasattr(receipt, name)
        for name in ("dispatch_key", "runtime_execution_id", "assignment_digest")
    ):
        raise _invalid("receipt is invalid")
    dispatch_key, dispatch_digest = stable_dispatch_identity(
        lease.tenant_id, execution_id, assignment.assignment_digest or ""
    )
    receipt_key = _receipt_value(receipt, "dispatch_key")
    receipt_execution = _receipt_value(receipt, "runtime_execution_id")
    receipt_assignment_digest = _receipt_value(receipt, "assignment_digest")
    if (
        type(receipt_key) is not str
        or receipt_key != dispatch_key
        or receipt_execution != str(execution_id)
        or receipt_assignment_digest != assignment.assignment_digest
    ):
        raise _invalid("receipt dispatch identity conflicts")

    handle = _receipt_value(receipt, "handle", required=False)
    observation = _receipt_value(receipt, "observation", required=False)
    if type(handle) not in {RuntimeExecutionHandle, type(None)}:
        raise _invalid("receipt handle is invalid")
    if handle is None and observation is None:
        raise _invalid("receipt requires a handle or observation")
    if handle is not None and (
        handle.runtime_execution_id != str(execution_id)
        or handle.runtime_version_id != str(lease.runtime_version_id)
        or handle.assignment_id != assignment.assignment_id
        or handle.assignment_digest != assignment.assignment_digest
    ):
        raise _invalid("receipt handle identity conflicts")
    if observation is not None:
        _validate_observation(observation, lease=lease, assignment=assignment)

    provider_ref = _receipt_value(receipt, "provider_execution_ref", required=False)
    provider_generation = _receipt_value(receipt, "provider_generation", required=False)
    if handle is not None:
        if provider_ref is not None and provider_ref != handle.provider_execution_ref:
            raise _invalid("receipt provider reference conflicts")
        if provider_generation is not None and provider_generation != handle.provider_generation:
            raise _invalid("receipt provider generation conflicts")
        provider_ref = handle.provider_execution_ref
        provider_generation = handle.provider_generation
    return CoordinatedDispatchReceiptV1(
        schema_version=1,
        dispatch_digest=dispatch_digest,
        assignment_digest=assignment.assignment_digest or "",
        runtime_execution_id=execution_id,
        assignment_id=UUID(assignment.assignment_id),
        provider_execution_ref=provider_ref,
        provider_generation=provider_generation,
        handle=handle,
        observation=observation,
    )


def classify_provider_observation(
    observation: RuntimeObservation,
) -> CoordinatedProviderObservationKind:
    """Accept only known terminal or explicit unknown terminal phases."""
    if type(observation) is not RuntimeObservation:
        raise _invalid("observation is invalid")
    if observation.phase in _KNOWN_TERMINAL:
        return CoordinatedProviderObservationKind.KNOWN_TERMINAL
    if observation.phase is RuntimePhase.LOST:
        return CoordinatedProviderObservationKind.LOST
    if observation.phase is RuntimePhase.OUTCOME_UNKNOWN:
        return CoordinatedProviderObservationKind.OUTCOME_UNKNOWN
    raise _invalid("observation phase is not terminal")


def synthetic_unknown_observation(
    lease: CoordinatedDeliveryLeaseV1,
    assignment: RuntimeAssignment,
    *,
    reason: str,
    observed_at: datetime,
) -> RuntimeObservation:
    """Create deterministic, provider-free OUTCOME_UNKNOWN evidence."""
    execution_id = _validate_assignment(lease, assignment)
    safe_reason = _safe_reason(reason)
    timestamp = _utc(observed_at)
    # The evidence identity is stable for one delivery authority and safe
    # reason.  ``observed_at`` belongs to the canonical observation digest,
    # while Inbox/acquisition replay prevents a later timestamp from being
    # submitted as a second evidence event.
    identity = ":".join(
        (
            lease.tenant_id,
            str(lease.task_id),
            str(lease.run_id),
            str(execution_id),
            assignment.assignment_id,
            assignment.assignment_digest or "",
            safe_reason,
        )
    )
    observation_id = uuid5(NAMESPACE_URL, f"{_UNKNOWN_NAMESPACE}:observation:{identity}")
    provider_event_uuid = uuid5(NAMESPACE_URL, f"{_UNKNOWN_NAMESPACE}:event:{identity}")
    provider_event_id = f"control-plane-unknown:{provider_event_uuid}"
    snapshot_digest = canonical_digest(
        {
            "kind": RuntimePhase.OUTCOME_UNKNOWN.value,
            "reason": safe_reason,
            "tenant_id": lease.tenant_id,
            "task_id": str(lease.task_id),
            "run_id": str(lease.run_id),
            "runtime_execution_id": str(execution_id),
            "assignment_id": assignment.assignment_id,
            "assignment_digest": assignment.assignment_digest,
            "observed_at": timestamp.isoformat(),
        }
    )
    return RuntimeObservation(
        observation_id=str(observation_id),
        runtime_execution_id=str(execution_id),
        assignment_id=assignment.assignment_id,
        assignment_digest=assignment.assignment_digest or "",
        phase=RuntimePhase.OUTCOME_UNKNOWN,
        observed_at=timestamp,
        provider_event_id=provider_event_id,
        snapshot_digest=snapshot_digest,
        extensions={"control_plane_reason": safe_reason},
    )


class CoordinatedRuntimeProviderEvidenceNormalizer:
    """Stateless facade over the pure evidence functions."""

    normalize_dispatch_receipt = staticmethod(normalize_dispatch_receipt)
    from_dispatch_receipt = staticmethod(normalize_dispatch_receipt)
    synthetic_unknown_observation = staticmethod(synthetic_unknown_observation)
    classify_observation = staticmethod(classify_provider_observation)


__all__ = [
    "CoordinatedProviderObservationKind",
    "CoordinatedRuntimeProviderEvidenceNormalizer",
    "classify_provider_observation",
    "normalize_dispatch_receipt",
    "synthetic_unknown_observation",
]
