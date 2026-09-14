from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from types import MappingProxyType
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from agentmesh.application.coordinated_runtime_delivery import (
    CoordinatedDeliveryLeaseV1,
    assignment_projection_digest,
    ownership_digest,
)
from agentmesh.application.managed_runtime_execution import ManagedRuntimeExecutionService
from agentmesh.application.ports import WorkflowExecutionResult, WorkflowWorkItem
from agentmesh.domain.tasks import RunRole
from agentmesh.infrastructure.runtime.langgraph_adapter import (
    EphemeralRuntimeLifecycleController,
    EphemeralRuntimeStateStore,
    LangGraphManagedAgentRuntime,
    LangGraphWorkflowBackend,
)
from agentmesh.observability import LangfuseAttemptTelemetry
from agentmesh.orchestration.workflow import LangGraphWorkflowRunner
from agentmesh.runtime_sdk import RuntimeAssignment

UTC = timezone.utc


class _Backend:
    def __init__(self):
        self.delivery = None

    def bind(self, assignment, task, run, attempt, work_item):
        raise AssertionError("delivery binding must not receive mutable domain entities")

    def bind_delivery(self, assignment, lease, work_item):
        self.delivery = (assignment, lease, work_item)

    def execute(self, assignment):
        raise AssertionError("delivery contract tests must not call the provider")


def _adapter() -> LangGraphManagedAgentRuntime:
    return LangGraphManagedAgentRuntime(
        backend=_Backend(),
        state_store=EphemeralRuntimeStateStore(),
        lifecycle_controller=EphemeralRuntimeLifecycleController(),
    )


def _lease(
    *, attempt_id=None, fencing_token=1, lease_token=None, deadline=None, work_item=None
):
    if work_item is None:
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


def _replacement_lease(lease: CoordinatedDeliveryLeaseV1) -> CoordinatedDeliveryLeaseV1:
    attempt_id = uuid4()
    lease_token = uuid4()
    deadline = datetime(2030, 1, 1, 0, 5, tzinfo=UTC)
    return replace(
        lease,
        attempt_id=attempt_id,
        fencing_token=lease.fencing_token + 1,
        lease_token=lease_token,
        lease_deadline=deadline,
        ownership_digest=ownership_digest(
            assignment_projection_digest=lease.assignment_projection_digest,
            tenant_id=lease.tenant_id,
            task_id=lease.task_id,
            run_id=lease.run_id,
            subtask_id=lease.subtask_id,
            attempt_id=attempt_id,
            fencing_token=lease.fencing_token + 1,
            lease_token=lease_token,
            lease_deadline=deadline,
        ),
    )


def test_delivery_assignment_is_byte_stable_and_excludes_replacement_ownership():
    adapter = _adapter()
    first_lease = _lease()
    first = adapter.assignment_for_delivery(first_lease, first_lease.work_item)
    replay = adapter.assignment_for_delivery(first_lease, first_lease.work_item)
    replacement = _replacement_lease(first_lease)
    recovered = adapter.assignment_for_delivery(replacement, replacement.work_item)
    assert first.to_dict() == replay.to_dict()
    assert first.to_dict() == recovered.to_dict()
    assert first.assignment_digest == recovered.assignment_digest
    assert first_lease.ownership_digest != replacement.ownership_digest


def test_delivery_assignment_thaws_nested_frozen_json_input():
    frozen_input = MappingProxyType(
        {
            "nested": (
                MappingProxyType(
                    {"value": "stable", "items": ("one", "two")}
                ),
            )
        }
    )
    work_item = WorkflowWorkItem("deliver", frozen_input)
    lease = _lease(work_item=work_item)

    assignment = _adapter().assignment_for_delivery(lease, work_item)

    assert type(assignment.structured_input) is dict
    assert type(assignment.structured_input["nested"]) is list
    assert type(assignment.structured_input["nested"][0]) is dict
    assert type(assignment.structured_input["nested"][0]["items"]) is list
    assert assignment.structured_input == {
        "nested": [{"value": "stable", "items": ["one", "two"]}]
    }
    assert isinstance(work_item.input, MappingProxyType)
    assert isinstance(work_item.input["nested"], tuple)
    assert isinstance(work_item.input["nested"][0], MappingProxyType)


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
    assert adapter._backend.delivery == (assignment, lease, lease.work_item)

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


