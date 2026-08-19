"""Black-box conformance tests for framework-neutral managed runtimes.

The assertions intentionally use only the public Runtime SDK port and DTOs.
Adapter-specific construction is confined to the factories below; the suite
does not inspect provider state, traces, nodes, or private implementation
details.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from agentmesh.infrastructure.runtime.langgraph_adapter import (
    EphemeralRuntimeLifecycleController,
    EphemeralRuntimeStateStore,
    LangGraphManagedAgentRuntime,
)
from agentmesh.infrastructure.runtime.subprocess_adapter import SubprocessAgentRuntime
from agentmesh.runtime_sdk import (
    ErrorCategory,
    ManagedAgentRuntime,
    RetryDisposition,
    RuntimeAssignment,
    RuntimeError,
    RuntimeObservation,
    RuntimePhase,
)

ROOT = Path(__file__).parents[3]


class _DeterministicBackend:
    def bind(self, assignment, task, run, attempt, work_item):
        return None

    def execute(self, assignment: RuntimeAssignment) -> RuntimeObservation:
        execution_id = assignment.correlation_ids["runtime_execution_id"]
        if assignment.structured_input.get("_conformance_failure"):
            return RuntimeObservation(
                observation_id=str(uuid4()),
                runtime_execution_id=execution_id,
                assignment_id=assignment.assignment_id,
                assignment_digest=assignment.assignment_digest,
                phase=RuntimePhase.FAILED,
                observed_at=datetime.now(timezone.utc),
                provider_event_id="conformance.failure",
                error=RuntimeError(
                    code="runtime.provider_failure",
                    category=ErrorCategory.PERMANENT,
                    message="deterministic conformance failure",
                    retry_disposition=RetryDisposition.NEVER,
                ),
            )
        return RuntimeObservation(
            observation_id=str(uuid4()),
            runtime_execution_id=execution_id,
            assignment_id=assignment.assignment_id,
            assignment_digest=assignment.assignment_digest,
            phase=RuntimePhase.SUCCEEDED,
            observed_at=datetime.now(timezone.utc),
            provider_event_id="conformance.success",
            output={"kind": "conformance.report.v1", "ok": True},
        )


@dataclass(frozen=True)
class _RuntimeCase:
    name: str
    factory: Callable[[], ManagedAgentRuntime]
    failure_mode: str


@pytest.fixture(params=("langgraph", "subprocess"), ids=("langgraph", "subprocess"))
def runtime_case(request: pytest.FixtureRequest, tmp_path: Path) -> _RuntimeCase:
    if request.param == "langgraph":
        return _RuntimeCase(
            name="langgraph",
            factory=lambda: LangGraphManagedAgentRuntime(
                backend=_DeterministicBackend(),
                state_store=EphemeralRuntimeStateStore(),
                lifecycle_controller=EphemeralRuntimeLifecycleController(),
            ),
            failure_mode="provider",
        )
    return _RuntimeCase(
        name="subprocess",
        factory=lambda: SubprocessAgentRuntime(
            command=[sys.executable, "-m", "agentmesh.reference_agent"],
            environment={"PYTHONPATH": str(ROOT / "src")},
            timeout_seconds=1.0,
            artifact_staging_dir=tmp_path / "artifacts",
        ),
        failure_mode="timeout",
    )


@contextmanager
def _opened(case: _RuntimeCase) -> Iterator[ManagedAgentRuntime]:
    adapter = case.factory()
    try:
        yield adapter
    finally:
        adapter.close()


def _assignment(
    adapter: ManagedAgentRuntime,
    *,
    mode: str | None = None,
    execution_id: str | None = None,
    required_capabilities: dict | None = None,
    failure: bool = False,
    delay_ms: int = 0,
) -> RuntimeAssignment:
    descriptor = adapter.descriptor()
    selected_mode = mode or (
        "inline" if "inline" in descriptor.capabilities.execution_mode else "managed_async"
    )
    fixture = {"delay_ms": delay_ms} if delay_ms else {}
    return RuntimeAssignment(
        assignment_id=str(uuid4()),
        tenant_id="tenant-conformance",
        task_id=str(uuid4()),
        run_id=str(uuid4()),
        agent_definition_id=str(uuid4()),
        agent_version_id=str(uuid4()),
        agent_version_digest="a" * 64,
        runtime_version_id=str(uuid4()),
        runtime_descriptor_digest=descriptor.digest(),
        execution_mode=selected_mode,
        run_role="EXECUTOR",
        revision=0,
        objective="black-box conformance report",
        structured_input={
            "value": "stable",
            "_reference_agent": fixture,
            **({"_conformance_failure": True} if failure else {}),
        },
        required_capabilities=required_capabilities or {},
        correlation_ids={"runtime_execution_id": execution_id or str(uuid4())},
    )


def _dispatch_key(assignment: RuntimeAssignment) -> str:
    return (
        f"runtime-dispatch:{assignment.tenant_id}:"
        f"{assignment.correlation_ids['runtime_execution_id']}"
    )


def _terminal(adapter: ManagedAgentRuntime, handle, timeout: float = 3.0) -> RuntimeObservation:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        observation = adapter.inspect(handle)
        if observation.phase.terminal:
            return observation
        time.sleep(0.01)
    raise AssertionError("managed runtime did not reach a terminal phase")


def test_descriptor_and_digest_are_stable(runtime_case: _RuntimeCase) -> None:
    with _opened(runtime_case) as adapter:
        first = adapter.descriptor()
        second = adapter.descriptor()
        assert first.to_dict() == second.to_dict()
        assert first.digest() == second.digest()
        assert first.digest() == adapter.descriptor().digest()


def test_incompatible_required_capability_is_rejected_before_dispatch(
    runtime_case: _RuntimeCase,
) -> None:
    with _opened(runtime_case) as adapter:
        capabilities = adapter.descriptor().capabilities
        missing = next(
            (
                name
                for name in ("reattach", "pause_resume", "checkpoint", "fork", "event_stream")
                if not getattr(capabilities, name)
            ),
            None,
        )
        if missing is None:
            pytest.skip("descriptor advertises every boolean capability")
        assignment = _assignment(adapter, required_capabilities={missing: True})
        report = adapter.validate(assignment)
        assert report.valid is False
        assert any(error.code == "runtime.capability_mismatch" for error in report.errors)
        with pytest.raises(ValueError, match="validation"):
            adapter.dispatch(assignment, dispatch_key=_dispatch_key(assignment))


def test_same_key_same_assignment_is_idempotent_and_single_observation(
    runtime_case: _RuntimeCase,
) -> None:
    with _opened(runtime_case) as adapter:
        assignment = _assignment(adapter)
        first = adapter.dispatch(assignment, dispatch_key=_dispatch_key(assignment))
        replay = adapter.dispatch(assignment, dispatch_key=_dispatch_key(assignment))
        assert first.to_dict() == replay.to_dict()
        assert first.handle is not None
        terminal = _terminal(adapter, first.handle)
        assert terminal.runtime_execution_id == first.runtime_execution_id
        assert terminal.assignment_id == assignment.assignment_id


def test_same_key_different_assignment_fails_closed(runtime_case: _RuntimeCase) -> None:
    with _opened(runtime_case) as adapter:
        assignment = _assignment(adapter)
        key = _dispatch_key(assignment)
        adapter.dispatch(assignment, dispatch_key=key)
        conflicting = replace(
            assignment,
            assignment_id=str(uuid4()),
            structured_input={"value": "different"},
            assignment_digest=None,
        )
        with pytest.raises(ValueError, match="different assignment"):
            adapter.dispatch(conflicting, dispatch_key=key)


def test_success_observation_has_identity_artifact_and_lineage(
    runtime_case: _RuntimeCase,
) -> None:
    with _opened(runtime_case) as adapter:
        assignment = _assignment(adapter)
        receipt = adapter.dispatch(assignment, dispatch_key=_dispatch_key(assignment))
        assert receipt.handle is not None
        observation = _terminal(adapter, receipt.handle)
        assert observation.phase is RuntimePhase.SUCCEEDED
        assert (
            observation.runtime_execution_id
            == assignment.correlation_ids["runtime_execution_id"]
        )
        assert observation.assignment_id == assignment.assignment_id
        assert observation.assignment_digest == assignment.assignment_digest
        for artifact in observation.output_artifact_refs:
            assert len(artifact.digest) == 64
            assert artifact.media_type
            assert artifact.size_bytes is not None
            assert artifact.size_bytes >= 0


def test_error_or_timeout_is_terminal_and_classified(runtime_case: _RuntimeCase) -> None:
    with _opened(runtime_case) as adapter:
        if runtime_case.failure_mode == "timeout":
            assignment = _assignment(adapter, delay_ms=3_000)
        else:
            assignment = _assignment(adapter, failure=True)
        receipt = adapter.dispatch(assignment, dispatch_key=_dispatch_key(assignment))
        assert receipt.handle is not None
        observation = _terminal(adapter, receipt.handle)
        assert observation.phase.terminal
        assert observation.error is not None
        if runtime_case.failure_mode == "timeout":
            assert observation.phase is RuntimePhase.TIMED_OUT
            assert observation.error.code == "runtime.timeout"
        else:
            assert observation.phase is RuntimePhase.FAILED
            assert observation.error.category is ErrorCategory.PERMANENT


def test_cancel_is_idempotent_when_descriptor_advertises_cancel(
    runtime_case: _RuntimeCase,
) -> None:
    with _opened(runtime_case) as adapter:
        if adapter.descriptor().capabilities.cancel == "none":
            pytest.skip("descriptor does not advertise cancellation")
        mode = (
            "managed_async"
            if "managed_async" in adapter.descriptor().capabilities.execution_mode
            else "inline"
        )
        assignment = _assignment(adapter, mode=mode, delay_ms=10_000)
        receipt = adapter.dispatch(assignment, dispatch_key=_dispatch_key(assignment))
        assert receipt.handle is not None
        deadline = datetime.now(timezone.utc) + timedelta(seconds=2)
        first = adapter.request_cancel(
            receipt.handle, cancellation_id="conformance-cancel", deadline=deadline
        )
        replay = adapter.request_cancel(
            receipt.handle, cancellation_id="conformance-cancel", deadline=deadline
        )
        assert first.to_dict() == replay.to_dict()
        observation = _terminal(adapter, receipt.handle)
        assert observation.phase is RuntimePhase.CANCELED


def test_unsupported_operations_fail_closed_from_descriptor(
    runtime_case: _RuntimeCase,
) -> None:
    with _opened(runtime_case) as adapter:
        assignment = _assignment(adapter)
        receipt = adapter.dispatch(assignment, dispatch_key=_dispatch_key(assignment))
        assert receipt.handle is not None
        capabilities = adapter.descriptor().capabilities
        if not capabilities.event_stream:
            with pytest.raises(ValueError, match="unsupported"):
                adapter.read_events(receipt.handle, cursor=None, limit=1)
        if not capabilities.pause_resume:
            with pytest.raises(ValueError, match="unsupported"):
                adapter.request_pause(receipt.handle, operation_id="pause-conformance")
            with pytest.raises(ValueError, match="unsupported"):
                adapter.request_resume(receipt.handle, operation_id="resume-conformance")


def test_close_rejects_new_dispatch_but_is_explicitly_cleaned_up(
    runtime_case: _RuntimeCase,
) -> None:
    adapter = runtime_case.factory()
    try:
        assignment = _assignment(adapter)
        receipt = adapter.dispatch(assignment, dispatch_key=_dispatch_key(assignment))
        assert receipt.handle is not None
        _terminal(adapter, receipt.handle)
        adapter.close()
        new_assignment = _assignment(adapter)
        with pytest.raises(ValueError, match="closed"):
            adapter.dispatch(new_assignment, dispatch_key=_dispatch_key(new_assignment))
    finally:
        adapter.close()


def test_reattach_false_does_not_claim_restart_recovery(runtime_case: _RuntimeCase) -> None:
    first = runtime_case.factory()
    assignment = _assignment(first)
    receipt = first.dispatch(assignment, dispatch_key=_dispatch_key(assignment))
    assert receipt.handle is not None
    _terminal(first, receipt.handle)
    assert first.descriptor().capabilities.reattach is False
    first.close()
    restarted = runtime_case.factory()
    try:
        with pytest.raises(ValueError, match="unknown"):
            restarted.inspect(receipt.handle)
    finally:
        restarted.close()
