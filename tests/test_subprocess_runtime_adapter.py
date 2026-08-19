from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest

from agentmesh.features import Feature, FeatureGateSet
from agentmesh.infrastructure.runtime.subprocess_adapter import SubprocessAgentRuntime
from agentmesh.runtime_sdk import (
    RuntimeAssignment,
    RuntimeLimits,
    RuntimePhase,
    canonical_json_bytes,
)

ROOT = Path(__file__).parents[1]
REFERENCE_SRC = ROOT / "examples" / "reference-agent" / "src"
REFERENCE_ENV = {"PYTHONPATH": os.pathsep.join((str(REFERENCE_SRC), str(ROOT / "src")))}


def _adapter(tmp_path, **kwargs) -> SubprocessAgentRuntime:
    command = kwargs.pop("command", [sys.executable, "-m", "agentmesh_reference_agent"])
    environment = kwargs.pop("environment", REFERENCE_ENV)
    return SubprocessAgentRuntime(
        command=command,
        environment=environment,
        artifact_staging_dir=tmp_path / "artifacts",
        **kwargs,
    )


def _assignment(
    adapter: SubprocessAgentRuntime,
    *,
    fixture: dict | None = None,
    mode: str = "inline",
    tenant_id: str = "tenant-a",
) -> RuntimeAssignment:
    return RuntimeAssignment(
        assignment_id=str(uuid4()),
        tenant_id=tenant_id,
        task_id=str(uuid4()),
        run_id=str(uuid4()),
        agent_definition_id=str(uuid4()),
        agent_version_id=str(uuid4()),
        agent_version_digest="a" * 64,
        runtime_version_id=str(uuid4()),
        runtime_descriptor_digest=adapter.descriptor().digest(),
        execution_mode=mode,
        run_role="EXECUTOR",
        revision=0,
        objective="produce a report",
        structured_input={"input": "stable", "_reference_agent": fixture or {}},
        correlation_ids={"runtime_execution_id": str(uuid4())},
    )


def _key(assignment: RuntimeAssignment, tenant: str | None = None) -> str:
    execution_id = assignment.correlation_ids["runtime_execution_id"]
    return f"runtime-dispatch:{tenant or assignment.tenant_id}:{execution_id}"


def _wait_terminal(adapter, handle, timeout: float = 5) -> RuntimePhase:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        phase = adapter.inspect(handle).phase
        if phase.terminal:
            return phase
        time.sleep(0.01)
    raise AssertionError("runtime did not reach a terminal phase")


