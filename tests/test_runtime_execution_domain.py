from datetime import datetime, timezone
from types import MappingProxyType
from uuid import uuid4

import pytest

from agentmesh.domain.errors import InvalidTaskInput, InvalidTaskTransition
from agentmesh.domain.runtime_execution import (
    ReattachEvidence,
    RuntimeExecution,
    RuntimeExecutionPhase,
    RuntimeLifecycleIntent,
    RuntimeLifecycleOperation,
    RuntimeLifecycleStatus,
)
from agentmesh.domain.tasks import TaskRun


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


def test_lifecycle_receipt_summary_is_an_immutable_json_projection() -> None:
    receipt = {"status": "accepted", "details": {"attempt": 1}}
    value = RuntimeLifecycleIntent(
        id=uuid4(),
        tenant_id="tenant-a",
        runtime_execution_id=uuid4(),
        operation_id="operation-1",
        operation=RuntimeLifecycleOperation.CANCEL,
        intent_digest="b" * 64,
        status=RuntimeLifecycleStatus.ACCEPTED,
        deadline=datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc),
        receipt_summary=receipt,
        version=1,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    receipt["details"]["attempt"] = 2
    assert type(value.receipt_summary) is MappingProxyType
    assert value.receipt_summary["details"]["attempt"] == 1


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


def test_prepared_execution_allows_safe_replacement_without_reattach() -> None:
    first = uuid4()
    value = _execution().claim(
        attempt_id=first,
        fencing_token=1,
        expected_owner_attempt_id=None,
        expected_fencing_token=None,
        expected_version=1,
    )
    replacement = uuid4()

    updated = value.claim(
        attempt_id=replacement,
        fencing_token=2,
        expected_owner_attempt_id=first,
        expected_fencing_token=1,
        expected_version=value.version,
        replacement_authorized=True,
    )

    assert updated.phase is RuntimeExecutionPhase.PREPARED
    assert updated.current_owner_attempt_id == replacement


def test_exact_claim_replay_is_idempotent_before_stale_cas_checks() -> None:
    attempt_id = uuid4()
    value = _execution().claim(
        attempt_id=attempt_id,
        fencing_token=1,
        expected_owner_attempt_id=None,
        expected_fencing_token=None,
        expected_version=1,
    )

    replay = value.claim(
        attempt_id=attempt_id,
        fencing_token=1,
        expected_owner_attempt_id=None,
        expected_fencing_token=None,
        expected_version=1,
    )

    assert replay is value
    assert replay.version == 2


