from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from agentmesh.application.managed_runtime_execution import ManagedRuntimeExecutionService
from agentmesh.application.ports import ManagedRuntimeControlPlaneFailure
from agentmesh.application.runtime_snapshots import (
    RuntimeAssignmentSnapshot,
    assignment_snapshot_for,
    handle_snapshot_for,
)
from agentmesh.domain.errors import InvalidTaskTransition
from agentmesh.domain.runtime_execution import RuntimeExecution, RuntimeExecutionPhase
from agentmesh.domain.tasks import Task, TaskAttempt, TaskRun
from agentmesh.infrastructure.runtime.langgraph_adapter import (
    EphemeralRuntimeLifecycleController,
    EphemeralRuntimeStateStore,
    LangGraphManagedAgentRuntime,
)
from agentmesh.runtime_sdk import (
    ErrorCategory,
    RetryDisposition,
    RuntimeError,
    RuntimeObservation,
    RuntimePhase,
    canonical_digest,
)


class _CountingBackend:
    def __init__(self) -> None:
        self.execute_calls = 0
        self.output: object = {"ok": True}

    def bind(self, assignment, task, run, attempt, work_item) -> None:
        return None

    def execute(self, assignment):
        self.execute_calls += 1
        return RuntimeObservation(
            observation_id=str(uuid4()),
            runtime_execution_id=assignment.correlation_ids["runtime_execution_id"],
            assignment_id=assignment.assignment_id,
            assignment_digest=assignment.assignment_digest,
            phase=RuntimePhase.SUCCEEDED,
            observed_at=datetime.now(timezone.utc),
            provider_event_id="counting-backend",
            output=self.output,
        )


class _InvalidTerminalBackend(_CountingBackend):
    def __init__(self, kind: str) -> None:
        super().__init__()
        self.kind = kind

    def execute(self, assignment):
        observation = super().execute(assignment)
        if self.kind == "usage":
            return replace(observation, usage={"tokens": 1})
        if self.kind == "action":
            return replace(observation, governed_action_requests=({"action": "write"},))
        if self.kind == "wait":
            return replace(observation, wait_refs=("wait://provider",))
        if self.kind == "error":
            from agentmesh.runtime_sdk import ErrorCategory, RetryDisposition, RuntimeError

            return replace(
                observation,
                error=RuntimeError(
                    code="provider.error",
                    category=ErrorCategory.PERMANENT,
                    message="contradictory success error",
                    retry_disposition=RetryDisposition.NEVER,
                ),
            )
        if self.kind == "identity":
            return replace(observation, runtime_execution_id=str(uuid4()))
        raise AssertionError(self.kind)


class _ProtocolErrorBackend(_CountingBackend):
    def execute(self, assignment):
        return replace(
            super().execute(assignment),
            error=RuntimeError(
                code="runtime.protocol_error",
                category=ErrorCategory.UNKNOWN,
                message="protocol conflict",
                retry_disposition=RetryDisposition.RECONCILE,
            ),
        )


class _Registry:
    def __init__(self) -> None:
        self.execution: RuntimeExecution | None = None
        self.observation_calls = 0
        self.handle_bind_calls = 0

    def prepare_execution(self, *, run_id, assignment_id, assignment_digest, dispatch_key,
                          execution_id):
        if self.execution is None:
            self.execution = RuntimeExecution.prepare(
                tenant_id="tenant-a",
                run_id=run_id,
                runtime_version_id=uuid4(),
                assignment_id=assignment_id,
                assignment_digest=assignment_digest,
                dispatch_key=dispatch_key,
                dispatch_digest=canonical_digest({"dispatch_key": dispatch_key}),
                execution_id=execution_id,
            )
        return self.execution

    def get_assignment_snapshot(self, execution_id):
        return None

    def prepare_execution_with_assignment_snapshot(
        self, *, run_id, assignment, dispatch_key, execution_id
    ):
        return self.prepare_execution(
            run_id=run_id,
            assignment_id=UUID(assignment.assignment_id),
            assignment_digest=assignment.assignment_digest,
            dispatch_key=dispatch_key,
            execution_id=execution_id,
        )

    def bind_handle_snapshot(self, *, handle, attempt_id, fencing_token):
        self.handle_bind_calls += 1

    def claim_execution_owner(
        self, *, execution_id, attempt_id, fencing_token,
        expected_owner_attempt_id, expected_fencing_token, expected_version,
        claim_reason, now,
    ):
        assert self.execution is not None
        if self.execution.current_owner_attempt_id is None:
            self.execution = self.execution.claim(
                attempt_id=attempt_id,
                fencing_token=fencing_token,
                expected_owner_attempt_id=expected_owner_attempt_id,
                expected_fencing_token=expected_fencing_token,
                expected_version=expected_version,
                now=now,
            )
        return self.execution

    def record_observation(self, **kwargs) -> None:
        self.observation_calls += 1

    def mark_execution_dispatching(
        self, *, execution_id, attempt_id, fencing_token, now=None
    ):
        assert self.execution is not None
        assert self.execution.id == execution_id
        assert self.execution.current_owner_attempt_id == attempt_id
        assert self.execution.current_fencing_token == fencing_token
        self.execution = self.execution.apply_observation(
            phase=RuntimeExecutionPhase.DISPATCHING,
            provider_sequence=None,
            now=now,
        )
        return self.execution


