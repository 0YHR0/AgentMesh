from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from agentmesh.application.coordinated_runtime_delivery import (
    CoordinatedDeliveryLeaseV1,
    CoordinatedDeliveryResult,
    CoordinatedDeliveryResultKind,
    RecoveryCrossedProof,
    assignment_projection_digest,
    canonical_work_item_bytes,
    ownership_digest,
)
from agentmesh.application.ports import WorkflowWorkItem
from agentmesh.domain.errors import InvalidTaskInput
from agentmesh.domain.runtime_execution import RuntimeExecutionPhase
from agentmesh.domain.tasks import RunRole, Task, TaskExecutionMode, TaskRun


@pytest.fixture
def lease_parts() -> dict:
    ids = [uuid4() for _ in range(8)]
    work_item = WorkflowWorkItem("objective", {"items": [{"value": 1}]})
    parts = {
        "tenant_id": "tenant",
        "task_id": ids[0],
        "run_id": ids[1],
        "subtask_id": ids[2],
        "role": RunRole.EXECUTOR,
        "runtime_version_id": ids[3],
        "runtime_execution_intent_id": ids[4],
        "agent_version_id": ids[5],
        "agent_version_digest": "a" * 64,
        "task_plan_version": 1,
        "task_plan_digest": "sha256:" + "b" * 64,
        "run_revision": 0,
        "work_item": work_item,
    }
    assignment = assignment_projection_digest(**parts)
    deadline = datetime(2030, 1, 1, tzinfo=timezone.utc)
    parts.update(attempt_id=ids[6], fencing_token=4, lease_token=ids[7], lease_deadline=deadline)
    parts.update(
        assignment_projection_digest=assignment,
        ownership_digest=ownership_digest(
            assignment_projection_digest=assignment,
            tenant_id=parts["tenant_id"],
            task_id=parts["task_id"],
            run_id=parts["run_id"],
            subtask_id=parts["subtask_id"],
            attempt_id=parts["attempt_id"],
            fencing_token=parts["fencing_token"],
            lease_token=parts["lease_token"],
            lease_deadline=deadline,
        ),
    )
    return parts


def _lease(parts: dict) -> CoordinatedDeliveryLeaseV1:
    return CoordinatedDeliveryLeaseV1(schema_version=1, **parts)


def test_exact_canonical_assignment_vector() -> None:
    value = {
        "tenant_id": "tenant",
        "task_id": UUID("00000000-0000-0000-0000-000000000001"),
        "run_id": UUID("00000000-0000-0000-0000-000000000002"),
        "subtask_id": UUID("00000000-0000-0000-0000-000000000003"),
        "role": RunRole.EXECUTOR,
        "runtime_version_id": UUID("00000000-0000-0000-0000-000000000004"),
        "runtime_execution_intent_id": UUID("00000000-0000-0000-0000-000000000005"),
        "agent_version_id": UUID("00000000-0000-0000-0000-000000000006"),
        "agent_version_digest": "a" * 64,
        "task_plan_version": 1,
        "task_plan_digest": "sha256:" + "b" * 64,
        "run_revision": 0,
        "work_item": WorkflowWorkItem("objective", {"items": [{"value": 1}]}),
    }
    assert assignment_projection_digest(**value) == (
        "2ae9504a2cc1dea1d7cc79fed3d406daad7903ed41c912ca13b3e7552a92f418"
    )


def test_digest_vector_and_ownership_separation(lease_parts: dict) -> None:
    # The projection is deliberately explicit and stable; this vector catches
    # accidental inclusion of attempt ownership fields.
    assert (
        assignment_projection_digest(
            **{
                key: lease_parts[key]
                for key in (
                    "tenant_id",
                    "task_id",
                    "run_id",
                    "subtask_id",
                    "role",
                    "runtime_version_id",
                    "runtime_execution_intent_id",
                    "agent_version_id",
                    "agent_version_digest",
                    "task_plan_version",
                    "task_plan_digest",
                    "run_revision",
                    "work_item",
                )
            }
        )
        == lease_parts["assignment_projection_digest"]
    )
    stable = {
        key: lease_parts[key]
        for key in lease_parts
        if key
        not in {
            "attempt_id",
            "fencing_token",
            "lease_token",
            "lease_deadline",
            "assignment_projection_digest",
            "ownership_digest",
        }
    }
    original = lease_parts["assignment_projection_digest"]
    for field, value in (
        ("attempt_id", uuid4()),
        ("fencing_token", 9),
        ("lease_token", uuid4()),
        ("lease_deadline", datetime(2031, 1, 1, tzinfo=timezone.utc)),
    ):
        updated = dict(lease_parts)
        updated[field] = value
        assert assignment_projection_digest(**stable) == original
        assert (
            ownership_digest(
                assignment_projection_digest=original,
                tenant_id=updated["tenant_id"],
                task_id=updated["task_id"],
                run_id=updated["run_id"],
                subtask_id=updated["subtask_id"],
                attempt_id=updated["attempt_id"],
                fencing_token=updated["fencing_token"],
                lease_token=updated["lease_token"],
                lease_deadline=updated["lease_deadline"],
            )
            != lease_parts["ownership_digest"]
        )


