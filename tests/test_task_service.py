import time
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from agentmesh.application.ports import (
    ManagedRuntimeAuthoritativeResult,
    ManagedRuntimeControlPlaneFailure,
    WorkflowExecutionResult,
)
from agentmesh.application.quota_services import QuotaController, QuotaPolicyService
from agentmesh.application.registry_services import AgentRegistryService
from agentmesh.application.runtime_comparison import RuntimeComparisonSnapshot
from agentmesh.application.runtime_conflicts import (
    build_managed_runtime_conflict_observation,
)
from agentmesh.application.services import RunExecutionService, TaskApplicationService
from agentmesh.domain.budgets import BudgetSettlementSource, TaskBudget
from agentmesh.domain.coordination import CoordinatedPlan, SubtaskSpec
from agentmesh.domain.errors import (
    IdempotencyConflict,
    InvalidMessage,
    InvalidTaskInput,
    InvalidTaskTransition,
    RunLeaseUnavailable,
)
from agentmesh.domain.messaging import MessageEnvelope
from agentmesh.domain.quotas import QuotaScope
from agentmesh.domain.runtime_execution import (
    RuntimeExecution,
    RuntimeExecutionPhase,
    RuntimeObservationOutcome,
    RuntimeTrustProfile,
    RuntimeVersionStatus,
)
from agentmesh.domain.tasks import (
    AcceptanceCriterion,
    AcceptanceCriterionKind,
    AttemptStatus,
    RunRole,
    RunStatus,
    Task,
    TaskExecutionMode,
    TaskRun,
    TaskStatus,
    utc_now,
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
from agentmesh.runtime_sdk.builtin import (
    LANGGRAPH_V2_DESCRIPTOR,
    builtin_langgraph_runtime_id,
    builtin_langgraph_version_id,
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
        self.version_id = version_id or builtin_langgraph_version_id("v2")
        self.calls = 0

    def require_builtin_langgraph_v2_in_uow(self, uow):
        self.calls += 1
        return type(
            "BuiltinVersion",
            (),
            {
                "id": self.version_id,
                "runtime_id": builtin_langgraph_runtime_id(),
                "status": RuntimeVersionStatus.PUBLISHED,
                "api_version": 1,
                "adapter_kind": "python-in-process",
                "descriptor": LANGGRAPH_V2_DESCRIPTOR,
                "configuration_digest": canonical_digest(
                    {
                        "runtime_key": LANGGRAPH_V2_DESCRIPTOR["runtime_key"],
                        "capabilities": LANGGRAPH_V2_DESCRIPTOR["capabilities"],
                        "limits": LANGGRAPH_V2_DESCRIPTOR["limits"],
                    }
                ),
                "artifact_digest": canonical_digest(
                    {
                        "package": "agentmesh",
                        "runtime": "agentmesh.langgraph",
                        "release": "v2",
                    }
                ),
                "trust_profile": RuntimeTrustProfile.BUILT_IN,
                "compatibility": {},
            },
        )()


class _PoisonWorkflowRunner:
    def run(self, *args, **kwargs):
        raise AssertionError("legacy WorkflowRunner must not execute a managed Run")


class _CapturingWorkflowRunner:
    def __init__(self, output):
        self.output = output
        self.work_items = []

    def run(self, *args, **kwargs):
        self.work_items.append(kwargs.get("work_item"))
        return WorkflowExecutionResult(output=dict(self.output))


class _AuthoritativeManagedExecution:
    def __init__(
        self,
        phase=RuntimePhase.SUCCEEDED,
        output=None,
        usage=None,
        registry=None,
        result_assignment_id=None,
        result_assignment_digest=None,
        observed_at=None,
        work_items=None,
    ) -> None:
        self.phase = phase
        self.output = {"managed": True} if output is None else output
        self.usage = {} if usage is None else usage
        self.registry = registry
        self.result_assignment_id = result_assignment_id
        self.result_assignment_digest = result_assignment_digest
        self.observed_at = observed_at
        self.calls = 0
        self.work_items = [] if work_items is None else work_items

    def execute_authoritative(self, task, run, attempt, **kwargs):
        self.calls += 1
        self.work_items.append(kwargs.get("work_item"))
        execution_id = run.runtime_execution_id or run.runtime_execution_intent_id
        assignment_id = uuid4()
        digest = "a" * 64
        if self.registry is not None:
            self.registry.execution = RuntimeExecution.prepare(
                tenant_id=task.tenant_id,
                run_id=run.id,
                runtime_version_id=run.runtime_version_id,
                assignment_id=assignment_id,
                assignment_digest=digest,
                dispatch_key=f"runtime-dispatch:{task.tenant_id}:{execution_id}",
                dispatch_digest=canonical_digest({"execution": str(execution_id)}),
                execution_id=execution_id,
            ).apply_observation(
                phase=RuntimeExecutionPhase.DISPATCHING,
                provider_sequence=None,
            )
        return ManagedRuntimeAuthoritativeResult(
            execution_id=execution_id,
            assignment_id=self.result_assignment_id or assignment_id,
            assignment_digest=self.result_assignment_digest or digest,
            observation=RuntimeObservation(
                observation_id=str(uuid4()),
                runtime_execution_id=str(execution_id),
                assignment_id=str(assignment_id),
                assignment_digest=digest,
                phase=self.phase,
                observed_at=self.observed_at or datetime.now(timezone.utc),
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


class _ControlPlaneFailureManagedExecution:
    def execute_authoritative(self, *args, **kwargs):
        raise ManagedRuntimeControlPlaneFailure("claim conflict")


class _SuppliedConflictManagedExecution:
    def __init__(self, registry, *, mode="canonical") -> None:
        self.registry = registry
        self.mode = mode

    def execute_authoritative(self, task, run, attempt, **kwargs):
        result = _AuthoritativeManagedExecution(registry=self.registry).execute_authoritative(
            task, run, attempt, **kwargs
        )
        conflict_candidate = replace(result.observation, usage={"unpriced": 1})
        conflict = build_managed_runtime_conflict_observation(
            conflict_candidate,
            expected_execution_id=result.execution_id,
            expected_assignment_id=result.assignment_id,
            expected_assignment_digest=result.assignment_digest,
            fallback_observed_at=conflict_candidate.observed_at,
        )
        if self.mode == "success":
            observation = result.observation
        else:
            observation = RunExecutionService._synthetic_runtime_unknown(
                execution_id=result.execution_id,
                assignment_id=result.assignment_id,
                assignment_digest=result.assignment_digest,
                observed_at=conflict.observed_at,
            )
            if self.mode == "noncanonical":
                observation = replace(observation, provider_event_id="forged")
        return replace(
            result,
            observation=observation,
            conflicting_observation=conflict,
        )


class _AtomicRuntimeRegistry:
    def __init__(self, outcome=RuntimeObservationOutcome.APPLIED, failure_stage=None) -> None:
        self.calls = 0
        self.execution = None
        self.assignment_snapshot = None
        self.outcome = outcome
        self.observations = []
        self.conflicts = []
        self.events = []
        self.failure_stage = failure_stage

    def get_assignment_snapshot(self, execution_id):
        return self.assignment_snapshot

    def record_observation_in_uow(self, uow, **kwargs):
        self.calls += 1
        self.events.append("observation")
        self.observations.append(kwargs)
        if self.failure_stage == "after_synthetic":
            raise ValueError("synthetic evidence failure")
        if self.execution is not None and self.outcome is RuntimeObservationOutcome.APPLIED:
            self.execution = self.execution.apply_observation(
                phase=kwargs["phase"],
                provider_sequence=kwargs["provider_sequence"],
            )
        return self.outcome

    def record_conflicting_observation_in_uow(self, uow, **kwargs):
        self.events.append("conflict")
        self.conflicts.append(kwargs)
        if self.failure_stage == "after_conflict":
            raise ValueError("conflict evidence failure")
        return type("ConflictEvidence", (), {})()

    def get_execution_for_run(self, run_id):
        return self.execution

    def snapshot(self):
        return {
            name: deepcopy(getattr(self, name))
            for name in ("calls", "execution", "observations", "conflicts", "events")
        }

    def restore(self, snapshot):
        for name, value in snapshot.items():
            setattr(self, name, deepcopy(value))


class _RuntimeRepositoryProbe:
    """Minimal persisted-runtime projection for executable finalizer tests."""

    def __init__(
        self,
        registry,
        attempt_id,
        fencing_token,
        *,
        cancel_intent=None,
        owner_attempt_id=None,
    ):
        self.registry = registry
        self.attempt_id = attempt_id
        self.fencing_token = fencing_token
        self.cancel_intent = cancel_intent
        self.owner_attempt_id = owner_attempt_id

    def get_execution(self, execution_id, *, tenant_id, for_update=False):
        if self.registry.execution is None or self.registry.execution.id != execution_id:
            return None
        return replace(
            self.registry.execution,
            current_owner_attempt_id=self.owner_attempt_id or self.attempt_id,
            current_fencing_token=self.fencing_token,
        )

    def find_cancel_intent(self, execution_id, *, tenant_id):
        return self.cancel_intent

    def get_version(self, version_id, *, tenant_id, for_update=False):
        return _BuiltinRuntimeAdmission(version_id).require_builtin_langgraph_v2_in_uow(None)

    def snapshot(self):
        return self.registry.snapshot()

    def restore(self, snapshot):
        self.registry.restore(snapshot)


class _RuntimeAwareFactory:
    def __init__(self, base, runtime_repository, *, resources=()):
        self.base = base
        self.runtime_repository = runtime_repository
        self.resources = tuple(resources)

    def __call__(self):
        uow = self.base()
        uow.runtimes = self.runtime_repository
        return _RuntimeAwareUnitOfWork(
            uow, self.runtime_repository, resources=self.resources
        )


class _RuntimeAwareUnitOfWork:
    """Add transaction-local Runtime registry snapshots to the in-memory UoW."""

    def __init__(self, uow, runtime_repository, *, resources=()):
        self._uow = uow
        self._runtime_repository = runtime_repository
        self._resources = tuple(resources)
        self._snapshot = None
        self._store_snapshot = None
        self._resource_snapshots = ()

    def __enter__(self):
        self._store_snapshot = deepcopy(self._uow._store.__dict__)
        self._uow.__enter__()
        self._snapshot = self._runtime_repository.snapshot()
        self._resource_snapshots = tuple(
            (resource, resource.snapshot())
            for resource in self._resources
            if hasattr(resource, "snapshot")
        )
        return self._uow

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is not None:
            self._runtime_repository.restore(self._snapshot)
            self._uow._store.__dict__.clear()
            self._uow._store.__dict__.update(deepcopy(self._store_snapshot))
            for resource, snapshot in self._resource_snapshots:
                resource.restore(snapshot)
        return self._uow.__exit__(exc_type, exc_value, traceback)

    def __getattr__(self, name):
        return getattr(self._uow, name)


def _persistent_store_snapshot(store):
    """Ignore read counters when comparing the transactional in-memory store."""
    return {
        key: deepcopy(value)
        for key, value in store.__dict__.items()
        if not key.endswith("_calls")
    }


class _MemoryCaptureProbe:
    def __init__(self) -> None:
        self.captures = 0
        self.assemble_calls = 0

    def assemble(self, task, run, work_item):
        self.assemble_calls += 1
        return type("Assembly", (), {"work_item": work_item})()

    def capture_completed_task_in_unit_of_work(self, uow, task):
        self.captures += 1

    def snapshot(self):
        return self.captures

    def restore(self, snapshot):
        self.captures = snapshot


class _ResearchProbe:
    def __init__(self, *, fail=False) -> None:
        self.calls = 0
        self.fail = fail

    def materialize_if_ready(self, task_id, *, actor):
        self.calls += 1
        if self.fail:
            raise RuntimeError("research unavailable")


def _managed_direct_finalizer_case(
    *,
    phase: RuntimePhase,
    budget: TaskBudget | None = None,
    output: dict | None = None,
    usage: dict | None = None,
    cancel_intent: object | None = None,
    quota: bool = False,
):
    """Build a runtime-aware managed DIRECT chain at the finalizer boundary."""
    uow_factory = InMemoryUnitOfWorkFactory()
    agents = AgentRegistryService(uow_factory=uow_factory, tenant_id="test-tenant")
    agents.ensure_builtin_agent("test-agent")
    if quota:
        quota_policies = QuotaPolicyService(uow_factory, "test-tenant")
        quota_policies.put_policy(
            scope=QuotaScope.TENANT,
            project_id=None,
            max_concurrent_attempts=2,
            weight=1,
            created_by="managed-finalizer-test",
        )
        quota_policies.put_policy(
            scope=QuotaScope.PROJECT,
            project_id="default",
            max_concurrent_attempts=2,
            weight=1,
            created_by="managed-finalizer-test",
        )
    gates = FeatureGateSet.from_config(
        "full",
        "managed_agent_runtime=true,managed_runtime_worker=true,"
        "managed_runtime_direct_cutover=true"
        + (",identity_rbac=true,quota_admission=true" if quota else ""),
    )
    tasks = TaskApplicationService(
        uow_factory=uow_factory,
        agent_id="test-agent",
        tenant_id="test-tenant",
        feature_gates=gates,
        runtime_registry_service=_BuiltinRuntimeAdmission(),
    )
    task_id = tasks.create_task(
        "managed direct finalizer matrix", budget=budget
    ).task.id
    run = tasks.request_run(task_id).runs[0]
    envelope = uow_factory.store.outbox[-1]
    registry = _AtomicRuntimeRegistry()
    managed = _AuthoritativeManagedExecution(
        phase=phase,
        output=output,
        usage=usage,
        registry=registry,
    )
    memory = _MemoryCaptureProbe()
    worker = RunExecutionService(
        uow_factory=uow_factory,
        workflow_runner=_PoisonWorkflowRunner(),
        managed_execution_service=managed,
        runtime_registry_service=registry,
        runtime_memory_service=memory,
        worker_id="managed-matrix-worker",
        consumer_name="managed-matrix-worker-v1",
        lease_duration=timedelta(minutes=5),
        feature_gates=gates,
    )
    task, leased_run, attempt = worker._acquire(
        envelope, task_id=task_id, run_id=run.id
    )
    result = managed.execute_authoritative(task, leased_run, attempt)
    worker._uow_factory = _RuntimeAwareFactory(
        uow_factory,
        _RuntimeRepositoryProbe(
            registry,
            attempt.id,
            attempt.fencing_token,
            cancel_intent=cancel_intent,
        ),
    )
    return uow_factory, tasks, worker, envelope, result, registry, memory, task_id, attempt


def _managed_reviewed_finalizer_case(
    *,
    role: RunRole,
    phase: RuntimePhase,
    budget: TaskBudget | None = None,
    cancel_intent=None,
    quota: bool = False,
    max_revisions: int = 1,
    review_deadline=None,
    acquire: bool = True,
):
    """Build a valid managed REVIEWED executor or reviewer chain."""
    uow_factory = InMemoryUnitOfWorkFactory()
    agents = AgentRegistryService(uow_factory=uow_factory, tenant_id="test-tenant")
    agents.ensure_builtin_agent("test-agent")
    agents.ensure_builtin_agent("test-reviewer", reviewer=True)
    if quota:
        quota_policies = QuotaPolicyService(uow_factory, "test-tenant")
        quota_policies.put_policy(
            scope=QuotaScope.TENANT,
            project_id=None,
            max_concurrent_attempts=2,
            weight=1,
            created_by="managed-reviewed-finalizer-test",
        )
        quota_policies.put_policy(
            scope=QuotaScope.PROJECT,
            project_id="default",
            max_concurrent_attempts=2,
            weight=1,
            created_by="managed-reviewed-finalizer-test",
        )
    gates = FeatureGateSet.from_config(
        "full",
        "managed_agent_runtime=true,managed_runtime_worker=true,"
        "managed_runtime_reviewed_cutover=true"
        + (",identity_rbac=true,quota_admission=true" if quota else ""),
    )
    tasks = TaskApplicationService(
        uow_factory=uow_factory,
        agent_id="test-agent",
        tenant_id="test-tenant",
        feature_gates=gates,
        runtime_registry_service=_BuiltinRuntimeAdmission(),
    )
    criterion = AcceptanceCriterion.create(
        key="summary",
        description="Summary exists",
        kind=AcceptanceCriterionKind.OUTPUT_PATH_EXISTS,
        path=("summary",),
    )
    task_id = tasks.create_task(
        "managed reviewed finalizer matrix",
        execution_mode=TaskExecutionMode.REVIEWED,
        acceptance_criteria=(criterion,),
        max_revisions=max_revisions,
        review_deadline=review_deadline,
        budget=budget,
    ).task.id
    initial_run = tasks.request_run(task_id).runs[0]
    envelope = uow_factory.store.outbox[-1]

    if role is RunRole.REVIEWER:
        reviewer_agent = agents.ensure_builtin_agent("test-reviewer", reviewer=True)
        reviewer_version = reviewer_agent.versions[-1]
        with uow_factory() as uow:
            task = uow.tasks.get(task_id, for_update=True)
            executor = uow.runs.get(initial_run.id, for_update=True)
            assert task is not None and executor is not None
            now = utc_now()
            task.start(executor.id, at=now)
            executor.start(at=now)
            executor.succeed({"summary": "candidate"}, at=now)
            reviewer = TaskRun.request(
                task.id,
                "test-reviewer",
                agent_version_id=reviewer_version.id,
                agent_version_digest=reviewer_version.content_digest,
                role=RunRole.REVIEWER,
                runtime_version_id=builtin_langgraph_version_id("v2"),
                runtime_authority="managed",
            )
            assert reviewer.agent_id == "test-reviewer"
            assert reviewer.runtime_authority == executor.runtime_authority == "managed"
            assert reviewer.runtime_version_id == executor.runtime_version_id
            assert reviewer.runtime_execution_intent_id != executor.runtime_execution_intent_id
            task.queue_review(executor.id, {"summary": "candidate"}, reviewer.id, at=now)
            reviewer_envelope = MessageEnvelope.run_requested(
                tenant_id=task.tenant_id,
                task_id=task.id,
                run_id=reviewer.id,
                causation_id=envelope.message_id,
                at=now,
            )
            uow.runs.save(executor)
            uow.runs.add(reviewer)
            uow.tasks.save(task)
            uow.outbox.add(reviewer_envelope)
            uow.commit()
        envelope = reviewer_envelope
        run = reviewer
    else:
        run = initial_run

    registry = _AtomicRuntimeRegistry()
    managed = _AuthoritativeManagedExecution(phase=phase, registry=registry)
    memory = _MemoryCaptureProbe()
    worker = RunExecutionService(
        uow_factory=uow_factory,
        workflow_runner=_PoisonWorkflowRunner(),
        managed_execution_service=managed,
        runtime_registry_service=registry,
        runtime_memory_service=memory,
        worker_id="managed-reviewed-worker",
        consumer_name="managed-reviewed-worker-v1",
        lease_duration=timedelta(minutes=5),
        executor_agent_id="test-agent",
        reviewer_agent_id="test-reviewer",
        feature_gates=gates,
    )
    if acquire:
        task, leased_run, attempt = worker._acquire(
            envelope, task_id=task_id, run_id=run.id
        )
        result = managed.execute_authoritative(task, leased_run, attempt)
        worker._uow_factory = _RuntimeAwareFactory(
            uow_factory,
            _RuntimeRepositoryProbe(
                registry, attempt.id, attempt.fencing_token, cancel_intent=cancel_intent
            ),
        )
    else:
        result = None
        attempt = None
    return uow_factory, tasks, worker, envelope, result, registry, memory, task_id, attempt


@pytest.mark.parametrize(
    ("phase", "expected_task_status", "expected_run_status", "expected_attempt_status", "error"),
    [
        (
            RuntimePhase.SUCCEEDED,
            TaskStatus.COMPLETED,
            RunStatus.SUCCEEDED,
            AttemptStatus.SUCCEEDED,
            None,
        ),
        (
            RuntimePhase.FAILED,
            TaskStatus.FAILED,
            RunStatus.FAILED,
            AttemptStatus.FAILED,
            "runtime.failed",
        ),
        (
            RuntimePhase.TIMED_OUT,
            TaskStatus.FAILED,
            RunStatus.FAILED,
            AttemptStatus.FAILED,
            "runtime.timed_out",
        ),
        (
            RuntimePhase.CANCELED,
            TaskStatus.CANCELED,
            RunStatus.CANCELED,
            AttemptStatus.CANCELED,
            "runtime.canceled",
        ),
    ],
)
def test_managed_direct_known_terminal_matrix(
    phase,
    expected_task_status,
    expected_run_status,
    expected_attempt_status,
    error,
):
    cancel_intent = object() if phase is RuntimePhase.CANCELED else None
    case = _managed_direct_finalizer_case(phase=phase, cancel_intent=cancel_intent)
    _uow_factory, tasks, worker, envelope, result, _registry, memory, task_id, attempt = case

    assert worker._finalize_managed(
        envelope,
        task_id=task_id,
        run_id=attempt.run_id,
        attempt_id=attempt.id,
        result=result,
    ) is (expected_task_status is TaskStatus.COMPLETED)
    aggregate = tasks.get_task(task_id)
    assert aggregate.task.status is expected_task_status
    assert aggregate.runs[0].status is expected_run_status
    assert aggregate.attempts[0].status is expected_attempt_status
    assert aggregate.task.error == (None if phase is RuntimePhase.CANCELED else error)
    assert aggregate.runs[0].error == (None if phase is RuntimePhase.CANCELED else error)
    assert aggregate.attempts[0].error == (
        None if phase in {RuntimePhase.SUCCEEDED, RuntimePhase.CANCELED} else error
    )
    assert memory.captures == int(expected_task_status is TaskStatus.COMPLETED)


def test_managed_direct_cancel_without_persisted_intent_is_failed():
    case = _managed_direct_finalizer_case(phase=RuntimePhase.CANCELED)
    _uow_factory, tasks, worker, envelope, result, _registry, memory, task_id, attempt = case

    assert worker._finalize_managed(
        envelope,
        task_id=task_id,
        run_id=attempt.run_id,
        attempt_id=attempt.id,
        result=result,
    ) is False
    aggregate = tasks.get_task(task_id)
    assert aggregate.task.status is TaskStatus.FAILED
    assert aggregate.task.error == "runtime.unrequested_cancellation"
    assert aggregate.runs[0].status is RunStatus.FAILED
    assert aggregate.attempts[0].status is AttemptStatus.FAILED
    assert memory.captures == 0


@pytest.mark.parametrize(
    ("phase", "source", "task_status", "attempt_status"),
    [
        (
            RuntimePhase.SUCCEEDED,
            BudgetSettlementSource.CONSERVATIVE_ESTIMATE,
            TaskStatus.COMPLETED,
            AttemptStatus.SUCCEEDED,
        ),
        (
            RuntimePhase.FAILED,
            BudgetSettlementSource.RELEASED,
            TaskStatus.FAILED,
            AttemptStatus.FAILED,
        ),
    ],
)
def test_managed_direct_budget_accounting_matrix(
    phase, source, task_status, attempt_status
):
    budget = TaskBudget.create(
        deadline=utc_now() + timedelta(minutes=5),
        max_tokens=100,
        token_reservation_per_attempt=10,
    )
    case = _managed_direct_finalizer_case(phase=phase, budget=budget)
    _uow_factory, tasks, worker, envelope, result, _registry, _memory, task_id, attempt = case
    assert worker._finalize_managed(
        envelope,
        task_id=task_id,
        run_id=attempt.run_id,
        attempt_id=attempt.id,
        result=result,
    ) is (task_status is TaskStatus.COMPLETED)
    aggregate = tasks.get_task(task_id)
    assert aggregate.task.status is task_status
    assert aggregate.attempts[0].status is attempt_status
    assert aggregate.attempts[0].budget_settlement_source is source
    assert aggregate.attempts[0].settled_tokens == (
        10 if source is BudgetSettlementSource.CONSERVATIVE_ESTIMATE else 0
    )
    assert aggregate.task.reserved_tokens == 0
    assert aggregate.task.settled_tokens == (
        10 if source is BudgetSettlementSource.CONSERVATIVE_ESTIMATE else 0
    )


@pytest.mark.parametrize("has_intent", [True, False])
def test_managed_direct_budgeted_cancellation_releases_exactly(has_intent):
    budget = TaskBudget.create(
        deadline=utc_now() + timedelta(minutes=5),
        max_tokens=100,
        token_reservation_per_attempt=10,
    )
    case = _managed_direct_finalizer_case(
        phase=RuntimePhase.CANCELED,
        budget=budget,
        cancel_intent=(object() if has_intent else None),
    )
    _uow_factory, tasks, worker, envelope, result, _registry, _memory, task_id, attempt = case
    worker._finalize_managed(
        envelope,
        task_id=task_id,
        run_id=attempt.run_id,
        attempt_id=attempt.id,
        result=result,
    )
    aggregate = tasks.get_task(task_id)
    assert aggregate.task.status is (TaskStatus.CANCELED if has_intent else TaskStatus.FAILED)
    assert aggregate.task.error == (
        None if has_intent else "runtime.unrequested_cancellation"
    )
    assert aggregate.attempts[0].budget_settlement_source is BudgetSettlementSource.RELEASED
    assert aggregate.attempts[0].settled_tokens == 0
    assert aggregate.task.reserved_tokens == 0
    assert aggregate.task.settled_tokens == 0


def test_managed_direct_known_and_unknown_release_quota_once(monkeypatch):
    calls = []
    original_release = QuotaController.release_attempt

    def record_release(uow, attempt):
        calls.append(attempt.id)
        return original_release(uow, attempt)

    monkeypatch.setattr(QuotaController, "release_attempt", staticmethod(record_release))
    known = _managed_direct_finalizer_case(phase=RuntimePhase.FAILED)
    _uow_factory, tasks, worker, envelope, result, _registry, _memory, task_id, attempt = known
    worker._finalize_managed(
        envelope,
        task_id=task_id,
        run_id=attempt.run_id,
        attempt_id=attempt.id,
        result=result,
    )
    assert calls == [attempt.id]
    assert worker.process(envelope) is False
    assert calls == [attempt.id]

    unknown = _managed_direct_finalizer_case(phase=RuntimePhase.OUTCOME_UNKNOWN)
    _uow_factory, tasks, worker, envelope, result, _registry, _memory, task_id, attempt = unknown
    worker._finalize_managed(
        envelope,
        task_id=task_id,
        run_id=attempt.run_id,
        attempt_id=attempt.id,
        result=result,
    )
    assert calls == [known[-1].id, attempt.id]


@pytest.mark.parametrize("phase", [RuntimePhase.SUCCEEDED, RuntimePhase.OUTCOME_UNKNOWN])
def test_managed_direct_real_quota_reservation_releases_and_replay_is_stable(phase):
    case = _managed_direct_finalizer_case(phase=phase, quota=True)
    uow_factory, tasks, worker, envelope, result, _registry, _memory, task_id, attempt = case
    reservations_before = deepcopy(uow_factory.store.quota_reservations)
    assert len(reservations_before) == 2
    assert {item.attempt_id for item in reservations_before.values()} == {attempt.id}
    assert all(item.released_at is None for item in reservations_before.values())

    worker._finalize_managed(
        envelope,
        task_id=task_id,
        run_id=attempt.run_id,
        attempt_id=attempt.id,
        result=result,
    )
    reservations_after = deepcopy(uow_factory.store.quota_reservations)
    assert len(reservations_after) == 2
    assert all(item.released_at is not None for item in reservations_after.values())
    aggregate_after = tasks.get_task(task_id)
    task_snapshot = deepcopy(aggregate_after.task)
    attempt_snapshot = deepcopy(aggregate_after.attempts[0])
    assert worker.process(envelope) is False
    assert uow_factory.store.quota_reservations == reservations_after
    aggregate_replay = tasks.get_task(task_id)
    assert aggregate_replay.task == task_snapshot
    assert aggregate_replay.attempts[0] == attempt_snapshot


def test_managed_direct_unknown_parks_conservatively_with_one_clock():
    budget = TaskBudget.create(
        deadline=utc_now() + timedelta(minutes=5),
        max_tokens=100,
        token_reservation_per_attempt=10,
    )
    case = _managed_direct_finalizer_case(
        phase=RuntimePhase.OUTCOME_UNKNOWN, budget=budget
    )
    uow_factory, tasks, worker, envelope, result, registry, memory, task_id, attempt = case
    assert worker._finalize_managed(
        envelope,
        task_id=task_id,
        run_id=attempt.run_id,
        attempt_id=attempt.id,
        result=result,
    ) is False
    aggregate = tasks.get_task(task_id)
    received_at = registry.observations[0]["now"]
    assert aggregate.task.status is TaskStatus.RECONCILIATION_REQUIRED
    assert aggregate.runs[0].status is RunStatus.RECONCILIATION_REQUIRED
    assert aggregate.attempts[0].status is AttemptStatus.OUTCOME_UNKNOWN
    assert aggregate.task.updated_at == received_at
    assert aggregate.runs[0].error == "runtime.provider_outcome_unknown"
    assert aggregate.attempts[0].completed_at == received_at
    assert (
        aggregate.attempts[0].budget_settlement_source
        is BudgetSettlementSource.CONSERVATIVE_ESTIMATE
    )
    assert aggregate.task.settled_tokens == 10
    assert aggregate.task.reserved_tokens == 0
    assert memory.captures == 0
    events = [
        item
        for item in uow_factory.store.outbox
        if item.schema_name == "agentmesh.runtime.reconciliation.required"
    ]
    assert len(events) == 1
    assert events[0].occurred_at == received_at


@pytest.mark.parametrize("role", [RunRole.EXECUTOR, RunRole.REVIEWER])
@pytest.mark.parametrize("phase", [RuntimePhase.OUTCOME_UNKNOWN, RuntimePhase.LOST])
def test_managed_reviewed_unknown_parks_without_continuation_or_memory(role, phase):
    case = _managed_reviewed_finalizer_case(
        role=role, phase=phase
    )
    uow_factory, tasks, worker, envelope, result, registry, memory, task_id, attempt = case
    runs_before = len(uow_factory.store.runs)
    outbox_before = len(uow_factory.store.outbox)

    assert worker._finalize_managed(
        envelope,
        task_id=task_id,
        run_id=attempt.run_id,
        attempt_id=attempt.id,
        result=result,
    ) is False
    aggregate = tasks.get_task(task_id)
    assert aggregate.task.status is TaskStatus.RECONCILIATION_REQUIRED
    if role is RunRole.REVIEWER:
        assert aggregate.task.candidate_output == {"summary": "candidate"}
    parked_run = next(run for run in aggregate.runs if run.id == attempt.run_id)
    assert parked_run.status is RunStatus.RECONCILIATION_REQUIRED
    parked_attempt = next(item for item in aggregate.attempts if item.id == attempt.id)
    assert parked_attempt.status is AttemptStatus.OUTCOME_UNKNOWN
    assert len(aggregate.runs) == runs_before
    assert len(uow_factory.store.outbox) == outbox_before + 1
    assert not [
        item
        for item in uow_factory.store.outbox[outbox_before:]
        if item.schema_name == "agentmesh.run.requested"
    ]
    assert memory.captures == 0
    assert len(
        [
            item
            for item in uow_factory.store.outbox
            if item.schema_name == "agentmesh.runtime.reconciliation.required"
        ]
    ) == 1

    registry_calls = registry.calls
    assert worker.process(envelope) is False
    assert registry.calls == registry_calls
    assert len(
        [
            item
            for item in uow_factory.store.outbox
            if item.schema_name == "agentmesh.runtime.reconciliation.required"
        ]
    ) == 1


@pytest.mark.parametrize("invalid_binding", ["role", "state", "subtask"])
def test_managed_reviewed_invalid_binding_fails_before_runtime_evidence(invalid_binding):
    case = _managed_reviewed_finalizer_case(
        role=RunRole.REVIEWER, phase=RuntimePhase.OUTCOME_UNKNOWN
    )
    uow_factory, tasks, worker, envelope, result, registry, _memory, task_id, attempt = case
    with uow_factory() as uow:
        task = uow.tasks.get(task_id, for_update=True)
        run = uow.runs.get(attempt.run_id, for_update=True)
        assert task is not None and run is not None
        if invalid_binding == "role":
            run.role = RunRole.SUPERVISOR
        elif invalid_binding == "state":
            task.status = TaskStatus.RUNNING
        else:
            run.subtask_id = uuid4()
        uow.tasks.save(task)
        uow.runs.save(run)
        uow.commit()

    with pytest.raises((InvalidTaskTransition, RunLeaseUnavailable)):
        worker._finalize_managed(
            envelope,
            task_id=task_id,
            run_id=attempt.run_id,
            attempt_id=attempt.id,
            result=result,
        )
    aggregate = tasks.get_task(task_id)
    assert aggregate.attempts[0].status is AttemptStatus.RUNNING
    assert registry.events == []
    assert not uow_factory.store.inbox


def test_managed_reviewed_executor_success_creates_reviewer_continuation():
    case = _managed_reviewed_finalizer_case(
        role=RunRole.EXECUTOR, phase=RuntimePhase.SUCCEEDED
    )
    uow_factory, tasks, worker, envelope, result, _registry, memory, task_id, attempt = case
    assert worker._finalize_managed(
        envelope,
        task_id=task_id,
        run_id=attempt.run_id,
        attempt_id=attempt.id,
        result=result,
    ) is False
    aggregate = tasks.get_task(task_id)
    assert aggregate.task.status is TaskStatus.REVIEWING
    assert aggregate.task.candidate_output == {"managed": True}
    assert aggregate.runs[0].status is RunStatus.SUCCEEDED
    assert len(aggregate.runs) == 2
    reviewer = next(run for run in aggregate.runs if run.role is RunRole.REVIEWER)
    messages = [
        item
        for item in uow_factory.store.outbox
        if item.schema_name == "agentmesh.run.requested"
        and item.payload["run_id"] == str(reviewer.id)
    ]
    assert len(messages) == 1
    assert messages[0].causation_id == envelope.message_id
    assert memory.captures == 0


def test_managed_reviewed_reviewer_accept_completes_and_captures_memory():
    case = _managed_reviewed_finalizer_case(
        role=RunRole.REVIEWER, phase=RuntimePhase.SUCCEEDED
    )
    uow_factory, tasks, worker, envelope, result, _registry, memory, task_id, attempt = case
    output = {"criteria": [{"key": "summary", "passed": True}], "feedback": []}
    result = replace(result, observation=replace(result.observation, output=output))
    assert worker._finalize_managed(
        envelope,
        task_id=task_id,
        run_id=attempt.run_id,
        attempt_id=attempt.id,
        result=result,
    ) is True
    aggregate = tasks.get_task(task_id)
    assert aggregate.task.status is TaskStatus.COMPLETED
    assert aggregate.task.output == {"summary": "candidate"}
    assert len(aggregate.runs) == 2
    assert memory.captures == 1


def test_managed_reviewed_reviewer_accept_ignores_future_run_budget_limit():
    case = _managed_reviewed_finalizer_case(
        role=RunRole.REVIEWER,
        phase=RuntimePhase.SUCCEEDED,
        budget=TaskBudget.create(max_runs=2),
    )
    uow_factory, tasks, worker, envelope, result, _registry, memory, task_id, attempt = case
    run_requested_before = len(
        [item for item in uow_factory.store.outbox if item.schema_name == "agentmesh.run.requested"]
    )
    output = {"criteria": [{"key": "summary", "passed": True}], "feedback": []}
    result = replace(result, observation=replace(result.observation, output=output))

    assert worker._finalize_managed(
        envelope,
        task_id=task_id,
        run_id=attempt.run_id,
        attempt_id=attempt.id,
        result=result,
    ) is True
    aggregate = tasks.get_task(task_id)
    assert aggregate.task.status is TaskStatus.COMPLETED
    assert aggregate.task.output == {"summary": "candidate"}
    assert aggregate.task.candidate_output == {"summary": "candidate"}
    assert len(aggregate.runs) == 2
    assert len(
        [item for item in uow_factory.store.outbox if item.schema_name == "agentmesh.run.requested"]
    ) == run_requested_before
    assert memory.captures == 1


def test_managed_reviewed_reviewer_reject_creates_revision_continuation():
    case = _managed_reviewed_finalizer_case(
        role=RunRole.REVIEWER, phase=RuntimePhase.SUCCEEDED
    )
    uow_factory, tasks, worker, envelope, result, _registry, memory, task_id, attempt = case
    output = {"criteria": [{"key": "summary", "passed": False}], "feedback": []}
    result = replace(result, observation=replace(result.observation, output=output))
    assert worker._finalize_managed(
        envelope,
        task_id=task_id,
        run_id=attempt.run_id,
        attempt_id=attempt.id,
        result=result,
    ) is False
    aggregate = tasks.get_task(task_id)
    assert aggregate.task.status is TaskStatus.READY
    assert aggregate.task.revision_count == 1
    assert len(aggregate.runs) == 3
    revision = next(run for run in aggregate.runs if run.revision_number == 1)
    messages = [
        item
        for item in uow_factory.store.outbox
        if item.schema_name == "agentmesh.run.requested"
        and item.payload["run_id"] == str(revision.id)
    ]
    assert len(messages) == 1
    assert messages[0].causation_id == envelope.message_id
    assert memory.captures == 0


def test_managed_reviewed_executor_budget_rejection_waits_without_reviewer():
    budget = TaskBudget.create(max_runs=1)
    case = _managed_reviewed_finalizer_case(
        role=RunRole.EXECUTOR, phase=RuntimePhase.SUCCEEDED, budget=budget
    )
    uow_factory, tasks, worker, envelope, result, _registry, memory, task_id, attempt = case
    run_requested_before = len(
        [item for item in uow_factory.store.outbox if item.schema_name == "agentmesh.run.requested"]
    )
    assert worker._finalize_managed(
        envelope,
        task_id=task_id,
        run_id=attempt.run_id,
        attempt_id=attempt.id,
        result=result,
    ) is False
    aggregate = tasks.get_task(task_id)
    assert aggregate.task.status is TaskStatus.WAITING_APPROVAL
    assert aggregate.task.candidate_output == {"managed": True}
    assert aggregate.task.budget_exhausted_reason == "budget_run_limit_exhausted"
    assert len(aggregate.runs) == 1
    assert len(
        [item for item in uow_factory.store.outbox if item.schema_name == "agentmesh.run.requested"]
    ) == run_requested_before
    assert memory.captures == 0


def test_managed_reviewed_executor_budget_deadline_rejection_waits_without_reviewer():
    budget = TaskBudget.create(deadline=utc_now() + timedelta(minutes=5))
    case = _managed_reviewed_finalizer_case(
        role=RunRole.EXECUTOR, phase=RuntimePhase.SUCCEEDED, budget=budget
    )
    uow_factory, tasks, worker, envelope, result, _registry, memory, task_id, attempt = case
    with uow_factory() as uow:
        task = uow.tasks.get(task_id, for_update=True)
        assert task is not None and task.budget is not None
        task.budget = replace(task.budget, deadline=utc_now() - timedelta(seconds=1))
        uow.tasks.save(task)
        uow.commit()
    run_requested_before = len(
        [item for item in uow_factory.store.outbox if item.schema_name == "agentmesh.run.requested"]
    )
    assert worker._finalize_managed(
        envelope,
        task_id=task_id,
        run_id=attempt.run_id,
        attempt_id=attempt.id,
        result=result,
    ) is False
    aggregate = tasks.get_task(task_id)
    assert aggregate.task.status is TaskStatus.WAITING_APPROVAL
    assert aggregate.task.candidate_output == {"managed": True}
    assert aggregate.task.budget_exhausted_reason == "budget_deadline_exceeded"
    assert len(aggregate.runs) == 1
    assert len(
        [item for item in uow_factory.store.outbox if item.schema_name == "agentmesh.run.requested"]
    ) == run_requested_before
    assert memory.captures == 0


@pytest.mark.parametrize("limit", ["revision", "deadline", "budget"])
def test_managed_reviewed_reviewer_reject_waits_without_revision(limit):
    budget = TaskBudget.create(max_runs=2) if limit == "budget" else None
    review_deadline = utc_now() + timedelta(minutes=5) if limit == "deadline" else None
    case = _managed_reviewed_finalizer_case(
        role=RunRole.REVIEWER,
        phase=RuntimePhase.SUCCEEDED,
        budget=budget,
        max_revisions=(0 if limit == "revision" else 1),
        review_deadline=review_deadline,
    )
    uow_factory, tasks, worker, envelope, result, _registry, memory, task_id, attempt = case
    if limit == "deadline":
        with uow_factory() as uow:
            task = uow.tasks.get(task_id, for_update=True)
            assert task is not None and task.review_deadline is not None
            task.review_deadline = utc_now() - timedelta(seconds=1)
            uow.tasks.save(task)
            uow.commit()
    run_requested_before = len(
        [item for item in uow_factory.store.outbox if item.schema_name == "agentmesh.run.requested"]
    )
    output = {"criteria": [{"key": "summary", "passed": False}], "feedback": []}
    result = replace(result, observation=replace(result.observation, output=output))
    assert worker._finalize_managed(
        envelope,
        task_id=task_id,
        run_id=attempt.run_id,
        attempt_id=attempt.id,
        result=result,
    ) is False
    aggregate = tasks.get_task(task_id)
    assert aggregate.task.status is TaskStatus.WAITING_APPROVAL
    assert aggregate.task.candidate_output == {"summary": "candidate"}
    assert aggregate.task.latest_review is not None
    assert aggregate.task.latest_review["accepted"] is False
    assert len(aggregate.runs) == 2
    assert len(
        [item for item in uow_factory.store.outbox if item.schema_name == "agentmesh.run.requested"]
    ) == run_requested_before
    assert memory.captures == 0


def test_managed_reviewed_reviewer_never_assembles_organizational_memory():
    case = _managed_reviewed_finalizer_case(
        role=RunRole.REVIEWER, phase=RuntimePhase.SUCCEEDED, acquire=False
    )
    uow_factory, tasks, worker, envelope, _result, _registry, memory, task_id, attempt = case
    assert worker.process(envelope) is True
    managed = worker._managed_execution_service
    assert managed is not None and managed.work_items
    work_item = managed.work_items[0]
    assert work_item.input["candidate_output"] == {"summary": "candidate"}
    assert "acceptance_criteria" in work_item.input
    assert "agentmesh_memory" not in work_item.input
    assert memory.assemble_calls == 0
    aggregate = tasks.get_task(task_id)
    assert aggregate.runs[1].status is RunStatus.FAILED


def test_legacy_reviewed_reviewer_never_assembles_organizational_memory(
    task_service, registry_service, uow_factory
):
    criterion = AcceptanceCriterion.create(
        key="summary",
        description="Summary exists",
        kind=AcceptanceCriterionKind.OUTPUT_PATH_EXISTS,
        path=("summary",),
    )
    task_id = task_service.create_task(
        "legacy reviewed reviewer memory boundary",
        execution_mode=TaskExecutionMode.REVIEWED,
        acceptance_criteria=(criterion,),
    ).task.id
    executor = task_service.request_run(task_id).runs[0]
    executor_envelope = uow_factory.store.outbox[-1]
    reviewer_definition = next(
        item
        for item in registry_service.list_definitions()
        if item.definition.name == "test-reviewer"
    )
    reviewer_version = reviewer_definition.versions[-1]
    with uow_factory() as uow:
        task = uow.tasks.get(task_id, for_update=True)
        persisted_executor = uow.runs.get(executor.id, for_update=True)
        assert task is not None and persisted_executor is not None
        now = utc_now()
        task.start(persisted_executor.id, at=now)
        persisted_executor.start(at=now)
        persisted_executor.succeed({"summary": "candidate"}, at=now)
        reviewer = TaskRun.request(
            task.id,
            "test-reviewer",
            agent_version_id=reviewer_version.id,
            agent_version_digest=reviewer_version.content_digest,
            role=RunRole.REVIEWER,
        )
        task.queue_review(
            persisted_executor.id,
            {"summary": "candidate"},
            reviewer.id,
            at=now,
        )
        uow.runs.save(persisted_executor)
        uow.runs.add(reviewer)
        uow.tasks.save(task)
        reviewer_envelope = MessageEnvelope.run_requested(
            tenant_id=task.tenant_id,
            task_id=task.id,
            run_id=reviewer.id,
            causation_id=executor_envelope.message_id,
            at=now,
        )
        uow.outbox.add(reviewer_envelope)
        uow.commit()

    runner = _CapturingWorkflowRunner(
        {"criteria": [{"key": "summary", "passed": True}], "feedback": []}
    )
    memory = _MemoryCaptureProbe()
    worker = RunExecutionService(
        uow_factory=uow_factory,
        workflow_runner=runner,
        runtime_memory_service=memory,
        worker_id="legacy-reviewed-memory-worker",
        consumer_name="legacy-reviewed-memory-worker-v1",
        lease_duration=timedelta(minutes=5),
        executor_agent_id="test-agent",
        reviewer_agent_id="test-reviewer",
        feature_gates=FeatureGateSet.from_config("minimal"),
    )

    assert worker.process(reviewer_envelope) is True
    assert runner.work_items
    work_item = runner.work_items[0]
    assert work_item.input["candidate_output"] == {"summary": "candidate"}
    assert "acceptance_criteria" in work_item.input
    assert "agentmesh_memory" not in work_item.input
    assert memory.assemble_calls == 0
    assert task_service.get_task(task_id).task.status is TaskStatus.COMPLETED


def test_managed_reviewed_invalid_decision_is_failed_with_actual_empty_settlement():
    budget = TaskBudget.create(
        deadline=utc_now() + timedelta(minutes=5),
        max_tokens=100,
        token_reservation_per_attempt=10,
    )
    case = _managed_reviewed_finalizer_case(
        role=RunRole.REVIEWER,
        phase=RuntimePhase.SUCCEEDED,
        budget=budget,
        quota=True,
    )
    uow_factory, tasks, worker, envelope, result, registry, memory, task_id, attempt = case
    result = replace(result, observation=replace(result.observation, output={}))
    assert worker._finalize_managed(
        envelope,
        task_id=task_id,
        run_id=attempt.run_id,
        attempt_id=attempt.id,
        result=result,
    ) is False
    aggregate = tasks.get_task(task_id)
    assert aggregate.task.status is TaskStatus.FAILED
    assert aggregate.task.error == "review.invalid_decision"
    assert aggregate.runs[1].status is RunStatus.FAILED
    assert aggregate.attempts[0].status is AttemptStatus.FAILED
    assert aggregate.attempts[0].budget_settlement_source is BudgetSettlementSource.ACTUAL
    assert aggregate.attempts[0].settled_tokens == 0
    assert aggregate.attempts[0].settled_cost_micros == 0
    assert aggregate.task.settled_tokens == 0
    assert aggregate.task.settled_cost_micros == 0
    assert aggregate.task.reserved_tokens == 0
    quota_after = deepcopy(uow_factory.store.quota_reservations)
    assert quota_after
    assert all(item.released_at is not None for item in quota_after.values())
    assert len(aggregate.runs) == 2
    assert memory.captures == 0
    assert registry.observations[0]["phase"] is RuntimeExecutionPhase.SUCCEEDED
    assert worker.process(envelope) is False
    assert len(uow_factory.store.inbox) == 1
    assert uow_factory.store.quota_reservations == quota_after
    assert len(
        [item for item in uow_factory.store.outbox if item.schema_name == "agentmesh.run.requested"]
    ) == 2


@pytest.mark.parametrize("role", [RunRole.EXECUTOR, RunRole.REVIEWER])
@pytest.mark.parametrize(
    "phase", [RuntimePhase.FAILED, RuntimePhase.TIMED_OUT, RuntimePhase.CANCELED]
)
@pytest.mark.parametrize("has_intent", [False, True])
def test_managed_reviewed_non_success_known_terminals(role, phase, has_intent):
    case = _managed_reviewed_finalizer_case(
        role=role,
        phase=phase,
        cancel_intent=(object() if has_intent else None),
    )
    uow_factory, tasks, worker, envelope, result, _registry, memory, task_id, attempt = case
    assert worker._finalize_managed(
        envelope,
        task_id=task_id,
        run_id=attempt.run_id,
        attempt_id=attempt.id,
        result=result,
    ) is False
    aggregate = tasks.get_task(task_id)
    expected_status = (
        TaskStatus.CANCELED
        if phase is RuntimePhase.CANCELED and has_intent
        else TaskStatus.FAILED
    )
    assert aggregate.task.status is expected_status
    assert aggregate.task.error == (
        None
        if phase is RuntimePhase.CANCELED and has_intent
        else "runtime.unrequested_cancellation"
        if phase is RuntimePhase.CANCELED
        else "runtime.timed_out"
        if phase is RuntimePhase.TIMED_OUT
        else "runtime.failed"
    )
    assert len(aggregate.runs) == (2 if role is RunRole.REVIEWER else 1)
    assert memory.captures == 0


@pytest.mark.parametrize("pause_phase", [RuntimePhase.SUCCEEDED, RuntimePhase.FAILED])
def test_managed_direct_pause_requested_uses_exact_terminal_path(pause_phase):
    case = _managed_direct_finalizer_case(phase=pause_phase)
    _uow_factory, tasks, worker, envelope, result, _registry, memory, task_id, attempt = case
    tasks.pause_task(task_id)
    assert worker._finalize_managed(
        envelope,
        task_id=task_id,
        run_id=attempt.run_id,
        attempt_id=attempt.id,
        result=result,
    ) is (pause_phase is RuntimePhase.SUCCEEDED)
    aggregate = tasks.get_task(task_id)
    expected = TaskStatus.COMPLETED if pause_phase is RuntimePhase.SUCCEEDED else TaskStatus.FAILED
    assert aggregate.task.status is expected
    assert aggregate.runs[0].status is (
        RunStatus.SUCCEEDED if pause_phase is RuntimePhase.SUCCEEDED else RunStatus.FAILED
    )
    assert aggregate.attempts[0].status is (
        AttemptStatus.SUCCEEDED if pause_phase is RuntimePhase.SUCCEEDED else AttemptStatus.FAILED
    )
    assert memory.captures == int(pause_phase is RuntimePhase.SUCCEEDED)


@pytest.mark.parametrize(
    ("phase", "invalid_observation"),
    [
        (RuntimePhase.SUCCEEDED, "usage"),
        (RuntimePhase.SUCCEEDED, "wait"),
        (RuntimePhase.SUCCEEDED, "actions"),
        (RuntimePhase.SUCCEEDED, "error"),
        (RuntimePhase.SUCCEEDED, "output"),
        (RuntimePhase.FAILED, "usage"),
        (RuntimePhase.CANCELED, "usage"),
        (RuntimePhase.TIMED_OUT, "usage"),
    ],
)
def test_managed_direct_invalid_terminal_contract_parks_as_unknown(
    phase, invalid_observation
):
    case = _managed_direct_finalizer_case(phase=phase)
    uow_factory, tasks, worker, envelope, result, registry, memory, task_id, attempt = case
    observation = result.observation
    if invalid_observation == "usage":
        observation = replace(observation, usage={"total": 1})
    elif invalid_observation == "wait":
        observation = replace(observation, wait_refs=("approval-1",))
    elif invalid_observation == "actions":
        observation = replace(observation, governed_action_requests=({"action": "write"},))
    elif invalid_observation == "output":
        observation = replace(observation, output="not-an-object")
    else:
        observation = replace(
            observation,
            error=RuntimeError(
                code="runtime.provider_error",
                category=ErrorCategory.UNKNOWN,
                message="provider error",
                retry_disposition=RetryDisposition.RECONCILE,
            ),
        )
    result = replace(result, observation=observation)

    assert worker._finalize_managed(
        envelope,
        task_id=task_id,
        run_id=attempt.run_id,
        attempt_id=attempt.id,
        result=result,
    ) is False
    aggregate = tasks.get_task(task_id)
    assert aggregate.task.status is TaskStatus.RECONCILIATION_REQUIRED
    assert aggregate.runs[0].status is RunStatus.RECONCILIATION_REQUIRED
    assert aggregate.attempts[0].status is AttemptStatus.OUTCOME_UNKNOWN
    assert memory.captures == 0
    assert len(registry.conflicts) == 1
    events = [
        item
        for item in uow_factory.store.outbox
        if item.schema_name == "agentmesh.runtime.reconciliation.required"
    ]
    assert len(events) == 1


@pytest.mark.parametrize(
    "invalid_binding", ["current_run", "role", "subtask", "owner", "fence"]
)
def test_managed_direct_invalid_business_binding_fails_before_authoritative_write(invalid_binding):
    case = _managed_direct_finalizer_case(phase=RuntimePhase.SUCCEEDED)
    uow_factory, tasks, worker, envelope, result, registry, memory, task_id, attempt = case
    if invalid_binding in {"owner", "fence"}:
        worker._uow_factory = _RuntimeAwareFactory(
            uow_factory,
            _RuntimeRepositoryProbe(
                registry,
                attempt.id,
                attempt.fencing_token + (1 if invalid_binding == "fence" else 0),
                owner_attempt_id=(uuid4() if invalid_binding == "owner" else None),
            ),
        )
    else:
        with uow_factory() as uow:
            task = uow.tasks.get(task_id, for_update=True)
            run = uow.runs.get(attempt.run_id, for_update=True)
            assert task is not None and run is not None
            if invalid_binding == "current_run":
                task.current_run_id = None
                uow.tasks.save(task)
            elif invalid_binding == "role":
                run.role = type(run.role).REVIEWER
                uow.runs.save(run)
            else:
                run.subtask_id = uuid4()
                uow.runs.save(run)
            uow.commit()
    with pytest.raises((InvalidMessage, RunLeaseUnavailable, InvalidTaskTransition)):
        worker._finalize_managed(
            envelope,
            task_id=task_id,
            run_id=attempt.run_id,
            attempt_id=attempt.id,
            result=result,
        )
    unchanged = tasks.get_task(task_id)
    assert unchanged.task.status is TaskStatus.RUNNING
    assert unchanged.runs[0].status is RunStatus.RUNNING
    assert unchanged.attempts[0].status is AttemptStatus.RUNNING
    assert memory.captures == 0
    assert registry.events == []
    assert not uow_factory.store.inbox


def test_managed_direct_coordinated_mode_still_fails_closed_before_accounting():
    for mode in (TaskExecutionMode.COORDINATED,):
        case = _managed_direct_finalizer_case(phase=RuntimePhase.SUCCEEDED)
        uow_factory, tasks, worker, envelope, result, registry, memory, task_id, attempt = case
        with uow_factory() as uow:
            task = uow.tasks.get(task_id, for_update=True)
            assert task is not None
            task.execution_mode = mode
            uow.tasks.save(task)
            uow.commit()
        with pytest.raises(InvalidTaskTransition, match="not enabled"):
            worker._finalize_managed(
                envelope,
                task_id=task_id,
                run_id=attempt.run_id,
                attempt_id=attempt.id,
                result=result,
            )
        unchanged = tasks.get_task(task_id)
        assert unchanged.task.status is TaskStatus.RUNNING
        assert unchanged.runs[0].status is RunStatus.RUNNING
        assert unchanged.attempts[0].status is AttemptStatus.RUNNING
        assert registry.events == []
        assert memory.captures == 0
        assert not uow_factory.store.inbox


def test_managed_direct_known_terminal_rolls_back_and_replays_once(monkeypatch):
    case = _managed_direct_finalizer_case(phase=RuntimePhase.SUCCEEDED)
    uow_factory, tasks, worker, envelope, result, registry, memory, task_id, attempt = case
    outbox_before = len(uow_factory.store.outbox)
    runtime_before = registry.snapshot()
    aggregate_before = tasks.get_task(task_id)
    with uow_factory() as probe:
        task_repository_type = type(probe.tasks)
        uow_type = type(probe)
    original_save = task_repository_type.save
    original_commit = uow_type.commit
    commit_calls = 0

    def fail_save(self, value):
        raise ValueError("business save failure")

    def count_commit(self):
        nonlocal commit_calls
        commit_calls += 1
        return original_commit(self)

    monkeypatch.setattr(task_repository_type, "save", fail_save)
    with pytest.raises(ValueError, match="business save failure"):
        worker._finalize_managed(
            envelope,
            task_id=task_id,
            run_id=attempt.run_id,
            attempt_id=attempt.id,
            result=result,
        )
    monkeypatch.setattr(task_repository_type, "save", original_save)
    monkeypatch.setattr(uow_type, "commit", count_commit)
    unchanged = tasks.get_task(task_id)
    assert unchanged.task.status is TaskStatus.RUNNING
    assert unchanged.runs[0].status is RunStatus.RUNNING
    assert unchanged.attempts[0].status is AttemptStatus.RUNNING
    assert unchanged.task.settled_tokens == aggregate_before.task.settled_tokens
    assert unchanged.task.reserved_tokens == aggregate_before.task.reserved_tokens
    assert unchanged.attempts[0].budget_settlement_source is None
    assert registry.snapshot() == runtime_before
    assert memory.captures == 0
    assert not uow_factory.store.inbox

    assert worker._finalize_managed(
        envelope,
        task_id=task_id,
        run_id=attempt.run_id,
        attempt_id=attempt.id,
        result=result,
    ) is True
    completed = tasks.get_task(task_id)
    assert completed.task.status is TaskStatus.COMPLETED
    assert completed.runs[0].status is RunStatus.SUCCEEDED
    assert completed.attempts[0].status is AttemptStatus.SUCCEEDED
    assert memory.captures == 1
    assert len(uow_factory.store.inbox) == 1
    assert len(uow_factory.store.outbox) == outbox_before
    assert commit_calls == 1
    assert worker.process(envelope) is False
    assert memory.captures == 1
    assert commit_calls == 1


def test_managed_direct_commit_failure_rolls_back_all_resources_and_replays(monkeypatch):
    budget = TaskBudget.create(
        deadline=utc_now() + timedelta(minutes=5),
        max_tokens=100,
        token_reservation_per_attempt=10,
    )
    case = _managed_direct_finalizer_case(
        phase=RuntimePhase.SUCCEEDED, budget=budget, quota=True
    )
    uow_factory, tasks, worker, envelope, result, registry, memory, task_id, attempt = case
    runtime_repo = _RuntimeRepositoryProbe(registry, attempt.id, attempt.fencing_token)
    worker._uow_factory = _RuntimeAwareFactory(
        uow_factory, runtime_repo, resources=(memory,)
    )
    store_before = _persistent_store_snapshot(uow_factory.store)
    runtime_before = registry.snapshot()
    task_before = tasks.get_task(task_id)
    assert task_before is not None
    with uow_factory() as probe:
        uow_type = type(probe)
    original_commit = uow_type.commit
    writes_seen = {}

    def commit_then_fail(self):
        writes_seen["business"] = any(
            value.status is TaskStatus.COMPLETED for value in self._tasks.values()
        )
        writes_seen["accounting"] = any(
            value.budget_settlement_source
            is BudgetSettlementSource.CONSERVATIVE_ESTIMATE
            for value in self._attempts.values()
        )
        writes_seen["quota"] = all(
            value.released_at is not None for value in self._quota_reservations.values()
        )
        writes_seen["inbox"] = bool(self._inbox)
        writes_seen["runtime"] = bool(registry.observations)
        writes_seen["memory"] = memory.captures == 1
        raise ValueError("commit failure")

    monkeypatch.setattr(uow_type, "commit", commit_then_fail)
    with pytest.raises(ValueError, match="commit failure"):
        worker._finalize_managed(
            envelope,
            task_id=task_id,
            run_id=attempt.run_id,
            attempt_id=attempt.id,
            result=result,
        )
    assert writes_seen == {
        "business": True,
        "accounting": True,
        "quota": True,
        "inbox": True,
        "runtime": True,
        "memory": True,
    }
    assert _persistent_store_snapshot(uow_factory.store) == store_before
    assert registry.snapshot() == runtime_before
    assert memory.captures == 0
    restored = tasks.get_task(task_id)
    assert restored.task == task_before.task
    assert restored.runs[0].status is RunStatus.RUNNING
    assert restored.attempts[0].budget_settlement_source is None
    assert not uow_factory.store.inbox
    assert len(uow_factory.store.quota_reservations) == 2
    assert all(
        item.released_at is None
        for item in uow_factory.store.quota_reservations.values()
    )

    monkeypatch.setattr(uow_type, "commit", original_commit)
    assert worker._finalize_managed(
        envelope,
        task_id=task_id,
        run_id=attempt.run_id,
        attempt_id=attempt.id,
        result=result,
    ) is True
    completed = tasks.get_task(task_id)
    assert completed.task.status is TaskStatus.COMPLETED
    assert (
        completed.attempts[0].budget_settlement_source
        is BudgetSettlementSource.CONSERVATIVE_ESTIMATE
    )
    assert all(
        item.released_at is not None
        for item in uow_factory.store.quota_reservations.values()
    )
    assert len(uow_factory.store.inbox) == 1
    assert memory.captures == 1
    assert worker.process(envelope) is False
    assert memory.captures == 1


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
    registry = _AtomicRuntimeRegistry()
    registry.assignment_snapshot = object()
    managed = _AuthoritativeManagedExecution(registry=registry)
    memory = _MemoryCaptureProbe()
    worker = RunExecutionService(
        uow_factory=uow_factory,
        workflow_runner=_PoisonWorkflowRunner(),
        managed_execution_service=managed,
        runtime_registry_service=registry,
        runtime_memory_service=memory,
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
    assert memory.assemble_calls == 0
    assert worker.process(envelope) is False
    assert managed.calls == 1
    assert registry.calls == 1


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


def test_managed_control_plane_failure_keeps_run_recoverable() -> None:
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
    task_id = tasks.create_task("recover control-plane failure").task.id
    tasks.request_run(task_id)
    envelope = uow_factory.store.outbox[-1]
    worker = RunExecutionService(
        uow_factory=uow_factory,
        workflow_runner=_PoisonWorkflowRunner(),
        managed_execution_service=_ControlPlaneFailureManagedExecution(),
        runtime_registry_service=_AtomicRuntimeRegistry(),
        worker_id="managed-worker",
        consumer_name="managed-worker-v1",
        lease_duration=timedelta(minutes=5),
    )

    with pytest.raises(ManagedRuntimeControlPlaneFailure):
        worker.process(envelope)

    recoverable = tasks.get_task(task_id)
    assert recoverable.task.status is TaskStatus.RUNNING
    assert recoverable.runs[0].status is RunStatus.RUNNING
    assert recoverable.attempts[0].status is AttemptStatus.RUNNING
    assert not uow_factory.store.inbox


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
    registry = _AtomicRuntimeRegistry()
    managed = _AuthoritativeManagedExecution(
        phase=RuntimePhase.OUTCOME_UNKNOWN, registry=registry
    )
    memory = _MemoryCaptureProbe()
    worker = RunExecutionService(
        uow_factory=uow_factory,
        workflow_runner=_PoisonWorkflowRunner(),
        managed_execution_service=managed,
        runtime_registry_service=registry,
        runtime_memory_service=memory,
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
    received_at = registry.observations[0]["now"]
    assert parked.task.updated_at == received_at
    assert parked.attempts[0].completed_at == received_at
    assert events[0].occurred_at == received_at
    assert worker.process(envelope) is False
    assert managed.calls == 1
    assert memory.captures == 0
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


def test_stale_crossed_execution_parking_rolls_back_task_and_attempt() -> None:
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
    task_id = tasks.create_task("stale crossed dispatch").task.id
    run = tasks.request_run(task_id).runs[0]
    envelope = uow_factory.store.outbox[-1]
    registry = _AtomicRuntimeRegistry(RuntimeObservationOutcome.STALE_OWNER)
    worker = RunExecutionService(
        uow_factory=uow_factory,
        workflow_runner=_PoisonWorkflowRunner(),
        managed_execution_service=_AuthoritativeManagedExecution(registry=registry),
        runtime_registry_service=registry,
        worker_id="managed-worker",
        consumer_name="managed-worker-v1",
        lease_duration=timedelta(seconds=-1),
    )
    _task, _run, owner = worker._acquire(
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
        attempt_id=owner.id,
        fencing_token=owner.fencing_token,
        expected_owner_attempt_id=None,
        expected_fencing_token=None,
        expected_version=1,
    )
    registry.execution = execution.apply_observation(
        phase=RuntimeExecutionPhase.DISPATCHING,
        provider_sequence=None,
    )

    with pytest.raises(RunLeaseUnavailable, match="STALE_OWNER"):
        worker.process(envelope)

    unchanged = tasks.get_task(task_id)
    assert unchanged.task.status is TaskStatus.RUNNING
    assert unchanged.runs[0].status is RunStatus.RUNNING
    assert unchanged.attempts[0].status is AttemptStatus.RUNNING
    assert not uow_factory.store.inbox
    assert not [
        item
        for item in uow_factory.store.outbox
        if item.schema_name == "agentmesh.runtime.reconciliation.required"
    ]


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
    runtime_registry = _AtomicRuntimeRegistry()
    provider_observed_at = datetime.now(timezone.utc) + timedelta(days=1)
    worker = RunExecutionService(
        uow_factory=uow_factory,
        workflow_runner=_PoisonWorkflowRunner(),
        managed_execution_service=_AuthoritativeManagedExecution(
            usage={"total": 1},
            registry=runtime_registry,
            observed_at=provider_observed_at,
        ),
        runtime_registry_service=runtime_registry,
        worker_id="managed-worker",
        consumer_name="managed-worker-v1",
        lease_duration=timedelta(minutes=5),
    )

    assert worker.process(envelope) is True
    conflict_count = len(runtime_registry.conflicts)
    observation_count = len(runtime_registry.observations)
    reconciliation_count = len(
        [
            item
            for item in uow_factory.store.outbox
            if item.schema_name == "agentmesh.runtime.reconciliation.required"
        ]
    )
    inbox_count = len(uow_factory.store.inbox)
    assert worker.process(envelope) is False
    assert len(runtime_registry.conflicts) == conflict_count
    assert len(runtime_registry.observations) == observation_count
    assert len(
        [
            item
            for item in uow_factory.store.outbox
            if item.schema_name == "agentmesh.runtime.reconciliation.required"
        ]
    ) == reconciliation_count
    assert len(uow_factory.store.inbox) == inbox_count
    rejected = tasks.get_task(task_id)
    assert rejected.task.status is TaskStatus.RECONCILIATION_REQUIRED
    assert rejected.runs[0].status is RunStatus.RECONCILIATION_REQUIRED
    assert rejected.attempts[0].status is AttemptStatus.OUTCOME_UNKNOWN
    assert runtime_registry.events == ["conflict", "observation"]
    assert len(runtime_registry.conflicts) == 1
    conflict = runtime_registry.conflicts[0]
    assert set(vars(conflict["observation"])) == {
        "observation_id",
        "observation_digest",
        "phase",
        "observed_at",
        "provider_sequence",
        "structural_invalid",
        "execution_id_mismatch",
        "assignment_id_mismatch",
        "assignment_digest_mismatch",
        "terminal_contract_invalid",
        "protocol_error_observation",
    }
    assert conflict["now"] == runtime_registry.observations[0]["now"]
    assert conflict["now"] < provider_observed_at
    assert runtime_registry.observations[0]["phase"] is RuntimeExecutionPhase.OUTCOME_UNKNOWN
    assert (
        runtime_registry.observations[0]["evidence"]["provider_event_id"]
        == "runtime.terminal_contract_invalid"
    )


@pytest.mark.parametrize("mode", ["noncanonical", "success"])
def test_managed_finalizer_rejects_conflict_with_non_synthetic_observation(mode: str) -> None:
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
    task_id = tasks.create_task("reject forged managed conflict").task.id
    tasks.request_run(task_id)
    envelope = uow_factory.store.outbox[-1]
    runtime_registry = _AtomicRuntimeRegistry()
    worker = RunExecutionService(
        uow_factory=uow_factory,
        workflow_runner=_PoisonWorkflowRunner(),
        managed_execution_service=_SuppliedConflictManagedExecution(
            runtime_registry, mode=mode
        ),
        runtime_registry_service=runtime_registry,
        worker_id="managed-worker",
        consumer_name="managed-worker-v1",
        lease_duration=timedelta(minutes=5),
    )

    with pytest.raises(InvalidMessage):
        worker.process(envelope)

    unchanged = tasks.get_task(task_id)
    assert unchanged.task.status is TaskStatus.RUNNING
    assert unchanged.runs[0].status is RunStatus.RUNNING
    assert unchanged.attempts[0].status is AttemptStatus.RUNNING
    assert runtime_registry.events == []
    assert not runtime_registry.conflicts


@pytest.mark.parametrize("failure_stage", ["after_conflict", "after_synthetic"])
def test_managed_finalizer_rolls_back_before_commit(failure_stage: str) -> None:
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
    task_id = tasks.create_task("rollback managed finalizer").task.id
    tasks.request_run(task_id)
    envelope = uow_factory.store.outbox[-1]
    runtime_registry = _AtomicRuntimeRegistry(failure_stage=failure_stage)
    worker = RunExecutionService(
        uow_factory=uow_factory,
        workflow_runner=_PoisonWorkflowRunner(),
        managed_execution_service=_AuthoritativeManagedExecution(
            usage={"total": 1}, registry=runtime_registry
        ),
        runtime_registry_service=runtime_registry,
        worker_id="managed-worker",
        consumer_name="managed-worker-v1",
        lease_duration=timedelta(minutes=5),
    )

    with pytest.raises(ValueError, match="evidence failure"):
        worker.process(envelope)

    unchanged = tasks.get_task(task_id)
    assert unchanged.task.status is TaskStatus.RUNNING
    assert unchanged.runs[0].status is RunStatus.RUNNING
    assert unchanged.attempts[0].status is AttemptStatus.RUNNING
    assert not uow_factory.store.inbox
    assert [
        item
        for item in uow_factory.store.outbox
        if item.schema_name == "agentmesh.runtime.reconciliation.required"
    ] == []


def test_managed_finalizer_rolls_back_when_reconciliation_outbox_fails(monkeypatch):
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
    task_id = tasks.create_task("rollback messaging").task.id
    tasks.request_run(task_id)
    envelope = uow_factory.store.outbox[-1]
    runtime_registry = _AtomicRuntimeRegistry()
    worker = RunExecutionService(
        uow_factory=uow_factory,
        workflow_runner=_PoisonWorkflowRunner(),
        managed_execution_service=_AuthoritativeManagedExecution(
            usage={"total": 1}, registry=runtime_registry
        ),
        runtime_registry_service=runtime_registry,
        worker_id="managed-worker",
        consumer_name="managed-worker-v1",
        lease_duration=timedelta(minutes=5),
    )
    with uow_factory() as probe:
        outbox_type = type(probe.outbox)
    original_add = outbox_type.add

    def fail_reconciliation(self, value):
        if value.schema_name == "agentmesh.runtime.reconciliation.required":
            raise ValueError("messaging failure")
        return original_add(self, value)

    monkeypatch.setattr(outbox_type, "add", fail_reconciliation)
    with pytest.raises(ValueError, match="messaging failure"):
        worker.process(envelope)

    unchanged = tasks.get_task(task_id)
    assert unchanged.task.status is TaskStatus.RUNNING
    assert unchanged.runs[0].status is RunStatus.RUNNING
    assert unchanged.attempts[0].status is AttemptStatus.RUNNING
    assert not uow_factory.store.inbox
    assert not [
        item
        for item in uow_factory.store.outbox
        if item.schema_name == "agentmesh.runtime.reconciliation.required"
    ]

def test_managed_finalizer_parks_result_assignment_metadata_conflict() -> None:
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
    task_id = tasks.create_task("reject crossed assignment metadata").task.id
    tasks.request_run(task_id)
    envelope = uow_factory.store.outbox[-1]
    runtime_registry = _AtomicRuntimeRegistry()
    worker = RunExecutionService(
        uow_factory=uow_factory,
        workflow_runner=_PoisonWorkflowRunner(),
        managed_execution_service=_AuthoritativeManagedExecution(
            registry=runtime_registry,
            result_assignment_id=uuid4(),
            result_assignment_digest="b" * 64,
        ),
        runtime_registry_service=runtime_registry,
        worker_id="managed-worker",
        consumer_name="managed-worker-v1",
        lease_duration=timedelta(minutes=5),
    )

    assert worker.process(envelope) is True

    parked = tasks.get_task(task_id)
    assert parked.task.status is TaskStatus.RECONCILIATION_REQUIRED
    assert parked.task.output is None
    assert parked.runs[0].status is RunStatus.RECONCILIATION_REQUIRED
    assert parked.attempts[0].status is AttemptStatus.OUTCOME_UNKNOWN
    assert runtime_registry.calls == 1
    assert runtime_registry.events == ["observation"]
    assert runtime_registry.conflicts == []
    observation = runtime_registry.observations[0]
    assert observation["phase"] is RuntimeExecutionPhase.OUTCOME_UNKNOWN
    assert observation["evidence"]["provider_event_id"] == "runtime.terminal_contract_invalid"
    assert observation["assignment_id"] == runtime_registry.execution.assignment_id
    assert observation["assignment_digest"] == runtime_registry.execution.assignment_digest


def test_late_managed_success_does_not_overwrite_cancellation() -> None:
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
    task_id = tasks.create_task("cancel during managed dispatch").task.id
    run = tasks.request_run(task_id).runs[0]
    envelope = uow_factory.store.outbox[-1]
    registry = _AtomicRuntimeRegistry()
    worker = RunExecutionService(
        uow_factory=uow_factory,
        workflow_runner=_PoisonWorkflowRunner(),
        managed_execution_service=_AuthoritativeManagedExecution(registry=registry),
        runtime_registry_service=registry,
        worker_id="managed-worker",
        consumer_name="managed-worker-v1",
        lease_duration=timedelta(minutes=5),
    )
    task, leased_run, attempt = worker._acquire(
        envelope, task_id=task_id, run_id=run.id
    )
    result = _AuthoritativeManagedExecution(registry=registry).execute_authoritative(
        task, leased_run, attempt
    )
    tasks.cancel_task(task_id)

    with pytest.raises(RunLeaseUnavailable, match="business chain"):
        worker._finalize_managed(
            envelope,
            task_id=task_id,
            run_id=run.id,
            attempt_id=attempt.id,
            result=result,
        )

    canceled = tasks.get_task(task_id)
    assert canceled.task.status is TaskStatus.CANCELED
    assert canceled.runs[0].status is RunStatus.CANCELED
    assert canceled.attempts[0].status is AttemptStatus.CANCELED
    assert canceled.task.output is None
    assert registry.calls == 0


@pytest.mark.parametrize(
    "phase",
    [
        RuntimePhase.SUCCEEDED,
        RuntimePhase.FAILED,
        RuntimePhase.CANCELED,
        RuntimePhase.TIMED_OUT,
        RuntimePhase.OUTCOME_UNKNOWN,
        RuntimePhase.LOST,
    ],
)
def test_managed_canceled_chain_requires_persisted_intent_and_is_runtime_only(phase) -> None:
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
    task_id = tasks.create_task("canceled runtime-only convergence").task.id
    run = tasks.request_run(task_id).runs[0]
    envelope = uow_factory.store.outbox[-1]
    registry = _AtomicRuntimeRegistry()
    worker = RunExecutionService(
        uow_factory=uow_factory,
        workflow_runner=_PoisonWorkflowRunner(),
        managed_execution_service=_AuthoritativeManagedExecution(
            phase=phase, registry=registry
        ),
        runtime_registry_service=registry,
        worker_id="managed-worker",
        consumer_name="managed-worker-v1",
        lease_duration=timedelta(minutes=5),
    )
    _task, leased_run, attempt = worker._acquire(envelope, task_id=task_id, run_id=run.id)
    result = _AuthoritativeManagedExecution(phase=phase, registry=registry).execute_authoritative(
        _task, leased_run, attempt
    )
    tasks.cancel_task(task_id)
    runtime_repo = _RuntimeRepositoryProbe(
        registry, attempt.id, attempt.fencing_token, cancel_intent=object()
    )
    worker._uow_factory = _RuntimeAwareFactory(uow_factory, runtime_repo)
    worker._finalize_managed(
        envelope,
        task_id=task_id,
        run_id=run.id,
        attempt_id=attempt.id,
        result=result,
    )
    canceled = tasks.get_task(task_id)
    assert canceled.task.status is TaskStatus.CANCELED
    assert canceled.runs[0].status is RunStatus.CANCELED
    assert canceled.attempts[0].status is AttemptStatus.CANCELED
    evidence = registry.observations[0]["evidence"]
    if phase is RuntimePhase.SUCCEEDED:
        assert evidence["quarantined_output"] == {"managed": True}
        assert not [item for item in uow_factory.store.outbox
                    if item.schema_name == "agentmesh.runtime.reconciliation.required"]
    else:
        expected_events = phase in {RuntimePhase.OUTCOME_UNKNOWN, RuntimePhase.LOST}
        assert len(
            [
                item
                for item in uow_factory.store.outbox
                if item.schema_name == "agentmesh.runtime.reconciliation.required"
            ]
        ) == int(expected_events)


def test_managed_canceled_chain_without_intent_fails_before_runtime_write() -> None:
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
    task_id = tasks.create_task("canceled intent fence").task.id
    run = tasks.request_run(task_id).runs[0]
    envelope = uow_factory.store.outbox[-1]
    registry = _AtomicRuntimeRegistry()
    worker = RunExecutionService(
        uow_factory=uow_factory,
        workflow_runner=_PoisonWorkflowRunner(),
        managed_execution_service=_AuthoritativeManagedExecution(registry=registry),
        runtime_registry_service=registry,
        worker_id="managed-worker",
        consumer_name="managed-worker-v1",
        lease_duration=timedelta(minutes=5),
    )
    _task, leased_run, attempt = worker._acquire(envelope, task_id=task_id, run_id=run.id)
    result = _AuthoritativeManagedExecution(registry=registry).execute_authoritative(
        _task, leased_run, attempt
    )
    tasks.cancel_task(task_id)
    worker._uow_factory = _RuntimeAwareFactory(
        uow_factory,
        _RuntimeRepositoryProbe(registry, attempt.id, attempt.fencing_token),
    )
    with pytest.raises(RunLeaseUnavailable, match="business chain"):
        worker._finalize_managed(
            envelope,
            task_id=task_id,
            run_id=run.id,
            attempt_id=attempt.id,
            result=result,
        )
    assert registry.events == []


def test_managed_success_honors_budget_deadline_during_atomic_finalization() -> None:
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
    task_id = tasks.create_task(
        "managed budget deadline",
        budget=TaskBudget.create(
            deadline=utc_now() + timedelta(minutes=5),
            max_tokens=100,
            token_reservation_per_attempt=10,
        ),
    ).task.id
    run = tasks.request_run(task_id).runs[0]
    envelope = uow_factory.store.outbox[-1]
    worker = RunExecutionService(
        uow_factory=uow_factory,
        workflow_runner=_PoisonWorkflowRunner(),
        managed_execution_service=_AuthoritativeManagedExecution(
            registry=(registry := _AtomicRuntimeRegistry())
        ),
        runtime_registry_service=registry,
        worker_id="managed-worker",
        consumer_name="managed-worker-v1",
        lease_duration=timedelta(minutes=5),
    )
    task, leased_run, attempt = worker._acquire(
        envelope, task_id=task_id, run_id=run.id
    )
    result = _AuthoritativeManagedExecution(registry=registry).execute_authoritative(
        task, leased_run, attempt
    )
    with uow_factory() as uow:
        current = uow.tasks.get(task_id, for_update=True)
        assert current is not None and current.budget is not None
        current.budget = replace(
            current.budget, deadline=utc_now() - timedelta(seconds=1)
        )
        uow.tasks.save(current)
        uow.commit()

    worker._finalize_managed(
        envelope,
        task_id=task_id,
        run_id=run.id,
        attempt_id=attempt.id,
        result=result,
    )

    waiting = tasks.get_task(task_id)
    assert waiting.task.status is TaskStatus.WAITING_APPROVAL
    assert waiting.task.error == "budget_deadline_exceeded"
    assert waiting.task.candidate_output == {"managed": True}
    assert waiting.runs[0].status is RunStatus.SUCCEEDED
    assert waiting.attempts[0].status is AttemptStatus.SUCCEEDED


def test_managed_completion_captures_memory_and_research_failure_is_non_authoritative() -> None:
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
    task_id = tasks.create_task("managed memory parity").task.id
    tasks.request_run(task_id)
    memory = _MemoryCaptureProbe()
    research = _ResearchProbe(fail=True)
    worker = RunExecutionService(
        uow_factory=uow_factory,
        workflow_runner=_PoisonWorkflowRunner(),
        managed_execution_service=_AuthoritativeManagedExecution(
            registry=(registry := _AtomicRuntimeRegistry())
        ),
        runtime_registry_service=registry,
        runtime_memory_service=memory,
        research_materialization_service=research,
        worker_id="managed-worker",
        consumer_name="managed-worker-v1",
        lease_duration=timedelta(minutes=5),
    )

    assert worker.process(uow_factory.store.outbox[-1]) is True
    assert tasks.get_task(task_id).task.status is TaskStatus.COMPLETED
    assert memory.captures == 1
    assert research.calls == 1


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
    # Initial admission now verifies the task has no prior Runs while holding
    # the Task lock; the batch listing itself still uses list_for_tasks.
    assert uow_factory.store.run_list_for_task_calls == 2
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

    def run(self, task, run, attempt, *, work_item=None):
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
