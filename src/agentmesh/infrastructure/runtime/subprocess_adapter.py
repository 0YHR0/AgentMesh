"""A3 generic subprocess runtime adapter.

The adapter is a process boundary, not an OS sandbox for hostile code.  It
owns a small supervisor record for each dispatch, uses a JSON-lines protocol,
and deliberately advertises only the lifecycle operations implemented here.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
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
_MAX_ARG_COUNT = 64
_MAX_ARG_BYTES = 32_768
_KILL_WAIT_SECONDS = 0.75
_ALLOWED_ENV = frozenset(
    {
        "PATH",
        "PYTHONPATH",
        "PYTHONHOME",
        "LANG",
        "LC_ALL",
        "TZ",
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
        "HOME",
    }
)
_SECRET_PATTERN = re.compile(
    r"(?i)(api[\s_-]?key|access[\s_-]?token|authorization|password|secret|credential)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)


def reference_subprocess_descriptor() -> RuntimeDescriptor:
    """Return the descriptor for the implemented subprocess contract."""

    return RuntimeDescriptor(
        runtime_key=REFERENCE_RUNTIME_KEY,
        display_name="AgentMesh Reference Subprocess",
        adapter_kind="python-subprocess-jsonl",
        capabilities=RuntimeCapabilities(
            execution_mode=("inline", "managed_async"),
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
        extensions={
            "isolation_note": "process boundary only; not an OS sandbox",
            "protocol": "agentmesh.reference-agent.v1",
        },
    )


@dataclass
class _ExecutionState:
    assignment: RuntimeAssignment
    dispatch_key: str
    handle: RuntimeExecutionHandle
    observation: RuntimeObservation
    receipt: DispatchReceipt
    process: subprocess.Popen[bytes] | None = None
    worker_thread: threading.Thread | None = None
    cancel_requested: Event = field(default_factory=Event)
    complete: Event = field(default_factory=Event)
    lifecycle: dict[str, LifecycleReceipt] = field(default_factory=dict)
    cancel_deadline: datetime | None = None
    workspace_root: str | None = None


class SubprocessAgentRuntime(ManagedAgentRuntime):
    """Run a reference Agent with a bounded, supervised subprocess."""

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
            or len(command) > _MAX_ARG_COUNT
            or any(type(item) is not str or not item for item in command)
            or sum(len(item.encode("utf-8")) for item in command) > _MAX_ARG_BYTES
        ):
            raise ValueError("subprocess argv is invalid or exceeds its limit")
        if timeout_seconds <= 0 or timeout_seconds > 300:
            raise ValueError("subprocess timeout is outside the allowed range")
        if not 1 <= max_stdout_bytes <= 262_144 or not 1 <= max_stderr_bytes <= 65_536:
            raise ValueError("subprocess output limit is outside the allowed range")
        supplied: dict[str, str] = {}
        for raw_key, value in dict(environment or {}).items():
            if type(raw_key) is not str or type(value) is not str:
                raise ValueError("subprocess environment must contain strings")
            key = raw_key.upper()
            if key not in _ALLOWED_ENV:
                raise ValueError("subprocess environment contains a non-allowlisted key")
            if key in supplied and supplied[key] != value:
                raise ValueError("subprocess environment contains duplicate keys")
            supplied[key] = value
        self._command = tuple(command)
        self._environment = supplied
        self._timeout_seconds = timeout_seconds
        self._max_stdout_bytes = max_stdout_bytes
        self._max_stderr_bytes = max_stderr_bytes
        self._descriptor = reference_subprocess_descriptor()
        self._lock = RLock()
        self._states: dict[str, _ExecutionState] = {}
        self._dispatches: dict[str, _ExecutionState] = {}
        self._closing = False
        self._owned_staging: tempfile.TemporaryDirectory[str] | None = None
        if artifact_staging_dir is None:
            self._owned_staging = tempfile.TemporaryDirectory(prefix="agentmesh-artifacts-")
            self._staging_dir = Path(self._owned_staging.name)
        else:
            self._staging_dir = Path(artifact_staging_dir)
            self._staging_dir.mkdir(parents=True, exist_ok=True)
        self._package_root = str(Path(__file__).resolve().parents[3])

    def descriptor(self) -> RuntimeDescriptor:
        return self._descriptor

    def validate(self, assignment: RuntimeAssignment) -> ValidationReport:
        errors: list[RuntimeError] = []
        if assignment.runtime_descriptor_digest != self._descriptor.digest():
            errors.append(
                RuntimeError(
                    code="runtime.descriptor_mismatch",
                    category=ErrorCategory.CONFLICT,
                    message="Runtime descriptor does not match the pinned version",
                    retry_disposition=RetryDisposition.NEVER,
                )
            )
        if assignment.execution_mode not in self._descriptor.capabilities.execution_mode:
            errors.append(
                RuntimeError(
                    code="runtime.execution_mode_unsupported",
                    category=ErrorCategory.VALIDATION,
                    message="Reference subprocess execution mode is unsupported",
                    retry_disposition=RetryDisposition.NEVER,
                )
            )
        if not self._descriptor.supports_required_capabilities(assignment.required_capabilities):
            errors.append(
                RuntimeError(
                    code="runtime.capability_mismatch",
                    category=ErrorCategory.VALIDATION,
                    message="Runtime descriptor does not satisfy required capabilities",
                    retry_disposition=RetryDisposition.NEVER,
                )
            )
        return ValidationReport(valid=not errors, errors=tuple(errors))

    def dispatch(self, assignment: RuntimeAssignment, *, dispatch_key: str) -> DispatchReceipt:
        report = self.validate(assignment)
        if not report.valid:
            raise ValueError("Runtime assignment validation failed")
        execution_id = self._parse_dispatch_key(assignment, dispatch_key)
        with self._lock:
            if self._closing:
                raise ValueError("Runtime adapter is closed")
            existing = self._dispatches.get(dispatch_key)
            if existing is None:
                existing_execution = self._states.get(execution_id)
                if existing_execution is not None:
                    raise ValueError("Runtime execution is already bound to another dispatch key")
                now = datetime.now(timezone.utc)
                handle = RuntimeExecutionHandle(
                    runtime_execution_id=execution_id,
                    runtime_version_id=assignment.runtime_version_id,
                    provider_execution_ref=(
                        "subprocess:" + sha256(dispatch_key.encode()).hexdigest()[:32]
                    ),
                    assignment_id=assignment.assignment_id,
                    assignment_digest=assignment.assignment_digest,
                    created_at=now,
                    provider_generation="reference-subprocess-v1",
                )
                observation = RuntimeObservation(
                    observation_id=str(uuid5(NAMESPACE_URL, execution_id + ":pending")),
                    runtime_execution_id=execution_id,
                    assignment_id=assignment.assignment_id,
                    assignment_digest=assignment.assignment_digest,
                    phase=RuntimePhase.DISPATCHING,
                    observed_at=now,
                    provider_event_id="dispatching",
                )
                receipt = DispatchReceipt(
                    dispatch_key=dispatch_key,
                    runtime_execution_id=execution_id,
                    assignment_digest=assignment.assignment_digest,
                    handle=handle,
                    observation=observation,
                )
                state = _ExecutionState(
                    assignment=assignment,
                    dispatch_key=dispatch_key,
                    handle=handle,
                    observation=observation,
                    receipt=receipt,
                )
                self._states[execution_id] = state
                self._dispatches[dispatch_key] = state
                first_dispatch = True
            else:
                state = existing
                if (
                    state.assignment.assignment_digest != assignment.assignment_digest
                    or state.assignment.assignment_id != assignment.assignment_id
                ):
                    raise ValueError("Runtime dispatch key has a different assignment")
                first_dispatch = False
            receipt = state.receipt
        if not first_dispatch:
            return receipt
        if assignment.execution_mode == "managed_async":
            worker = threading.Thread(
                target=self._run,
                args=(state,),
                name=f"agentmesh-runtime-{execution_id[:8]}",
                daemon=True,
            )
            state.worker_thread = worker
            worker.start()
            return receipt
        return self._run(state)

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
        if type(cancellation_id) is not str or not cancellation_id.strip():
            raise ValueError("cancellation_id is required")
        if type(deadline) is not datetime or deadline.tzinfo is None:
            raise ValueError("cancellation deadline must be timezone-aware")
        with self._lock:
            state = self._states.get(handle.runtime_execution_id)
            if state is None or state.handle != handle:
                raise ValueError("Runtime execution handle is unknown")
            replay = state.lifecycle.get(cancellation_id)
            if replay is not None:
                if state.cancel_deadline != deadline:
                    raise ValueError("cancellation replay has a different deadline")
                return replay
            if state.lifecycle:
                raise ValueError("a different cancellation intent already exists")
            if deadline <= datetime.now(timezone.utc):
                raise ValueError("cancellation deadline is expired")
            phase = state.observation.phase
            accepted = not phase.terminal
            receipt = LifecycleReceipt(
                operation_id=cancellation_id,
                runtime_execution_id=handle.runtime_execution_id,
                operation="cancel",
                accepted=accepted,
                observed_phase=RuntimePhase.CANCEL_REQUESTED if accepted else phase,
                safe_message=(
                    "process-group cancellation requested"
                    if accepted
                    else "execution is already terminal"
                ),
            )
            state.lifecycle[cancellation_id] = receipt
            state.cancel_deadline = deadline
            if accepted:
                state.cancel_requested.set()
                process = state.process
            else:
                process = None
        if process is not None:
            self._terminate_process(process)
        return receipt

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
            self._closing = True
            states = tuple(self._states.values())
        for state in states:
            if not state.complete.is_set():
                state.cancel_requested.set()
                if state.process is not None:
                    self._terminate_process(state.process)
        for state in states:
            worker = state.worker_thread
            if worker is not None and worker is not threading.current_thread():
                worker.join(timeout=self._timeout_seconds + _KILL_WAIT_SECONDS + 1)
        if self._owned_staging is not None:
            self._owned_staging.cleanup()
            self._owned_staging = None

    def _run(self, state: _ExecutionState) -> DispatchReceipt:
        workspace = tempfile.TemporaryDirectory(prefix="agentmesh-runtime-")
        state.workspace_root = workspace.name
        process: subprocess.Popen[bytes] | None = None
        try:
            payload = (
                canonical_json_bytes(
                    {
                        "schema": "agentmesh.reference-agent.v1",
                        "operation": "execute",
                        "assignment": state.assignment.to_dict(),
                    }
                )
                + b"\n"
            )
            if len(payload) > self._descriptor.limits.max_assignment_bytes:
                observation = self._failure_observation(
                    state, "runtime.assignment_limit", b"assignment exceeds limit"
                )
                return self._finish(state, observation)
            if state.cancel_requested.is_set():
                return self._finish(state, self._canceled_observation(state))
            env = self._build_environment(workspace.name)
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
            try:
                process = subprocess.Popen(**process_kwargs)
            except (OSError, ValueError) as exc:
                return self._finish(
                    state,
                    self._failure_observation(
                        state, "runtime.process_launch", type(exc).__name__.encode()
                    ),
                )
            with self._lock:
                state.process = process
                state.observation = replace(state.observation, phase=RuntimePhase.RUNNING)
            if state.cancel_requested.is_set():
                self._terminate_process(process)
            stdout_box: list[bytes] = []
            stderr_box: list[bytes] = []
            stdout_overflow = Event()
            stderr_overflow = Event()
            readers = [
                threading.Thread(
                    target=_bounded_reader,
                    args=(process.stdout, self._max_stdout_bytes, stdout_box, stdout_overflow),
                    daemon=True,
                ),
                threading.Thread(
                    target=_bounded_reader,
                    args=(process.stderr, self._max_stderr_bytes, stderr_box, stderr_overflow),
                    daemon=True,
                ),
            ]
            for reader in readers:
                reader.start()
            writer = threading.Thread(
                target=_write_stdin, args=(process.stdin, payload), daemon=True
            )
            writer.start()
            deadline = time.monotonic() + self._timeout_seconds
            reason: str | None = None
            while process.poll() is None:
                if state.cancel_requested.is_set():
                    reason = "runtime.canceled"
                    self._terminate_process(process)
                    break
                if stdout_overflow.is_set():
                    reason = "runtime.stdout_limit"
                    self._terminate_process(process)
                    break
                if stderr_overflow.is_set():
                    reason = "runtime.stderr_limit"
                    self._terminate_process(process)
                    break
                if time.monotonic() >= deadline:
                    reason = "runtime.timeout"
                    self._terminate_process(process)
                    break
                time.sleep(0.01)
            self._wait_bounded(process)
            for reader in readers:
                reader.join(timeout=_KILL_WAIT_SECONDS)
            writer.join(timeout=_KILL_WAIT_SECONDS)
            stderr = b"".join(stderr_box)
            if reason is None and stdout_overflow.is_set():
                reason = "runtime.stdout_limit"
            if reason is None and stderr_overflow.is_set():
                reason = "runtime.stderr_limit"
            if state.cancel_requested.is_set():
                observation = self._canceled_observation(state, stderr)
            elif reason == "runtime.timeout":
                observation = self._failure_observation(state, reason, stderr)
            elif reason is not None:
                observation = self._failure_observation(state, reason, stderr)
            elif process.returncode != 0:
                observation = self._failure_observation(state, "runtime.process_exit", stderr)
            else:
                observation = self._decode_result(state, b"".join(stdout_box))
            return self._finish(state, observation)
        except Exception as exc:  # supervisor must never strand a receipt
            if process is not None:
                self._terminate_process(process)
                self._wait_bounded(process)
            return self._finish(
                state,
                self._failure_observation(
                    state, "runtime.supervisor_error", type(exc).__name__.encode()
                ),
            )
        finally:
            with self._lock:
                state.process = None
            workspace.cleanup()
            state.complete.set()

    def _finish(self, state: _ExecutionState, observation: RuntimeObservation) -> DispatchReceipt:
        receipt = DispatchReceipt(
            dispatch_key=state.dispatch_key,
            runtime_execution_id=state.handle.runtime_execution_id,
            assignment_digest=state.assignment.assignment_digest,
            handle=state.handle,
            observation=observation,
        )
        with self._lock:
            state.observation = observation
            state.receipt = receipt
        return receipt

    def _decode_result(self, state: _ExecutionState, stdout: bytes) -> RuntimeObservation:
        try:
            if len(stdout) > self._descriptor.limits.max_result_bytes:
                raise ValueError("result exceeds limit")
            lines = stdout.splitlines()
            if len(lines) != 1:
                raise ValueError("reference protocol requires one result line")
            response = json.loads(lines[0].decode("utf-8"))
            if type(response) is not dict:
                raise ValueError("reference result must be an object")
            if set(response) != {"schema", "type", "observation", "artifact"}:
                raise ValueError("reference result contains unknown fields")
            if response["schema"] != "agentmesh.reference-agent.v1" or response["type"] != "result":
                raise ValueError("reference result schema is invalid")
            observation = RuntimeObservation.from_dict(response["observation"])
            if (
                observation.runtime_execution_id != state.handle.runtime_execution_id
                or observation.assignment_id != state.assignment.assignment_id
                or observation.assignment_digest != state.assignment.assignment_digest
                or observation.phase is not RuntimePhase.SUCCEEDED
            ):
                raise ValueError("reference result identity or terminal phase is invalid")
            artifact = response["artifact"]
            if type(artifact) is not dict or set(artifact) != {"name", "media_type", "content"}:
                raise ValueError("reference artifact schema is invalid")
            if artifact["name"] != "report.json" or artifact["media_type"] != "application/json":
                raise ValueError("reference artifact metadata is invalid")
            content = artifact["content"]
            if type(content) is not str:
                raise ValueError("reference artifact content is invalid")
            content_bytes = content.encode("utf-8")
            if len(content_bytes) > self._descriptor.limits.max_result_bytes:
                raise ValueError("reference artifact exceeds result limit")
            return replace(
                observation,
                output_artifact_refs=(self._stage_artifact(state, content_bytes),),
            )
        except Exception as exc:
            return self._failure_observation(state, "runtime.protocol_error", str(exc).encode())

    def _stage_artifact(self, state: _ExecutionState, content: bytes) -> ArtifactRef:
        digest = sha256(content).hexdigest()
        artifact_id = str(uuid5(NAMESPACE_URL, state.handle.runtime_execution_id + ":artifact"))
        version_id = str(uuid5(NAMESPACE_URL, state.handle.runtime_execution_id + ":artifact:v1"))
        final_path = self._staging_dir / f"{state.handle.runtime_execution_id}.json"
        if final_path.name != f"{state.handle.runtime_execution_id}.json":
            raise ValueError("unsafe artifact path")
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=self._staging_dir, prefix=".staging-", suffix=".tmp", delete=False
            ) as staged:
                staged.write(content)
                staged.flush()
                os.fsync(staged.fileno())
                temporary_path = Path(staged.name)
            os.replace(temporary_path, final_path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        return ArtifactRef(
            artifact_id=artifact_id,
            version_id=version_id,
            digest=digest,
            size_bytes=len(content),
            media_type="application/json",
        )

    def _build_environment(self, workspace: str) -> dict[str, str]:
        env = {
            key: value
            for key in _ALLOWED_ENV
            if key not in {"PYTHONPATH", "PYTHONHOME", "TEMP", "TMP", "HOME"}
            and (value := os.environ.get(key)) is not None
        }
        env["PYTHONPATH"] = self._environment.get("PYTHONPATH", self._package_root)
        env["TEMP"] = workspace
        env["TMP"] = workspace
        env["HOME"] = workspace
        for key, value in self._environment.items():
            if key not in {"TEMP", "TMP", "HOME"}:
                env[key] = value
        return env

    def _failure_observation(
        self, state: _ExecutionState, code: str, stderr: bytes | str = b""
    ) -> RuntimeObservation:
        safe = _redact_bounded(stderr, self._max_stderr_bytes)
        return RuntimeObservation(
            observation_id=str(
                uuid5(NAMESPACE_URL, state.handle.runtime_execution_id + ":" + code)
            ),
            runtime_execution_id=state.handle.runtime_execution_id,
            assignment_id=state.assignment.assignment_id,
            assignment_digest=state.assignment.assignment_digest,
            phase=RuntimePhase.TIMED_OUT if code == "runtime.timeout" else RuntimePhase.FAILED,
            observed_at=datetime.now(timezone.utc),
            provider_event_id=code,
            progress={"stderr": safe} if safe else {},
            error=RuntimeError(
                code=code,
                category=ErrorCategory.TRANSIENT
                if code == "runtime.timeout"
                else ErrorCategory.PERMANENT,
                message="reference subprocess did not produce a successful result",
                retry_disposition=RetryDisposition.NEW_EXECUTION
                if code == "runtime.timeout"
                else RetryDisposition.NEVER,
            ),
        )

    def _canceled_observation(
        self, state: _ExecutionState, stderr: bytes | str = b""
    ) -> RuntimeObservation:
        safe = _redact_bounded(stderr, self._max_stderr_bytes)
        return RuntimeObservation(
            observation_id=str(
                uuid5(NAMESPACE_URL, state.handle.runtime_execution_id + ":canceled")
            ),
            runtime_execution_id=state.handle.runtime_execution_id,
            assignment_id=state.assignment.assignment_id,
            assignment_digest=state.assignment.assignment_digest,
            phase=RuntimePhase.CANCELED,
            observed_at=datetime.now(timezone.utc),
            provider_event_id="runtime.canceled",
            progress={"stderr": safe} if safe else {},
            error=RuntimeError(
                code="runtime.canceled",
                category=ErrorCategory.TRANSIENT,
                message="reference subprocess was canceled",
                retry_disposition=RetryDisposition.NEVER,
            ),
        )

    @staticmethod
    def _parse_dispatch_key(assignment: RuntimeAssignment, dispatch_key: str) -> str:
        if type(dispatch_key) is not str or len(dispatch_key.encode("utf-8")) > 512:
            raise ValueError("Runtime dispatch key is invalid")
        prefix = f"runtime-dispatch:{assignment.tenant_id}:"
        if not dispatch_key.startswith(prefix):
            raise ValueError("Runtime dispatch key tenant binding is invalid")
        value = dispatch_key[len(prefix) :]
        execution_id = assignment.correlation_ids.get("runtime_execution_id")
        try:
            if value != execution_id:
                raise ValueError("Runtime dispatch key execution binding is invalid")
            UUID(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("Runtime dispatch key execution binding is invalid") from exc
        return value

    @staticmethod
    def _wait_bounded(process: subprocess.Popen[bytes]) -> None:
        try:
            process.wait(timeout=_KILL_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            SubprocessAgentRuntime._terminate_process(process)
            try:
                process.wait(timeout=_KILL_WAIT_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()

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
                    timeout=_KILL_WAIT_SECONDS,
                )
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            try:
                process.kill()
            except OSError:
                pass


def _bounded_reader(stream: Any, limit: int, box: list[bytes], overflow: Event) -> None:
    try:
        total = 0
        while True:
            chunk = stream.read(min(4096, limit + 1))
            if not chunk:
                return
            remaining = limit - total
            if len(chunk) > remaining:
                if remaining > 0:
                    box.append(chunk[:remaining])
                overflow.set()
                return
            box.append(chunk)
            total += len(chunk)
    except (OSError, ValueError):
        return


def _write_stdin(stream: Any, payload: bytes) -> None:
    try:
        stream.write(payload)
        stream.close()
    except (BrokenPipeError, OSError, ValueError):
        return


def _redact_bounded(value: bytes | str, limit: int) -> str:
    raw = value if isinstance(value, bytes) else value.encode("utf-8", errors="replace")
    text = raw.decode("utf-8", errors="replace")
    redacted = _SECRET_PATTERN.sub(r"\1=<redacted>", text).encode("utf-8")
    return redacted[:limit].decode("utf-8", errors="replace")
