import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from agentmesh.application.ports import ManagedRuntimeAuthoritativeResult
from agentmesh.application.registry_services import AgentRegistryService
from agentmesh.application.runtime_comparison import RuntimeComparisonSnapshot
from agentmesh.application.services import RunExecutionService, TaskApplicationService
from agentmesh.domain.coordination import CoordinatedPlan, SubtaskSpec
from agentmesh.domain.errors import (
    IdempotencyConflict,
    InvalidTaskInput,
    InvalidTaskTransition,
    RunLeaseUnavailable,
)
from agentmesh.domain.runtime_execution import (
    RuntimeExecution,
    RuntimeExecutionPhase,
    RuntimeObservationOutcome,
)
from agentmesh.domain.tasks import (
    AcceptanceCriterion,
    AcceptanceCriterionKind,
    AttemptStatus,
    RunStatus,
    Task,
    TaskExecutionMode,
    TaskStatus,
)
from agentmesh.features import FeatureGateSet
from agentmesh.orchestration.agent import (
    DeterministicAcceptanceReviewer,
    DeterministicAgentExecutor,
)
from agentmesh.orchestration.workflow import LangGraphWorkflowRunner
from agentmesh.runtime_sdk import (
    ErrorCategory,
    RetryDisposition,
    RuntimeError,
    RuntimeObservation,
    RuntimePhase,
    canonical_digest,
)
from tests.fakes import InMemoryUnitOfWorkFactory


class _FailingRuntimeAdmission:
    def prepare_execution_in_uow(self, uow, **kwargs):
        raise InvalidTaskTransition("runtime preparation failed")


class _SuccessfulRuntimeAdmission:
    def prepare_execution_in_uow(self, uow, *, run_id, **kwargs):
        run = uow.runs.get(run_id, for_update=True)
        assert run is not None
        execution_id = run.runtime_execution_intent_id or uuid4()
        run.bind_runtime_execution(execution_id)
        uow.runs.save(run)
        return type("PreparedExecution", (), {"id": execution_id})()


class _CountingManagedExecution:
    def __init__(self) -> None:
        self.calls = 0

    def execute_shadow(self, *args, **kwargs) -> RuntimeComparisonSnapshot:
        self.calls += 1
        return RuntimeComparisonSnapshot(
            terminal_state="SUCCEEDED", output={"shadow": True}, usage={}
        )


class _BuiltinRuntimeAdmission:
    def __init__(self, version_id=None) -> None:
        self.version_id = version_id or uuid4()
        self.calls = 0

    def require_builtin_langgraph_v2_in_uow(self, uow):
        self.calls += 1
        return type("BuiltinVersion", (), {"id": self.version_id})()


class _PoisonWorkflowRunner:
    def run(self, *args, **kwargs):
        raise AssertionError("legacy WorkflowRunner must not execute a managed Run")


class _AuthoritativeManagedExecution:
    def __init__(self, phase=RuntimePhase.SUCCEEDED, output=None, usage=None) -> None:
        self.phase = phase
        self.output = {"managed": True} if output is None else output
        self.usage = {} if usage is None else usage
        self.calls = 0

    def execute_authoritative(self, task, run, attempt, **kwargs):
        self.calls += 1
        execution_id = run.runtime_execution_id or run.runtime_execution_intent_id
        assignment_id = uuid4()
        digest = "a" * 64
        return ManagedRuntimeAuthoritativeResult(
            execution_id=execution_id,
            assignment_id=assignment_id,
            assignment_digest=digest,
            observation=RuntimeObservation(
                observation_id=str(uuid4()),
                runtime_execution_id=str(execution_id),
                assignment_id=str(assignment_id),
                assignment_digest=digest,
                phase=self.phase,
                observed_at=datetime.now(timezone.utc),
                provider_event_id="managed-test",
                output=self.output if self.phase is RuntimePhase.SUCCEEDED else None,
                usage=self.usage,
                error=(
                    RuntimeError(
                        code="runtime.provider_outcome_unknown",
                        category=ErrorCategory.UNKNOWN,
                        message="provider outcome unknown",
                        retry_disposition=RetryDisposition.RECONCILE,
                    )
                    if self.phase in {RuntimePhase.OUTCOME_UNKNOWN, RuntimePhase.LOST}
                    else None
                ),
            ),
            dispatch_crossed=True,
        )


class _AtomicRuntimeRegistry:
    def __init__(self) -> None:
        self.calls = 0
        self.execution = None

    def record_observation_in_uow(self, uow, **kwargs):
        self.calls += 1
        return RuntimeObservationOutcome.APPLIED

    def get_execution_for_run(self, run_id):
        return self.execution


