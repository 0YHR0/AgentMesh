from datetime import datetime, timezone
from uuid import uuid4

import pytest

from agentmesh.domain.errors import InvalidTaskTransition
from agentmesh.domain.runtime_execution import (
    ReattachEvidence,
    RuntimeExecution,
    RuntimeExecutionPhase,
)


def _execution() -> RuntimeExecution:
    return RuntimeExecution.prepare(
        tenant_id="tenant-a",
        run_id=uuid4(),
        runtime_version_id=uuid4(),
        assignment_id=uuid4(),
        assignment_digest="a" * 64,
        dispatch_key="runtime-dispatch:tenant-a:one",
        dispatch_digest="b" * 64,
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def test_observation_preserves_sequence_when_provider_omits_it() -> None:
    value = _execution().apply_observation(
        phase=RuntimeExecutionPhase.DISPATCHING,
        provider_sequence=1,
    )
    value = value.apply_observation(
        phase=RuntimeExecutionPhase.ACCEPTED,
        provider_sequence=None,
    )
    assert value.provider_sequence == 1


def test_phase_graph_rejects_backward_transition() -> None:
    value = _execution().apply_observation(
        phase=RuntimeExecutionPhase.DISPATCHING,
        provider_sequence=1,
    )
    with pytest.raises(InvalidTaskTransition):
        value.apply_observation(
            phase=RuntimeExecutionPhase.PREPARED,
            provider_sequence=2,
        )


def test_replacement_claim_requires_verified_reattach_evidence() -> None:
    value = _execution().claim(
        attempt_id=uuid4(),
        fencing_token=1,
        expected_owner_attempt_id=None,
        expected_fencing_token=None,
        expected_version=1,
    )
    value = value.apply_observation(
        phase=RuntimeExecutionPhase.DISPATCHING,
        provider_sequence=1,
        provider_execution_ref="opaque-provider-ref",
    )
    replacement = uuid4()
    with pytest.raises(InvalidTaskTransition):
        value.claim(
            attempt_id=replacement,
            fencing_token=2,
            expected_owner_attempt_id=value.current_owner_attempt_id,
            expected_fencing_token=1,
            expected_version=value.version,
            replacement_authorized=True,
        )
    updated = value.claim(
        attempt_id=replacement,
        fencing_token=2,
        expected_owner_attempt_id=value.current_owner_attempt_id,
        expected_fencing_token=1,
        expected_version=value.version,
        replacement_authorized=True,
        reattach_evidence=ReattachEvidence(
            execution_id=value.id,
            assignment_digest=value.assignment_digest,
            provider_execution_ref="opaque-provider-ref",
            inspected_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
    )
    assert updated.current_owner_attempt_id == replacement
