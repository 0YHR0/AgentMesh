from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from agentmesh.application.runtime_snapshots import (
    RuntimeAssignmentSnapshot,
    RuntimeHandleSnapshot,
    snapshot_payload,
)
from agentmesh.domain.errors import InvalidTaskInput
from agentmesh.domain.runtime_execution import (
    RuntimeExecutionPhase,
    RuntimeIntegrityIncident,
    RuntimeIntegrityIncidentStatus,
)
from agentmesh.runtime_sdk.assignment import RuntimeAssignment, RuntimeExecutionHandle
from agentmesh.runtime_sdk.canonical import canonical_digest


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _assignment() -> RuntimeAssignment:
    return RuntimeAssignment(
        assignment_id=str(uuid4()),
        tenant_id="tenant-a",
        task_id=str(uuid4()),
        run_id=str(uuid4()),
        agent_definition_id=str(uuid4()),
        agent_version_id=str(uuid4()),
        agent_version_digest="a" * 64,
        runtime_version_id=str(uuid4()),
        runtime_descriptor_digest="b" * 64,
        execution_mode="managed_async",
        run_role="EXECUTOR",
        revision=0,
        objective="bounded objective",
        structured_input={"value": "safe"},
        deadline=_now() + timedelta(minutes=5),
    )


def _assignment_snapshot(assignment: RuntimeAssignment) -> RuntimeAssignmentSnapshot:
    return RuntimeAssignmentSnapshot(
        id=uuid4(),
        tenant_id=assignment.tenant_id,
        runtime_execution_id=uuid4(),
        contract_name=assignment.schema_name,
        contract_major=assignment.schema_version,
        assignment_id=UUID(assignment.assignment_id),
        assignment_digest=assignment.assignment_digest or "",
        canonical_payload=assignment.to_dict(),
        created_at=_now(),
    )


def _handle(assignment: RuntimeAssignment, execution_id: UUID) -> RuntimeExecutionHandle:
    return RuntimeExecutionHandle(
        runtime_execution_id=str(execution_id),
        runtime_version_id=assignment.runtime_version_id,
        provider_execution_ref="opaque-provider-ref",
        assignment_id=assignment.assignment_id,
        assignment_digest=assignment.assignment_digest or "",
        created_at=_now(),
    )


def test_assignment_snapshot_accepts_real_runtime_dto_and_freezes_payload() -> None:
    assignment = _assignment()
    value = _assignment_snapshot(assignment)
    assert value.canonical_payload["deadline"].endswith("Z")
    assert value.canonical_payload["assignment_id"] == assignment.assignment_id
    assert value.canonical_payload["structured_input"] == {"value": "safe"}


@pytest.mark.parametrize(
    "payload",
    [
        {"bad": float("nan")},
        {"bad": float("inf")},
        {"bad": "surrogate\ud800"},
        {"bad": 9_007_199_254_740_992},
        ["root must be object"],
        {"schema_name": "agentmesh.runtime-assignment"},
    ],
)
def test_snapshot_rejects_non_jcs_or_partial_payload(payload: object) -> None:
    with pytest.raises(InvalidTaskInput):
        RuntimeHandleSnapshot(
            id=uuid4(),
            tenant_id="tenant-a",
            runtime_execution_id=uuid4(),
            handle_digest="b" * 64,
            canonical_payload=payload,  # type: ignore[arg-type]
            created_at=_now(),
        )


def test_handle_snapshot_accepts_real_runtime_dto_and_checks_digest_identity() -> None:
    assignment = _assignment()
    execution_id = uuid4()
    handle = _handle(assignment, execution_id)
    value = RuntimeHandleSnapshot(
        id=uuid4(),
        tenant_id=assignment.tenant_id,
        runtime_execution_id=execution_id,
        handle_digest=canonical_digest(handle.to_dict()),
        canonical_payload=handle.to_dict(),
        created_at=_now(),
    )
    assert value.canonical_payload["created_at"].endswith("Z")
    with pytest.raises(InvalidTaskInput):
        RuntimeHandleSnapshot(
            **{**value.__dict__, "handle_digest": "b" * 64}
        )
    with pytest.raises(InvalidTaskInput):
        RuntimeHandleSnapshot(
            **{
                **value.__dict__,
                "runtime_execution_id": uuid4(),
            }
        )


def test_snapshot_rejects_naive_or_oversize_handle_payload() -> None:
    assignment = _assignment()
    handle = _handle(assignment, uuid4())
    with pytest.raises(InvalidTaskInput):
        RuntimeHandleSnapshot(
            **{**RuntimeHandleSnapshot(
                id=uuid4(),
                tenant_id=assignment.tenant_id,
                runtime_execution_id=UUID(handle.runtime_execution_id),
                handle_digest=canonical_digest(handle.to_dict()),
                canonical_payload=handle.to_dict(),
                created_at=_now(),
            ).__dict__, "created_at": datetime.now()}
        )
    with pytest.raises(InvalidTaskInput):
        snapshot_payload({"value": "x" * 70_000}, limit=65_536)


def test_integrity_incident_has_closed_status_and_phase_invariants() -> None:
    now = _now()
    value = RuntimeIntegrityIncident(
        id=uuid4(),
        tenant_id="tenant-a",
        runtime_execution_id=uuid4(),
        accepted_observation_id="accepted",
        accepted_observation_digest="a" * 64,
        accepted_phase=RuntimeExecutionPhase.SUCCEEDED,
        conflicting_observation_id="conflict",
        conflicting_observation_digest="b" * 64,
        conflicting_phase=RuntimeExecutionPhase.LOST,
        status=RuntimeIntegrityIncidentStatus.OPEN,
        reason="late conflicting terminal",
        created_at=now,
        updated_at=now,
    )
    assert value.status is RuntimeIntegrityIncidentStatus.OPEN
    with pytest.raises(InvalidTaskInput):
        RuntimeIntegrityIncident(**{**value.__dict__, "status": "RESOLVED"})  # type: ignore[arg-type]
    with pytest.raises(InvalidTaskInput):
        RuntimeIntegrityIncident(
            **{**value.__dict__, "accepted_phase": RuntimeExecutionPhase.LOST}
        )
    with pytest.raises(InvalidTaskInput):
        RuntimeIntegrityIncident(
            **{
                **value.__dict__,
                "conflicting_observation_digest": "a" * 64,
            }
        )
    with pytest.raises(InvalidTaskInput):
        RuntimeIntegrityIncident(
            **{**value.__dict__, "updated_at": now - timedelta(seconds=1)}
        )