def test_lease_is_defensive_and_result_union_is_closed(lease_parts: dict) -> None:
    source = lease_parts["work_item"].input
    lease = _lease(lease_parts)
    assert lease.work_item is not lease_parts["work_item"]
    assert dict(lease.work_item.input)["items"][0]["value"] == 1
    assert lease.work_item.input["items"][0]["value"] == 1
    assert canonical_work_item_bytes(lease.work_item) == (
        b'{"input":{"items":[{"value":1}]},"objective":"objective"}'
    )
    source["items"][0]["value"] = 99
    source["items"].append({"value": 100})
    assert lease.work_item.input["items"][0]["value"] == 1
    with pytest.raises(TypeError):
        lease.work_item.input["items"] = []
    assert source["items"][0]["value"] == 99
    assert CoordinatedDeliveryResult.acquired(lease).lease is lease
    with pytest.raises(InvalidTaskInput):
        CoordinatedDeliveryResult(CoordinatedDeliveryResultKind.IN_PROGRESS, lease=lease)


def test_role_subtask_and_scalar_boundaries(lease_parts: dict) -> None:
    supervisor = dict(lease_parts, role=RunRole.SUPERVISOR, subtask_id=None)
    digest = assignment_projection_digest(
        **{
            key: supervisor[key]
            for key in (
                "tenant_id",
                "task_id",
                "run_id",
                "subtask_id",
                "role",
                "runtime_version_id",
                "runtime_execution_intent_id",
                "agent_version_id",
                "agent_version_digest",
                "task_plan_version",
                "task_plan_digest",
                "run_revision",
                "work_item",
            )
        }
    )
    assert digest != lease_parts["assignment_projection_digest"]
    for bad in (
        dict(lease_parts, tenant_id=" tenant"),
        dict(lease_parts, role=RunRole.SUPERVISOR),
        dict(lease_parts, role=RunRole.EXECUTOR, subtask_id=None),
        dict(lease_parts, fencing_token=True),
        dict(lease_parts, lease_deadline=datetime(2030, 1, 1)),
        dict(lease_parts, agent_version_digest="A" * 64),
    ):
        if bad.get("role") is not lease_parts["role"] or bad.get("subtask_id") is None:
            with pytest.raises(InvalidTaskInput):
                _lease(bad)
        else:
            with pytest.raises(InvalidTaskInput):
                _lease(bad)


def test_recovery_crossed_proof_never_is_a_usable_lease(lease_parts: dict) -> None:
    proof = RecoveryCrossedProof(
        execution_id=lease_parts["run_id"],
        expired_owner_attempt_id=lease_parts["attempt_id"],
        expired_owner_fencing_token=lease_parts["fencing_token"],
        phase=RuntimeExecutionPhase.RUNNING,
        version=2,
        persisted_handle_snapshot_id=uuid4(),
        persisted_handle_snapshot_digest="c" * 64,
    )
    result = CoordinatedDeliveryResult.recover_crossed(proof)
    assert result.lease is None
    assert result.recovery_crossed_proof is proof
    for kind in (
        CoordinatedDeliveryResultKind.NOT_APPLICABLE,
        CoordinatedDeliveryResultKind.IN_PROGRESS,
        CoordinatedDeliveryResultKind.REPLAY_PROCESSED,
        CoordinatedDeliveryResultKind.BLOCKED_BY_DRAIN,
        CoordinatedDeliveryResultKind.WAITING_APPROVAL,
    ):
        assert CoordinatedDeliveryResult.without_lease(kind).lease is None
        with pytest.raises(InvalidTaskInput):
            CoordinatedDeliveryResult(kind, recovery_crossed_proof=proof)


