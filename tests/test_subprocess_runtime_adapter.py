from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from agentmesh.features import Feature, FeatureGateSet
from agentmesh.infrastructure.runtime.subprocess_adapter import SubprocessAgentRuntime
from agentmesh.runtime_sdk import RuntimeAssignment, RuntimePhase


def _adapter(tmp_path, **kwargs) -> SubprocessAgentRuntime:
    command = kwargs.pop("command", [sys.executable, "-m", "agentmesh.reference_agent"])
    return SubprocessAgentRuntime(
        command=command,
        artifact_staging_dir=tmp_path / "artifacts",
        **kwargs,
    )


def _assignment(
    adapter: SubprocessAgentRuntime, *, fixture: dict | None = None
) -> RuntimeAssignment:
    return RuntimeAssignment(
        assignment_id=str(uuid4()),
        tenant_id="tenant-a",
        task_id=str(uuid4()),
        run_id=str(uuid4()),
        agent_definition_id=str(uuid4()),
        agent_version_id=str(uuid4()),
        agent_version_digest="a" * 64,
        runtime_version_id=str(uuid4()),
        runtime_descriptor_digest=adapter.descriptor().digest(),
        execution_mode="inline",
        run_role="EXECUTOR",
        revision=0,
        objective="produce a report",
        structured_input={"input": "stable", "_reference_agent": fixture or {}},
        correlation_ids={"runtime_execution_id": str(uuid4())},
    )


def test_reference_process_stages_deterministic_report_and_replay_is_idempotent(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    assignment = _assignment(adapter)
    first = adapter.dispatch(assignment, dispatch_key="dispatch-1")
    replay = adapter.dispatch(assignment, dispatch_key="dispatch-1")

    assert first == replay
    assert first.observation.phase is RuntimePhase.SUCCEEDED
    assert first.observation.output["kind"] == "agentmesh.reference.report.v1"
    assert len(first.observation.output_artifact_refs) == 1
    staged = tmp_path / "artifacts" / f"{first.runtime_execution_id}.json"
    assert staged.exists()
    assert adapter.inspect(first.handle).output == first.observation.output
    adapter.close()


def test_same_dispatch_key_rejects_a_different_assignment(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    first = _assignment(adapter)
    adapter.dispatch(first, dispatch_key="dispatch-1")
    second = _assignment(adapter)
    with pytest.raises(ValueError, match="different assignment"):
        adapter.dispatch(second, dispatch_key="dispatch-1")


def test_concurrent_replay_runs_one_backend_process(tmp_path, monkeypatch) -> None:
    adapter = _adapter(tmp_path)
    assignment = _assignment(adapter, fixture={"delay_ms": 100})
    calls = 0
    original = adapter._run

    def counted(state, key):
        nonlocal calls
        calls += 1
        return original(state, key)

    monkeypatch.setattr(adapter, "_run", counted)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda _: adapter.dispatch(assignment, dispatch_key="same"), range(2))
        )
    assert calls == 1
    assert results[0] == results[1]


