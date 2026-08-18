"""LangGraph implementation of the framework-neutral runtime boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from threading import RLock
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from agentmesh.application.ports import WorkflowExecutionResult, WorkflowRunner, WorkflowWorkItem
from agentmesh.domain.tasks import Task, TaskAttempt, TaskRun
from agentmesh.runtime_sdk import (
    DispatchReceipt,
    ErrorCategory,
    LifecycleReceipt,
    ManagedAgentRuntime,
    RetryDisposition,
    RuntimeAssignment,
    RuntimeDescriptor,
    RuntimeError,
    RuntimeEvent,
    RuntimeEventPage,
    RuntimeExecutionHandle,
    RuntimeObservation,
    RuntimePhase,
    ValidationReport,
)
from agentmesh.runtime_sdk.builtin import langgraph_v2_descriptor

LANGGRAPH_DESCRIPTOR = langgraph_v2_descriptor()


@dataclass
class _ExecutionState:
    assignment: RuntimeAssignment
    handle: RuntimeExecutionHandle
    observation: RuntimeObservation
    events: list[RuntimeEvent]
    lifecycle: dict[str, LifecycleReceipt]


class RuntimeStateStore(Protocol):
    """Durable provider state required for restart-safe inspect/reattach."""

    def get(self, dispatch_key: str) -> _ExecutionState | None: ...
    def put(self, dispatch_key: str, state: _ExecutionState) -> None: ...
    def values(self) -> tuple[_ExecutionState, ...]: ...


class RuntimeLifecycleController(Protocol):
    """Provider-side lifecycle implementation, called outside the UoW."""

    def request(
        self,
        operation: str,
        handle: RuntimeExecutionHandle,
        *,
        operation_id: str,
        deadline: datetime | None,
    ) -> None: ...


class RuntimeAssignmentBackend(Protocol):
    """Outward execution backend; it owns framework-specific context mapping."""

    def bind(
        self,
        assignment: RuntimeAssignment,
        task: Task,
        run: TaskRun,
        attempt: TaskAttempt,
        work_item: WorkflowWorkItem | None,
    ) -> None: ...

    def execute(self, assignment: RuntimeAssignment) -> RuntimeObservation: ...


class EphemeralRuntimeStateStore:
    """Test-only state store shared by adapter instances to test restart."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._values: dict[str, _ExecutionState] = {}

    def get(self, dispatch_key: str) -> _ExecutionState | None:
        with self._lock:
            return self._values.get(dispatch_key)

    def put(self, dispatch_key: str, state: _ExecutionState) -> None:
        with self._lock:
            self._values[dispatch_key] = state

    def values(self) -> tuple[_ExecutionState, ...]:
        with self._lock:
            return tuple(self._values.values())


class EphemeralRuntimeLifecycleController:
    """Test-only lifecycle backend; production must inject a real controller."""

    def request(
        self,
        operation: str,
        handle: RuntimeExecutionHandle,
        *,
        operation_id: str,
        deadline: datetime | None,
    ) -> None:
        return None


class LangGraphWorkflowBackend:
    """Maps a canonical assignment to the existing LangGraph workflow call."""

    def __init__(self, workflow_runner: WorkflowRunner) -> None:
        self._workflow_runner = workflow_runner
        self._contexts: dict[str, tuple[Task, TaskRun, TaskAttempt, WorkflowWorkItem | None]] = {}

    def bind(
        self,
        assignment: RuntimeAssignment,
        task: Task,
        run: TaskRun,
        attempt: TaskAttempt,
        work_item: WorkflowWorkItem | None,
    ) -> None:
        self._contexts[assignment.assignment_id] = (task, run, attempt, work_item)

    def execute(self, assignment: RuntimeAssignment) -> RuntimeObservation:
        context = self._contexts.get(assignment.assignment_id)
        if context is None:
            raise ValueError("Runtime assignment context is unavailable")
        task, run, attempt, work_item = context
        result = self._workflow_runner.run(task, run, attempt, work_item=work_item)
        return _observation_from_result(assignment, result)


def _observation_from_result(
    assignment: RuntimeAssignment, result: WorkflowExecutionResult
) -> RuntimeObservation:
    usage: dict[str, int] = {}
    for record in result.usage_records:
        for key, value in record.usage_details.items():
            usage[key] = usage.get(key, 0) + value
    return RuntimeObservation(
        observation_id=str(uuid5(NAMESPACE_URL, assignment.assignment_id + ":result")),
        runtime_execution_id=_execution_id_from_assignment(assignment),
        assignment_id=assignment.assignment_id,
        assignment_digest=assignment.assignment_digest,
        phase=RuntimePhase.SUCCEEDED,
        observed_at=datetime.now(timezone.utc),
        provider_event_id="result",
        output=result.output,
        usage=usage,
    )