def test_real_task_plan_fields_and_replacement_attempt_keep_assignment_stable() -> None:
    task = Task.create(
        tenant_id="tenant",
        objective="coordinate",
        execution_mode=TaskExecutionMode.COORDINATED,
        plan_version=1,
        plan_digest="sha256:" + "d" * 64,
        max_concurrency=2,
    )
    run = TaskRun.request(
        task.id,
        "agent",
        role=RunRole.EXECUTOR,
        subtask_id=uuid4(),
        runtime_version_id=uuid4(),
        runtime_authority="managed",
    )
    stable = dict(
        tenant_id=task.tenant_id,
        task_id=task.id,
        run_id=run.id,
        subtask_id=run.subtask_id,
        role=run.role,
        runtime_version_id=run.runtime_version_id,
        runtime_execution_intent_id=run.runtime_execution_intent_id,
        agent_version_id=uuid4(),
        agent_version_digest="e" * 64,
        task_plan_version=task.plan_version,
        task_plan_digest=task.plan_digest,
        run_revision=run.revision_number,
        work_item=WorkflowWorkItem(task.objective, task.input),
    )
    first = assignment_projection_digest(**stable)
    # Task.version is optimistic aggregate state and is intentionally absent.
    task.version += 100
    assert assignment_projection_digest(**stable) == first
    first_ownership = ownership_digest(
        assignment_projection_digest=first,
        tenant_id=stable["tenant_id"],
        task_id=stable["task_id"],
        run_id=stable["run_id"],
        subtask_id=stable["subtask_id"],
        attempt_id=uuid4(),
        fencing_token=9,
        lease_token=uuid4(),
        lease_deadline=datetime(2031, 1, 1, tzinfo=timezone.utc),
    )
    second_ownership = ownership_digest(
        assignment_projection_digest=first,
        tenant_id=stable["tenant_id"],
        task_id=stable["task_id"],
        run_id=stable["run_id"],
        subtask_id=stable["subtask_id"],
        attempt_id=uuid4(),
        fencing_token=10,
        lease_token=uuid4(),
        lease_deadline=datetime(2032, 1, 1, tzinfo=timezone.utc),
    )
    assert assignment_projection_digest(**stable) == first
    assert first_ownership != second_ownership


def test_every_stable_assignment_field_is_sensitive(lease_parts: dict) -> None:
    stable_keys = (
        "tenant_id",
        "task_id",
        "run_id",
        "subtask_id",
        "role",
        "runtime_version_id",
        "runtime_execution_intent_id",
        "agent_version_id",
        "agent_version_digest",
        "task_plan_version",
        "task_plan_digest",
        "run_revision",
        "work_item",
    )
    stable = {key: lease_parts[key] for key in stable_keys}
    original = assignment_projection_digest(**stable)
    changes = {
        "tenant_id": "another-tenant",
        "task_id": uuid4(),
        "run_id": uuid4(),
        "subtask_id": uuid4(),
        "role": RunRole.SUPERVISOR,
        "runtime_version_id": uuid4(),
        "runtime_execution_intent_id": uuid4(),
        "agent_version_id": uuid4(),
        "agent_version_digest": "f" * 64,
        "task_plan_version": 2,
        "task_plan_digest": "sha256:" + "f" * 64,
        "run_revision": 1,
        "work_item": WorkflowWorkItem("other", {"x": True}),
    }
    for key, changed in changes.items():
        candidate = dict(stable, **{key: changed})
        if key == "role":
            candidate["subtask_id"] = None
        assert assignment_projection_digest(**candidate) != original


@pytest.mark.parametrize(
    "field,value",
    [
        ("task_id", "not-uuid"),
        ("run_id", "not-uuid"),
        ("subtask_id", "not-uuid"),
        ("runtime_version_id", "not-uuid"),
        ("runtime_execution_intent_id", "not-uuid"),
        ("agent_version_id", None),
        ("agent_version_digest", "A" * 64),
        ("task_plan_digest", "b" * 64),
        ("task_plan_version", 0),
        ("task_plan_version", True),
        ("run_revision", True),
        ("work_item", WorkflowWorkItem("x", {"bad": object()})),
        ("work_item", WorkflowWorkItem("x", [])),
        ("work_item", WorkflowWorkItem("x" * 70_000, {})),
    ],
)
def test_invalid_assignment_inputs_are_rejected(
    lease_parts: dict, field: str, value: object
) -> None:
    stable = {
        key: lease_parts[key]
        for key in (
            "tenant_id",
            "task_id",
            "run_id",
            "subtask_id",
            "role",
            "runtime_version_id",
            "runtime_execution_intent_id",
            "agent_version_id",
            "agent_version_digest",
            "task_plan_version",
            "task_plan_digest",
            "run_revision",
            "work_item",
        )
    }
    with pytest.raises((InvalidTaskInput, ValueError, TypeError)):
        assignment_projection_digest(**dict(stable, **{field: value}))