class _SnapshotRegistry(_Registry):
    def __init__(self) -> None:
        super().__init__()
        self.assignment_snapshot: RuntimeAssignmentSnapshot | None = None
        self.handle_snapshot = None

    def get_assignment_snapshot(self, execution_id):
        if (
            self.assignment_snapshot is not None
            and self.assignment_snapshot.runtime_execution_id == execution_id
        ):
            return self.assignment_snapshot
        return None

    def prepare_execution_with_assignment_snapshot(
        self, *, run_id, assignment, dispatch_key, execution_id
    ):
        execution = super().prepare_execution(
            run_id=run_id,
            assignment_id=UUID(assignment.assignment_id),
            assignment_digest=assignment.assignment_digest,
            dispatch_key=dispatch_key,
            execution_id=execution_id,
        )
        candidate = assignment_snapshot_for(
            assignment,
            tenant_id=assignment.tenant_id,
            runtime_execution_id=execution.id,
            created_at=datetime.now(timezone.utc),
        )
        if self.assignment_snapshot is None:
            self.assignment_snapshot = candidate
        elif self.assignment_snapshot.canonical_payload != candidate.canonical_payload:
            raise AssertionError("changed Assignment replay")
        return execution

    def bind_handle_snapshot(self, *, handle, attempt_id, fencing_token):
        self.handle_snapshot = handle_snapshot_for(
            handle, tenant_id="tenant-a", created_at=handle.created_at
        )
        assert self.execution is not None
        self.execution = self.execution.bind_handle(
            provider_execution_ref=handle.provider_execution_ref,
            provider_generation=handle.provider_generation,
        )


class _CountingAssignmentBuilder:
    def __init__(self, delegate) -> None:
        self.delegate = delegate
        self.calls = 0

    def assignment_for(self, *args, **kwargs):
        self.calls += 1
        return self.delegate.assignment_for(*args, **kwargs)


class _CrashBeforeHandleRegistry(_SnapshotRegistry):
    def bind_handle_snapshot(self, *, handle, attempt_id, fencing_token):
        raise RuntimeError("crash before handle bind")


class _BoundaryRegistry(_Registry):
    def __init__(self) -> None:
        super().__init__()
        self.active = False
        self.events: list[str] = []

    def prepare_execution(self, **kwargs):
        self.events.append("prepare:start")
        self.active = True
        try:
            return super().prepare_execution(**kwargs)
        finally:
            self.active = False
            self.events.append("prepare:end")

    def claim_execution_owner(self, **kwargs):
        self.events.append("claim:start")
        self.active = True
        try:
            return super().claim_execution_owner(**kwargs)
        finally:
            self.active = False
            self.events.append("claim:end")

    def mark_execution_dispatching(self, **kwargs):
        self.events.append("mark:start")
        self.active = True
        try:
            return super().mark_execution_dispatching(**kwargs)
        finally:
            self.active = False
            self.events.append("mark:end")


class _MarkFailureRegistry(_Registry):
    def mark_execution_dispatching(self, **kwargs):
        raise RuntimeError("database unavailable")


