from datetime import datetime, timezone
from types import MappingProxyType, SimpleNamespace
from uuid import uuid4

import pytest

from agentmesh.application.coordinated_runtime_delivery import (
    CoordinatedDeliveryLeaseV1,
    assignment_projection_digest,
    ownership_digest,
    stable_dispatch_identity,
)
from agentmesh.application.coordinated_runtime_provider_evidence import (
    CoordinatedProviderObservationKind,
    classify_provider_observation,
    normalize_dispatch_receipt,
    synthetic_unknown_observation,
)
from agentmesh.application.ports import WorkflowWorkItem
from agentmesh.domain.errors import InvalidTaskInput
from agentmesh.domain.tasks import RunRole
from agentmesh.runtime_sdk import (
    RuntimeAssignment,
    RuntimeExecutionHandle,
    RuntimeObservation,
    RuntimePhase,
    thaw_json,
)

UTC = timezone.utc
NOW = datetime(2030, 1, 1, tzinfo=UTC)


def _assignment_and_receipt(*, observation=None, work_item=None):
    task_id, run_id, execution_id = uuid4(), uuid4(), uuid4()
    runtime_version_id, agent_version_id = uuid4(), uuid4()
    subtask_id = uuid4()
    if work_item is None:
        work_item = WorkflowWorkItem("deliver", {"value": "stable"})
    projection = assignment_projection_digest(
        tenant_id="tenant-a",
        task_id=task_id,
        run_id=run_id,
        subtask_id=subtask_id,
        role=RunRole.EXECUTOR,
        runtime_version_id=runtime_version_id,
        runtime_execution_intent_id=execution_id,
        agent_version_id=agent_version_id,
        agent_version_digest="a" * 64,
        task_plan_version=1,
        task_plan_digest="sha256:" + "b" * 64,
        run_revision=0,
        work_item=work_item,
    )
    lease_token = uuid4()
    attempt_id = uuid4()
    deadline = datetime(2030, 1, 1, 0, 5, tzinfo=UTC)
    lease = CoordinatedDeliveryLeaseV1(
        schema_version=1,
        tenant_id="tenant-a",
        task_id=task_id,
        run_id=run_id,
        subtask_id=subtask_id,
        attempt_id=attempt_id,
        role=RunRole.EXECUTOR,
        fencing_token=1,
        lease_token=lease_token,
        lease_deadline=deadline,
        runtime_version_id=runtime_version_id,
        runtime_execution_intent_id=execution_id,
        task_plan_version=1,
        task_plan_digest="sha256:" + "b" * 64,
        run_revision=0,
        agent_version_id=agent_version_id,
        agent_version_digest="a" * 64,
        work_item=work_item,
        assignment_projection_digest=projection,
        ownership_digest=ownership_digest(
            assignment_projection_digest=projection,
            tenant_id="tenant-a",
            task_id=task_id,
            run_id=run_id,
            subtask_id=subtask_id,
            attempt_id=attempt_id,
            fencing_token=1,
            lease_token=lease_token,
            lease_deadline=deadline,
        ),
    )
    assignment = RuntimeAssignment(
        assignment_id=str(uuid4()),
        tenant_id="tenant-a",
        task_id=str(task_id),
        run_id=str(run_id),
        agent_definition_id=str(uuid4()),
        agent_version_id=str(agent_version_id),
        agent_version_digest="a" * 64,
        runtime_version_id=str(runtime_version_id),
        runtime_descriptor_digest="c" * 64,
        execution_mode="inline",
        run_role=RunRole.EXECUTOR.value,
        revision=0,
        objective=work_item.objective,
        structured_input=thaw_json(work_item.input),
        correlation_ids={"runtime_execution_id": str(execution_id)},
        extensions={"coordinated_delivery": {"assignment_projection_digest": projection}},
    )
    handle = RuntimeExecutionHandle(
        runtime_execution_id=str(lease.runtime_execution_intent_id),
        runtime_version_id=str(lease.runtime_version_id),
        provider_execution_ref="provider-execution",
        assignment_id=assignment.assignment_id,
        assignment_digest=assignment.assignment_digest,
        created_at=NOW,
    )
    dispatch_key, _ = stable_dispatch_identity(
        lease.tenant_id, lease.runtime_execution_intent_id, assignment.assignment_digest
    )
    receipt = SimpleNamespace(
        dispatch_key=dispatch_key,
        runtime_execution_id=str(lease.runtime_execution_intent_id),
        assignment_digest=assignment.assignment_digest,
        handle=handle,
        observation=observation,
    )
    return lease, assignment, receipt


def test_public_dispatch_identity_matches_receipt_normalizer():
    lease, assignment, receipt = _assignment_and_receipt()
    normalized = normalize_dispatch_receipt(lease, assignment, receipt)
    _, expected_digest = stable_dispatch_identity(
        lease.tenant_id, lease.runtime_execution_intent_id, assignment.assignment_digest
    )
    assert normalized.dispatch_digest == expected_digest