def test_proof_phase_and_handle_identity_bounds(lease_parts: dict) -> None:
    for phase in (
        RuntimeExecutionPhase.PREPARED,
        RuntimeExecutionPhase.SUCCEEDED,
        RuntimeExecutionPhase.LOST,
        RuntimeExecutionPhase.OUTCOME_UNKNOWN,
    ):
        with pytest.raises(InvalidTaskInput):
            RecoveryCrossedProof(lease_parts["run_id"], lease_parts["attempt_id"], 1, phase, 1)
    with pytest.raises(InvalidTaskInput):
        RecoveryCrossedProof(
            lease_parts["run_id"],
            lease_parts["attempt_id"],
            1,
            RuntimeExecutionPhase.RUNNING,
            1,
            uuid4(),
            None,
        )
    with pytest.raises(InvalidTaskInput):
        RecoveryCrossedProof(
            lease_parts["run_id"],
            lease_parts["attempt_id"],
            1,
            RuntimeExecutionPhase.RUNNING,
            1,
            uuid4(),
            "C" * 64,
        )


def test_ownership_scalar_boundaries(lease_parts: dict) -> None:
    args = dict(
        assignment_projection_digest=lease_parts["assignment_projection_digest"],
        tenant_id=lease_parts["tenant_id"],
        task_id=lease_parts["task_id"],
        run_id=lease_parts["run_id"],
        subtask_id=lease_parts["subtask_id"],
        attempt_id=lease_parts["attempt_id"],
        fencing_token=lease_parts["fencing_token"],
        lease_token=lease_parts["lease_token"],
        lease_deadline=lease_parts["lease_deadline"],
    )
    for field, value in (
        ("assignment_projection_digest", "x"),
        ("attempt_id", "x"),
        ("fencing_token", 0),
        ("fencing_token", -1),
        ("fencing_token", True),
        ("lease_token", "x"),
        ("lease_deadline", datetime(2030, 1, 1)),
    ):
        with pytest.raises(InvalidTaskInput):
            ownership_digest(**dict(args, **{field: value}))


def test_recovery_fencing_token_must_be_positive() -> None:
    for value in (0, -1, True):
        with pytest.raises(InvalidTaskInput):
            RecoveryCrossedProof(uuid4(), uuid4(), value, RuntimeExecutionPhase.RUNNING, 1)


def test_result_reason_combinations_are_closed(lease_parts: dict) -> None:
    lease = _lease(lease_parts)
    assert (
        CoordinatedDeliveryResult.without_lease(
            CoordinatedDeliveryResultKind.BLOCKED_BY_DRAIN, reason="draining"
        ).reason
        == "draining"
    )
    assert (
        CoordinatedDeliveryResult.without_lease(
            CoordinatedDeliveryResultKind.WAITING_APPROVAL, reason="budget"
        ).reason
        == "budget"
    )
    for kind in (
        CoordinatedDeliveryResultKind.ACQUIRED,
        CoordinatedDeliveryResultKind.RECOVERED_PRE_BOUNDARY,
    ):
        with pytest.raises(InvalidTaskInput):
            CoordinatedDeliveryResult(kind, lease=lease, reason="illegal")
    with pytest.raises(InvalidTaskInput):
        CoordinatedDeliveryResult.without_lease(
            CoordinatedDeliveryResultKind.IN_PROGRESS, reason=""
        )


def test_contract_module_has_no_infrastructure_or_caller_imports() -> None:
    import ast
    from pathlib import Path

    module = Path(__file__).parents[1] / "src/agentmesh/application/coordinated_runtime_delivery.py"
    tree = ast.parse(module.read_text(encoding="utf-8"))
    imports = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)] + [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    ]
    forbidden = ("repository", "unit_of_work", "worker", "adapter", "postgres", "database")
    assert not any(any(term in item.lower() for term in forbidden) for item in imports)