def test_detached_backend_dispatches_once_and_replays_same_assignment():
    lease = _lease()

    class Runner:
        def __init__(self):
            self.calls = []

        def run_delivery(self, got_lease, got_work_item):
            self.calls.append((got_lease, got_work_item))
            return WorkflowExecutionResult(output={"run_id": str(got_lease.run_id)})

    runner = Runner()
    backend = LangGraphWorkflowBackend(runner)
    adapter = LangGraphManagedAgentRuntime(
        backend=backend,
        state_store=EphemeralRuntimeStateStore(),
        lifecycle_controller=EphemeralRuntimeLifecycleController(),
    )
    assignment = adapter.assignment_for_delivery(lease, lease.work_item)
    adapter.bind_delivery_context(assignment, lease, lease.work_item)
    key = f"runtime-dispatch:{lease.tenant_id}:{lease.runtime_execution_intent_id}"

    first = adapter.dispatch(assignment, dispatch_key=key)
    replacement = _replacement_lease(lease)
    replacement_assignment = adapter.assignment_for_delivery(
        replacement, replacement.work_item
    )
    adapter.bind_delivery_context(
        replacement_assignment, replacement, replacement.work_item
    )
    replay = adapter.dispatch(replacement_assignment, dispatch_key=key)

    assert first.observation.output == {"run_id": str(lease.run_id)}
    assert replay.handle == first.handle
    assert replacement_assignment.to_dict() == assignment.to_dict()
    assert runner.calls == [(lease, lease.work_item)]


def test_detached_runner_uses_lease_identities_without_domain_entities():
    lease = _lease()

    class Executor:
        def __init__(self):
            self.context = None

        def execute(self, *, objective, input, context):
            self.context = context
            return {"objective": objective, "value": input["value"]}

    executor = Executor()
    runner = LangGraphWorkflowRunner(
        agent_executor=executor,
        checkpointer=InMemorySaver(),
    )

    result = runner.run_delivery(lease, lease.work_item)

    context = executor.context
    assert result.output == {"objective": "deliver", "value": "stable"}
    assert context.task_id == lease.task_id
    assert context.run_id == lease.run_id
    assert context.attempt_id == lease.attempt_id
    assert context.trace_id == lease.attempt_id.hex
    assert context.thread_id == str(lease.run_id)
    assert context.agent_id == str(
        uuid5(NAMESPACE_URL, f"agentmesh:agent:{lease.agent_version_id}")
    )
    assert context.agent_version_id == lease.agent_version_id


def test_detached_telemetry_exports_hashed_tenant_and_bounded_identifiers():
    lease = _lease()

    class Context:
        def __enter__(self):
            return self

        def __exit__(self, *error_info):
            return False

    class Client:
        def __init__(self):
            self.calls = []

        def start_as_current_observation(self, **values):
            self.calls.append(values)
            return Context()

    propagated = []
    client = Client()
    telemetry = LangfuseAttemptTelemetry(
        client,
        lambda **values: (propagated.append(values) or Context()),
    )

    with telemetry.observe_delivery(lease):
        pass

    root = client.calls[0]
    metadata = root["metadata"]
    assert metadata["task_id"] == str(lease.task_id)
    assert metadata["run_id"] == str(lease.run_id)
    assert metadata["attempt_id"] == str(lease.attempt_id)
    assert metadata["tenant_key"] != lease.tenant_id
    assert lease.tenant_id not in repr(root)
    assert "objective" not in metadata
    assert "input" not in metadata
    assert propagated[0]["metadata"]["tenant_key"] == metadata["tenant_key"]