@pytest.mark.parametrize(
    "field",
    ["dispatch_key", "runtime_execution_id", "assignment_digest"],
)
def test_receipt_identity_tampering_fails_closed(field):
    lease, assignment, receipt = _assignment_and_receipt()
    values = vars(receipt).copy()
    values[field] = "tampered"
    with pytest.raises(InvalidTaskInput):
        normalize_dispatch_receipt(lease, assignment, SimpleNamespace(**values))


def test_receipt_preserves_handle_and_observation_for_ordered_consumption():
    lease, assignment, receipt = _assignment_and_receipt()
    observation = RuntimeObservation(
        observation_id="00000000-0000-0000-0000-000000000001",
        runtime_execution_id=str(lease.runtime_execution_intent_id),
        assignment_id=assignment.assignment_id,
        assignment_digest=assignment.assignment_digest,
        phase=RuntimePhase.SUCCEEDED,
        observed_at=NOW,
        provider_event_id="provider-event",
        output={},
    )
    receipt.observation = observation
    normalized = normalize_dispatch_receipt(lease, assignment, receipt)
    assert normalized.handle is not None
    assert normalized.observation == observation
    assert (
        classify_provider_observation(observation)
        is CoordinatedProviderObservationKind.KNOWN_TERMINAL
    )


def test_optional_observation_is_allowed_only_with_handle():
    lease, assignment, receipt = _assignment_and_receipt()
    assert normalize_dispatch_receipt(lease, assignment, receipt).observation is None
    receipt.handle = None
    with pytest.raises(InvalidTaskInput):
        normalize_dispatch_receipt(lease, assignment, receipt)


def test_frozen_work_item_json_matches_thawed_assignment_json():
    work_item = WorkflowWorkItem(
        "deliver",
        MappingProxyType(
            {
                "nested": (
                    MappingProxyType({"accepted_handoffs": (), "value": "stable"}),
                )
            }
        ),
    )
    lease, assignment, receipt = _assignment_and_receipt(work_item=work_item)

    normalized = normalize_dispatch_receipt(lease, assignment, receipt)

    assert normalized.runtime_execution_id == lease.runtime_execution_intent_id


def test_real_frozen_work_item_value_difference_fails_closed():
    work_item = WorkflowWorkItem(
        "deliver",
        MappingProxyType(
            {
                "nested": (
                    MappingProxyType({"accepted_handoffs": (), "value": "stable"}),
                )
            }
        ),
    )
    lease, assignment, receipt = _assignment_and_receipt(work_item=work_item)
    tampered_values = assignment.to_dict(include_digest=False)
    tampered_values["structured_input"]["nested"][0]["value"] = "tampered"
    tampered = RuntimeAssignment.from_dict(tampered_values)

    with pytest.raises(InvalidTaskInput, match="work item conflicts"):
        normalize_dispatch_receipt(lease, tampered, receipt)


@pytest.mark.parametrize(
    "phase", [RuntimePhase.RUNNING, RuntimePhase.PAUSED, RuntimePhase.DISPATCHING]
)
def test_active_observation_phase_is_rejected(phase):
    lease, assignment, _ = _assignment_and_receipt()
    observation = RuntimeObservation(
        observation_id="00000000-0000-0000-0000-000000000001",
        runtime_execution_id=str(lease.runtime_execution_intent_id),
        assignment_id=assignment.assignment_id,
        assignment_digest=assignment.assignment_digest,
        phase=phase,
        observed_at=NOW,
        provider_event_id="provider-event",
    )
    with pytest.raises(InvalidTaskInput):
        classify_provider_observation(observation)


def test_synthetic_unknown_is_deterministic_and_time_bound_in_digest():
    lease, assignment, _ = _assignment_and_receipt()
    first = synthetic_unknown_observation(
        lease, assignment, reason="runtime.dispatch_response_lost", observed_at=NOW
    )
    replay = synthetic_unknown_observation(
        lease, assignment, reason="runtime.dispatch_response_lost", observed_at=NOW
    )
    later = synthetic_unknown_observation(
        lease,
        assignment,
        reason="runtime.dispatch_response_lost",
        observed_at=NOW.replace(hour=1),
    )
    assert first == replay
    assert first.observation_id == later.observation_id
    assert first.provider_event_id == later.provider_event_id
    assert first.snapshot_digest != later.snapshot_digest
    assert (
        classify_provider_observation(first)
        is CoordinatedProviderObservationKind.OUTCOME_UNKNOWN
    )


@pytest.mark.parametrize("reason", ["provider.secret", "x" * 257, "unsafe/reason"])
def test_synthetic_unknown_rejects_unsafe_reason(reason):
    lease, assignment, _ = _assignment_and_receipt()
    with pytest.raises(InvalidTaskInput):
        synthetic_unknown_observation(lease, assignment, reason=reason, observed_at=NOW)