def test_descriptor_is_honest_and_unsupported_operations_fail_closed(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    descriptor = adapter.descriptor()
    assert descriptor.capabilities.reattach is False
    assert descriptor.capabilities.event_stream is False
    assert descriptor.capabilities.tool_bridge == ()
    assert descriptor.capabilities.artifact_io == ("reference",)
    assignment = _assignment(adapter)
    receipt = adapter.dispatch(assignment, dispatch_key="dispatch-1")
    with pytest.raises(ValueError, match="unsupported"):
        adapter.read_events(receipt.handle, cursor=None, limit=1)
    with pytest.raises(ValueError, match="unsupported"):
        adapter.request_pause(receipt.handle, operation_id="pause-1")
    with pytest.raises(ValueError, match="unsupported"):
        adapter.request_resume(receipt.handle, operation_id="resume-1")


def test_timeout_crash_and_malformed_output_are_mapped_and_stderr_is_bounded(tmp_path) -> None:
    adapter = _adapter(tmp_path, timeout_seconds=0.1, max_stderr_bytes=64)
    timed_out = adapter.dispatch(
        _assignment(adapter, fixture={"delay_ms": 500}), dispatch_key="timeout"
    )
    assert timed_out.observation.phase is RuntimePhase.TIMED_OUT
    crashed = adapter.dispatch(_assignment(adapter, fixture={"crash": True}), dispatch_key="crash")
    assert crashed.observation.phase is RuntimePhase.FAILED
    malformed = adapter.dispatch(
        _assignment(adapter, fixture={"malformed": True}), dispatch_key="malformed"
    )
    assert malformed.observation.phase is RuntimePhase.FAILED
    assert len(malformed.observation.progress.get("stderr", "")) <= 64


def test_oversize_stdout_is_rejected_before_result_parsing(tmp_path) -> None:
    adapter = _adapter(tmp_path, max_stdout_bytes=128)
    receipt = adapter.dispatch(
        _assignment(adapter, fixture={"stdout_bytes": 2_000}), dispatch_key="oversize"
    )
    assert receipt.observation.phase is RuntimePhase.FAILED
    assert receipt.observation.error.code == "runtime.stdout_limit"


def test_cancel_kills_process_group_and_does_not_leave_workspace(tmp_path) -> None:
    adapter = _adapter(tmp_path, timeout_seconds=5)
    assignment = _assignment(adapter, fixture={"delay_ms": 10_000})
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(adapter.dispatch, assignment, dispatch_key="cancel")
        handle = None
        for _ in range(100):
            state = adapter._states.get(assignment.correlation_ids["runtime_execution_id"])
            if state is not None:
                handle = state.handle
                break
        assert handle is not None
        adapter.request_cancel(
            handle,
            cancellation_id="cancel-1",
            deadline=datetime.now(timezone.utc) + timedelta(seconds=1),
        )
        receipt = future.result(timeout=5)
    assert receipt.observation.phase is RuntimePhase.CANCELED
    assert not list(tmp_path.glob("agentmesh-runtime-*"))


def test_environment_is_allowlisted_and_rejects_secrets(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://should-not-pass")
    monkeypatch.setenv("UNRELATED_SECRET", "should-not-pass")
    adapter = _adapter(tmp_path)
    assert "DATABASE_URL" not in adapter._environment
    assert "UNRELATED_SECRET" not in adapter._environment
    with pytest.raises(ValueError, match="non-allowlisted"):
        _adapter(tmp_path, environment={"DATABASE_URL": "bad"})


def test_command_must_be_structured_argv_and_never_a_shell_string(tmp_path) -> None:
    with pytest.raises(ValueError, match="sequence of strings"):
        _adapter(tmp_path, command="python -m agentmesh.reference_agent")


def test_reference_agent_has_no_framework_or_control_plane_imports() -> None:
    source = open("src/agentmesh/reference_agent/__main__.py", encoding="utf-8").read()
    assert "agentmesh.runtime_sdk" in source
    assert "langgraph" not in source
    assert "agentmesh.application" not in source
    assert "agentmesh.domain" not in source
    assert "agentmesh.infrastructure" not in source


def test_subprocess_gate_is_explicit_and_excluded_from_default_profiles() -> None:
    minimal = FeatureGateSet.from_config("minimal")
    full = FeatureGateSet.from_config("full")
    assert not minimal.is_enabled(Feature.GENERIC_SUBPROCESS_RUNTIME)
    assert not full.is_enabled(Feature.GENERIC_SUBPROCESS_RUNTIME)
    enabled = FeatureGateSet.from_config(
        "minimal", "managed_agent_runtime=true,generic_subprocess_runtime=true"
    )
    assert enabled.is_enabled(Feature.GENERIC_SUBPROCESS_RUNTIME)
