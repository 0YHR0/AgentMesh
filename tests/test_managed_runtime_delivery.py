from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from agentmesh.application.coordinated_runtime_delivery import (
    CoordinatedDeliveryLeaseV1,
    assignment_projection_digest,
    ownership_digest,
)
from agentmesh.application.managed_runtime_execution import ManagedRuntimeExecutionService
from agentmesh.application.ports import WorkflowWorkItem
from agentmesh.domain.tasks import RunRole
from agentmesh.infrastructure.runtime.langgraph_adapter import (
    EphemeralRuntimeLifecycleController,
    EphemeralRuntimeStateStore,
    LangGraphManagedAgentRuntime,
)
from agentmesh.runtime_sdk import RuntimeAssignment

UTC = timezone.utc


class _Backend:
    def bind(self, assignment, task, run, attempt, work_item):
        raise AssertionError("delivery binding must not receive mutable domain entities")

    def execute(self, assignment):
        raise AssertionError("delivery contract tests must not call the provider")


def _adapter() -> LangGraphManagedAgentRuntime:
    return LangGraphManagedAgentRuntime(
        backend=_Backend(),
        state_store=EphemeralRuntimeStateStore(),
        lifecycle_controller=EphemeralRuntimeLifecycleController(),
    )


def _lease(*, attempt_id=None, fencing_token=1, lease_token=None, deadline=None):
    work_item = WorkflowWorkItem("deliver", {"value": "stable"})
    task_id = uuid4()
    run_id = uuid4()
    subtask_id = uuid4()
    runtime_version_id = uuid4()
    execution_intent_id = uuid4()
    agent_version_id = uuid4()
    stable = assignment_projection_digest(
        tenant_id="tenant-a",
        task_id=task_id,
        run_id=run_id,
        subtask_id=subtask_id,
        role=RunRole.EXECUTOR,
        runtime_version_id=runtime_version_id,
        runtime_execution_intent_id=execution_intent_id,
        agent_version_id=agent_version_id,
        agent_version_digest="a" * 64,
        task_plan_version=1,
        task_plan_digest="sha256:" + "b" * 64,
        run_revision=0,
        work_item=work_item,
    )
    selected_attempt = attempt_id or uuid4()
    selected_token = lease_token or uuid4()
    selected_deadline = deadline or datetime(2030, 1, 1, tzinfo=UTC)
    return CoordinatedDeliveryLeaseV1(
        schema_version=1,
        tenant_id="tenant-a",
        task_id=task_id,
        run_id=run_id,
        subtask_id=subtask_id,
        attempt_id=selected_attempt,
        role=RunRole.EXECUTOR,
        fencing_token=fencing_token,
        lease_token=selected_token,
        lease_deadline=selected_deadline,
        runtime_version_id=runtime_version_id,
        runtime_execution_intent_id=execution_intent_id,
        task_plan_version=1,
        task_plan_digest="sha256:" + "b" * 64,
        run_revision=0,
        agent_version_id=agent_version_id,
        agent_version_digest="a" * 64,
        work_item=work_item,
        assignment_projection_digest=stable,
        ownership_digest=ownership_digest(
            assignment_projection_digest=stable,
            tenant_id="tenant-a",
            task_id=task_id,
            run_id=run_id,
            subtask_id=subtask_id,
            attempt_id=selected_attempt,
            fencing_token=fencing_token,
            lease_token=selected_token,
            lease_deadline=selected_deadline,
        ),
    )


def test_delivery_assignment_is_byte_stable_and_excludes_replacement_ownership():
    adapter = _adapter()
    first_lease = _lease()
    first = adapter.assignment_for_delivery(first_lease, first_lease.work_item)
    replay = adapter.assignment_for_delivery(first_lease, first_lease.work_item)
    replacement_attempt = uuid4()
    replacement_token = uuid4()
    replacement_deadline = datetime(2030, 1, 1, 0, 5, tzinfo=UTC)
    replacement = replace(
        first_lease,
        attempt_id=replacement_attempt,
        fencing_token=2,
        lease_token=replacement_token,
        lease_deadline=replacement_deadline,
        ownership_digest=ownership_digest(
            assignment_projection_digest=first_lease.assignment_projection_digest,
            tenant_id=first_lease.tenant_id,
            task_id=first_lease.task_id,
            run_id=first_lease.run_id,
            subtask_id=first_lease.subtask_id,
            attempt_id=replacement_attempt,
            fencing_token=2,
            lease_token=replacement_token,
            lease_deadline=replacement_deadline,
        ),
    )
    recovered = adapter.assignment_for_delivery(replacement, replacement.work_item)
    assert first.to_dict() == replay.to_dict()
    assert first.to_dict() == recovered.to_dict()
    assert first.assignment_digest == recovered.assignment_digest
    assert first_lease.ownership_digest != replacement.ownership_digest


def test_delivery_assignment_rejects_changed_work_item_or_stable_projection():
    adapter = _adapter()
    lease = _lease()
    changed = WorkflowWorkItem("deliver", {"value": "changed"})
    with pytest.raises(ValueError, match="work item"):
        adapter.assignment_for_delivery(lease, changed)

    tampered = replace(lease, ownership_digest=lease.ownership_digest)
    object.__setattr__(tampered, "assignment_projection_digest", "c" * 64)
    with pytest.raises(ValueError, match="stable projection"):
        adapter.assignment_for_delivery(tampered, lease.work_item)


def test_delivery_context_binding_accepts_only_matching_detached_assignment():
    adapter = _adapter()
    lease = _lease()
    assignment = adapter.assignment_for_delivery(lease, lease.work_item)
    adapter.bind_delivery_context(assignment, lease, lease.work_item)
    assert adapter._delivery_contexts[assignment.assignment_id] == (lease, lease.work_item)

    changed_payload = assignment.to_dict(include_digest=False)
    changed_payload["structured_input"] = {"value": "changed"}
    changed = RuntimeAssignment.from_dict(changed_payload)
    with pytest.raises(ValueError, match="does not match"):
        adapter.bind_delivery_context(changed, lease, lease.work_item)


def test_managed_execution_port_delegates_detached_delivery_contract():
    lease = _lease()
    work_item = lease.work_item
    expected = object()

    class Builder:
        def assignment_for_delivery(self, got_lease, got_work_item):
            assert got_lease is lease and got_work_item is work_item
            return expected

    class Adapter:
        def bind_delivery_context(self, assignment, got_lease, got_work_item):
            assert assignment is expected
            assert got_lease is lease and got_work_item is work_item

    service = ManagedRuntimeExecutionService(
        registry=object(), adapter=Adapter(), assignment_builder=Builder()
    )
    assert service.assignment_for_delivery(lease, work_item) is expected
    service.bind_delivery_context(expected, lease, work_item)
