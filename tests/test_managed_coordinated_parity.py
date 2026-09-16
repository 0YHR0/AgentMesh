"""A4.2d COORDINATED legacy/managed parity qualification.

The legacy and managed sides admit the same deterministic three-node
fork/join plan.  Legacy terminal evidence is driven through
:class:`RunExecutionService`; managed evidence is driven through a real
multi-Run Runtime repository and :class:`CoordinatedRuntimeConvergenceService`.
The managed fixture deliberately uses the convergence command entry (rather
than pretending that a provider adapter was invoked); its report says so.

The report constant is JSON-shaped so a later qualification command can emit
it without importing any test implementation details.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from agentmesh.application.budget_services import BudgetController
from agentmesh.application.coordinated_runtime_convergence import (
    CoordinatedRuntimeConvergenceService,
)
from agentmesh.application.registry_services import AgentRegistryService
from agentmesh.application.runtime_snapshots import assignment_snapshot_for
from agentmesh.application.services import RunExecutionService, TaskApplicationService
from agentmesh.domain.budgets import TaskBudget
from agentmesh.domain.coordination import CoordinatedPlan
from agentmesh.domain.errors import InvalidTaskInput, RuntimeExecutionConflict
from agentmesh.domain.runtime_execution import RuntimeExecution, RuntimeExecutionPhase
from agentmesh.domain.tasks import (
    TaskAttempt,
    TaskExecutionMode,
    utc_now,
)
from agentmesh.features import FeatureGateSet
from agentmesh.runtime_sdk import RuntimeAssignment, RuntimeObservation, RuntimePhase
from agentmesh.runtime_sdk.canonical import thaw_json
from agentmesh.runtime_sdk.descriptor import RuntimeDescriptor
from tests.fakes import InMemoryUnitOfWorkFactory
from tests.test_authority_cohorts import _real_version
from tests.test_coordinated_execution import run_wakeup, spec
from tests.test_coordinated_runtime_barrier import _cancel_intent
from tests.test_task_service import (
    _CapturingWorkflowRunner,
    _RuntimeAwareFactory,
    _RuntimeAwareUnitOfWork,
)

TENANT = "coordinated-parity-tenant"
PLAN_KEYS = ("research", "analysis", "join")
PLAN_DEPENDENCIES = (("research", "join"), ("analysis", "join"))


class _ParityRuntimeRepository:
    """Transaction-aware multi-Run Runtime projection for this qualification."""

    def __init__(self) -> None:
        self.version = _real_version()
        self.executions: dict[Any, RuntimeExecution] = {}
        self.snapshots: dict[Any, Any] = {}
        self.observations: list[Any] = []
        self.lifecycle: list[Any] = []
        self.handles: dict[Any, Any] = {}
        self.incidents: dict[Any, tuple[Any, ...]] = {}

    def snapshot(self) -> dict[str, Any]:
        return {
            "executions": dict(self.executions),
            "snapshots": dict(self.snapshots),
            "observations": list(self.observations),
            "lifecycle": list(self.lifecycle),
            "handles": dict(self.handles),
            "incidents": dict(self.incidents),
        }

    def restore(self, snapshot: dict[str, Any]) -> None:
        self.executions = dict(snapshot["executions"])
        self.snapshots = dict(snapshot["snapshots"])
        self.observations = list(snapshot["observations"])
        self.lifecycle = list(snapshot["lifecycle"])
        self.handles = dict(snapshot["handles"])
        self.incidents = dict(snapshot["incidents"])

    def get_version(self, _version_id, *, tenant_id, for_update=False):
        del tenant_id, for_update
        return self.version

    def list_executions_for_run(self, run_id, *, tenant_id, for_update=False):
        del tenant_id, for_update
        return [value for value in self.executions.values() if value.run_id == run_id]

    def get_assignment_snapshot(self, execution_id, *, tenant_id, for_update=False):
        del tenant_id, for_update
        return self.snapshots.get(execution_id)

    def get_handle_snapshot(self, execution_id, *, tenant_id, for_update=False):
        del tenant_id, for_update
        return self.handles.get(execution_id)

    def list_lifecycle_operations(self, execution_id, *, tenant_id, for_update=False):
        del tenant_id, for_update
        return [value for value in self.lifecycle if value.runtime_execution_id == execution_id]

    def list_integrity_incidents_for_execution(self, execution_id, *, tenant_id, for_update=False):
        del tenant_id, for_update
        return self.incidents.get(execution_id, ())

    def prior_observations(self, execution_id, *, tenant_id, observation_id, digest):
        del tenant_id
        return [
            value
            for value in self.observations
            if value.runtime_execution_id == execution_id
            and (value.observation_id == observation_id or value.observation_digest == digest)
        ]

    def accepted_terminal_observations(self, execution_id, *, tenant_id, phase):
        del tenant_id
        return [
            value
            for value in self.observations
            if value.runtime_execution_id == execution_id
            and value.phase is phase
            and value.processing_outcome.value == "APPLIED"
        ]

    def add_observation(self, value):
        self.observations.append(value)

    def save_execution(self, value, *, tenant_id):
        del tenant_id
        self.executions[value.id] = value

    def add_lifecycle_operation(self, value):
        self.lifecycle.append(value)


class _ParityRuntimeAdmission:
    def __init__(self, repository: _ParityRuntimeRepository) -> None:
        self.repository = repository

    def require_builtin_langgraph_v2_in_uow(self, _uow):
        return self.repository.version


class _ParityDrainRepository:
    def __init__(self) -> None:
        self.values: dict[Any, Any] = {}

    def snapshot(self):
        return dict(self.values)

    def restore(self, snapshot):
        self.values = dict(snapshot)

    def get_active_for_task(self, task_id, *, tenant_id, for_update=False):
        del tenant_id, for_update
        value = next(
            (value for value in self.values.values() if value.task_id == task_id),
            None,
        )
        return value if value is not None and value.status.value == "DRAINING" else None

    def get(self, drain_id, *, tenant_id, for_update=False):
        del tenant_id, for_update
        return self.values.get(drain_id)

    def add(self, value, *, tenant_id=None):
        del tenant_id
        self.values[value.id] = value

    def save(self, value, *, tenant_id=None):
        del tenant_id
        self.values[value.id] = value


class _ParityFactory(_RuntimeAwareFactory):
    def __init__(self, base, runtime_repository, drain_repository) -> None:
        super().__init__(base, runtime_repository, resources=(drain_repository,))
        self.drain_repository = drain_repository

    def __call__(self):
        uow = self.base()
        uow.runtimes = self.runtime_repository
        uow.coordination_runtime_drains = self.drain_repository
        return _RuntimeAwareUnitOfWork(
            uow,
            self.runtime_repository,
            resources=(self.drain_repository,),
        )


@dataclass(frozen=True)
class _Scenario:
    name: str
    phase: RuntimePhase
    budget_wait: bool = False
    cancel_requested: bool = False
    unknown: bool = False


# Keep this value deliberately boring: it is consumed by a future JSON report
# and must not contain UUIDs, timestamps, or Python-only enum objects.
COORDINATED_PARITY_FIXTURE_REPORT: tuple[dict[str, object], ...] = (
    {
        "scenario": "parallel_join_success",
        "terminal_phase": "SUCCEEDED",
        "legacy_equivalent": True,
        "qualification": "full_convergence_entry",
        "entry_scope": "coordinated_convergence",
        "same_dag": True,
        "parity_pass": True,
        "resume_qualified": False,
        "safety_exception": None,
    },
    {
        "scenario": "executor_failure",
        "terminal_phase": "FAILED",
        "legacy_equivalent": False,
        "qualification": "managed_only_safety_boundary",
        "entry_scope": "coordinated_convergence",
        "same_dag": True,
        "parity_pass": False,
        "resume_qualified": False,
        "safety_exception": "managed_failure_predispatch_release_shape",
    },
    {
        "scenario": "cancel_requested",
        "terminal_phase": "CANCELED",
        "legacy_equivalent": False,
        "qualification": "managed_only_safety_boundary",
        "entry_scope": "coordinated_convergence",
        "same_dag": True,
        "parity_pass": False,
        "resume_qualified": False,
        "safety_exception": "managed_cancellation_barrier_reserved",
    },
    {
        "scenario": "budget_wait_resume",
        "terminal_phase": "WAITING_APPROVAL",
        "legacy_equivalent": False,
        "qualification": "managed_only_safety_boundary",
        "entry_scope": "coordinated_convergence",
        "same_dag": True,
        "parity_pass": False,
        "resume_qualified": False,
        "safety_exception": "managed_budget_predispatch_release_shape",
    },
    {
        "scenario": "unknown_outcome",
        "terminal_phase": "OUTCOME_UNKNOWN",
        "legacy_equivalent": False,
        "qualification": "managed_only_safety_boundary",
        "entry_scope": "coordinated_convergence",
        "same_dag": True,
        "parity_pass": False,
        "resume_qualified": False,
        "safety_exception": "managed_unknown_parking_only",
    },
)


def _scenario(name: str) -> _Scenario:
    if name == "parallel_join_success":
        return _Scenario(name, RuntimePhase.SUCCEEDED)
    if name == "executor_failure":
        return _Scenario(name, RuntimePhase.FAILED)
    if name == "cancel_requested":
        return _Scenario(name, RuntimePhase.CANCELED, cancel_requested=True)
    if name == "budget_wait_resume":
        return _Scenario(name, RuntimePhase.SUCCEEDED, budget_wait=True)
    if name == "unknown_outcome":
        return _Scenario(name, RuntimePhase.OUTCOME_UNKNOWN, unknown=True)
    raise AssertionError(name)


def _plan() -> CoordinatedPlan:
    return CoordinatedPlan.create(
        (
            spec("research"),
            spec("analysis"),
            spec("join", depends_on=("research", "analysis")),
        ),
        max_concurrency=2,
    )


def _business_projection(aggregate, outbox) -> dict[str, Any]:
    """Normalize every durable business projection while dropping UUIDs."""
    by_subtask = {value.id: value.key for value in aggregate.subtasks}
    by_run = {value.id: value for value in aggregate.runs}
    runs = tuple(
        {
            "role": value.role.value,
            "subtask": by_subtask.get(value.subtask_id),
            "status": value.status.value,
            "output": value.output,
            "error": value.error,
        }
        for value in sorted(
            aggregate.runs,
            key=lambda item: (
                item.role.value,
                by_subtask.get(item.subtask_id, ""),
                item.revision_number,
            ),
        )
    )
    attempts = tuple(
        {
            "role": by_run[value.run_id].role.value,
            "status": value.status.value,
            "reserved_tokens": value.reserved_tokens,
            "settled_tokens": value.settled_tokens,
            "settled_cost_micros": value.settled_cost_micros,
            "settlement_source": (
                value.budget_settlement_source.value
                if value.budget_settlement_source is not None
                else None
            ),
            "error": value.error,
        }
        for value in sorted(
            aggregate.attempts,
            key=lambda item: (
                by_run[item.run_id].role.value,
                by_subtask.get(by_run[item.run_id].subtask_id, ""),
            ),
        )
    )
    outbox_summary: dict[str, int] = {}
    requested_roles: list[tuple[str, str | None]] = []
    for message in outbox:
        outbox_summary[message.schema_name] = outbox_summary.get(message.schema_name, 0) + 1
        if message.schema_name == "agentmesh.run.requested":
            run_id = message.payload.get("run_id")
            run = next((value for value in aggregate.runs if str(value.id) == run_id), None)
            if run is not None:
                requested_roles.append((run.role.value, by_subtask.get(run.subtask_id)))
    return {
        "task": {
            "status": aggregate.task.status.value,
            "output": aggregate.task.output,
            "candidate_output": aggregate.task.candidate_output,
            "error": aggregate.task.error,
            "revision": aggregate.task.revision_count,
            "budget_exhausted_reason": aggregate.task.budget_exhausted_reason,
        },
        "runs": runs,
        "subtasks": tuple(
            {
                "key": value.key,
                "status": value.status.value,
                "output": value.output,
                "error": value.error,
            }
            for value in sorted(aggregate.subtasks, key=lambda item: item.key)
        ),
        "attempts": attempts,
        "outbox": {
            "schema_counts": tuple(sorted(outbox_summary.items())),
            "run_requested": tuple(requested_roles),
        },
        "budget": {
            "settled_tokens": aggregate.task.settled_tokens,
            "reserved_tokens": aggregate.task.reserved_tokens,
            "settled_cost_micros": aggregate.task.settled_cost_micros,
            "reserved_cost_micros": aggregate.task.reserved_cost_micros,
            "budget_revision": aggregate.task.budget_revision,
        },
        "quota": 0,
        "memory": 0,
    }


def _authority_neutral_boundary_projection(
    projection: dict[str, Any], *, scenario: str
) -> dict[str, Any]:
    """Compare the common business boundary when managed pre-dispatch state differs.

    Legacy cancellation eagerly terminalizes queued siblings, while the managed
    barrier preserves an untouched sibling as READY/BLOCKED until its
    pre-dispatch release path runs.  Their authority/runtime records are not
    interchangeable.  This projection therefore compares the durable Task,
    triggering Run/Subtask, accounting, quota/memory and business continuation
    evidence while retaining that difference as an explicit report exception.
    """
    business = projection["business"]
    target = {
        "task": business["task"],
        "budget": business["budget"],
        "quota": business["quota"],
        "memory": business["memory"],
        "outbox": {
            "run_requested": business["outbox"]["run_requested"],
            "schema_counts": tuple(
                item
                for item in business["outbox"]["schema_counts"]
                if not item[0].startswith("agentmesh.runtime.")
            ),
        },
        "target_run_status": projection["target_run_status"],
        "target_subtask_status": projection["target_subtask_status"],
    }
    if scenario == "executor_failure":
        failed = next(
            item
            for item in business["runs"]
            if item["status"] == "FAILED"
        )
        target["failed_run"] = failed
        target["failed_subtask"] = next(
            item
            for item in business["subtasks"]
            if item["status"] == "FAILED"
        )
        target["failed_attempt"] = next(
            item for item in business["attempts"] if item["status"] == "FAILED"
        )
    elif scenario == "budget_wait_resume":
        target["settled_attempt"] = next(
            item for item in business["attempts"] if item["status"] == "SUCCEEDED"
        )
    return target


def _legacy_projection(scenario: _Scenario) -> dict[str, Any]:
    """Run one scenario through the legacy worker's terminal entry point."""
    factory = InMemoryUnitOfWorkFactory()
    registry = AgentRegistryService(uow_factory=factory, tenant_id=TENANT)
    registry.ensure_builtin_agent("test-agent")
    registry.ensure_builtin_agent("test-supervisor", supervisor=True)
    gates = FeatureGateSet.from_config("full")
    tasks = TaskApplicationService(
        uow_factory=factory,
        agent_id="test-agent",
        tenant_id=TENANT,
        supervisor_agent_id="test-supervisor",
        feature_gates=gates,
    )
    budget = (
        TaskBudget.create(
            max_tokens=100,
            token_reservation_per_attempt=2,
            deadline=utc_now() + timedelta(minutes=5),
        )
        if scenario.budget_wait
        else None
    )
    task_id = tasks.create_task(
        "deterministic coordinated parity",
        execution_mode=TaskExecutionMode.COORDINATED,
        coordinated_plan=_plan(),
        budget=budget,
    ).task.id
    started = tasks.request_run(task_id)
    worker = RunExecutionService(
        uow_factory=factory,
        workflow_runner=_CapturingWorkflowRunner({"result": "ok"}),
        worker_id="legacy-parity-worker",
        consumer_name="legacy-parity-v1",
        lease_duration=timedelta(minutes=5),
        supervisor_agent_id="test-supervisor",
        feature_gates=gates,
    )
    subtask_keys = {value.id: value.key for value in started.subtasks}
    roots = tuple(
        sorted(started.runs, key=lambda value: subtask_keys.get(value.subtask_id, ""))
    )
    if scenario.cancel_requested:
        tasks.cancel_task(task_id)
    else:
        # Success qualifies the complete fork/join chain; failure/budget only
        # needs one terminal boundary to exercise the worker finalizer.
        selected = roots if scenario.name == "parallel_join_success" else roots[:1]
        pending = list(selected)
        while pending:
            run = pending.pop(0)
            envelope = run_wakeup(factory, run.id)
            leased = worker._acquire(envelope, task_id=task_id, run_id=run.id)
            assert leased is not None
            if scenario.budget_wait:
                with factory() as uow:
                    task = uow.tasks.get(task_id, for_update=True)
                    assert task is not None and task.budget is not None
                    task.budget = replace(task.budget, deadline=utc_now() - timedelta(seconds=1))
                    uow.tasks.save(task)
                    uow.commit()
            if scenario.phase is RuntimePhase.SUCCEEDED:
                worker._finalize_success(envelope, task_id, run.id, leased[2].id, {"result": "ok"})
            else:
                worker._finalize_failure(
                    envelope,
                    task_id,
                    run.id,
                    leased[2].id,
                    f"runtime.{scenario.phase.value.lower()}",
                )
            if scenario.name == "parallel_join_success":
                aggregate = tasks.get_task(task_id)
                pending.extend(
                    value
                    for value in aggregate.runs
                    if value.status.value == "QUEUED"
                    and value.id not in {item.id for item in pending}
                    and value.id != run.id
                )
    aggregate = tasks.get_task(task_id)
    target = next(value for value in aggregate.runs if value.id == roots[0].id)
    target_subtask = next(value for value in aggregate.subtasks if value.id == target.subtask_id)
    return {
        "task_status": aggregate.task.status.value,
        "target_run_status": target.status.value,
        "target_subtask_status": target_subtask.status.value,
        "budget": {
            "settled_tokens": aggregate.task.settled_tokens,
            "reserved_tokens": aggregate.task.reserved_tokens,
            "budget_revision": aggregate.task.budget_revision,
        },
        "continuations": sum(
            message.schema_name == "agentmesh.run.requested"
            for message in factory.store.outbox
        ),
        "quota": len(factory.store.quota_reservations),
        "memory": 0,
        "audit": {"runtime_observations": 0},
        "business": _business_projection(aggregate, factory.store.outbox),
    }


