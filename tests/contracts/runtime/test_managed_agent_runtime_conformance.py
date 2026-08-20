"""Black-box conformance tests for framework-neutral managed runtimes.

The assertions intentionally use only the public Runtime SDK port and DTOs.
Adapter-specific construction is confined to the factories below; the suite
does not inspect provider state, traces, nodes, or private implementation
details.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from threading import Barrier, Lock
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from agentmesh.infrastructure.runtime.langgraph_adapter import (
    EphemeralRuntimeLifecycleController,
    EphemeralRuntimeStateStore,
    LangGraphManagedAgentRuntime,
)
from agentmesh.infrastructure.runtime.subprocess_adapter import SubprocessAgentRuntime
from agentmesh.runtime_sdk import (
    ArtifactRef,
    ErrorCategory,
    ManagedAgentRuntime,
    RetryDisposition,
    RuntimeAssignment,
    RuntimeError,
    RuntimeObservation,
    RuntimePhase,
    canonical_json_bytes,
)

ROOT = Path(__file__).parents[3]


@dataclass
class _EffectLedger:
    path: Path
    _lock: Lock = field(default_factory=Lock)

    def record(self, assignment_digest: str) -> None:
        with self._lock:
            with self.path.open("a", encoding="ascii") as marker:
                marker.write(assignment_digest + "\n")

    def count(self, assignment_digest: str | None = None) -> int:
        if not self.path.exists():
            return 0
        values = self.path.read_text(encoding="ascii").splitlines()
        return sum(assignment_digest is None or value == assignment_digest for value in values)


class _DeterministicBackend:
    def __init__(self, ledger: _EffectLedger, barrier: Barrier | None = None) -> None:
        self._ledger = ledger
        self._barrier = barrier

    def bind(self, assignment, task, run, attempt, work_item):
        return None

    def execute(self, assignment: RuntimeAssignment) -> RuntimeObservation:
        self._ledger.record(assignment.assignment_digest)
        if self._barrier is not None:
            self._barrier.wait(timeout=2)
        execution_id = assignment.correlation_ids["runtime_execution_id"]
        case = assignment.structured_input.get("_conformance_case")
        if case == "exception":
            raise ValueError("controlled provider exception")
        if case == "malformed":
            return object()
        report = {"kind": "conformance.report.v1", "ok": True}
        report_bytes = canonical_json_bytes(report)
        artifact = ArtifactRef(
            artifact_id=str(uuid5(NAMESPACE_URL, assignment.assignment_id + ":artifact")),
            version_id=str(uuid5(NAMESPACE_URL, assignment.assignment_id + ":artifact:v1")),
            digest=sha256(report_bytes).hexdigest(),
            size_bytes=len(report_bytes),
            media_type="application/json",
        )
        if assignment.structured_input.get("_conformance_failure"):
            observation = RuntimeObservation(
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
        else:
            observation = RuntimeObservation(
                observation_id=str(uuid4()),
                runtime_execution_id=execution_id,
                assignment_id=assignment.assignment_id,
                assignment_digest=assignment.assignment_digest,
                phase=RuntimePhase.SUCCEEDED,
                observed_at=datetime.now(timezone.utc),
                provider_event_id="conformance.success",
                output=report,
                output_artifact_refs=(artifact,),
            )
        if case == "identity":
            object.__setattr__(observation, "assignment_id", str(uuid4()))
        elif case == "nonterminal":
            object.__setattr__(observation, "phase", RuntimePhase.RUNNING)
        elif case == "oversized":
            object.__setattr__(observation, "output", "x" * 300_000)
        return observation


@dataclass(frozen=True)
class _RuntimeCase:
    name: str
    factory: Callable[[], ManagedAgentRuntime]
    failure_mode: str
    ledger: _EffectLedger


@pytest.fixture(params=("langgraph", "subprocess"), ids=("langgraph", "subprocess"))
def runtime_case(request: pytest.FixtureRequest, tmp_path: Path) -> _RuntimeCase:
    ledger = _EffectLedger(tmp_path / "provider-effects.log")
    if request.param == "langgraph":
        return _RuntimeCase(
            name="langgraph",
            factory=lambda: LangGraphManagedAgentRuntime(
                backend=_DeterministicBackend(ledger),
                state_store=EphemeralRuntimeStateStore(),
                lifecycle_controller=EphemeralRuntimeLifecycleController(),
            ),
            failure_mode="provider",
            ledger=ledger,
        )
    wrapper = (
        "import json,subprocess,sys,tempfile\n"
        "request=sys.stdin.buffer.read(262145)\n"
        "if len(request)>262144: raise SystemExit(2)\n"
        "assignment=json.loads(request)['assignment']\n"
        "marker=open(sys.argv[1],'a',encoding='ascii')\n"
        "marker.write(assignment['assignment_digest']+'\\n')\n"
        "marker.flush()\n"
        "marker.close()\n"
        "with tempfile.TemporaryFile(mode='w+b') as replay:\n"
        "    replay.write(request)\n"
        "    replay.flush()\n"
        "    replay.seek(0)\n"
        "    child=subprocess.Popen([sys.executable,'-m','agentmesh_reference_agent'],"
        "stdin=replay)\n"
        "    raise SystemExit(child.wait())"
    )
    return _RuntimeCase(
        name="subprocess",
        factory=lambda: SubprocessAgentRuntime(
            command=[sys.executable, "-c", wrapper, str(ledger.path)],
            environment={
                "PYTHONPATH": os.pathsep.join(
                    (str(ROOT / "examples" / "reference-agent" / "src"), str(ROOT / "src"))
                )
            },
            timeout_seconds=1.0,
            max_stdout_bytes=512_000,
            artifact_staging_dir=tmp_path / "artifacts",
        ),
        failure_mode="timeout",
        ledger=ledger,
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
    provider_case: str | None = None,
    fixture_overrides: dict | None = None,
    input_padding_bytes: int = 0,
) -> RuntimeAssignment:
    descriptor = adapter.descriptor()
    selected_mode = mode or (
        "inline" if "inline" in descriptor.capabilities.execution_mode else "managed_async"
    )
    fixture = {"delay_ms": delay_ms} if delay_ms else {}
    if provider_case in {"malformed", "identity", "nonterminal", "oversized"}:
        fixture[{
            "identity": "identity_mismatch",
            "nonterminal": "nonterminal",
            "oversized": "oversized_result",
            "malformed": "malformed",
        }[provider_case]] = True
    fixture.update(fixture_overrides or {})
    structured_input = {
        "value": "stable",
        "_reference_agent": fixture,
    }
    if input_padding_bytes:
        structured_input["padding"] = "x" * input_padding_bytes
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
        structured_input=structured_input
        | ({"_conformance_failure": True} if failure else {})
        | ({"_conformance_case": provider_case} if provider_case else {}),
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
        assert runtime_case.ledger.count() == 0


def test_capability_matrix_rejects_mode_array_and_cancel_mismatches(
    runtime_case: _RuntimeCase,
) -> None:
    with _opened(runtime_case) as adapter:
        capabilities = adapter.descriptor().capabilities
        requirements: list[dict] = []
        unsupported_mode = next(
            mode for mode in ("inline", "managed_async") if mode not in capabilities.execution_mode
        ) if len(capabilities.execution_mode) < 2 else None
        if unsupported_mode:
            requirements.append({"execution_mode": [unsupported_mode]})
        array_name = next(
            (name for name in ("tool_bridge", "artifact_io", "isolation_profiles", "modalities")
             if not getattr(capabilities, name)),
            None,
        )
        if array_name:
            requirements.append({array_name: [
                {"tool_bridge": "governed_action_v1", "artifact_io": "reference",
                 "isolation_profiles": "remote", "modalities": "audio"}[array_name]
            ]})
        requirements.append({"cancel": "forced" if capabilities.cancel != "forced" else "none"})
        for required in requirements:
            assignment = _assignment(adapter, required_capabilities=required)
            assert adapter.validate(assignment).valid is False
            with pytest.raises(ValueError, match="validation"):
                adapter.dispatch(assignment, dispatch_key=_dispatch_key(assignment))
        assert runtime_case.ledger.count() == 0


def test_undeclared_execution_mode_is_rejected_without_required_capabilities(
    runtime_case: _RuntimeCase,
) -> None:
    with _opened(runtime_case) as adapter:
        if "managed_async" in adapter.descriptor().capabilities.execution_mode:
            pytest.skip("descriptor advertises both execution modes")
        assignment = _assignment(adapter, mode="managed_async")
        report = adapter.validate(assignment)
        assert report.valid is False
        assert any(error.code == "runtime.execution_mode_unsupported" for error in report.errors)
        with pytest.raises(ValueError, match="managed_async|validation"):
            adapter.dispatch(assignment, dispatch_key=_dispatch_key(assignment))
        assert runtime_case.ledger.count() == 0


def test_unknown_required_capability_fails_closed_directly(runtime_case: _RuntimeCase) -> None:
    with _opened(runtime_case) as adapter:
        assert adapter.descriptor().supports_required_capabilities({"unknown": True}) is False


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
        assert runtime_case.ledger.count(assignment.assignment_digest) == 1


def test_concurrent_same_key_replay_has_one_external_effect(
    runtime_case: _RuntimeCase,
) -> None:
    with _opened(runtime_case) as adapter:
        assignment = _assignment(adapter)
        key = _dispatch_key(assignment)
        with ThreadPoolExecutor(max_workers=4) as pool:
            receipts = list(
                pool.map(lambda _: adapter.dispatch(assignment, dispatch_key=key), range(4))
            )
        assert all(
            (receipt.runtime_execution_id, receipt.assignment_digest)
            == (receipts[0].runtime_execution_id, receipts[0].assignment_digest)
            for receipt in receipts
        )
        assert receipts[0].handle is not None
        _terminal(adapter, receipts[0].handle)
        assert runtime_case.ledger.count(assignment.assignment_digest) == 1


def test_different_langgraph_keys_execute_concurrently_without_global_admission_lock(
    tmp_path: Path,
) -> None:
    barrier = Barrier(2)
    ledger = _EffectLedger(tmp_path / "provider-effects.log")
    adapter = LangGraphManagedAgentRuntime(
        backend=_DeterministicBackend(ledger, barrier),
        state_store=EphemeralRuntimeStateStore(),
        lifecycle_controller=EphemeralRuntimeLifecycleController(),
    )
    try:
        assignments = (_assignment(adapter), _assignment(adapter))
        with ThreadPoolExecutor(max_workers=2) as pool:
            receipts = list(
                pool.map(
                    lambda assignment: adapter.dispatch(
                        assignment, dispatch_key=_dispatch_key(assignment)
                    ),
                    assignments,
                )
            )
        assert all(receipt.observation.phase is RuntimePhase.SUCCEEDED for receipt in receipts)
        assert ledger.count() == 2
    finally:
        adapter.close()


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
        assert len(observation.output_artifact_refs) == 1
        artifact = observation.output_artifact_refs[0]
        expected = canonical_json_bytes(observation.output)
        assert artifact.digest == sha256(expected).hexdigest()
        assert artifact.size_bytes == len(expected)
        assert artifact.media_type == "application/json"
        assert artifact.artifact_id
        assert artifact.version_id


@pytest.mark.parametrize("provider_case", ("malformed", "identity", "nonterminal", "oversized"))
def test_malformed_provider_results_fail_closed_with_stable_protocol_evidence(
    runtime_case: _RuntimeCase, provider_case: str
) -> None:
    with _opened(runtime_case) as adapter:
        assignment = _assignment(adapter, provider_case=provider_case)
        receipt = adapter.dispatch(assignment, dispatch_key=_dispatch_key(assignment))
        assert receipt.handle is not None
        observation = _terminal(adapter, receipt.handle)
        assert observation.phase is RuntimePhase.FAILED
        assert observation.error is not None
        assert observation.error.code == "runtime.protocol_error"
        assert observation.output is None
        assert observation.output_artifact_refs == ()


def test_subprocess_transport_headroom_still_enforces_result_bound(
    runtime_case: _RuntimeCase,
) -> None:
    if runtime_case.name != "subprocess":
        pytest.skip("transport envelope is specific to the subprocess adapter")
    with _opened(runtime_case) as adapter:
        result_assignment = _assignment(adapter, provider_case="oversized")
        result_receipt = adapter.dispatch(
            result_assignment, dispatch_key=_dispatch_key(result_assignment)
        )
        result_observation = _terminal(adapter, result_receipt.handle)
        assert adapter.descriptor().limits.max_result_bytes == 262_144
        assert result_observation.phase is RuntimePhase.FAILED
        assert result_observation.error is not None
        assert result_observation.error.code == "runtime.protocol_error"

        transport_assignment = _assignment(
            adapter, fixture_overrides={"stdout_bytes": 524_289}
        )
        transport_receipt = adapter.dispatch(
            transport_assignment, dispatch_key=_dispatch_key(transport_assignment)
        )
        transport_observation = _terminal(adapter, transport_receipt.handle)
        assert transport_observation.phase is RuntimePhase.FAILED
        assert transport_observation.error is not None
        assert transport_observation.error.code == "runtime.stdout_limit"


def test_subprocess_near_assignment_limit_replays_without_pipe_deadlock(
    runtime_case: _RuntimeCase,
) -> None:
    if runtime_case.name != "subprocess":
        pytest.skip("request replay transport is specific to the subprocess adapter")
    with _opened(runtime_case) as adapter:
        assignment = _assignment(adapter, input_padding_bytes=220_000)
        receipt = adapter.dispatch(assignment, dispatch_key=_dispatch_key(assignment))
        observation = _terminal(adapter, receipt.handle, timeout=5.0)
        assert observation.phase is RuntimePhase.SUCCEEDED
        assert runtime_case.ledger.count(assignment.assignment_digest) == 1


def test_backend_exception_is_not_misclassified_as_protocol_corruption(
    runtime_case: _RuntimeCase,
) -> None:
    if runtime_case.failure_mode != "provider":
        pytest.skip("subprocess failures use its process/JSONL error taxonomy")
    with _opened(runtime_case) as adapter:
        assignment = _assignment(adapter, provider_case="exception")
        receipt = adapter.dispatch(assignment, dispatch_key=_dispatch_key(assignment))
        observation = _terminal(adapter, receipt.handle)
        assert observation.phase is RuntimePhase.OUTCOME_UNKNOWN
        assert observation.error is not None
        assert observation.error.code == "runtime.provider_outcome_unknown"
        assert observation.error.category is ErrorCategory.UNKNOWN
        assert observation.error.retry_disposition is RetryDisposition.RECONCILE
        assert observation.error.retry_disposition not in (
            RetryDisposition.NEW_EXECUTION,
            RetryDisposition.SAME_EXECUTION,
        )


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
