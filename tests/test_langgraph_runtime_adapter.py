from datetime import datetime, timezone
from uuid import uuid4

import pytest

from agentmesh.application.runtime_comparison import (
    RuntimeComparisonSnapshot,
    compare_snapshots,
)
from agentmesh.domain.tasks import TaskRun
from agentmesh.infrastructure.runtime.langgraph_adapter import (
    LANGGRAPH_DESCRIPTOR,
    EphemeralRuntimeLifecycleController,
    EphemeralRuntimeStateStore,
    KeyedAdmissionRegistry,
    LangGraphManagedAgentRuntime,
)
from agentmesh.runtime_sdk import (
    RuntimeAssignment,
    RuntimePhase,
)
from agentmesh.runtime_sdk.builtin import langgraph_descriptor, langgraph_v2_descriptor


def _adapter() -> LangGraphManagedAgentRuntime:
    return LangGraphManagedAgentRuntime(
        backend=_Backend(),
        state_store=EphemeralRuntimeStateStore(),
        lifecycle_controller=EphemeralRuntimeLifecycleController(),
    )


class _Backend:
    def bind(self, assignment, task, run, attempt, work_item):
        return None

    def execute(self, assignment):
        from agentmesh.runtime_sdk import RuntimeObservation

        return RuntimeObservation(
            observation_id=str(uuid4()),
            runtime_execution_id=assignment.correlation_ids["runtime_execution_id"],
            assignment_id=assignment.assignment_id,
            assignment_digest=assignment.assignment_digest,
            phase=RuntimePhase.SUCCEEDED,
            observed_at=datetime.now(timezone.utc),
            provider_event_id="fixture",
            output={"ok": True},
        )


class _TrackingLifecycleController(EphemeralRuntimeLifecycleController):
    def __init__(self) -> None:
        self.calls: list[str] = []

    def request(self, operation, handle, *, operation_id, deadline):
        self.calls.append(operation)


def _assignment(mode: str = "inline") -> RuntimeAssignment:
    execution_id = str(uuid4())
    return RuntimeAssignment(
        assignment_id=str(uuid4()),
        tenant_id="tenant-a",
        task_id=str(uuid4()),
        run_id=str(uuid4()),
        agent_definition_id=str(uuid4()),
        agent_version_id=str(uuid4()),
        agent_version_digest="a" * 64,
        runtime_version_id=str(uuid4()),
        runtime_descriptor_digest=_adapter().descriptor().digest(),
        execution_mode=mode,
        run_role="EXECUTOR",
        revision=0,
        objective="deterministic fixture",
        structured_input={"value": "safe"},
        correlation_ids={"runtime_execution_id": execution_id},
    )


def test_descriptor_matches_published_langgraph_descriptor() -> None:
    adapter = _adapter()
    assert adapter.descriptor().to_dict() == LANGGRAPH_DESCRIPTOR
    assert adapter.descriptor().to_dict() == langgraph_v2_descriptor()
    assert langgraph_descriptor()["capabilities"]["tool_bridge"] == ["governed_action_v1"]
    assert langgraph_descriptor()["capabilities"]["artifact_io"] == ["reference"]
    assert langgraph_v2_descriptor()["capabilities"]["tool_bridge"] == []
    assert langgraph_v2_descriptor()["capabilities"]["artifact_io"] == []
    assert adapter.validate(_assignment()).valid is True


def test_dispatch_is_stable_and_rejects_same_key_different_assignment() -> None:
    adapter = _adapter()
    assignment = _assignment()
    execution_id = assignment.correlation_ids["runtime_execution_id"]
    key = f"runtime-dispatch:{assignment.tenant_id}:{execution_id}"
    first = adapter.dispatch(assignment, dispatch_key=key)
    second = adapter.dispatch(assignment, dispatch_key=key)
    assert first.to_dict() == second.to_dict()
    assert first.handle is not None
    assert first.handle.provider_execution_ref.startswith("langgraph-thread:")
    assert first.handle.provider_generation == "langgraph-v2-inline"
    with pytest.raises(ValueError, match="invalid"):
        adapter.dispatch(_assignment(), dispatch_key=key)


def test_invalid_dispatch_keys_do_not_allocate_admission_guards() -> None:
    registry = KeyedAdmissionRegistry()
    adapter = LangGraphManagedAgentRuntime(
        backend=_Backend(),
        state_store=EphemeralRuntimeStateStore(),
        lifecycle_controller=EphemeralRuntimeLifecycleController(),
        admission_registry=registry,
    )
    assignment = _assignment()
    valid_key = (
        f"runtime-dispatch:{assignment.tenant_id}:"
        f"{assignment.correlation_ids['runtime_execution_id']}"
    )
    invalid_keys = [
        "runtime-dispatch:other-tenant:" + str(uuid4()),
        "runtime-dispatch:" + assignment.tenant_id + ":" + str(uuid4()),
        valid_key + "x" * 513,
    ]
    for invalid_key in invalid_keys:
        with pytest.raises(ValueError, match="invalid"):
            adapter.dispatch(assignment, dispatch_key=invalid_key)
    assert registry.active_count == 0
    receipt = adapter.dispatch(assignment, dispatch_key=valid_key)
    assert receipt.observation.phase is RuntimePhase.SUCCEEDED
    assert registry.active_count == 0


