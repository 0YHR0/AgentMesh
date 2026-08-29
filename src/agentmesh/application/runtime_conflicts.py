"""Safe conflict envelopes for managed Runtime terminal observations."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from uuid import NAMESPACE_URL, UUID, uuid5

from agentmesh.application.ports import ManagedRuntimeConflictObservation
from agentmesh.application.runtime_contracts import validate_terminal_observation
from agentmesh.domain.errors import InvalidTaskInput
from agentmesh.runtime_sdk import RuntimeObservation, RuntimePhase, canonical_json_bytes

_STATIC_CONFLICT_REASON = "runtime.terminal_contract_invalid"


def build_managed_runtime_conflict_observation(
    candidate: object,
    *,
    expected_execution_id: UUID,
    expected_assignment_id: UUID,
    expected_assignment_digest: str,
    fallback_observed_at: datetime,
) -> ManagedRuntimeConflictObservation:
    """Build a bounded, identity-safe conflict DTO from provider output.

    Only a fully decoded and canonically bounded RuntimeObservation contributes
    its raw canonical digest and decoded phase/time/sequence.  Everything else
    is represented by one deterministic static marker.
    """
    _validate_expected_context(
        expected_execution_id,
        expected_assignment_id,
        expected_assignment_digest,
        fallback_observed_at,
    )
    fallback = fallback_observed_at.astimezone(timezone.utc)
    if type(candidate) is not RuntimeObservation:
        return _static_marker(expected_execution_id, fallback)
    try:
        canonical_bytes = canonical_json_bytes(candidate.to_dict())
        if len(canonical_bytes) > 65_536:
            return _static_marker(expected_execution_id, fallback)
        digest = sha256(canonical_bytes).hexdigest()
    except Exception:
        # Provider-owned values cross an untrusted serialization boundary;
        # any encoding failure is reduced to the deterministic static marker.
        return _static_marker(expected_execution_id, fallback)

    execution_mismatch = candidate.runtime_execution_id != str(expected_execution_id)
    assignment_mismatch = candidate.assignment_id != str(expected_assignment_id)
    assignment_digest_mismatch = candidate.assignment_digest != expected_assignment_digest
    terminal_contract_invalid = False
    try:
        validate_terminal_observation(
            candidate,
            runtime_execution_id=expected_execution_id,
            assignment_id=expected_assignment_id,
            assignment_digest=expected_assignment_digest,
        )
    except InvalidTaskInput:
        # Validation failures are expected provider-contract evidence. Other
        # exceptions indicate an application defect and must propagate.
        terminal_contract_invalid = True
    protocol_error = candidate.error is not None and candidate.error.code == (
        "runtime.protocol_error"
    )
    return ManagedRuntimeConflictObservation(
        observation_id=uuid5(NAMESPACE_URL, f"{expected_execution_id}:{digest}"),
        observation_digest=digest,
        phase=candidate.phase,
        observed_at=candidate.observed_at.astimezone(timezone.utc),
        provider_sequence=candidate.provider_sequence,
        structural_invalid=False,
        execution_id_mismatch=execution_mismatch,
        assignment_id_mismatch=assignment_mismatch,
        assignment_digest_mismatch=assignment_digest_mismatch,
        terminal_contract_invalid=terminal_contract_invalid,
        protocol_error_observation=protocol_error,
    )


def _validate_expected_context(
    expected_execution_id: UUID,
    expected_assignment_id: UUID,
    expected_assignment_digest: str,
    fallback_observed_at: datetime,
) -> None:
    if (
        type(expected_execution_id) is not UUID
        or type(expected_assignment_id) is not UUID
        or type(expected_assignment_digest) is not str
        or len(expected_assignment_digest) != 64
        or any(character not in "0123456789abcdef" for character in expected_assignment_digest)
        or type(fallback_observed_at) is not datetime
        or fallback_observed_at.tzinfo is None
        or fallback_observed_at.utcoffset() is None
    ):
        raise InvalidTaskInput("Managed Runtime conflict context is invalid")


def _static_marker(
    expected_execution_id: UUID, fallback_observed_at: datetime
) -> ManagedRuntimeConflictObservation:
    digest = sha256(
        canonical_json_bytes(
            {
                "expected_execution_id": str(expected_execution_id),
                "reason": _STATIC_CONFLICT_REASON,
                "structural_invalid": True,
            }
        )
    ).hexdigest()
    return ManagedRuntimeConflictObservation(
        observation_id=uuid5(NAMESPACE_URL, f"{expected_execution_id}:{digest}"),
        observation_digest=digest,
        phase=RuntimePhase.OUTCOME_UNKNOWN,
        observed_at=fallback_observed_at,
        provider_sequence=None,
        structural_invalid=True,
        execution_id_mismatch=False,
        assignment_id_mismatch=False,
        assignment_digest_mismatch=False,
        terminal_contract_invalid=True,
        protocol_error_observation=False,
    )