def _execution_service_with_gates(uow_factory, gates, managed):
    workflow = LangGraphWorkflowRunner(
        agent_executor=DeterministicAgentExecutor(),
        reviewer_executor=DeterministicAcceptanceReviewer(),
        checkpointer=InMemorySaver(),
    )
    return RunExecutionService(
        uow_factory=uow_factory,
        workflow_runner=workflow,
        managed_execution_service=managed,
        worker_id="gate-test-worker",
        consumer_name="gate-test-consumer",
        lease_duration=timedelta(minutes=5),
        executor_agent_id="test-agent",
        reviewer_agent_id="test-reviewer",
        supervisor_agent_id="test-supervisor",
        feature_gates=gates,
    )


def test_request_and_execute_task(
    task_service: TaskApplicationService,
    execution_service: RunExecutionService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    created = task_service.create_task(
        "Explain the AgentMesh execution path",
        {"format": "short"},
    )

    queued = task_service.request_run(created.task.id)

    assert queued.task.status == TaskStatus.READY
    assert queued.runs[0].status == RunStatus.QUEUED
    assert queued.runs[0].agent_version_id is not None
    assert queued.runs[0].agent_version_digest is not None
    assert len(uow_factory.store.outbox) == 1

    assert execution_service.process(uow_factory.store.outbox[0]) is True
    completed = task_service.get_task(created.task.id)

    assert completed.task.status == TaskStatus.COMPLETED
    assert completed.task.output is not None
    assert completed.task.output["agent"]["id"] == "test-agent"
    assert completed.task.output["input"] == {"format": "short"}
    assert completed.runs[0].status == RunStatus.SUCCEEDED
    assert completed.attempts[0].status == AttemptStatus.SUCCEEDED


def test_direct_cutover_admits_new_run_with_builtin_v2_and_stable_intent(
    uow_factory: InMemoryUnitOfWorkFactory,
    registry_service: AgentRegistryService,
) -> None:
    runtime = _BuiltinRuntimeAdmission()
    service = TaskApplicationService(
        uow_factory=uow_factory,
        agent_id="test-agent",
        tenant_id="test-tenant",
        feature_gates=FeatureGateSet.from_config(
            "full",
            "managed_agent_runtime=true,managed_runtime_worker=true,"
            "managed_runtime_direct_cutover=true",
        ),
        runtime_registry_service=runtime,
    )
    task = service.create_task("direct managed admission", execution_mode=TaskExecutionMode.DIRECT)
    admitted = service.request_run(task.task.id)
    run = admitted.runs[0]
    assert run.runtime_authority == "managed"
    assert run.comparison_mode == "off"
    assert run.runtime_version_id == runtime.version_id
    assert run.runtime_execution_intent_id is not None
    assert run.runtime_execution_id is None
    assert runtime.calls == 1


def test_worker_uses_persisted_managed_authority_and_never_legacy(
    uow_factory: InMemoryUnitOfWorkFactory,
    registry_service: AgentRegistryService,
) -> None:
    admission = _BuiltinRuntimeAdmission()
    tasks = TaskApplicationService(
        uow_factory=uow_factory,
        agent_id="test-agent",
        tenant_id="test-tenant",
        feature_gates=FeatureGateSet.from_config(
            "full",
            "managed_agent_runtime=true,managed_runtime_worker=true,"
            "managed_runtime_direct_cutover=true",
        ),
        runtime_registry_service=admission,
    )
    task_id = tasks.create_task("managed authority").task.id
    run = tasks.request_run(task_id).runs[0]
    envelope = uow_factory.store.outbox[-1]
    managed = _AuthoritativeManagedExecution()
    registry = _AtomicRuntimeRegistry()
    worker = RunExecutionService(
        uow_factory=uow_factory,
        workflow_runner=_PoisonWorkflowRunner(),
        managed_execution_service=managed,
        runtime_registry_service=registry,
        worker_id="managed-worker",
        consumer_name="managed-worker-v1",
        lease_duration=timedelta(minutes=5),
        # Rollout gates are deliberately off: persisted authority wins.
        feature_gates=FeatureGateSet.from_config("minimal"),
    )

    assert run.runtime_authority == "managed"
    assert worker.process(envelope) is True
    completed = tasks.get_task(task_id)
    assert completed.task.status is TaskStatus.COMPLETED
    assert completed.task.output == {"managed": True}
    assert completed.runs[0].status is RunStatus.SUCCEEDED
    assert completed.attempts[0].status is AttemptStatus.SUCCEEDED
    assert managed.calls == 1
    assert registry.calls == 1
    assert worker.process(envelope) is False
    assert managed.calls == 1


def test_managed_run_missing_service_fails_before_state_mutation(
    uow_factory: InMemoryUnitOfWorkFactory,
    registry_service: AgentRegistryService,
) -> None:
    tasks = TaskApplicationService(
        uow_factory=uow_factory,
        agent_id="test-agent",
        tenant_id="test-tenant",
        feature_gates=FeatureGateSet.from_config(
            "full",
            "managed_agent_runtime=true,managed_runtime_worker=true,"
            "managed_runtime_direct_cutover=true",
        ),
        runtime_registry_service=_BuiltinRuntimeAdmission(),
    )
    task_id = tasks.create_task("missing managed service").task.id
    tasks.request_run(task_id)
    envelope = uow_factory.store.outbox[-1]
    worker = RunExecutionService(
        uow_factory=uow_factory,
        workflow_runner=_PoisonWorkflowRunner(),
        worker_id="managed-worker",
        consumer_name="managed-worker-v1",
        lease_duration=timedelta(minutes=5),
    )

    with pytest.raises(InvalidTaskInput, match="unavailable"):
        worker.process(envelope)

    unchanged = tasks.get_task(task_id)
    assert unchanged.task.status is TaskStatus.READY
    assert unchanged.runs[0].status is RunStatus.QUEUED
    assert unchanged.attempts == []


def test_unknown_managed_outcome_parks_once_without_redispatch(
    uow_factory: InMemoryUnitOfWorkFactory,
    registry_service: AgentRegistryService,
) -> None:
    tasks = TaskApplicationService(
        uow_factory=uow_factory,
        agent_id="test-agent",
        tenant_id="test-tenant",
        feature_gates=FeatureGateSet.from_config(
            "full",
            "managed_agent_runtime=true,managed_runtime_worker=true,"
            "managed_runtime_direct_cutover=true",
        ),
        runtime_registry_service=_BuiltinRuntimeAdmission(),
    )
    task_id = tasks.create_task("uncertain managed authority").task.id
    tasks.request_run(task_id)
    envelope = uow_factory.store.outbox[-1]
    managed = _AuthoritativeManagedExecution(phase=RuntimePhase.OUTCOME_UNKNOWN)
    worker = RunExecutionService(
        uow_factory=uow_factory,
        workflow_runner=_PoisonWorkflowRunner(),
        managed_execution_service=managed,
        runtime_registry_service=_AtomicRuntimeRegistry(),
        worker_id="managed-worker",
        consumer_name="managed-worker-v1",
        lease_duration=timedelta(minutes=5),
    )

    assert worker.process(envelope) is True
    parked = tasks.get_task(task_id)
    assert parked.task.status is TaskStatus.RECONCILIATION_REQUIRED
    assert parked.runs[0].status is RunStatus.RECONCILIATION_REQUIRED
    assert parked.attempts[0].status is AttemptStatus.OUTCOME_UNKNOWN
    events = [
        item
        for item in uow_factory.store.outbox
        if item.schema_name == "agentmesh.runtime.reconciliation.required"
    ]
    assert len(events) == 1
    assert events[0].payload["reason_code"] == "runtime.provider_outcome_unknown"
    assert worker.process(envelope) is False
    assert managed.calls == 1
    assert len(
        [
            item
            for item in uow_factory.store.outbox
            if item.schema_name == "agentmesh.runtime.reconciliation.required"
        ]
    ) == 1


def test_expired_dispatching_owner_parks_before_replacement_attempt() -> None:
    uow_factory = InMemoryUnitOfWorkFactory()
    agents = AgentRegistryService(uow_factory=uow_factory, tenant_id="test-tenant")
    agents.ensure_builtin_agent("test-agent")
    tasks = TaskApplicationService(
        uow_factory=uow_factory,
        agent_id="test-agent",
        tenant_id="test-tenant",
        feature_gates=FeatureGateSet.from_config(
            "full",
            "managed_agent_runtime=true,managed_runtime_worker=true,"
            "managed_runtime_direct_cutover=true",
        ),
        runtime_registry_service=_BuiltinRuntimeAdmission(),
    )
    task_id = tasks.create_task("recover crossed dispatch").task.id
    run = tasks.request_run(task_id).runs[0]
    envelope = uow_factory.store.outbox[-1]
    registry = _AtomicRuntimeRegistry()
    managed = _AuthoritativeManagedExecution()
    worker = RunExecutionService(
        uow_factory=uow_factory,
        workflow_runner=_PoisonWorkflowRunner(),
        managed_execution_service=managed,
        runtime_registry_service=registry,
        worker_id="managed-worker",
        consumer_name="managed-worker-v1",
        lease_duration=timedelta(seconds=-1),
    )
    _task, _run, expired_owner = worker._acquire(
        envelope, task_id=task_id, run_id=run.id
    )
    execution_id = run.runtime_execution_intent_id
    execution = RuntimeExecution.prepare(
        tenant_id="test-tenant",
        run_id=run.id,
        runtime_version_id=run.runtime_version_id,
        assignment_id=uuid4(),
        assignment_digest="a" * 64,
        dispatch_key=f"runtime-dispatch:test-tenant:{execution_id}",
        dispatch_digest=canonical_digest({"execution": str(execution_id)}),
        execution_id=execution_id,
    ).claim(
        attempt_id=expired_owner.id,
        fencing_token=expired_owner.fencing_token,
        expected_owner_attempt_id=None,
        expected_fencing_token=None,
        expected_version=1,
    )
    registry.execution = execution.apply_observation(
        phase=RuntimeExecutionPhase.DISPATCHING,
        provider_sequence=None,
    )

    assert worker.process(envelope) is True
    parked = tasks.get_task(task_id)
    assert [attempt.status for attempt in parked.attempts] == [AttemptStatus.OUTCOME_UNKNOWN]
    assert parked.task.status is TaskStatus.RECONCILIATION_REQUIRED
    assert parked.runs[0].status is RunStatus.RECONCILIATION_REQUIRED
    assert managed.calls == 0
    assert registry.calls == 1


def test_managed_success_with_usage_fails_control_plane_result() -> None:
    uow_factory = InMemoryUnitOfWorkFactory()
    registry = AgentRegistryService(uow_factory=uow_factory, tenant_id="test-tenant")
    registry.ensure_builtin_agent("test-agent")
    admission = _BuiltinRuntimeAdmission()
    tasks = TaskApplicationService(
        uow_factory=uow_factory,
        agent_id="test-agent",
        tenant_id="test-tenant",
        feature_gates=FeatureGateSet.from_config(
            "full",
            "managed_agent_runtime=true,managed_runtime_worker=true,"
            "managed_runtime_direct_cutover=true",
        ),
        runtime_registry_service=admission,
    )
    task_id = tasks.create_task("reject unpriced usage").task.id
    tasks.request_run(task_id)
    envelope = uow_factory.store.outbox[-1]
    worker = RunExecutionService(
        uow_factory=uow_factory,
        workflow_runner=_PoisonWorkflowRunner(),
        managed_execution_service=_AuthoritativeManagedExecution(usage={"total": 1}),
        runtime_registry_service=_AtomicRuntimeRegistry(),
        worker_id="managed-worker",
        consumer_name="managed-worker-v1",
        lease_duration=timedelta(minutes=5),
    )

    assert worker.process(envelope) is True
    rejected = tasks.get_task(task_id)
    assert rejected.task.status is TaskStatus.FAILED
    assert rejected.task.error == "runtime.authoritative_result_rejected"
    assert rejected.runs[0].status is RunStatus.FAILED
    assert rejected.attempts[0].status is AttemptStatus.FAILED


def test_direct_cutover_gate_off_keeps_new_runs_legacy_and_existing_managed(
    uow_factory: InMemoryUnitOfWorkFactory,
    registry_service: AgentRegistryService,
) -> None:
    runtime = _BuiltinRuntimeAdmission()
    gated = TaskApplicationService(
        uow_factory=uow_factory,
        agent_id="test-agent",
        tenant_id="test-tenant",
        feature_gates=FeatureGateSet.from_config(
            "full",
            "managed_agent_runtime=true,managed_runtime_worker=true,"
            "managed_runtime_direct_cutover=true",
        ),
        runtime_registry_service=runtime,
    )
    first = gated.create_task("managed then rollback").task
    managed = gated.request_run(first.id).runs[0]
    assert managed.runtime_authority == "managed"

    legacy_service = TaskApplicationService(
        uow_factory=uow_factory,
        agent_id="test-agent",
        tenant_id="test-tenant",
        feature_gates=FeatureGateSet.from_config("full"),
    )
    assert legacy_service.get_task(first.id).runs[0].runtime_authority == "managed"

    second = legacy_service.create_task("legacy after rollback").task
    legacy = legacy_service.request_run(second.id).runs[0]
    assert legacy.runtime_authority == "legacy"
    assert legacy.runtime_execution_intent_id is None

    assert runtime.calls == 1


def test_direct_cutover_does_not_switch_reviewed_or_coordinated_runs(
    uow_factory: InMemoryUnitOfWorkFactory,
    registry_service: AgentRegistryService,
) -> None:
    runtime = _BuiltinRuntimeAdmission()
    service = TaskApplicationService(
        uow_factory=uow_factory,
        agent_id="test-agent",
        tenant_id="test-tenant",
        feature_gates=FeatureGateSet.from_config(
            "full",
            "managed_agent_runtime=true,managed_runtime_worker=true,"
            "managed_runtime_direct_cutover=true",
        ),
        runtime_registry_service=runtime,
    )
    reviewed = service.create_task(
        "reviewed remains legacy",
        execution_mode=TaskExecutionMode.REVIEWED,
        acceptance_criteria=(
            AcceptanceCriterion.create(
                key="summary",
                description="A summary is present",
                kind=AcceptanceCriterionKind.OUTPUT_PATH_EXISTS,
                path=("summary",),
            ),
        ),
    ).task
    reviewed_run = service.request_run(reviewed.id).runs[0]
    assert reviewed_run.runtime_authority == "legacy"

    coordinated = service.create_task(
        "coordinated remains scheduler-owned",
        execution_mode=TaskExecutionMode.COORDINATED,
        coordinated_plan=CoordinatedPlan.create(
            (
                SubtaskSpec.create(key="one", objective="First subtask"),
                SubtaskSpec.create(key="two", objective="Second subtask"),
            ),
            max_concurrency=2,
        ),
    )
    coordinated_runs = service.request_run(coordinated.task.id).runs
    assert coordinated_runs
    assert all(run.runtime_authority == "legacy" for run in coordinated_runs)

    federated = Task.create(
        tenant_id="test-tenant",
        objective="federated remains delegation-owned",
        execution_mode=TaskExecutionMode.FEDERATED,
    )
    with uow_factory() as uow:
        uow.tasks.add(federated)
        uow.commit()
    with pytest.raises(InvalidTaskInput, match="A2A delegation endpoint"):
        service.request_run(federated.id)
    assert runtime.calls == 0


def test_deterministic_admission_rolls_back_run_queue_and_outbox(
    uow_factory: InMemoryUnitOfWorkFactory,
    registry_service: AgentRegistryService,
) -> None:
    service = TaskApplicationService(
        uow_factory=uow_factory,
        agent_id="test-agent",
        tenant_id="test-tenant",
        feature_gates=FeatureGateSet.from_config(
            "full",
            "managed_agent_runtime=true,managed_runtime_worker=true,dual_record_runtime=true",
        ),
        runtime_registry_service=_FailingRuntimeAdmission(),
    )
    task_id = service.create_task("atomic runtime admission").task.id
    with pytest.raises(InvalidTaskTransition, match="runtime preparation failed"):
        service.request_run(
            task_id,
            runtime_version_id=uuid4(),
            comparison_mode="deterministic_shadow",
            assignment_id=uuid4(),
            assignment_digest="a" * 64,
        )
    assert not uow_factory.store.runs
    assert not uow_factory.store.outbox
    assert uow_factory.store.tasks[task_id].status is TaskStatus.CREATED
    assert not uow_factory.store.idempotency


def test_deterministic_admission_binds_before_outbox_and_keeps_legacy_authority(
    uow_factory: InMemoryUnitOfWorkFactory,
    registry_service: AgentRegistryService,
) -> None:
    service = TaskApplicationService(
        uow_factory=uow_factory,
        agent_id="test-agent",
        tenant_id="test-tenant",
        feature_gates=FeatureGateSet.from_config(
            "full",
            "managed_agent_runtime=true,managed_runtime_worker=true,dual_record_runtime=true",
        ),
        runtime_registry_service=_SuccessfulRuntimeAdmission(),
    )
    task_id = service.create_task("atomic runtime admission success").task.id
    admitted = service.request_run(
        task_id,
        runtime_version_id=uuid4(),
        comparison_mode="deterministic_shadow",
        assignment_id=uuid4(),
        assignment_digest="a" * 64,
    )
    assert admitted.runs[0].runtime_execution_id is not None
    assert uow_factory.store.outbox
    assert uow_factory.store.runs[admitted.runs[0].id].runtime_execution_id is not None

    managed = _CountingManagedExecution()
    worker = _execution_service_with_gates(
        uow_factory,
        FeatureGateSet.from_config(
            "full",
            "managed_agent_runtime=true,managed_runtime_worker=true,dual_record_runtime=true",
        ),
        managed,
    )
    assert worker.process(uow_factory.store.outbox[-1]) is True
    assert managed.calls == 1
    completed = service.get_task(task_id)
    assert completed.task.output is not None
    assert completed.task.output["agent"]["id"] == "test-agent"
    assert len(uow_factory.store.runtime_comparisons) == 1
    assert sum(
        item.schema_name == "agentmesh.runtime.comparison.recorded"
        for item in uow_factory.store.outbox
    ) == 1


def test_worker_gate_off_never_calls_managed_runtime(
    task_service: TaskApplicationService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    managed = _CountingManagedExecution()
    worker = _execution_service_with_gates(
        uow_factory, FeatureGateSet.from_config("minimal"), managed
    )
    task_id = task_service.create_task("legacy-only worker").task.id
    task_service.request_run(task_id)

    assert worker.process(uow_factory.store.outbox[-1]) is True
    assert managed.calls == 0


def test_worker_gate_changes_do_not_admit_an_existing_off_run(
    task_service: TaskApplicationService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    managed = _CountingManagedExecution()
    gates = FeatureGateSet.from_config(
        "full",
        "managed_agent_runtime=true,managed_runtime_worker=true,dual_record_runtime=true",
    )
    worker = _execution_service_with_gates(uow_factory, gates, managed)
    task_id = task_service.create_task("snapshotted legacy run").task.id
    task_service.request_run(task_id)

    assert worker.process(uow_factory.store.outbox[-1]) is True
    assert managed.calls == 0


def test_duplicate_delivery_is_ignored_after_inbox_commit(
    task_service: TaskApplicationService,
    execution_service: RunExecutionService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    task_id = task_service.create_task("Exactly once business effect").task.id
    task_service.request_run(task_id)
    envelope = uow_factory.store.outbox[0]

    assert execution_service.process(envelope) is True
    assert execution_service.process(envelope) is False
    assert len(uow_factory.store.attempts) == 1


def test_run_request_is_not_repeatable_after_queue(task_service: TaskApplicationService) -> None:
    task_id = task_service.create_task("Only once").task.id
    task_service.request_run(task_id)

    with pytest.raises(InvalidTaskTransition):
        task_service.request_run(task_id)


def test_idempotency_key_replays_same_run(task_service: TaskApplicationService) -> None:
    task_id = task_service.create_task("Idempotent run").task.id
    first = task_service.request_run(task_id, idempotency_key="request-1")
    replay = task_service.request_run(task_id, idempotency_key="request-1")

    assert replay.task.id == first.task.id
    assert replay.runs[0].id == first.runs[0].id


def test_idempotency_key_cannot_be_reused_for_another_task(
    task_service: TaskApplicationService,
) -> None:
    first = task_service.create_task("First").task.id
    second = task_service.create_task("Second").task.id
    task_service.request_run(first, idempotency_key="shared-key")

    with pytest.raises(IdempotencyConflict):
        task_service.request_run(second, idempotency_key="shared-key")


def test_task_creation_idempotency_key_replays_same_task(
    task_service: TaskApplicationService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    first = task_service.create_task(
        "Run the scheduled operation",
        {"occurrence": "2026-07-29T12:00:00Z"},
        idempotency_key="operation:daily-report:occurrence:2026-07-29",
    )
    replay = task_service.create_task(
        "Run the scheduled operation",
        {"occurrence": "2026-07-29T12:00:00Z"},
        idempotency_key="operation:daily-report:occurrence:2026-07-29",
    )

    assert replay.task.id == first.task.id
    assert len(uow_factory.store.tasks) == 1


def test_task_creation_idempotency_key_rejects_different_input(
    task_service: TaskApplicationService,
) -> None:
    task_service.create_task(
        "Run the scheduled operation",
        {"occurrence": "first"},
        idempotency_key="operation:daily-report:occurrence:shared",
    )

    with pytest.raises(IdempotencyConflict):
        task_service.create_task(
            "Run a different operation",
            {"occurrence": "second"},
            idempotency_key="operation:daily-report:occurrence:shared",
        )


def test_run_keeps_immutable_agent_version_when_default_changes(
    task_service: TaskApplicationService,
    registry_service: AgentRegistryService,
) -> None:
    first_task = task_service.create_task("Use the original Agent Version")
    first_run = task_service.request_run(first_task.task.id).runs[0]
    definition = next(
        item.definition
        for item in registry_service.list_definitions()
        if item.definition.name == "test-agent"
    )
    next_version = registry_service.create_version(
        definition.id,
        semantic_version="0.2.0",
        role="General task executor",
        instructions="Complete the task using the new immutable version.",
        declared_capabilities=["general.task"],
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        runtime_adapter="deterministic-local",
        execution_modes=["async"],
    )
    registry_service.submit_version(next_version.id)
    next_version = registry_service.publish_version(
        next_version.id,
        verified_capabilities=["general.task"],
        make_default=True,
    )

    persisted_first_run = task_service.get_task(first_task.task.id).runs[0]
    second_task = task_service.create_task("Use the new Agent Version")
    second_run = task_service.request_run(second_task.task.id).runs[0]

    assert persisted_first_run.agent_version_id == first_run.agent_version_id
    assert persisted_first_run.agent_version_digest == first_run.agent_version_digest
    assert second_run.agent_version_id == next_version.id
    assert second_run.agent_version_digest == next_version.content_digest
    affected = registry_service.list_affected_active_runs(first_run.agent_version_id)
    assert [run.id for run in affected] == [first_run.id]


def test_list_tasks_batch_loads_child_collections(
    task_service: TaskApplicationService,
    execution_service: RunExecutionService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    assert task_service.list_tasks(limit=10, offset=100) == []
    assert uow_factory.store.run_list_for_tasks_calls == 0
    assert uow_factory.store.attempt_list_for_tasks_calls == 0

    created_only = task_service.create_task("Created only")
    queued_task = task_service.create_task("Queued only")
    queued = task_service.request_run(queued_task.task.id)
    completed_task = task_service.create_task("Completed")
    completed_run = task_service.request_run(completed_task.task.id)
    wakeup = next(
        envelope
        for envelope in reversed(uow_factory.store.outbox)
        if envelope.payload["run_id"] == str(completed_run.runs[0].id)
    )
    assert execution_service.process(wakeup) is True

    values = task_service.list_tasks(limit=10, offset=0)
    by_id = {value.task.id: value for value in values}

    assert by_id[created_only.task.id].runs == []
    assert by_id[created_only.task.id].attempts == []
    assert [run.id for run in by_id[queued_task.task.id].runs] == [queued.runs[0].id]
    assert by_id[queued_task.task.id].attempts == []
    assert [run.id for run in by_id[completed_task.task.id].runs] == [completed_run.runs[0].id]
    assert len(by_id[completed_task.task.id].attempts) == 1
    assert uow_factory.store.run_list_for_task_calls == 0
    assert uow_factory.store.attempt_list_for_task_calls == 0
    assert uow_factory.store.run_list_for_tasks_calls == 1
    assert uow_factory.store.attempt_list_for_tasks_calls == 1


def test_queued_task_pause_consumes_old_wakeup_then_resume_completes(
    task_service: TaskApplicationService,
    execution_service: RunExecutionService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    task_id = task_service.create_task("Pause queued work").task.id
    queued = task_service.request_run(task_id)
    original_wakeup = uow_factory.store.outbox[0]

    paused = task_service.pause_task(task_id)
    outbox_size = len(uow_factory.store.outbox)
    paused_again = task_service.pause_task(task_id)

    assert paused.task.status == TaskStatus.PAUSED
    assert paused.runs[0].status == RunStatus.PAUSED
    assert paused.runs[0].paused_at is not None
    assert paused_again.task.status == TaskStatus.PAUSED
    assert len(uow_factory.store.outbox) == outbox_size
    assert execution_service.process(original_wakeup) is False
    assert not uow_factory.store.attempts

    resumed = task_service.resume_task(task_id)
    resume_wakeup = next(
        item
        for item in reversed(uow_factory.store.outbox)
        if item.schema_name == original_wakeup.schema_name
        and item.message_id != original_wakeup.message_id
    )
    outbox_size = len(uow_factory.store.outbox)
    resumed_again = task_service.resume_task(task_id)

    assert resumed.task.status == TaskStatus.READY
    assert resumed.runs[0].status == RunStatus.QUEUED
    assert resumed.runs[0].resumed_at is not None
    assert resumed_again.task.status == TaskStatus.READY
    assert len(uow_factory.store.outbox) == outbox_size
    assert execution_service.process(resume_wakeup) is True
    assert task_service.get_task(task_id).task.status == TaskStatus.COMPLETED
    assert queued.runs[0].id == resumed.runs[0].id


class _PauseOnceExecutor:
    def __init__(self, task_service: TaskApplicationService) -> None:
        self._task_service = task_service
        self.calls = 0

    def execute(self, *, objective, input, context):
        self.calls += 1
        if self.calls == 1:
            self._task_service.pause_task(context.task_id)
        return {"objective": objective, "input": dict(input), "checkpointed": True}


def test_running_task_resumes_from_checkpoint_without_reexecuting_agent(
    task_service: TaskApplicationService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    executor = _PauseOnceExecutor(task_service)
    workflow = LangGraphWorkflowRunner(
        agent_executor=executor,
        checkpointer=InMemorySaver(),
    )
    execution_service = RunExecutionService(
        uow_factory=uow_factory,
        workflow_runner=workflow,
        worker_id="pause-worker",
        consumer_name="pause-worker-v1",
        lease_duration=timedelta(minutes=5),
    )
    task_id = task_service.create_task("Pause after checkpoint").task.id
    task_service.request_run(task_id)
    original_wakeup = uow_factory.store.outbox[0]

    assert execution_service.process(original_wakeup) is True
    paused = task_service.get_task(task_id)
    assert paused.task.status == TaskStatus.PAUSED
    assert paused.task.output is None
    assert paused.runs[0].status == RunStatus.PAUSED
    assert paused.attempts[0].status == AttemptStatus.PAUSED

    task_service.resume_task(task_id)
    resume_wakeup = next(
        item
        for item in reversed(uow_factory.store.outbox)
        if item.schema_name == original_wakeup.schema_name
        and item.message_id != original_wakeup.message_id
    )
    assert execution_service.process(resume_wakeup) is True

    completed = task_service.get_task(task_id)
    assert completed.task.status == TaskStatus.COMPLETED
    assert completed.task.output == {
        "objective": "Pause after checkpoint",
        "input": {},
        "checkpointed": True,
    }
    assert [attempt.status for attempt in completed.attempts] == [
        AttemptStatus.PAUSED,
        AttemptStatus.SUCCEEDED,
    ]
    assert [attempt.fencing_token for attempt in completed.attempts] == [1, 2]
    assert executor.calls == 1


def test_expired_attempt_converges_pause_request_without_reexecution(
    task_service: TaskApplicationService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    executor = _PauseOnceExecutor(task_service)
    execution_service = RunExecutionService(
        uow_factory=uow_factory,
        workflow_runner=LangGraphWorkflowRunner(
            agent_executor=executor,
            checkpointer=InMemorySaver(),
        ),
        worker_id="expired-worker",
        consumer_name="expired-worker-v1",
        lease_duration=timedelta(seconds=-1),
    )
    task_id = task_service.create_task("Recover a paused crashed worker").task.id
    queued = task_service.request_run(task_id)
    wakeup = uow_factory.store.outbox[0]
    assert (
        execution_service._acquire(
            wakeup,
            task_id=task_id,
            run_id=queued.runs[0].id,
        )
        is not None
    )

    requested = task_service.pause_task(task_id)
    assert requested.task.status == TaskStatus.PAUSE_REQUESTED
    assert execution_service.process(wakeup) is False

    paused = task_service.get_task(task_id)
    assert paused.task.status == TaskStatus.PAUSED
    assert paused.runs[0].status == RunStatus.PAUSED
    assert paused.attempts[0].status == AttemptStatus.LEASE_EXPIRED
    assert executor.calls == 0

    with pytest.raises(RunLeaseUnavailable):
        execution_service._finalize_success(
            wakeup,
            task_id,
            queued.runs[0].id,
            paused.attempts[0].id,
            {"late": True},
        )
    assert task_service.get_task(task_id).task.status == TaskStatus.PAUSED


class _SlowWorkflowRunner:
    def __init__(self, sleep_seconds: float) -> None:
        self._sleep_seconds = sleep_seconds

    def run(self, task, run, attempt):
        from agentmesh.application.ports import WorkflowExecutionResult

        time.sleep(self._sleep_seconds)
        return WorkflowExecutionResult(output={"renewed": True})


def test_running_attempt_lease_is_renewed_while_workflow_runs(
    task_service: TaskApplicationService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    execution_service = RunExecutionService(
        uow_factory=uow_factory,
        workflow_runner=_SlowWorkflowRunner(sleep_seconds=0.12),
        worker_id="renew-worker",
        consumer_name="renew-worker-v1",
        lease_duration=timedelta(seconds=1),
        lease_renewal_interval=timedelta(seconds=0.02),
    )
    task_id = task_service.create_task("Renew a running lease").task.id
    task_service.request_run(task_id)
    wakeup = uow_factory.store.outbox[0]

    assert execution_service.process(wakeup) is True

    completed = task_service.get_task(task_id)
    attempt = completed.attempts[0]
    assert completed.task.status == TaskStatus.COMPLETED
    assert attempt.status == AttemptStatus.SUCCEEDED
    assert attempt.heartbeat_at > attempt.started_at


def test_default_lease_renewal_interval_is_before_expiry() -> None:
    assert RunExecutionService._default_renewal_interval(timedelta(seconds=1)) == timedelta(
        seconds=1 / 3
    )


def test_attempt_lease_renewal_requires_current_live_owner(
    task_service: TaskApplicationService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    execution_service = RunExecutionService(
        uow_factory=uow_factory,
        workflow_runner=_SlowWorkflowRunner(sleep_seconds=0),
        worker_id="renew-worker",
        consumer_name="renew-worker-v1",
        lease_duration=timedelta(seconds=1),
    )
    task_id = task_service.create_task("Reject stale renewal").task.id
    queued = task_service.request_run(task_id)
    wakeup = uow_factory.store.outbox[0]
    task, run, attempt = execution_service._acquire(
        wakeup,
        task_id=task_id,
        run_id=queued.runs[0].id,
    )
    del task, run
    original_expires_at = attempt.lease_expires_at

    assert (
        execution_service._renew_attempt_lease(
            run_id=queued.runs[0].id,
            attempt_id=attempt.id,
            lease_token=attempt.lease_token,
        )
        is True
    )
    renewed = task_service.get_task(task_id).attempts[0]
    assert renewed.lease_expires_at >= original_expires_at

    with uow_factory() as uow:
        renewed.lease_expires_at = renewed.started_at - timedelta(seconds=1)
        uow.attempts.save(renewed)
        uow.commit()

    assert (
        execution_service._renew_attempt_lease(
            run_id=queued.runs[0].id,
            attempt_id=attempt.id,
            lease_token=attempt.lease_token,
        )
        is False
    )