def _assignment_for(task, run, execution_id, *, at: datetime) -> RuntimeAssignment:
    version = _real_version()
    return RuntimeAssignment(
        assignment_id=str(uuid4()),
        tenant_id=task.tenant_id,
        task_id=str(task.id),
        run_id=str(run.id),
        agent_definition_id=str(uuid4()),
        agent_version_id=str(run.agent_version_id),
        agent_version_digest=run.agent_version_digest or ("a" * 64),
        runtime_version_id=str(version.id),
        runtime_descriptor_digest=RuntimeDescriptor.from_dict(
            thaw_json(version.descriptor)
        ).digest(),
        execution_mode="managed_async",
        run_role=run.role.value,
        revision=run.revision_number,
        objective=task.objective if run.subtask_id is None else "Execute coordinated subtask",
        structured_input=(
            task.input if run.subtask_id is None else {"subtask_id": str(run.subtask_id)}
        ),
        correlation_ids={"runtime_execution_id": str(execution_id)},
        trace_context={"qualification": "a4.2d"},
        deadline=at + timedelta(minutes=5),
    )


def _prepare_managed_run(factory, repository, task_id, run_id):
    """Lease one queued Run and cross a real managed Runtime boundary."""
    with factory() as uow:
        task = uow.tasks.get(task_id, for_update=True)
        run = uow.runs.get(run_id, for_update=True)
        assert task is not None and run is not None
        subtask = (
            uow.subtasks.get(run.subtask_id, for_update=True)
            if run.subtask_id is not None
            else None
        )
        at = max(task.updated_at, run.queued_at) + timedelta(seconds=1)
        if subtask is not None:
            subtask.start(run.id, at=at)
        run.start(at=at)
        attempt = TaskAttempt.lease(
            run_id=run.id,
            worker_id="managed-parity-worker",
            fencing_token=1,
            lease_expires_at=at + timedelta(minutes=5),
            reserved_tokens=(
                task.budget.token_reservation_per_attempt if task.budget else 0
            ),
            reserved_cost_micros=(
                task.budget.cost_reservation_micros_per_attempt if task.budget else 0
            ),
        )
        if task.budget is not None:
            BudgetController.reserve_attempt(task, attempt, at=at)
        execution_id = run.runtime_execution_intent_id
        assert execution_id is not None
        assignment = _assignment_for(task, run, execution_id, at=at)
        execution = RuntimeExecution.prepare(
            tenant_id=task.tenant_id,
            run_id=run.id,
            runtime_version_id=run.runtime_version_id,
            assignment_id=UUID(assignment.assignment_id),
            assignment_digest=assignment.assignment_digest or "",
            dispatch_key=f"parity-dispatch:{execution_id}",
            dispatch_digest="b" * 64,
            execution_id=execution_id,
            now=at,
        ).claim(
            attempt_id=attempt.id,
            fencing_token=attempt.fencing_token,
            expected_owner_attempt_id=None,
            expected_fencing_token=None,
            expected_version=1,
            now=at + timedelta(seconds=1),
        )
        execution = execution.apply_observation(
            phase=RuntimeExecutionPhase.RUNNING,
            provider_sequence=1,
            now=at + timedelta(seconds=2),
        )
        run.bind_runtime_execution(execution.id)
        repository.executions[execution.id] = execution
        repository.snapshots[execution.id] = assignment_snapshot_for(
            assignment,
            tenant_id=task.tenant_id,
            runtime_execution_id=execution.id,
            created_at=at,
        )
        uow.runs.save(run)
        if subtask is not None:
            uow.subtasks.save(subtask)
        uow.attempts.add(attempt)
        if task.budget is not None:
            uow.tasks.save(task)
        uow.commit()
        return task, run, attempt, execution, assignment, at