def test_same_or_lower_fence_for_a_different_owner_is_rejected() -> None:
    owner = uuid4()
    value = _execution().claim(
        attempt_id=owner,
        fencing_token=3,
        expected_owner_attempt_id=None,
        expected_fencing_token=None,
        expected_version=1,
    )

    with pytest.raises(InvalidTaskInput, match="fencing token"):
        value.claim(
            attempt_id=uuid4(),
            fencing_token=3,
            expected_owner_attempt_id=owner,
            expected_fencing_token=3,
            expected_version=value.version,
            replacement_authorized=True,
        )
    with pytest.raises(InvalidTaskInput, match="fencing token"):
        value.claim(
            attempt_id=uuid4(),
            fencing_token=2,
            expected_owner_attempt_id=owner,
            expected_fencing_token=3,
            expected_version=value.version,
            replacement_authorized=True,
        )


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (RuntimeExecutionPhase.PREPARED, RuntimeExecutionPhase.SUCCEEDED),
        (RuntimeExecutionPhase.PREPARED, RuntimeExecutionPhase.FAILED),
        (RuntimeExecutionPhase.PREPARED, RuntimeExecutionPhase.CANCELED),
        (RuntimeExecutionPhase.PREPARED, RuntimeExecutionPhase.TIMED_OUT),
        (RuntimeExecutionPhase.PREPARED, RuntimeExecutionPhase.LOST),
        (RuntimeExecutionPhase.PREPARED, RuntimeExecutionPhase.OUTCOME_UNKNOWN),
        (RuntimeExecutionPhase.PREPARED, RuntimeExecutionPhase.CANCEL_REQUESTED),
        (RuntimeExecutionPhase.DISPATCHING, RuntimeExecutionPhase.SUCCEEDED),
        (RuntimeExecutionPhase.DISPATCHING, RuntimeExecutionPhase.CANCELED),
        (RuntimeExecutionPhase.DISPATCHING, RuntimeExecutionPhase.TIMED_OUT),
        (RuntimeExecutionPhase.PAUSE_REQUESTED, RuntimeExecutionPhase.CANCELED),
        (RuntimeExecutionPhase.PAUSE_REQUESTED, RuntimeExecutionPhase.OUTCOME_UNKNOWN),
        (RuntimeExecutionPhase.WAITING_INPUT, RuntimeExecutionPhase.FAILED),
        (RuntimeExecutionPhase.WAITING_APPROVAL, RuntimeExecutionPhase.SUCCEEDED),
        (RuntimeExecutionPhase.CANCEL_REQUESTED, RuntimeExecutionPhase.LOST),
    ],
)
def test_phase_graph_accepts_terminal_and_late_terminal_edges(
    source: RuntimeExecutionPhase, target: RuntimeExecutionPhase
) -> None:
    value = _execution()
    path = {
        RuntimeExecutionPhase.PREPARED: [],
        RuntimeExecutionPhase.DISPATCHING: [RuntimeExecutionPhase.DISPATCHING],
        RuntimeExecutionPhase.PAUSE_REQUESTED: [
            RuntimeExecutionPhase.DISPATCHING,
            RuntimeExecutionPhase.RUNNING,
            RuntimeExecutionPhase.PAUSE_REQUESTED,
        ],
        RuntimeExecutionPhase.WAITING_INPUT: [
            RuntimeExecutionPhase.DISPATCHING,
            RuntimeExecutionPhase.RUNNING,
            RuntimeExecutionPhase.WAITING_INPUT,
        ],
        RuntimeExecutionPhase.WAITING_APPROVAL: [
            RuntimeExecutionPhase.DISPATCHING,
            RuntimeExecutionPhase.RUNNING,
            RuntimeExecutionPhase.WAITING_APPROVAL,
        ],
        RuntimeExecutionPhase.CANCEL_REQUESTED: [
            RuntimeExecutionPhase.DISPATCHING,
            RuntimeExecutionPhase.RUNNING,
            RuntimeExecutionPhase.CANCEL_REQUESTED,
        ],
    }[source]
    for sequence, phase in enumerate(path, start=1):
        value = value.apply_observation(phase=phase, provider_sequence=sequence)
    result = value.apply_observation(phase=target, provider_sequence=None)
    assert result.phase is target


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (RuntimeExecutionPhase.PREPARED, RuntimeExecutionPhase.PAUSED),
        (RuntimeExecutionPhase.DISPATCHING, RuntimeExecutionPhase.PREPARED),
        (RuntimeExecutionPhase.CANCEL_REQUESTED, RuntimeExecutionPhase.RUNNING),
        (RuntimeExecutionPhase.SUCCEEDED, RuntimeExecutionPhase.FAILED),
    ],
)
def test_phase_graph_rejects_unsupported_edges(
    source: RuntimeExecutionPhase, target: RuntimeExecutionPhase
) -> None:
    value = _execution()
    if source is RuntimeExecutionPhase.DISPATCHING:
        value = value.apply_observation(phase=source, provider_sequence=1)
    elif source is RuntimeExecutionPhase.CANCEL_REQUESTED:
        value = value.apply_observation(
            phase=RuntimeExecutionPhase.DISPATCHING, provider_sequence=1
        )
        value = value.apply_observation(
            phase=RuntimeExecutionPhase.RUNNING, provider_sequence=2
        )
        value = value.apply_observation(phase=source, provider_sequence=3)
    elif source is RuntimeExecutionPhase.SUCCEEDED:
        value = value.apply_observation(
            phase=RuntimeExecutionPhase.DISPATCHING, provider_sequence=1
        )
        value = value.apply_observation(phase=source, provider_sequence=2)
    with pytest.raises(InvalidTaskTransition):
        value.apply_observation(phase=target, provider_sequence=None)


def test_run_comparison_off_is_an_immutable_admission_snapshot() -> None:
    run = TaskRun.request(uuid4(), "agent", runtime_version_id=uuid4())
    run.runtime_execution_id = uuid4()

    with pytest.raises(InvalidTaskTransition, match="disabled at Run creation"):
        run.pin_runtime_comparison()


def test_managed_run_admission_generates_intent_and_requires_pinned_version() -> None:
    runtime_version_id = uuid4()
    run = TaskRun.request(
        uuid4(),
        "agent",
        runtime_authority="managed",
        runtime_version_id=runtime_version_id,
    )

    assert run.runtime_authority == "managed"
    assert run.comparison_mode == "off"
    assert run.runtime_version_id == runtime_version_id
    assert run.runtime_execution_intent_id is not None

    with pytest.raises(InvalidTaskInput, match="Managed Runtime authority"):
        TaskRun.request(uuid4(), "agent", runtime_authority="managed")
    with pytest.raises(InvalidTaskInput, match="Managed Runtime authority"):
        TaskRun.request(
            uuid4(),
            "agent",
            runtime_authority="managed",
            runtime_version_id=runtime_version_id,
            comparison_mode="deterministic_shadow",
        )


def test_run_runtime_authority_cannot_change_after_admission() -> None:
    run = TaskRun.request(uuid4(), "agent", runtime_authority="managed", runtime_version_id=uuid4())
    run.set_runtime_authority("managed")
    with pytest.raises(InvalidTaskTransition, match="authority is immutable"):
        run.set_runtime_authority("legacy")


def test_run_runtime_execution_must_match_deterministic_intent() -> None:
    run = TaskRun.request_deterministic_shadow(
        uuid4(), "agent", runtime_version_id=uuid4()
    )
    with pytest.raises(InvalidTaskTransition, match="conflicts with its intent"):
        run.bind_runtime_execution(uuid4())