class _BoundaryAdapter:
    def __init__(
        self,
        delegate: LangGraphManagedAgentRuntime,
        registry: _BoundaryRegistry,
        fail_at: str | None = None,
    ) -> None:
        self._delegate = delegate
        self._registry = registry
        self._fail_at = fail_at

    def assignment_for(self, *args, **kwargs):
        return self._delegate.assignment_for(*args, **kwargs)

    def validate(self, assignment):
        assert self._registry.active is False
        self._registry.events.append("validate")
        if self._fail_at == "validate":
            raise ValueError("assignment validation failed")
        return self._delegate.validate(assignment)

    def bind_context(self, *args, **kwargs):
        assert self._registry.active is False
        self._registry.events.append("bind")
        if self._fail_at == "bind":
            raise ValueError("assignment bind failed")
        return self._delegate.bind_context(*args, **kwargs)

    def dispatch(self, *args, **kwargs):
        assert self._registry.active is False
        self._registry.events.append("dispatch")
        return self._delegate.dispatch(*args, **kwargs)


def _fixture(*, lease_expires_at: datetime | None = None):
    task = Task.create(tenant_id="tenant-a", objective="deterministic task", input={})
    run = TaskRun.request(
        task.id,
        "demo-agent",
        agent_version_id=uuid4(),
        agent_version_digest="a" * 64,
        runtime_version_id=uuid4(),
    )
    run.runtime_execution_id = uuid4()
    attempt = TaskAttempt.lease(
        run_id=run.id,
        worker_id="worker-a",
        fencing_token=1,
        lease_expires_at=lease_expires_at
        or datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    backend = _CountingBackend()
    adapter = LangGraphManagedAgentRuntime(
        backend=backend,
        state_store=EphemeralRuntimeStateStore(),
        lifecycle_controller=EphemeralRuntimeLifecycleController(),
    )
    registry = _Registry()
    service = ManagedRuntimeExecutionService(
        registry=registry,
        adapter=adapter,
        assignment_builder=adapter,
    )
    return service, task, run, attempt, backend, registry


def test_active_replay_uses_same_dispatch_key_and_backend_runs_once() -> None:
    service, task, run, attempt, backend, registry = _fixture()

    first = service.execute_shadow(task, run, attempt)
    second = service.execute_shadow(task, run, attempt)

    assert first.digest() == second.digest()
    assert backend.execute_calls == 1
    assert registry.observation_calls == 2


def test_expired_attempt_fails_before_adapter_or_registry_side_effect() -> None:
    expired = datetime.now(timezone.utc) - timedelta(seconds=1)
    service, task, run, attempt, backend, registry = _fixture(lease_expires_at=expired)

    with pytest.raises(InvalidTaskTransition, match="lease is not active"):
        service.execute_shadow(task, run, attempt)

    assert backend.execute_calls == 0
    assert registry.execution is None


def test_shadow_preserves_canonical_non_mapping_output() -> None:
    service, task, run, attempt, backend, _registry = _fixture()
    backend.output = ["result", 1]

    snapshot = service.execute_shadow(task, run, attempt)

    assert snapshot.output == ["result", 1]


def test_adapter_calls_start_after_registry_prepare_and_claim_return() -> None:
    _service, task, run, attempt, backend, _registry = _fixture()
    registry = _BoundaryRegistry()
    delegate = LangGraphManagedAgentRuntime(
        backend=backend,
        state_store=EphemeralRuntimeStateStore(),
        lifecycle_controller=EphemeralRuntimeLifecycleController(),
    )
    service = ManagedRuntimeExecutionService(
        registry=registry,
        adapter=_BoundaryAdapter(delegate, registry),
        assignment_builder=delegate,
    )

    service.execute_shadow(task, run, attempt)

    assert registry.events == [
        "prepare:start",
        "prepare:end",
        "claim:start",
        "claim:end",
        "validate",
        "bind",
        "mark:start",
        "mark:end",
        "dispatch",
    ]
    assert backend.execute_calls == 1


@pytest.mark.parametrize("failure_stage", ["validate", "bind"])
def test_shadow_pre_dispatch_failure_does_not_cross_dispatch_boundary(
    failure_stage: str,
) -> None:
    _service, task, run, attempt, backend, _registry = _fixture()
    registry = _BoundaryRegistry()
    delegate = LangGraphManagedAgentRuntime(
        backend=backend,
        state_store=EphemeralRuntimeStateStore(),
        lifecycle_controller=EphemeralRuntimeLifecycleController(),
    )
    service = ManagedRuntimeExecutionService(
        registry=registry,
        adapter=_BoundaryAdapter(delegate, registry, fail_at=failure_stage),
        assignment_builder=delegate,
    )

    with pytest.raises(ValueError):
        service.execute_shadow(task, run, attempt)

    assert registry.execution is not None
    assert registry.execution.phase is RuntimeExecutionPhase.PREPARED
    assert backend.execute_calls == 0


def test_authoritative_execution_returns_uncommitted_observation() -> None:
    service, task, run, attempt, backend, registry = _fixture()
    run.runtime_authority = "managed"

    result = service.execute_authoritative(task, run, attempt)

    assert result.observation.phase is RuntimePhase.SUCCEEDED
    assert result.observation.output == {"ok": True}
    assert result.dispatch_crossed is True
    assert backend.execute_calls == 1
    assert registry.observation_calls == 0
    assert registry.execution is not None
    assert registry.execution.phase is RuntimeExecutionPhase.DISPATCHING


def test_replacement_loads_assignment_and_handle_snapshots_without_rebuilding() -> None:
    service, task, run, attempt, backend, _registry = _fixture()
    run.runtime_authority = "managed"
    adapter = LangGraphManagedAgentRuntime(
        backend=backend,
        state_store=EphemeralRuntimeStateStore(),
        lifecycle_controller=EphemeralRuntimeLifecycleController(),
    )
    registry = _SnapshotRegistry()
    builder = _CountingAssignmentBuilder(adapter)
    service = ManagedRuntimeExecutionService(
        registry=registry,
        adapter=adapter,
        assignment_builder=builder,
    )

    first = service.execute_authoritative(task, run, attempt)
    replacement = service.execute_authoritative(task, run, attempt)

    assert first.observation.phase is RuntimePhase.SUCCEEDED
    assert replacement.observation.phase is RuntimePhase.OUTCOME_UNKNOWN
    assert registry.assignment_snapshot is not None
    assert registry.handle_snapshot is not None
    assert builder.calls == 1
    assert backend.execute_calls == 1


def test_dispatch_crash_before_handle_bind_keeps_crossed_recoverable_state() -> None:
    service, task, run, attempt, backend, _registry = _fixture()
    run.runtime_authority = "managed"
    adapter = LangGraphManagedAgentRuntime(
        backend=backend,
        state_store=EphemeralRuntimeStateStore(),
        lifecycle_controller=EphemeralRuntimeLifecycleController(),
    )
    registry = _CrashBeforeHandleRegistry()
    builder = _CountingAssignmentBuilder(adapter)
    service = ManagedRuntimeExecutionService(
        registry=registry,
        adapter=adapter,
        assignment_builder=builder,
    )

    first = service.execute_authoritative(task, run, attempt)
    second = service.execute_authoritative(task, run, attempt)

    assert first.observation.phase is RuntimePhase.OUTCOME_UNKNOWN
    assert first.observation.error is not None
    assert first.observation.error.code == "runtime.handle_contract_invalid"
    assert second.observation.phase is RuntimePhase.OUTCOME_UNKNOWN
    assert registry.execution is not None
    assert registry.execution.phase is RuntimeExecutionPhase.DISPATCHING
    assert registry.assignment_snapshot is not None
    assert registry.handle_snapshot is None
    assert builder.calls == 1
    assert backend.execute_calls == 1


def test_authoritative_validation_precedes_persistent_execution_preparation() -> None:
    _service, task, run, attempt, backend, _registry = _fixture()
    run.runtime_authority = "managed"
    registry = _BoundaryRegistry()
    delegate = LangGraphManagedAgentRuntime(
        backend=backend,
        state_store=EphemeralRuntimeStateStore(),
        lifecycle_controller=EphemeralRuntimeLifecycleController(),
    )
    service = ManagedRuntimeExecutionService(
        registry=registry,
        adapter=_BoundaryAdapter(delegate, registry),
        assignment_builder=delegate,
    )

    service.execute_authoritative(task, run, attempt)

    assert registry.events == [
        "validate",
        "bind",
        "prepare:start",
        "prepare:end",
        "claim:start",
        "claim:end",
        "mark:start",
        "mark:end",
        "dispatch",
    ]


@pytest.mark.parametrize("kind", ["usage", "action", "wait", "error", "identity"])
def test_provider_terminal_contract_conflict_parks_unknown_before_observation_write(
    kind: str,
) -> None:
    task = Task.create(tenant_id="tenant-a", objective="deterministic task", input={})
    run = TaskRun.request(
        task.id,
        "demo-agent",
        agent_version_id=uuid4(),
        agent_version_digest="a" * 64,
        runtime_version_id=uuid4(),
    )
    run.runtime_execution_id = uuid4()
    attempt = TaskAttempt.lease(
        run_id=run.id,
        worker_id="worker-a",
        fencing_token=1,
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    backend = _InvalidTerminalBackend(kind)
    adapter = LangGraphManagedAgentRuntime(
        backend=backend,
        state_store=EphemeralRuntimeStateStore(),
        lifecycle_controller=EphemeralRuntimeLifecycleController(),
    )
    registry = _Registry()
    service = ManagedRuntimeExecutionService(
        registry=registry,
        adapter=adapter,
        assignment_builder=adapter,
    )
    run.runtime_authority = "managed"

    result = service.execute_authoritative(task, run, attempt)

    assert result.observation.phase is RuntimePhase.OUTCOME_UNKNOWN
    assert result.observation.error is not None
    assert result.observation.error.code == "runtime.terminal_contract_invalid"
    assert result.conflicting_observation is not None
    assert any(
        vars(result.conflicting_observation)[name]
        for name in (
            "structural_invalid",
            "execution_id_mismatch",
            "assignment_id_mismatch",
            "assignment_digest_mismatch",
            "terminal_contract_invalid",
            "protocol_error_observation",
        )
    )
    assert registry.observation_calls == 0
    assert registry.execution is not None
    assert registry.execution.phase is RuntimeExecutionPhase.DISPATCHING


def test_protocol_error_returns_safe_conflict_envelope():
    service, task, run, attempt, _backend, _registry = _fixture()
    run.runtime_authority = "managed"
    adapter = LangGraphManagedAgentRuntime(
        backend=_ProtocolErrorBackend(),
        state_store=EphemeralRuntimeStateStore(),
        lifecycle_controller=EphemeralRuntimeLifecycleController(),
    )
    registry = _Registry()
    service = ManagedRuntimeExecutionService(
        registry=registry,
        adapter=adapter,
        assignment_builder=adapter,
    )

    result = service.execute_authoritative(task, run, attempt)

    assert result.observation.phase is RuntimePhase.OUTCOME_UNKNOWN
    assert result.conflicting_observation is not None
    assert result.conflicting_observation.protocol_error_observation is True
    assert result.conflicting_observation.terminal_contract_invalid is True


def test_unexpected_terminal_validator_error_propagates(monkeypatch):
    service, task, run, attempt, _backend, _registry = _fixture()
    run.runtime_authority = "managed"

    def raise_unexpected(*args, **kwargs):
        raise TypeError("validator defect")

    monkeypatch.setattr(
        "agentmesh.application.managed_runtime_execution.validate_terminal_observation",
        raise_unexpected,
    )

    with pytest.raises(TypeError, match="validator defect"):
        service.execute_authoritative(task, run, attempt)


def test_replacement_attempt_keeps_canonical_assignment_identity() -> None:
    _service, task, run, first, _backend, _registry = _fixture()
    adapter = LangGraphManagedAgentRuntime(
        backend=_CountingBackend(),
        state_store=EphemeralRuntimeStateStore(),
        lifecycle_controller=EphemeralRuntimeLifecycleController(),
    )
    replacement = TaskAttempt.lease(
        run_id=run.id,
        worker_id="worker-b",
        fencing_token=2,
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )

    first_assignment = adapter.assignment_for(task, run, first)
    replacement_assignment = adapter.assignment_for(task, run, replacement)

    assert first_assignment.assignment_id == replacement_assignment.assignment_id
    assert first_assignment.assignment_digest == replacement_assignment.assignment_digest


def test_mark_dispatching_failure_never_calls_provider_and_remains_prepared() -> None:
    _service, task, run, attempt, backend, _registry = _fixture()
    run.runtime_authority = "managed"
    registry = _MarkFailureRegistry()
    adapter = LangGraphManagedAgentRuntime(
        backend=backend,
        state_store=EphemeralRuntimeStateStore(),
        lifecycle_controller=EphemeralRuntimeLifecycleController(),
    )
    service = ManagedRuntimeExecutionService(
        registry=registry,
        adapter=adapter,
        assignment_builder=adapter,
    )

    with pytest.raises(ManagedRuntimeControlPlaneFailure, match="did not commit"):
        service.execute_authoritative(task, run, attempt)

    assert backend.execute_calls == 0
    assert registry.execution is not None
    assert registry.execution.phase is RuntimeExecutionPhase.PREPARED