def _execution_id_from_assignment(assignment: RuntimeAssignment) -> str:
    value = assignment.correlation_ids.get("runtime_execution_id")
    if type(value) is not str:
        raise ValueError("Runtime assignment execution identity is unavailable")
    UUID(value)
    return value


class LangGraphManagedAgentRuntime(ManagedAgentRuntime):
    """LangGraph adapter with explicit durable state/lifecycle dependencies."""

    def __init__(
        self,
        *,
        backend: RuntimeAssignmentBackend,
        state_store: RuntimeStateStore,
        lifecycle_controller: RuntimeLifecycleController,
    ) -> None:
        self._backend = backend
        self._descriptor = RuntimeDescriptor.from_dict(langgraph_v2_descriptor())
        self._state_store = state_store
        self._lifecycle_controller = lifecycle_controller

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
        return ValidationReport(valid=True)

    def dispatch(self, assignment: RuntimeAssignment, *, dispatch_key: str) -> DispatchReceipt:
        execution_id = self._execution_id_from_key(assignment, dispatch_key)
        report = self.validate(assignment)
        if not report.valid:
            raise ValueError("Runtime assignment validation failed")
        existing = self._state_store.get(dispatch_key)
        if existing is not None:
            if existing.assignment.assignment_digest != assignment.assignment_digest:
                raise ValueError("Runtime dispatch key has a different assignment")
            return DispatchReceipt(
                dispatch_key=dispatch_key,
                runtime_execution_id=execution_id,
                assignment_digest=assignment.assignment_digest,
                handle=existing.handle,
                observation=existing.observation,
            )

        # A2 exposes only deterministic inline dispatch.  A real managed_async
        # path requires the durable LangGraph checkpointer/lifecycle backend.
        if assignment.execution_mode != "inline":
            raise ValueError("LangGraph managed_async dispatch is not enabled")
        observation = self._backend.execute(assignment)
        now = observation.observed_at
        provider_ref = "langgraph-thread:" + sha256(dispatch_key.encode()).hexdigest()[:32]
        handle = RuntimeExecutionHandle(
            runtime_execution_id=execution_id,
            runtime_version_id=assignment.runtime_version_id,
            provider_execution_ref=provider_ref,
            assignment_id=assignment.assignment_id,
            assignment_digest=assignment.assignment_digest,
            created_at=now,
            provider_generation="langgraph-v2-inline",
        )
        if observation.runtime_execution_id != execution_id:
            raise ValueError("Runtime backend returned an inconsistent execution identity")
        state = _ExecutionState(
            assignment=assignment,
            handle=handle,
            observation=observation,
            events=[
                RuntimeEvent(
                    event_id="dispatch",
                    runtime_execution_id=execution_id,
                    assignment_digest=assignment.assignment_digest,
                    sequence=0,
                    observation=observation,
                )
            ],
            lifecycle={},
        )
        # The injected store is the provider boundary; no control-plane UoW
        # or row lock is held during this operation.
        self._state_store.put(dispatch_key, state)
        return DispatchReceipt(
            dispatch_key=dispatch_key,
            runtime_execution_id=execution_id,
            assignment_digest=assignment.assignment_digest,
            handle=handle,
            observation=observation,
        )

    def inspect(self, handle: RuntimeExecutionHandle) -> RuntimeObservation:
        return self._state_for_handle(handle).observation

    def read_events(
        self, handle: RuntimeExecutionHandle, *, cursor: str | None, limit: int
    ) -> RuntimeEventPage:
        if not self._descriptor.capabilities.event_stream:
            raise ValueError("Runtime event stream is unsupported")
        if type(limit) is not int or isinstance(limit, bool) or not 1 <= limit <= 256:
            raise ValueError("Runtime event limit is invalid")
        try:
            position = int(cursor) if cursor is not None else -1
        except (TypeError, ValueError) as exc:
            raise ValueError("Runtime event cursor is invalid") from exc
        state = self._state_for_handle(handle)
        events = tuple(event for event in state.events if event.sequence > position)
        page = events[:limit]
        has_more = len(events) > len(page)
        return RuntimeEventPage(
            events=page,
            next_cursor=str(page[-1].sequence) if has_more and page else None,
            has_more=has_more,
        )

    def request_cancel(
        self, handle: RuntimeExecutionHandle, *, cancellation_id: str, deadline: datetime
    ) -> LifecycleReceipt:
        if self._descriptor.capabilities.cancel == "none":
            raise ValueError("Runtime cancellation is unsupported")
        return self._lifecycle(
            handle, operation="cancel", operation_id=cancellation_id, deadline=deadline
        )

    def request_pause(
        self, handle: RuntimeExecutionHandle, *, operation_id: str
    ) -> LifecycleReceipt:
        if not self._descriptor.capabilities.pause_resume:
            raise ValueError("Runtime pause is unsupported")
        return self._lifecycle(handle, operation="pause", operation_id=operation_id)

    def request_resume(
        self, handle: RuntimeExecutionHandle, *, operation_id: str
    ) -> LifecycleReceipt:
        if not self._descriptor.capabilities.pause_resume:
            raise ValueError("Runtime resume is unsupported")
        return self._lifecycle(handle, operation="resume", operation_id=operation_id)

    def close(self) -> None:
        return None

    def bind_context(
        self,
        assignment: RuntimeAssignment,
        task: Task,
        run: TaskRun,
        attempt: TaskAttempt,
        work_item: WorkflowWorkItem | None,
    ) -> None:
        self._backend.bind(assignment, task, run, attempt, work_item)

    def assignment_for(
        self,
        task: Task,
        run: TaskRun,
        attempt: TaskAttempt,
        *,
        work_item: WorkflowWorkItem | None = None,
    ) -> RuntimeAssignment:
        if (
            run.runtime_version_id is None
            or run.agent_version_id is None
            or run.agent_version_digest is None
        ):
            raise ValueError("Managed Runtime requires pinned Runtime and Agent Versions")
        input_value = work_item.input if work_item is not None else dict(task.input)
        objective = work_item.objective if work_item is not None else task.objective
        return RuntimeAssignment(
            assignment_id=str(uuid5(NAMESPACE_URL, f"agentmesh:assignment:{run.id}")),
            tenant_id=task.tenant_id,
            task_id=str(task.id),
            run_id=str(run.id),
            agent_definition_id=str(uuid5(NAMESPACE_URL, f"agentmesh:agent:{run.agent_id}")),
            agent_version_id=str(run.agent_version_id),
            agent_version_digest=run.agent_version_digest,
            runtime_version_id=str(run.runtime_version_id),
            runtime_descriptor_digest=self._descriptor.digest(),
            execution_mode="inline",
            run_role=run.role.value,
            revision=run.revision_number,
            objective=objective,
            structured_input=input_value,
            acceptance_contract={"criteria": [item.to_dict() for item in task.acceptance_criteria]},
            trace_context={"trace_id": attempt.trace_id},
            correlation_ids={
                "task_id": str(task.id),
                "run_id": str(run.id),
                "runtime_execution_id": str(run.runtime_execution_id or ""),
            },
        )

    def _lifecycle(
        self,
        handle: RuntimeExecutionHandle,
        *,
        operation: str,
        operation_id: str,
        deadline: datetime | None = None,
    ) -> LifecycleReceipt:
        state = self._state_for_handle(handle)
        existing = state.lifecycle.get(operation_id)
        if existing is not None:
            return existing
        self._lifecycle_controller.request(
            operation,
            handle,
            operation_id=operation_id,
            deadline=deadline,
        )
        phase = {
            "cancel": RuntimePhase.CANCEL_REQUESTED,
            "pause": RuntimePhase.PAUSE_REQUESTED,
            "resume": RuntimePhase.RUNNING,
        }[operation]
        receipt = LifecycleReceipt(
            operation_id=operation_id,
            runtime_execution_id=handle.runtime_execution_id,
            operation=operation,
            accepted=True,
            observed_phase=phase,
            safe_message="request accepted",
        )
        state.lifecycle[operation_id] = receipt
        return receipt

    def _state_for_handle(self, handle: RuntimeExecutionHandle) -> _ExecutionState:
        for state in self._state_store.values():
            if state.handle == handle:
                return state
        raise ValueError("Runtime execution handle is unknown")

    @staticmethod
    def _execution_id_from_key(assignment: RuntimeAssignment, dispatch_key: str) -> str:
        prefix = f"runtime-dispatch:{assignment.tenant_id}:"
        if type(dispatch_key) is not str or not dispatch_key.startswith(prefix):
            raise ValueError("Runtime dispatch key is invalid")
        value = dispatch_key[len(prefix) :]
        try:
            UUID(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("Runtime dispatch key is invalid") from exc
        return value