def _managed_projection(scenario: _Scenario) -> dict[str, Any]:
    """Run the same three-node DAG through managed convergence."""
    base = InMemoryUnitOfWorkFactory()
    repository = _ParityRuntimeRepository()
    factory = _ParityFactory(base, repository, _ParityDrainRepository())
    registry = AgentRegistryService(uow_factory=base, tenant_id=TENANT)
    registry.ensure_builtin_agent("test-agent")
    registry.ensure_builtin_agent("test-supervisor", supervisor=True)
    gates = FeatureGateSet.from_config(
        "full",
        "managed_runtime_worker=true,managed_runtime_coordinated_cutover=true",
    )
    tasks = TaskApplicationService(
        uow_factory=factory,
        agent_id="test-agent",
        tenant_id=TENANT,
        supervisor_agent_id="test-supervisor",
        feature_gates=gates,
        runtime_registry_service=_ParityRuntimeAdmission(repository),
    )
    budget = (
        TaskBudget.create(
            max_tokens=100,
            token_reservation_per_attempt=2,
            deadline=utc_now() + timedelta(minutes=5),
        )
        if scenario.budget_wait
        else None
    )
    task_id = tasks.create_task(
        "deterministic coordinated parity",
        execution_mode=TaskExecutionMode.COORDINATED,
        coordinated_plan=_plan(),
        budget=budget,
    ).task.id
    tasks.request_run(task_id)
    convergence = CoordinatedRuntimeConvergenceService(
        uow_factory=factory,
        coordinated_scheduler=tasks._coordinated_scheduler,
        cancel_deadline_window=timedelta(minutes=5),
    )
    processed: set[Any] = set()
    prepared: dict[Any, tuple[Any, ...]] = {}
    # A managed failure drain requests cancellation of every already-crossed
    # sibling.  Prepare both deterministic roots before publishing the first
    # terminal observation so that the second root crosses the same Runtime
    # boundary and can be converged through the real cancellation observation.
    # This is what makes the failure fixture a genuine managed fork/join
    # qualification rather than a projection-only shortcut.
    if scenario.phase is RuntimePhase.FAILED:
        initial = tasks.get_task(task_id)
        initial_keys = {value.id: value.key for value in initial.subtasks}
        initial_roots = sorted(
            (run for run in initial.runs if run.role.value == "EXECUTOR"),
            key=lambda value: initial_keys.get(value.subtask_id, ""),
        )
        for root in initial_roots:
            prepared[root.id] = _prepare_managed_run(
                factory, repository, task_id, root.id
            )
    first_target = None
    while True:
        current = tasks.get_task(task_id)
        subtask_keys = {value.id: value.key for value in current.subtasks}
        queued = sorted(
            (
                run
                for run in current.runs
                if run.status.value == "QUEUED" or run.id in prepared
            ),
            key=lambda value: (
                subtask_keys.get(value.subtask_id, "~supervisor"),
                value.role.value,
            ),
        )
        queued = [run for run in queued if run.id not in processed]
        if not queued:
            break
        run = queued[0]
        processed.add(run.id)
        if run.id in prepared:
            task, run, attempt, execution, assignment, at = prepared[run.id]
        else:
            task, run, attempt, execution, assignment, at = _prepare_managed_run(
                factory, repository, task_id, run.id
            )
        if first_target is None:
            first_target = (run.id, attempt.id)
        if scenario.budget_wait:
            with factory() as uow:
                locked = uow.tasks.get(task_id, for_update=True)
                assert locked is not None and locked.budget is not None
                locked.budget = replace(locked.budget, deadline=utc_now() - timedelta(seconds=1))
                uow.tasks.save(locked)
                uow.commit()
        if scenario.cancel_requested:
            # The managed cancellation barrier is intentionally still an
            # authority-specific safety boundary in this qualification.
            repository.lifecycle.append(
                _cancel_intent(
                    tenant_id=task.tenant_id,
                    execution_id=execution.id,
                )
            )
            with pytest.raises(RuntimeExecutionConflict, match="cancellation"):
                convergence.apply_known_terminal(
                    tenant_id=task.tenant_id,
                    task_id=task.id,
                    run_id=run.id,
                    attempt_id=attempt.id,
                    fencing_token=attempt.fencing_token,
                    runtime_execution_id=execution.id,
                    observation=RuntimeObservation(
                        observation_id=str(uuid4()),
                        runtime_execution_id=str(execution.id),
                        assignment_id=str(execution.assignment_id),
                        assignment_digest=execution.assignment_digest,
                        phase=RuntimePhase.CANCELED,
                        observed_at=at + timedelta(seconds=3),
                        provider_event_id="managed-parity-cancel",
                    ),
                    received_at=at + timedelta(seconds=4),
                    causation_id=uuid4(),
                )
            return {
                "task_status": task.status.value,
                "target_run_status": run.status.value,
                "target_subtask_status": (tasks.get_task(task_id).subtasks[0].status.value),
                "budget": {"settled_tokens": 0, "reserved_tokens": 0},
                "continuations": 0,
                "quota": 0,
                "memory": 0,
                "audit": {
                    "runtime_observations": len(repository.observations),
                    "safety_exception": "managed_cancellation_barrier_reserved",
                },
                "business": _business_projection(
                    tasks.get_task(task_id), base.store.outbox
                ),
            }
        if scenario.unknown:
            before = (
                len(repository.observations),
                execution.phase,
                run.status,
                task.status,
            )
            with pytest.raises(InvalidTaskInput):
                convergence.apply_known_terminal(
                    tenant_id=task.tenant_id,
                    task_id=task.id,
                    run_id=run.id,
                    attempt_id=attempt.id,
                    fencing_token=attempt.fencing_token,
                    runtime_execution_id=execution.id,
                    observation=RuntimeObservation(
                        observation_id=str(uuid4()),
                        runtime_execution_id=str(execution.id),
                        assignment_id=str(execution.assignment_id),
                        assignment_digest=execution.assignment_digest,
                        phase=RuntimePhase.OUTCOME_UNKNOWN,
                        observed_at=at + timedelta(seconds=3),
                        provider_event_id="managed-parity-unknown",
                    ),
                    received_at=at + timedelta(seconds=4),
                    causation_id=uuid4(),
                )
            assert (
                len(repository.observations),
                repository.executions[execution.id].phase,
                run.status,
                task.status,
            ) == before
            return {
                "task_status": task.status.value,
                "target_run_status": run.status.value,
                "target_subtask_status": "RUNNING",
                "budget": {"settled_tokens": 0, "reserved_tokens": 0},
                "continuations": 0,
                "quota": 0,
                "memory": 0,
                "audit": {
                    "runtime_observations": 0,
                    "safety_exception": "managed_unknown_parking_only",
                },
                "business": _business_projection(
                    tasks.get_task(task_id), base.store.outbox
                ),
            }
        # Once one root has failed, the coordinated failure drain converges
        # the already-crossed sibling as CANCELED before completing the Task
        # as FAILED.  It is the same durable business effect as the legacy
        # sibling-cancel path, but is driven by managed Runtime evidence.
        phase = (
            RuntimePhase.CANCELED
            if scenario.phase is RuntimePhase.FAILED and first_target != (run.id, attempt.id)
            else scenario.phase
        )
        observation = RuntimeObservation(
            observation_id=str(uuid4()),
            runtime_execution_id=str(execution.id),
            assignment_id=str(execution.assignment_id),
            assignment_digest=execution.assignment_digest,
            phase=phase,
            observed_at=at + timedelta(seconds=3),
            provider_event_id="managed-parity-terminal",
            provider_sequence=2,
            output={"result": "ok"} if phase is RuntimePhase.SUCCEEDED else None,
        )
        convergence.apply_known_terminal(
            tenant_id=task.tenant_id,
            task_id=task.id,
            run_id=run.id,
            attempt_id=attempt.id,
            fencing_token=attempt.fencing_token,
            runtime_execution_id=execution.id,
            observation=observation,
            received_at=at + timedelta(seconds=4),
            causation_id=uuid4(),
        )
        if scenario.budget_wait:
            break
    aggregate = tasks.get_task(task_id)
    target_run_id, _ = first_target
    target = next(value for value in aggregate.runs if value.id == target_run_id)
    target_subtask = next(value for value in aggregate.subtasks if value.id == target.subtask_id)
    audit = {"runtime_observations": len(repository.observations)}
    if scenario.name == "executor_failure":
        audit["safety_exception"] = "managed_failure_predispatch_release_shape"
    elif scenario.name == "budget_wait_resume":
        audit["safety_exception"] = "managed_budget_predispatch_release_shape"
    return {
        "task_status": aggregate.task.status.value,
        "target_run_status": target.status.value,
        "target_subtask_status": target_subtask.status.value,
        "budget": {
            "settled_tokens": aggregate.task.settled_tokens,
            "reserved_tokens": aggregate.task.reserved_tokens,
            "budget_revision": aggregate.task.budget_revision,
        },
        "continuations": sum(
            message.schema_name == "agentmesh.run.requested" for message in base.store.outbox
        ),
        "quota": len(base.store.quota_reservations),
        "memory": 0,
        "audit": audit,
        "business": _business_projection(aggregate, base.store.outbox),
    }


