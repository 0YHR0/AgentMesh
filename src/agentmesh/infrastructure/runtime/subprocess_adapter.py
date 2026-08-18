"""A3 generic subprocess runtime adapter.

This adapter is intentionally small and conservative: one JSON-lines request,
one structured result, an isolated temporary workspace, and an explicit
environment allowlist.  It is not a general untrusted-code sandbox.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from threading import Event, RLock
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from agentmesh.runtime_sdk import (
    ArtifactRef,
    DispatchReceipt,
    ErrorCategory,
    LifecycleReceipt,
    ManagedAgentRuntime,
    RetryDisposition,
    RuntimeAssignment,
    RuntimeCapabilities,
    RuntimeDescriptor,
    RuntimeError,
    RuntimeEventPage,
    RuntimeExecutionHandle,
    RuntimeLimits,
    RuntimeObservation,
    RuntimePhase,
    ValidationReport,
    canonical_json_bytes,
)

REFERENCE_RUNTIME_KEY = "agentmesh.reference.subprocess"
_ALLOWED_ENV = frozenset(
    {
        "PATH",
        "PYTHONPATH",
        "PYTHONHOME",
        "LANG",
        "LC_ALL",
        "TZ",
        # Required by the platform process loader, copied one key at a time.
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
        "HOME",
    }
)
_SECRET_PATTERN = re.compile(
    r"(?i)(api[_-]?key|access[_-]?token|authorization|password|secret|credential)"
    r"(\s*[:=]\s*)[^\s,;]+"
)


def reference_subprocess_descriptor() -> RuntimeDescriptor:
    """Descriptor advertises only behavior implemented by this adapter."""

    return RuntimeDescriptor(
        runtime_key=REFERENCE_RUNTIME_KEY,
        display_name="AgentMesh Reference Subprocess",
        adapter_kind="python-subprocess-jsonl",
        capabilities=RuntimeCapabilities(
            execution_mode=("inline",),
            reattach=False,
            cancel="forced",
            pause_resume=False,
            checkpoint=False,
            fork=False,
            event_stream=False,
            tool_bridge=(),
            artifact_io=("reference",),
            isolation_profiles=("isolated",),
            modalities=("structured",),
        ),
        limits=RuntimeLimits(
            max_assignment_bytes=262_144,
            max_event_bytes=65_536,
            max_result_bytes=262_144,
            max_artifact_refs=128,
        ),
    )


@dataclass
class _ExecutionState:
    assignment: RuntimeAssignment
    handle: RuntimeExecutionHandle
    observation: RuntimeObservation
    receipt: DispatchReceipt | None = None
    process: subprocess.Popen[bytes] | None = None
    cancel_requested: Event = field(default_factory=Event)
    workspace: tempfile.TemporaryDirectory[str] | None = None
    complete: Event = field(default_factory=Event)


class SubprocessAgentRuntime(ManagedAgentRuntime):
    """Run a reference Agent with structured argv and bounded protocol IO."""

    def __init__(
        self,
        *,
        command: Sequence[str],
        environment: Mapping[str, str] | None = None,
        timeout_seconds: float = 30.0,
        max_stdout_bytes: int = 262_144,
        max_stderr_bytes: int = 8_192,
        artifact_staging_dir: str | os.PathLike[str] | None = None,
    ) -> None:
        if (
            isinstance(command, (str, bytes))
            or not command
            or any(type(item) is not str or not item for item in command)
        ):
            raise ValueError("subprocess command must be a non-empty sequence of strings")
        if timeout_seconds <= 0 or timeout_seconds > 300:
            raise ValueError("subprocess timeout is outside the allowed range")
        if not 1 <= max_stdout_bytes <= 262_144 or not 1 <= max_stderr_bytes <= 65_536:
            raise ValueError("subprocess output limit is outside the allowed range")
        supplied = dict(environment or {})
        if any(type(key) is not str or type(value) is not str for key, value in supplied.items()):
            raise ValueError("subprocess environment must contain strings")
        unknown = set(supplied) - _ALLOWED_ENV
        if unknown:
            raise ValueError("subprocess environment contains a non-allowlisted key")
        self._command = tuple(command)
        self._environment = supplied
        self._timeout_seconds = timeout_seconds
        self._max_stdout_bytes = max_stdout_bytes
        self._max_stderr_bytes = max_stderr_bytes
        self._descriptor = reference_subprocess_descriptor()
        self._lock = RLock()
        self._states: dict[str, _ExecutionState] = {}
        self._dispatches: dict[str, _ExecutionState] = {}
        self._owned_staging: tempfile.TemporaryDirectory[str] | None = None
        if artifact_staging_dir is None:
            self._owned_staging = tempfile.TemporaryDirectory(prefix="agentmesh-artifacts-")
            self._staging_dir = Path(self._owned_staging.name)
        else:
            self._staging_dir = Path(artifact_staging_dir)
        self._package_root = str(Path(__file__).resolve().parents[3])
        if self._staging_dir is not None:
            self._staging_dir.mkdir(parents=True, exist_ok=True)

    def descriptor(self) -> RuntimeDescriptor:
        return self._descriptor

    def validate(self, assignment: RuntimeAssignment) -> ValidationReport:
        if assignment.runtime_descriptor_digest != self._descriptor.digest():
            return ValidationReport(
                valid=False,
                errors=(
                    RuntimeError(
                        code="runtime.descriptor_mismatch",
                        category=ErrorCategory.CONFLICT,
                        message="Runtime descriptor does not match the pinned version",
                        retry_disposition=RetryDisposition.NEVER,
                    ),
                ),
            )
        if assignment.execution_mode != "inline":
            return ValidationReport(
                valid=False,
                errors=(
                    RuntimeError(
                        code="runtime.execution_mode_unsupported",
                        category=ErrorCategory.VALIDATION,
                        message="Reference subprocess supports inline execution only",
                        retry_disposition=RetryDisposition.NEVER,
                    ),
                ),
            )
        return ValidationReport(valid=True)

    def dispatch(self, assignment: RuntimeAssignment, *, dispatch_key: str) -> DispatchReceipt:
        report = self.validate(assignment)
        if not report.valid:
            raise ValueError("Runtime assignment validation failed")
        if not isinstance(dispatch_key, str) or not dispatch_key:
            raise ValueError("dispatch key is required")
        execution_id = self._execution_id(assignment, dispatch_key)
        with self._lock:
            existing = self._dispatches.get(dispatch_key)
            if existing is not None:
                if existing.assignment.assignment_digest != assignment.assignment_digest:
                    raise ValueError("Runtime dispatch key has a different assignment")
                if existing.receipt is not None:
                    return existing.receipt
                running = existing
                should_run = False
            else:
                now = datetime.now(timezone.utc)
                handle = RuntimeExecutionHandle(
                    runtime_execution_id=execution_id,
                    runtime_version_id=assignment.runtime_version_id,
                    provider_execution_ref="subprocess:"
                    + sha256(dispatch_key.encode()).hexdigest()[:32],
                    assignment_id=assignment.assignment_id,
                    assignment_digest=assignment.assignment_digest,
                    created_at=now,
                    provider_generation="reference-subprocess-v1",
                )
                pending = RuntimeObservation(
                    observation_id=str(uuid5(NAMESPACE_URL, execution_id + ":pending")),
                    runtime_execution_id=execution_id,
                    assignment_id=assignment.assignment_id,
                    assignment_digest=assignment.assignment_digest,
                    phase=RuntimePhase.DISPATCHING,
                    observed_at=now,
                    provider_event_id="dispatching",
                )
                running = _ExecutionState(
                    assignment=assignment,
                    handle=handle,
                    observation=pending,
                    cancel_requested=Event(),
                )
                self._states[execution_id] = running
                self._dispatches[dispatch_key] = running
                should_run = True
        if running.receipt is not None:
            return running.receipt
        if not should_run:
            running.complete.wait(self._timeout_seconds + 5)
            if running.receipt is None:
                raise ValueError("concurrent subprocess dispatch did not complete")
            return running.receipt
        return self._run(running, dispatch_key)

    def inspect(self, handle: RuntimeExecutionHandle) -> RuntimeObservation:
        with self._lock:
            state = self._states.get(handle.runtime_execution_id)
        if state is None or state.handle != handle:
            raise ValueError("Runtime execution handle is unknown")
        return state.observation

    def read_events(
        self, handle: RuntimeExecutionHandle, *, cursor: str | None, limit: int
    ) -> RuntimeEventPage:
        raise ValueError("Runtime event stream is unsupported")

    def request_cancel(
        self, handle: RuntimeExecutionHandle, *, cancellation_id: str, deadline: datetime
    ) -> LifecycleReceipt:
        with self._lock:
            state = self._states.get(handle.runtime_execution_id)
            if state is None or state.handle != handle:
                raise ValueError("Runtime execution handle is unknown")
            existing = state.observation.phase
            if existing.terminal:
                return LifecycleReceipt(
                    operation_id=cancellation_id,
                    runtime_execution_id=handle.runtime_execution_id,
                    operation="cancel",
                    accepted=False,
                    observed_phase=existing,
                    safe_message="execution is already terminal",
                )
            state.cancel_requested.set()
            process = state.process
        if process is not None:
            self._terminate_process(process)
        return LifecycleReceipt(
            operation_id=cancellation_id,
            runtime_execution_id=handle.runtime_execution_id,
            operation="cancel",
            accepted=True,
            observed_phase=RuntimePhase.CANCEL_REQUESTED,
            safe_message="process-group cancellation requested",
        )

    def request_pause(
        self, handle: RuntimeExecutionHandle, *, operation_id: str
    ) -> LifecycleReceipt:
        raise ValueError("Runtime pause is unsupported")

    def request_resume(
        self, handle: RuntimeExecutionHandle, *, operation_id: str
    ) -> LifecycleReceipt:
        raise ValueError("Runtime resume is unsupported")

    def close(self) -> None:
        with self._lock:
            states = tuple(self._states.values())
        for state in states:
            if state.process is not None and state.process.poll() is None:
                state.cancel_requested.set()
                self._terminate_process(state.process)
        # TemporaryDirectory cleanup is done by _run finally; this is only an
        # orphan safety net for a caller closing during a failed dispatch.
        for state in states:
            if state.workspace is not None:
                state.workspace.cleanup()
                state.workspace = None
        if self._owned_staging is not None:
            self._owned_staging.cleanup()
            self._owned_staging = None

    def _run(self, state: _ExecutionState, dispatch_key: str) -> DispatchReceipt:
        workspace = tempfile.TemporaryDirectory(prefix="agentmesh-runtime-")
        state.workspace = workspace
        try:
            request = {
                "schema": "agentmesh.reference-agent.v1",
                "operation": "execute",
                "assignment": state.assignment.to_dict(),
            }
            payload = canonical_json_bytes(request) + b"\n"
            if len(payload) > self._descriptor.limits.max_assignment_bytes:
                raise ValueError("subprocess assignment exceeds limit")
            # Only this explicit package path and caller-approved keys are
            # inherited; os.environ is never copied into the child.
            env = {
                key: value
                for key in _ALLOWED_ENV
                if key not in {"PYTHONPATH", "PYTHONHOME"}
                and (value := os.environ.get(key)) is not None
            }
            env["PYTHONPATH"] = self._environment.get("PYTHONPATH", self._package_root)
            env.update(self._environment)
            process_kwargs: dict[str, Any] = {
                "args": self._command,
                "stdin": subprocess.PIPE,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "cwd": workspace.name,
                "env": env,
                "shell": False,
            }
            if os.name == "nt":
                process_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                process_kwargs["start_new_session"] = True
            process = subprocess.Popen(**process_kwargs)
            with self._lock:
                state.process = process
                state.observation = replace(state.observation, phase=RuntimePhase.RUNNING)
                if state.cancel_requested.is_set():
                    self._terminate_process(process)
            try:
                stdout, stderr = process.communicate(input=payload, timeout=self._timeout_seconds)
            except subprocess.TimeoutExpired:
                self._terminate_process(process)
                stdout, stderr = process.communicate()
                observation = self._failure_observation(
                    state, RuntimePhase.TIMED_OUT, "runtime.timeout", stderr
                )
            else:
                if state.cancel_requested.is_set():
                    observation = self._failure_observation(
                        state, RuntimePhase.CANCELED, "runtime.canceled", stderr
                    )
                elif len(stdout) > self._max_stdout_bytes:
                    observation = self._failure_observation(
                        state, RuntimePhase.FAILED, "runtime.stdout_limit", stderr
                    )
                elif len(stderr) > self._max_stderr_bytes:
                    observation = self._failure_observation(
                        state, RuntimePhase.FAILED, "runtime.stderr_limit", stderr
                    )
                elif process.returncode != 0:
                    observation = self._failure_observation(
                        state, RuntimePhase.FAILED, "runtime.process_exit", stderr
                    )
                else:
                    observation = self._decode_result(state, stdout)
            receipt = DispatchReceipt(
                dispatch_key=dispatch_key,
                runtime_execution_id=state.handle.runtime_execution_id,
                assignment_digest=state.assignment.assignment_digest,
                handle=state.handle,
                observation=observation,
            )
            with self._lock:
                state.observation = observation
                state.receipt = receipt
            return receipt
        finally:
            with self._lock:
                state.process = None
            workspace.cleanup()
            state.workspace = None
            state.complete.set()

    def _decode_result(self, state: _ExecutionState, stdout: bytes) -> RuntimeObservation:
        try:
            if len(stdout) > self._descriptor.limits.max_result_bytes:
                raise ValueError("result exceeds limit")
            lines = stdout.splitlines()
            if len(lines) != 1:
                raise ValueError("reference protocol requires one result line")
            response = json.loads(lines[0].decode("utf-8"))
            if type(response) is not dict or response.get("type") != "result":
                raise ValueError("malformed reference result")
            observation = RuntimeObservation.from_dict(response.get("observation"))
            if (
                observation.runtime_execution_id != state.handle.runtime_execution_id
                or observation.assignment_digest != state.assignment.assignment_digest
            ):
                raise ValueError("reference result identity mismatch")
            artifact = response.get("artifact")
            if type(artifact) is not dict:
                raise ValueError("reference result artifact is missing")
            content = artifact.get("content")
            if type(content) is not str:
                raise ValueError("reference artifact content is invalid")
            content_bytes = content.encode("utf-8")
            if len(content_bytes) > self._descriptor.limits.max_result_bytes:
                raise ValueError("reference artifact exceeds result limit")
            artifact_ref = self._stage_artifact(state, content_bytes, artifact.get("media_type"))
            return replace(observation, output_artifact_refs=(artifact_ref,))
        except Exception as exc:
            return self._failure_observation(
                state, RuntimePhase.FAILED, "runtime.protocol_error", str(exc).encode()
            )

    def _stage_artifact(
        self, state: _ExecutionState, content: bytes, media_type: Any
    ) -> ArtifactRef:
        digest = sha256(content).hexdigest()
        artifact_id = str(uuid5(NAMESPACE_URL, state.handle.runtime_execution_id + ":artifact"))
        version_id = str(uuid5(NAMESPACE_URL, state.handle.runtime_execution_id + ":artifact:v1"))
        if self._staging_dir is not None:
            path = self._staging_dir / f"{state.handle.runtime_execution_id}.json"
            path.write_bytes(content)
        return ArtifactRef(
            artifact_id=artifact_id,
            version_id=version_id,
            digest=digest,
            size_bytes=len(content),
            media_type=media_type if isinstance(media_type, str) else "application/json",
        )

    def _failure_observation(
        self, state: _ExecutionState, phase: RuntimePhase, code: str, stderr: bytes | str
    ) -> RuntimeObservation:
        safe = _redact_bounded(stderr, self._max_stderr_bytes)
        return RuntimeObservation(
            observation_id=str(
                uuid5(NAMESPACE_URL, state.handle.runtime_execution_id + ":" + phase.value)
            ),
            runtime_execution_id=state.handle.runtime_execution_id,
            assignment_id=state.assignment.assignment_id,
            assignment_digest=state.assignment.assignment_digest,
            phase=phase,
            observed_at=datetime.now(timezone.utc),
            provider_event_id=code,
            progress={"stderr": safe} if safe else {},
            error=RuntimeError(
                code=code,
                category=ErrorCategory.TRANSIENT
                if phase is RuntimePhase.TIMED_OUT
                else ErrorCategory.PERMANENT,
                message="reference subprocess did not produce a successful result",
                retry_disposition=RetryDisposition.NEW_EXECUTION
                if phase is RuntimePhase.TIMED_OUT
                else RetryDisposition.NEVER,
            ),
        )

    @staticmethod
    def _execution_id(assignment: RuntimeAssignment, dispatch_key: str) -> str:
        value = assignment.correlation_ids.get("runtime_execution_id")
        if not isinstance(value, str):
            raise ValueError("Runtime assignment execution identity is unavailable")
        try:
            UUID(value)
        except ValueError as exc:
            raise ValueError("Runtime assignment execution identity is invalid") from exc
        return value

    @staticmethod
    def _terminate_process(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ValueError):
            process.kill()


def _redact_bounded(value: bytes | str, limit: int) -> str:
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
    text = _SECRET_PATTERN.sub(r"\1=<redacted>", text)
    return text[:limit]
