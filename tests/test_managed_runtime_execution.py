from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from agentmesh.application.managed_runtime_execution import ManagedRuntimeExecutionService
from agentmesh.domain.errors import InvalidTaskTransition
from agentmesh.domain.runtime_execution import RuntimeExecution, RuntimeExecutionPhase
from agentmesh.domain.tasks import Task, TaskAttempt, TaskRun
from agentmesh.infrastructure.runtime.langgraph_adapter import (
    EphemeralRuntimeLifecycleController,
    EphemeralRuntimeStateStore,
    LangGraphManagedAgentRuntime,
)
from agentmesh.runtime_sdk import RuntimeObservation, RuntimePhase, canonical_digest


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


class _Registry:
    def __init__(self) -> None:
        self.execution: RuntimeExecution | None = None
        self.observation_calls = 0

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


class _BoundaryAdapter:
    def __init__(self, delegate: LangGraphManagedAgentRuntime, registry: _BoundaryRegistry) -> None:
        self._delegate = delegate
        self._registry = registry

    def assignment_for(self, *args, **kwargs):
        return self._delegate.assignment_for(*args, **kwargs)

    def validate(self, assignment):
        assert self._registry.active is False
        self._registry.events.append("validate")
        return self._delegate.validate(assignment)

    def bind_context(self, *args, **kwargs):
        assert self._registry.active is False
        self._registry.events.append("bind")
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
        "dispatch",
    ]
    assert backend.execute_calls == 1


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