def test_reference_process_stages_deterministic_report_and_replay_is_idempotent(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    assignment = _assignment(adapter)
    first = adapter.dispatch(assignment, dispatch_key=_key(assignment))
    replay = adapter.dispatch(assignment, dispatch_key=_key(assignment))

    assert first == replay
    assert first.observation.phase is RuntimePhase.SUCCEEDED
    assert first.observation.output["kind"] == "agentmesh.reference.report.v1"
    assert len(first.observation.output_artifact_refs) == 1
    staged = tmp_path / "artifacts" / f"{first.runtime_execution_id}.json"
    assert staged.exists()
    assert (
        sha256(staged.read_bytes()).hexdigest() == first.observation.output_artifact_refs[0].digest
    )
    assert adapter.inspect(first.handle).output == first.observation.output
    adapter.close()


def test_managed_async_returns_public_handle_and_cancel_is_idempotent(tmp_path) -> None:
    adapter = _adapter(tmp_path, timeout_seconds=5)
    assignment = _assignment(adapter, fixture={"delay_ms": 10_000}, mode="managed_async")
    receipt = adapter.dispatch(assignment, dispatch_key=_key(assignment))
    assert receipt.handle is not None
    assert receipt.observation.phase is RuntimePhase.DISPATCHING
    deadline = datetime.now(timezone.utc) + timedelta(seconds=2)
    first = adapter.request_cancel(receipt.handle, cancellation_id="cancel-1", deadline=deadline)
    replay = adapter.request_cancel(receipt.handle, cancellation_id="cancel-1", deadline=deadline)
    assert first == replay
    assert _wait_terminal(adapter, receipt.handle) is RuntimePhase.CANCELED
    assert (
        adapter.dispatch(assignment, dispatch_key=_key(assignment)).observation.phase
        is RuntimePhase.CANCELED
    )


def test_cancellation_rejects_invalid_expired_and_conflicting_intents(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    assignment = _assignment(adapter, fixture={"delay_ms": 5_000}, mode="managed_async")
    receipt = adapter.dispatch(assignment, dispatch_key=_key(assignment))
    with pytest.raises(ValueError, match="timezone"):
        adapter.request_cancel(receipt.handle, cancellation_id="bad", deadline=datetime.now())
    with pytest.raises(ValueError, match="expired"):
        adapter.request_cancel(
            receipt.handle,
            cancellation_id="expired",
            deadline=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
    deadline = datetime.now(timezone.utc) + timedelta(seconds=2)
    adapter.request_cancel(receipt.handle, cancellation_id="cancel-1", deadline=deadline)
    with pytest.raises(ValueError, match="different cancellation"):
        adapter.request_cancel(
            receipt.handle,
            cancellation_id="cancel-2",
            deadline=datetime.now(timezone.utc) + timedelta(seconds=2),
        )


def test_dispatch_identity_is_exact_and_never_starts_a_second_process(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    assignment = _assignment(adapter)
    key = _key(assignment)
    adapter.dispatch(assignment, dispatch_key=key)
    for invalid in (
        "dispatch-1",
        _key(assignment, "tenant-b"),
        f"runtime-dispatch:{assignment.tenant_id}:{uuid4()}",
    ):
        with pytest.raises(ValueError, match="dispatch key"):
            adapter.dispatch(assignment, dispatch_key=invalid)
    other = _assignment(adapter)
    with pytest.raises(ValueError, match="dispatch key"):
        adapter.dispatch(other, dispatch_key=key)


def test_concurrent_replay_runs_one_backend_process(tmp_path, monkeypatch) -> None:
    adapter = _adapter(tmp_path)
    assignment = _assignment(adapter, fixture={"delay_ms": 100}, mode="managed_async")
    calls = 0
    original = adapter._run

    def counted(state):
        nonlocal calls
        calls += 1
        return original(state)

    monkeypatch.setattr(adapter, "_run", counted)
    key = _key(assignment)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: adapter.dispatch(assignment, dispatch_key=key), range(2)))
    assert calls == 1
    assert results[0] == results[1]
    _wait_terminal(adapter, results[0].handle)


def test_descriptor_is_honest_and_unsupported_operations_fail_closed(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    descriptor = adapter.descriptor()
    assert descriptor.capabilities.execution_mode == ("inline", "managed_async")
    assert descriptor.capabilities.reattach is False
    assert descriptor.capabilities.event_stream is False
    assert descriptor.capabilities.tool_bridge == ()
    assert descriptor.extensions["isolation_note"] == "process boundary only; not an OS sandbox"
    assignment = _assignment(adapter)
    receipt = adapter.dispatch(assignment, dispatch_key=_key(assignment))
    with pytest.raises(ValueError, match="unsupported"):
        adapter.read_events(receipt.handle, cursor=None, limit=1)
    with pytest.raises(ValueError, match="unsupported"):
        adapter.request_pause(receipt.handle, operation_id="pause-1")


def test_timeout_crash_malformed_padding_and_oversize_are_terminal(tmp_path) -> None:
    adapter = _adapter(tmp_path, timeout_seconds=0.5, max_stderr_bytes=64)
    cases = (
        ({"delay_ms": 3_000}, RuntimePhase.TIMED_OUT, "runtime.timeout"),
        ({"crash": True}, RuntimePhase.FAILED, "runtime.process_exit"),
        ({"malformed": True}, RuntimePhase.FAILED, "runtime.protocol_error"),
        ({"stdout_bytes": 64}, RuntimePhase.FAILED, "runtime.protocol_error"),
        ({"stdout_bytes": 1_000_000}, RuntimePhase.FAILED, "runtime.stdout_limit"),
    )
    for fixture, phase, code in cases:
        assignment = _assignment(adapter, fixture=fixture)
        result = adapter.dispatch(assignment, dispatch_key=_key(assignment))
        assert result.observation.phase is phase
        assert result.observation.error.code == code


def test_launch_and_pre_spawn_payload_errors_leave_terminal_replayable_receipts(tmp_path) -> None:
    adapter = _adapter(tmp_path, command=[str(tmp_path / "does-not-exist")])
    assignment = _assignment(adapter)
    result = adapter.dispatch(assignment, dispatch_key=_key(assignment))
    assert result.observation.error.code == "runtime.process_launch"
    assert adapter.dispatch(assignment, dispatch_key=_key(assignment)) == result

    limited = _adapter(tmp_path)
    limited._descriptor = replace(
        limited.descriptor(), limits=RuntimeLimits(max_assignment_bytes=1)
    )
    oversized = _assignment(limited)
    result = limited.dispatch(oversized, dispatch_key=_key(oversized))
    assert result.observation.error.code == "runtime.assignment_limit"
    assert limited.dispatch(oversized, dispatch_key=_key(oversized)) == result


def test_stdout_is_terminated_incrementally_before_fixture_finishes(tmp_path) -> None:
    adapter = _adapter(tmp_path, max_stdout_bytes=128, timeout_seconds=5)
    assignment = _assignment(adapter, fixture={"stdout_bytes": 1_000_000})
    started = time.monotonic()
    result = adapter.dispatch(assignment, dispatch_key=_key(assignment))
    assert time.monotonic() - started < 3
    assert result.observation.error.code == "runtime.stdout_limit"


def test_stderr_is_redacted_as_bounded_utf8_bytes(tmp_path) -> None:
    adapter = _adapter(tmp_path, max_stderr_bytes=48)
    secret = "password=hunter2 API key:abc Authorization:Bearer-token " + "界" * 100
    assignment = _assignment(adapter, fixture={"stderr": secret, "crash": True})
    result = adapter.dispatch(assignment, dispatch_key=_key(assignment))
    rendered = result.observation.progress.get("stderr", "")
    assert "hunter2" not in rendered
    assert "Bearer-token" not in rendered
    assert len(rendered.encode("utf-8")) <= 48


@pytest.mark.parametrize("mutation", ("unknown", "identity", "nonterminal"))
def test_protocol_rejects_unknown_or_inconsistent_result_schema(tmp_path, mutation) -> None:
    mutation_code = {
        "unknown": "value['extra']=1;",
        "identity": "value['observation']['assignment_id']='00000000-0000-0000-0000-000000000001';",
        "nonterminal": (
            "value['observation']['phase']='RUNNING'; "
            "value['observation']['output']=None; "
            "value['observation']['output_artifact_refs']=[];"
        ),
    }[mutation]
    script = (
        "import json,subprocess,sys; "
        "req=sys.stdin.buffer.read(); "
        "p=subprocess.run([sys.executable,'-m','agentmesh_reference_agent'], "
        "input=req,capture_output=True); "
        "value=json.loads(p.stdout); " + mutation_code + " "
        "sys.stdout.write(json.dumps(value))"
    )
    adapter = _adapter(tmp_path, command=[sys.executable, "-c", script])
    assignment = _assignment(adapter)
    result = adapter.dispatch(assignment, dispatch_key=_key(assignment))
    assert result.observation.phase is RuntimePhase.FAILED
    assert result.observation.error.code == "runtime.protocol_error"


def test_workspace_and_environment_are_execution_scoped(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://secret")
    monkeypatch.setenv("PARENT_ONLY", "must-not-pass")
    adapter = _adapter(tmp_path)
    assignment = _assignment(
        adapter,
        fixture={
            "include_env": ["TEMP", "TMP", "HOME", "DATABASE_URL", "PARENT_ONLY", "DOCKER_HOST"]
        },
    )
    result = adapter.dispatch(assignment, dispatch_key=_key(assignment))
    environment = result.observation.output["environment"]
    workspace = Path(adapter._states[result.runtime_execution_id].workspace_root)
    assert environment["TEMP"] == environment["TMP"] == environment["HOME"]
    assert not workspace.exists()
    assert environment["DATABASE_URL"] is None
    assert environment["PARENT_ONLY"] is None
    assert environment["DOCKER_HOST"] is None


@pytest.mark.skipif(os.name == "nt", reason="process-group assertion is POSIX-specific")
def test_cancel_removes_process_group_child(tmp_path) -> None:
    adapter = _adapter(tmp_path, timeout_seconds=5)
    assignment = _assignment(
        adapter,
        fixture={"spawn_tree": True, "child_pid_file": "child.pid", "delay_ms": 10_000},
        mode="managed_async",
    )
    receipt = adapter.dispatch(assignment, dispatch_key=_key(assignment))
    state = adapter._states[receipt.runtime_execution_id]
    deadline = time.monotonic() + 2
    pid_file = None
    while time.monotonic() < deadline and state.workspace_root:
        candidate = Path(state.workspace_root) / "child.pid"
        if candidate.exists():
            pid_file = candidate
            break
        time.sleep(0.01)
    assert pid_file is not None
    child_pid = int(pid_file.read_text(encoding="ascii"))
    adapter.request_cancel(
        receipt.handle,
        cancellation_id="cancel-tree",
        deadline=datetime.now(timezone.utc) + timedelta(seconds=2),
    )
    assert _wait_terminal(adapter, receipt.handle) is RuntimePhase.CANCELED
    for _ in range(50):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    else:
        pytest.fail("process-group child survived cancellation")


def test_reference_package_builds_without_dependencies_and_runs_from_path(tmp_path) -> None:
    if importlib.util.find_spec("wheel") is None:
        pytest.skip("wheel is installed by the repository dev test profile")
    package = ROOT / "examples" / "reference-agent"
    wheel_dir = tmp_path / "wheel"
    wheel_dir.mkdir()
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--no-index",
            str(package),
            "-w",
            str(wheel_dir),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert list(wheel_dir.glob("agentmesh_reference_agent-*.whl"))
    adapter = _adapter(tmp_path)
    assignment = _assignment(adapter)
    payload = (
        canonical_json_bytes(
            {
                "schema": "agentmesh.reference-agent.v1",
                "operation": "execute",
                "assignment": assignment.to_dict(),
            }
        )
        + b"\n"
    )
    completed = subprocess.run(
        [sys.executable, "-m", "agentmesh_reference_agent"],
        input=payload,
        capture_output=True,
        env={**os.environ, **REFERENCE_ENV},
        cwd=tmp_path,
        check=True,
    )
    assert json.loads(completed.stdout)["type"] == "result"


def test_command_must_be_structured_argv_and_never_a_shell_string(tmp_path) -> None:
    with pytest.raises(ValueError, match="argv"):
        _adapter(tmp_path, command="python -m agentmesh_reference_agent")


def test_reference_agent_has_no_framework_or_control_plane_imports() -> None:
    for path in (ROOT / "examples" / "reference-agent" / "src").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "agentmesh.runtime_sdk" in source or path.name == "__init__.py"
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