def test_inline_terminal_events_cursor_and_lifecycle_idempotency() -> None:
    adapter = _adapter()
    assignment = _assignment("inline")
    execution_id = assignment.correlation_ids["runtime_execution_id"]
    key = f"runtime-dispatch:{assignment.tenant_id}:{execution_id}"
    receipt = adapter.dispatch(assignment, dispatch_key=key)
    assert receipt.observation.phase is RuntimePhase.SUCCEEDED
    assert receipt.handle is not None
    assert adapter.inspect(receipt.handle).phase is RuntimePhase.SUCCEEDED
    deadline = datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="unsupported"):
        adapter.read_events(receipt.handle, cursor=None, limit=1)
    with pytest.raises(ValueError, match="unsupported"):
        adapter.request_cancel(receipt.handle, cancellation_id="cancel-1", deadline=deadline)
    with pytest.raises(ValueError, match="unsupported"):
        adapter.request_pause(receipt.handle, operation_id="pause-1")
    with pytest.raises(ValueError, match="unsupported"):
        adapter.request_resume(receipt.handle, operation_id="resume-1")
    managed = _assignment("managed_async")
    managed_key = f"runtime-dispatch:tenant-a:{managed.correlation_ids['runtime_execution_id']}"
    with pytest.raises(ValueError, match="managed_async"):
        adapter.dispatch(managed, dispatch_key=managed_key)


def test_shared_store_restart_recovers_terminal_handle_without_overclaiming_reattach() -> None:
    store = EphemeralRuntimeStateStore()
    first_adapter = LangGraphManagedAgentRuntime(
        backend=_Backend(),
        state_store=store,
        lifecycle_controller=EphemeralRuntimeLifecycleController(),
    )
    assignment = _assignment()
    key = (
        f"runtime-dispatch:{assignment.tenant_id}:"
        f"{assignment.correlation_ids['runtime_execution_id']}"
    )
    first = first_adapter.dispatch(assignment, dispatch_key=key)

    # A new adapter instance represents a process restart.  Recovery is only
    # claimed because the same injected store contains the terminal provider
    # state; v2 still honestly declares reattach/lifecycle capabilities off.
    restarted = LangGraphManagedAgentRuntime(
        backend=_Backend(),
        state_store=store,
        lifecycle_controller=EphemeralRuntimeLifecycleController(),
    )
    recovered = restarted.inspect(first.handle)
    replay = restarted.dispatch(assignment, dispatch_key=key)
    assert recovered.to_dict() == first.observation.to_dict()
    assert replay.to_dict() == first.to_dict()
    assert restarted.descriptor().capabilities.reattach is False


def test_close_is_non_destructive_and_does_not_cancel_provider_state() -> None:
    controller = _TrackingLifecycleController()
    adapter = LangGraphManagedAgentRuntime(
        backend=_Backend(),
        state_store=EphemeralRuntimeStateStore(),
        lifecycle_controller=controller,
    )
    assignment = _assignment()
    key = (
        f"runtime-dispatch:{assignment.tenant_id}:"
        f"{assignment.correlation_ids['runtime_execution_id']}"
    )
    receipt = adapter.dispatch(assignment, dispatch_key=key)

    adapter.close()

    assert controller.calls == []
    assert adapter.inspect(receipt.handle).phase is RuntimePhase.SUCCEEDED


def test_runtime_comparison_records_all_authority_dimensions() -> None:
    left = RuntimeComparisonSnapshot(
        terminal_state="SUCCEEDED",
        output={"answer": 1},
        usage={"input_tokens": 2},
        artifact_refs=("artifact-a",),
        review={"accepted": True},
        revision=1,
        audit={"policy": "same"},
    )
    right = RuntimeComparisonSnapshot(
        terminal_state="SUCCEEDED",
        output={"answer": 2},
        usage={"input_tokens": 2},
        artifact_refs=("artifact-a",),
        review={"accepted": True},
        revision=1,
        audit={"policy": "same"},
    )
    report = compare_snapshots(
        task_id=uuid4(),
        run_id=uuid4(),
        authoritative=left,
        comparison=right,
        authoritative_path="managed-runtime",
    )
    assert report.matches is False
    assert report.mismatches == ("output_digest",)
    assert report.authoritative_digest != report.comparison_digest


def test_runtime_comparison_digest_excludes_provider_observation_identity() -> None:
    left = RuntimeComparisonSnapshot(
        terminal_state="SUCCEEDED",
        output={"answer": 1},
        usage={"input_tokens": 2},
        evidence_id="provider-observation-a",
    )
    right = RuntimeComparisonSnapshot(
        terminal_state="SUCCEEDED",
        output={"answer": 1},
        usage={"input_tokens": 2},
        evidence_id="provider-observation-b",
    )
    assert left.digest() == right.digest()


def test_descriptor_mismatch_fails_closed() -> None:
    adapter = _adapter()
    assignment = _assignment()
    object.__setattr__(assignment, "runtime_descriptor_digest", "f" * 64)
    assert adapter.validate(assignment).valid is False


def test_deterministic_shadow_run_snapshots_path_and_execution_intent() -> None:
    run = TaskRun.request_deterministic_shadow(
        uuid4(),
        "demo-agent",
        runtime_version_id=uuid4(),
    )
    assert run.runtime_authority == "legacy"
    assert run.comparison_mode == "deterministic_shadow"
    assert run.runtime_execution_id is None
    assert run.runtime_execution_intent_id is not None