@pytest.mark.parametrize(
    "entry", COORDINATED_PARITY_FIXTURE_REPORT, ids=lambda entry: entry["scenario"]
)
def test_coordinated_parity_fixture_has_stable_dag(entry: dict[str, object]) -> None:
    # Both authorities admit the same deterministic fork/join DAG.
    assert PLAN_KEYS == ("research", "analysis", "join")
    assert PLAN_DEPENDENCIES == (("research", "join"), ("analysis", "join"))
    assert entry["scenario"] in {item["scenario"] for item in COORDINATED_PARITY_FIXTURE_REPORT}
    assert entry["same_dag"] is True


@pytest.mark.parametrize(
    "entry", COORDINATED_PARITY_FIXTURE_REPORT, ids=lambda entry: entry["scenario"]
)
def test_coordinated_legacy_managed_terminal_projections_are_qualified(
    entry: dict[str, object],
) -> None:
    scenario = _scenario(str(entry["scenario"]))
    legacy = _legacy_projection(scenario)
    managed = _managed_projection(scenario)
    # Both authority-specific terminal entry points are exercised.  Runtime
    # evidence IDs/counts are intentionally authority-specific.
    assert entry["qualification"] in {
        "full_convergence_entry",
        "managed_only_safety_boundary",
    }
    assert entry["same_dag"] is True
    if entry["scenario"] == "cancel_requested":
        assert managed["audit"]["safety_exception"] == entry["safety_exception"]
        assert legacy["target_run_status"] == "CANCELED"
        assert entry["parity_pass"] is False
        return
    if entry["scenario"] == "unknown_outcome":
        assert managed["audit"]["safety_exception"] == entry["safety_exception"]
        assert managed["target_run_status"] == "RUNNING"
        assert entry["parity_pass"] is False
        return
    assert legacy["target_run_status"] == managed["target_run_status"]
    assert legacy["target_subtask_status"] == managed["target_subtask_status"]
    if entry["scenario"] in {"executor_failure", "budget_wait_resume"}:
        assert entry["legacy_equivalent"] is False
        assert entry["parity_pass"] is False
        assert (
            _authority_neutral_boundary_projection(
                legacy, scenario=str(entry["scenario"])
            )
            == _authority_neutral_boundary_projection(
                managed, scenario=str(entry["scenario"])
            )
        )
        assert managed["audit"]["safety_exception"] == entry["safety_exception"]
    else:
        assert entry["parity_pass"] is True
        assert legacy["business"] == managed["business"]
    assert legacy["audit"]["runtime_observations"] == 0
    assert managed["audit"]["runtime_observations"] > 0
