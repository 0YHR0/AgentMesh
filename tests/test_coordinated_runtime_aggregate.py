from __future__ import annotations

import ast
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from uuid import uuid4

import pytest

from agentmesh.application.coordinated_runtime import CoordinatedRuntimeAggregateLocker
from agentmesh.domain.coordination import (
    COORDINATION_USER_CANCEL_REQUESTED,
    CoordinationRuntimeBoundary,
    Subtask,
    SubtaskStatus,
)
from agentmesh.domain.errors import RuntimeExecutionConflict
from agentmesh.domain.runtime_execution import (
    RuntimeExecution,
    RuntimeExecutionPhase,
    RuntimeTrustProfile,
    RuntimeVersion,
    RuntimeVersionStatus,
)
from agentmesh.domain.tasks import (
    AttemptStatus,
    RunRole,
    RunStatus,
    Task,
    TaskAttempt,
    TaskExecutionMode,
    TaskRun,
    TaskStatus,
)

UTC = timezone.utc


def _version(version_id):
    now = datetime.now(UTC)
    return RuntimeVersion(
        id=version_id,
        runtime_id=uuid4(),
        api_version=1,
        adapter_kind="test",
        artifact_digest="a" * 64,
        configuration_digest="b" * 64,
        descriptor=MappingProxyType({}),
        trust_profile=RuntimeTrustProfile.BUILT_IN,
        compatibility=MappingProxyType({}),
        status=RuntimeVersionStatus.PUBLISHED,
        created_at=now,
        published_at=now,
    )


class _Repo:
    def __init__(
        self,
        *,
        task,
        subtasks=(),
        runs=(),
        attempts=None,
        execution=None,
        version=None,
        revalidation_subtasks=None,
        revalidation_runs=None,
    ):
        self.task = task
        self.subtasks = tuple(subtasks)
        self.runs = tuple(runs)
        self.attempts = attempts or {}
        self.execution = execution
        self.executions = {execution.run_id: [execution]} if execution is not None else {}
        self.version = version
        self.revalidation_subtasks = revalidation_subtasks
        self.revalidation_runs = revalidation_runs
        self.subtask_calls = 0
        self.run_calls = 0
        self.calls: list[tuple[str, object]] = []

    def task_get(self, task_id, *, for_update=False):
        self.calls.append(("task.get", for_update))
        return self.task

    def drain(self, task_id, *, tenant_id, for_update=False):
        self.calls.append(("drain.get_active", for_update))
        return None

    def subtask_list(self, task_id, *, for_update=False):
        self.calls.append(("subtasks.list", for_update))
        self.subtask_calls += 1
        if not for_update and self.subtask_calls >= 2 and self.revalidation_subtasks is not None:
            return list(self.revalidation_subtasks)
        return list(self.subtasks)

    def run_list(self, task_id, *, for_update=False):
        self.calls.append(("runs.list", for_update))
        self.run_calls += 1
        if not for_update and self.run_calls >= 2 and self.revalidation_runs is not None:
            return list(self.revalidation_runs)
        return list(self.runs)

    def version_get(self, version_id, *, tenant_id, for_update=False):
        self.calls.append(("version.get", (version_id, for_update)))
        return self.version

    def attempt_latest(self, run_id, *, for_update=False):
        self.calls.append(("attempt.latest", (run_id, for_update)))
        return self.attempts.get(run_id)

    def execution_list(self, run_id, *, tenant_id, for_update=False):
        self.calls.append(("execution.list", (run_id, for_update)))
        return list(self.executions.get(run_id, []))

    def assignment(self, execution_id, *, tenant_id, for_update=False):
        self.calls.append(("assignment.get", for_update))
        return None

    def handle(self, execution_id, *, tenant_id, for_update=False):
        self.calls.append(("handle.get", for_update))
        return None

    def lifecycle(self, execution_id, *, tenant_id, for_update=False):
        self.calls.append(("lifecycle.list", for_update))
        return []

    def incidents(self, execution_id, *, tenant_id, for_update=False):
        self.calls.append(("incident.list", for_update))
        return []


class _Uow:
    def __init__(self, repo: _Repo):
        self._repo = repo
        self.tasks = type("Tasks", (), {"get": repo.task_get})()
        self.coordination_runtime_drains = type(
            "Drains", (), {"get_active_for_task": repo.drain}
        )()
        self.subtasks = type("Subtasks", (), {"list_for_task": repo.subtask_list})()
        self.runs = type("Runs", (), {"list_for_task": repo.run_list})()
        self.runtimes = type(
            "Runtimes",
            (),
            {
                "get_version": repo.version_get,
                "list_executions_for_run": repo.execution_list,
                "get_assignment_snapshot": repo.assignment,
                "get_handle_snapshot": repo.handle,
                "list_lifecycle_operations": repo.lifecycle,
                "list_integrity_incidents_for_execution": repo.incidents,
            },
        )()
        self.attempts = type("Attempts", (), {"latest_for_run": repo.attempt_latest})()


def _task() -> Task:
    task = Task.create(
        tenant_id="tenant-a",
        objective="coordinated",
        execution_mode=TaskExecutionMode.COORDINATED,
        plan_version=1,
        plan_digest="sha256:plan",
        max_concurrency=2,
    )
    task.start_coordination(at=datetime.now(UTC))
    return task


def _project_phase_statuses(subtask, run, attempt, phase: RuntimeExecutionPhase) -> None:
    """Project a persisted chain using the coordinated reload status matrix."""
    if phase is RuntimeExecutionPhase.SUCCEEDED:
        attempt.status = AttemptStatus.SUCCEEDED
        run.status = RunStatus.SUCCEEDED
        subtask.status = SubtaskStatus.COMPLETED
    elif phase in {RuntimeExecutionPhase.FAILED, RuntimeExecutionPhase.TIMED_OUT}:
        attempt.status = AttemptStatus.FAILED
        run.status = RunStatus.FAILED
        subtask.status = SubtaskStatus.FAILED
    elif phase is RuntimeExecutionPhase.CANCELED:
        attempt.status = AttemptStatus.CANCELED
        run.status = RunStatus.CANCELED
        subtask.status = SubtaskStatus.CANCELED
    elif phase in {RuntimeExecutionPhase.LOST, RuntimeExecutionPhase.OUTCOME_UNKNOWN}:
        attempt.status = AttemptStatus.OUTCOME_UNKNOWN
        run.status = RunStatus.RECONCILIATION_REQUIRED
        subtask.status = SubtaskStatus.RECONCILIATION_REQUIRED


def _managed_chain(task: Task, *, phase: RuntimeExecutionPhase | None = None):
    at = datetime.now(UTC) + timedelta(seconds=1)
    subtask = Subtask.create(
        subtask_id=uuid4(),
        task_id=task.id,
        key="worker",
        objective="worker",
        input={},
        required_capabilities=("general.task",),
        preferred_agent_id=None,
        initially_ready=True,
    )
    run = TaskRun.request(
        task.id,
        "agent",
        role=RunRole.EXECUTOR,
        subtask_id=subtask.id,
        runtime_version_id=uuid4(),
        runtime_authority="managed",
        at=at,
    )
    subtask.queue(run.id, at=at + timedelta(seconds=1))
    subtask.start(run.id, at=at + timedelta(seconds=2))
    run.start(at=at + timedelta(seconds=2))
    attempt = TaskAttempt.lease(
        run_id=run.id,
        worker_id="worker",
        fencing_token=1,
        lease_expires_at=at + timedelta(hours=1),
    )
    execution = RuntimeExecution.prepare(
        tenant_id=task.tenant_id,
        run_id=run.id,
        runtime_version_id=run.runtime_version_id,
        assignment_id=uuid4(),
        assignment_digest="a" * 64,
        dispatch_key=f"dispatch:{run.id}",
        dispatch_digest="b" * 64,
        execution_id=run.runtime_execution_intent_id,
        now=at,
    ).claim(
        attempt_id=attempt.id,
        fencing_token=attempt.fencing_token,
        expected_owner_attempt_id=None,
        expected_fencing_token=None,
        expected_version=1,
        now=at + timedelta(seconds=1),
    )
    run.bind_runtime_execution(execution.id)
    if phase is not None:
        execution = execution.apply_observation(
            phase=phase,
            provider_sequence=1,
            now=execution.updated_at + timedelta(seconds=1),
        )
        _project_phase_statuses(subtask, run, attempt, phase)
    return subtask, run, attempt, execution


def _managed_supervisor_chain(task: Task, *, phase: RuntimeExecutionPhase | str):
    at = datetime.now(UTC) + timedelta(seconds=1)
    terminal_subtask = Subtask.create(
        subtask_id=uuid4(),
        task_id=task.id,
        key="completed-worker",
        objective="completed worker",
        input={},
        required_capabilities=("general.task",),
        preferred_agent_id=None,
        initially_ready=True,
    )
    terminal_subtask.status = SubtaskStatus.COMPLETED
    run = TaskRun.request(
        task.id,
        "supervisor",
        role=RunRole.SUPERVISOR,
        runtime_version_id=uuid4(),
        runtime_authority="managed",
        at=at,
    )
    task.queue_supervisor(run.id, at=at + timedelta(seconds=1))
    if phase == "queued":
        return terminal_subtask, run, None, None
    run.start(at=at + timedelta(seconds=2))
    attempt = TaskAttempt.lease(
        run_id=run.id,
        worker_id="supervisor-worker",
        fencing_token=7,
        lease_expires_at=at + timedelta(hours=1),
    )
    if phase == "no-execution":
        return terminal_subtask, run, attempt, None
    execution = RuntimeExecution.prepare(
        tenant_id=task.tenant_id,
        run_id=run.id,
        runtime_version_id=run.runtime_version_id,
        assignment_id=uuid4(),
        assignment_digest="c" * 64,
        dispatch_key=f"supervisor:{run.id}",
        dispatch_digest="d" * 64,
        execution_id=run.runtime_execution_intent_id,
        now=at + timedelta(seconds=3),
    ).claim(
        attempt_id=attempt.id,
        fencing_token=attempt.fencing_token,
        expected_owner_attempt_id=None,
        expected_fencing_token=None,
        expected_version=1,
        now=at + timedelta(seconds=3),
    )
    run.bind_runtime_execution(execution.id)
    if phase is RuntimeExecutionPhase.PREPARED:
        return terminal_subtask, run, attempt, execution
    execution = replace(
        execution,
        phase=phase,
        provider_sequence=1,
        version=execution.version + 1,
        updated_at=at + timedelta(seconds=4),
        terminal_at=(
            at + timedelta(seconds=4)
            if phase
            in {
                RuntimeExecutionPhase.SUCCEEDED,
                RuntimeExecutionPhase.FAILED,
                RuntimeExecutionPhase.CANCELED,
                RuntimeExecutionPhase.TIMED_OUT,
            }
            else None
        ),
    )
    if phase in {RuntimeExecutionPhase.LOST, RuntimeExecutionPhase.OUTCOME_UNKNOWN}:
        attempt.status = AttemptStatus.OUTCOME_UNKNOWN
        attempt.error = "runtime.outcome_unknown"
        run.status = RunStatus.RECONCILIATION_REQUIRED
        run.error = "runtime.outcome_unknown"
        task.status = TaskStatus.RECONCILIATION_REQUIRED
        task.error = "coordination.runtime_reconciliation_required"
    elif phase is RuntimeExecutionPhase.SUCCEEDED:
        attempt.status = AttemptStatus.SUCCEEDED
        run.status = RunStatus.SUCCEEDED
        run.output = {"answer": 42}
        task.status = TaskStatus.COMPLETED
        task.output = {"answer": 42}
    elif phase in {RuntimeExecutionPhase.FAILED, RuntimeExecutionPhase.TIMED_OUT}:
        attempt.status = AttemptStatus.FAILED
        attempt.error = "runtime.failed"
        run.status = RunStatus.FAILED
        run.error = "runtime.failed"
        task.status = TaskStatus.FAILED
        task.error = "runtime.failed"
    elif phase is RuntimeExecutionPhase.CANCELED:
        attempt.status = AttemptStatus.CANCELED
        attempt.error = "runtime.canceled"
        run.status = RunStatus.CANCELED
        run.error = "runtime.canceled"
        task.status = TaskStatus.CANCELED
    return terminal_subtask, run, attempt, execution


def test_lock_expands_in_fixed_order_and_reaches_classifier() -> None:
    task = _task()
    subtask, run, attempt, execution = _managed_chain(task)
    repo = _Repo(
        task=task,
        subtasks=(subtask,),
        runs=(run,),
        attempts={run.id: attempt},
        execution=execution,
        version=_version(run.runtime_version_id),
    )
    aggregate = CoordinatedRuntimeAggregateLocker().lock(
        _Uow(repo), tenant_id=task.tenant_id, task_id=task.id
    )
    assert aggregate.runs == (run,)
    assert aggregate.subtasks == (subtask,)
    assert aggregate.latest_attempts[run.id] == attempt
    assert aggregate.executions == (execution,)
    assert (
        aggregate.boundary_classifications[run.id]
        is CoordinationRuntimeBoundary.NOT_CROSSED_PREPARED
    )
    names = [name for name, _value in repo.calls]
    assert names.index("task.get") < names.index("drain.get_active")
    locked_version = next(
        index for index, call in enumerate(repo.calls) if call[0] == "version.get"
    )
    locked_subtasks = next(
        index for index, call in enumerate(repo.calls) if call == ("subtasks.list", True)
    )
    locked_runs = next(
        index for index, call in enumerate(repo.calls) if call == ("runs.list", True)
    )
    assert names.index("drain.get_active") < locked_version
    assert locked_version < locked_subtasks < locked_runs
    assert any(name == "subtasks.list" and value is True for name, value in repo.calls)
    assert any(name == "runs.list" and value is True for name, value in repo.calls)


@pytest.mark.parametrize("with_execution", [False, True])
def test_relock_accepts_exact_provider_free_cancel_projection(with_execution: bool) -> None:
    task = _task()
    subtask, run, attempt, execution = _managed_chain(task)
    at = max(subtask.updated_at, run.started_at, attempt.started_at) + timedelta(seconds=2)
    if with_execution:
        execution = execution.abort_before_dispatch(
            attempt_id=attempt.id,
            fencing_token=attempt.fencing_token,
            now=at,
        )
    else:
        run.runtime_execution_id = None
        execution = None
    subtask.cancel_before_managed_dispatch(run.id, at=at)
    run.cancel_before_managed_dispatch(at=at)
    attempt.cancel_before_managed_dispatch(at=at)
    repo = _Repo(
        task=task,
        subtasks=(subtask,),
        runs=(run,),
        attempts={run.id: attempt},
        execution=execution,
        version=_version(run.runtime_version_id),
    )

    aggregate = CoordinatedRuntimeAggregateLocker().lock(
        _Uow(repo), tenant_id=task.tenant_id, task_id=task.id
    )

    assert aggregate.boundary_classifications == {}


def test_relock_accepts_exact_queued_provider_free_cancel_projection() -> None:
    task = _task()
    at = datetime.now(UTC) + timedelta(seconds=1)
    subtask = Subtask.create(
        subtask_id=uuid4(),
        task_id=task.id,
        key="queued",
        objective="queued",
        input={},
        required_capabilities=("general.task",),
        preferred_agent_id=None,
        initially_ready=True,
    )
    run = TaskRun.request(
        task.id,
        "agent",
        role=RunRole.EXECUTOR,
        subtask_id=subtask.id,
        runtime_version_id=uuid4(),
        runtime_authority="managed",
        at=at,
    )
    subtask.queue(run.id, at=at)
    subtask.cancel_before_managed_dispatch(run.id, at=at)
    run.cancel_before_managed_dispatch(at=at)
    repo = _Repo(
        task=task,
        subtasks=(subtask,),
        runs=(run,),
        version=_version(run.runtime_version_id),
    )

    aggregate = CoordinatedRuntimeAggregateLocker().lock(
        _Uow(repo), tenant_id=task.tenant_id, task_id=task.id
    )

    assert aggregate.boundary_classifications == {}


@pytest.mark.parametrize("corrupt", ["run", "attempt", "subtask"])
def test_relock_rejects_near_miss_provider_free_cancel_marker(corrupt: str) -> None:
    task = _task()
    subtask, run, attempt, _execution = _managed_chain(task)
    run.runtime_execution_id = None
    at = max(subtask.updated_at, run.started_at, attempt.started_at) + timedelta(seconds=1)
    subtask.cancel_before_managed_dispatch(run.id, at=at)
    run.cancel_before_managed_dispatch(at=at)
    attempt.cancel_before_managed_dispatch(at=at)
    target = {"run": run, "attempt": attempt, "subtask": subtask}[corrupt]
    target.error = "runtime.canceled"
    repo = _Repo(
        task=task,
        subtasks=(subtask,),
        runs=(run,),
        attempts={run.id: attempt},
        version=_version(run.runtime_version_id),
    )

    with pytest.raises(RuntimeExecutionConflict):
        CoordinatedRuntimeAggregateLocker().lock(
            _Uow(repo), tenant_id=task.tenant_id, task_id=task.id
        )


def test_relock_rejects_generic_canceled_projection_without_marker() -> None:
    task = _task()
    subtask, run, attempt, _execution = _managed_chain(task)
    run.runtime_execution_id = None
    subtask.cancel()
    run.cancel()
    attempt.cancel()
    repo = _Repo(
        task=task,
        subtasks=(subtask,),
        runs=(run,),
        attempts={run.id: attempt},
        version=_version(run.runtime_version_id),
    )

    with pytest.raises(RuntimeExecutionConflict):
        CoordinatedRuntimeAggregateLocker().lock(
            _Uow(repo), tenant_id=task.tenant_id, task_id=task.id
        )


def test_relock_rejects_marked_cancel_with_nonterminal_runtime() -> None:
    task = _task()
    subtask, run, attempt, execution = _managed_chain(task)
    at = max(subtask.updated_at, run.started_at, attempt.started_at) + timedelta(seconds=1)
    subtask.cancel_before_managed_dispatch(run.id, at=at)
    run.cancel_before_managed_dispatch(at=at)
    attempt.cancel_before_managed_dispatch(at=at)
    repo = _Repo(
        task=task,
        subtasks=(subtask,),
        runs=(run,),
        attempts={run.id: attempt},
        execution=execution,
        version=_version(run.runtime_version_id),
    )

    with pytest.raises(RuntimeExecutionConflict):
        CoordinatedRuntimeAggregateLocker().lock(
            _Uow(repo), tenant_id=task.tenant_id, task_id=task.id
        )


@pytest.mark.parametrize("ownership_field", ["current_owner_attempt_id", "current_fencing_token"])
def test_relock_rejects_marked_cancel_with_stale_runtime_ownership(
    ownership_field: str,
) -> None:
    task = _task()
    subtask, run, attempt, execution = _managed_chain(task)
    at = max(subtask.updated_at, run.started_at, attempt.started_at) + timedelta(seconds=1)
    execution = execution.abort_before_dispatch(
        attempt_id=attempt.id,
        fencing_token=attempt.fencing_token,
        now=at,
    )
    execution = replace(
        execution,
        **{
            ownership_field: (
                uuid4()
                if ownership_field == "current_owner_attempt_id"
                else attempt.fencing_token + 1
            )
        },
    )
    subtask.cancel_before_managed_dispatch(run.id, at=at)
    run.cancel_before_managed_dispatch(at=at)
    attempt.cancel_before_managed_dispatch(at=at)
    repo = _Repo(
        task=task,
        subtasks=(subtask,),
        runs=(run,),
        attempts={run.id: attempt},
        execution=execution,
        version=_version(run.runtime_version_id),
    )

    with pytest.raises(RuntimeExecutionConflict):
        CoordinatedRuntimeAggregateLocker().lock(
            _Uow(repo), tenant_id=task.tenant_id, task_id=task.id
        )


def test_historical_provider_free_canceled_supervisor_needs_no_task_pointer() -> None:
    task = _task()
    terminal_subtask, run, attempt, _execution = _managed_supervisor_chain(
        task, phase="no-execution"
    )
    at = max(task.updated_at, run.started_at, attempt.started_at) + timedelta(seconds=1)
    run.cancel_before_managed_dispatch(at=at)
    attempt.cancel_before_managed_dispatch(at=at)
    task.release_never_dispatched_supervisor_run(run.id, at=at)
    repo = _Repo(
        task=task,
        subtasks=(terminal_subtask,),
        runs=(run,),
        attempts={run.id: attempt},
        version=_version(run.runtime_version_id),
    )

    aggregate = CoordinatedRuntimeAggregateLocker().lock(
        _Uow(repo), tenant_id=task.tenant_id, task_id=task.id
    )

    assert task.current_run_id is None
    assert aggregate.boundary_classifications == {}
    assert run.error == attempt.error == COORDINATION_USER_CANCEL_REQUESTED


@pytest.mark.parametrize(
    ("boundary", "phase"),
    [
        (CoordinationRuntimeBoundary.NOT_CROSSED_QUEUED, None),
        (CoordinationRuntimeBoundary.NOT_CROSSED_NO_EXECUTION, "none"),
        (CoordinationRuntimeBoundary.NOT_CROSSED_PREPARED, RuntimeExecutionPhase.PREPARED),
        (CoordinationRuntimeBoundary.CROSSED_ACTIVE, RuntimeExecutionPhase.DISPATCHING),
        (CoordinationRuntimeBoundary.KNOWN_TERMINAL, RuntimeExecutionPhase.SUCCEEDED),
        (CoordinationRuntimeBoundary.RECONCILIATION_EVIDENCE, RuntimeExecutionPhase.LOST),
    ],
)
def test_lock_reaches_every_boundary_result(boundary, phase) -> None:
    task = _task()
    subtask, run, attempt, execution = _managed_chain(task)
    version = _version(run.runtime_version_id)
    if phase is None:
        subtask.status = SubtaskStatus.READY
        run.status = RunStatus.QUEUED
        subtask.current_run_id = run.id
        run.runtime_execution_id = None
        execution = None
        attempt = None
    elif phase == "none":
        run.runtime_execution_id = None
        execution = None
    elif phase is not RuntimeExecutionPhase.PREPARED:
        execution = execution.apply_observation(
            phase=phase,
            provider_sequence=1,
            now=execution.updated_at + timedelta(seconds=1),
        )
        _project_phase_statuses(subtask, run, attempt, phase)
    repo = _Repo(
        task=task,
        subtasks=(subtask,),
        runs=(run,),
        attempts={run.id: attempt} if attempt is not None else {},
        execution=execution,
        version=version,
    )
    aggregate = CoordinatedRuntimeAggregateLocker().lock(
        _Uow(repo), tenant_id=task.tenant_id, task_id=task.id
    )
    assert aggregate.boundary_classifications[run.id] is boundary


@pytest.mark.parametrize(
    ("boundary", "phase"),
    [
        (CoordinationRuntimeBoundary.NOT_CROSSED_QUEUED, "queued"),
        (CoordinationRuntimeBoundary.NOT_CROSSED_NO_EXECUTION, "no-execution"),
        (CoordinationRuntimeBoundary.NOT_CROSSED_PREPARED, RuntimeExecutionPhase.PREPARED),
        *[
            (CoordinationRuntimeBoundary.CROSSED_ACTIVE, phase)
            for phase in (
                RuntimeExecutionPhase.DISPATCHING,
                RuntimeExecutionPhase.ACCEPTED,
                RuntimeExecutionPhase.RUNNING,
                RuntimeExecutionPhase.WAITING_INPUT,
                RuntimeExecutionPhase.WAITING_APPROVAL,
                RuntimeExecutionPhase.PAUSE_REQUESTED,
                RuntimeExecutionPhase.PAUSED,
                RuntimeExecutionPhase.CANCEL_REQUESTED,
            )
        ],
        *[
            (CoordinationRuntimeBoundary.KNOWN_TERMINAL, phase)
            for phase in (
                RuntimeExecutionPhase.SUCCEEDED,
                RuntimeExecutionPhase.FAILED,
                RuntimeExecutionPhase.CANCELED,
                RuntimeExecutionPhase.TIMED_OUT,
            )
        ],
        (CoordinationRuntimeBoundary.RECONCILIATION_EVIDENCE, RuntimeExecutionPhase.LOST),
        (
            CoordinationRuntimeBoundary.RECONCILIATION_EVIDENCE,
            RuntimeExecutionPhase.OUTCOME_UNKNOWN,
        ),
    ],
)
def test_lock_classifies_every_current_managed_supervisor_boundary(boundary, phase) -> None:
    task = _task()
    subtask, run, attempt, execution = _managed_supervisor_chain(task, phase=phase)
    repo = _Repo(
        task=task,
        subtasks=(subtask,),
        runs=(run,),
        attempts={run.id: attempt} if attempt is not None else {},
        execution=execution,
        version=_version(run.runtime_version_id),
    )

    aggregate = CoordinatedRuntimeAggregateLocker().lock(
        _Uow(repo), tenant_id=task.tenant_id, task_id=task.id
    )

    assert aggregate.boundary_classifications[run.id] is boundary


@pytest.mark.parametrize(
    "mutation",
    [
        "incomplete-subtask",
        "wrong-task-pointer",
        "subtask-bound",
        "missing-attempt",
        "wrong-local-status",
        "wrong-owner",
        "duplicate-unresolved",
        "multiple-terminal",
        "terminal-output-mismatch",
        "terminal-error-mismatch",
        "parked-error-mismatch",
    ],
)
def test_lock_rejects_invalid_current_managed_supervisor_projection(mutation) -> None:
    task = _task()
    phase = {
        "multiple-terminal": RuntimeExecutionPhase.SUCCEEDED,
        "terminal-output-mismatch": RuntimeExecutionPhase.SUCCEEDED,
        "terminal-error-mismatch": RuntimeExecutionPhase.FAILED,
        "parked-error-mismatch": RuntimeExecutionPhase.OUTCOME_UNKNOWN,
    }.get(mutation, RuntimeExecutionPhase.RUNNING)
    subtask, run, attempt, execution = _managed_supervisor_chain(task, phase=phase)
    executions = [execution]
    if mutation == "incomplete-subtask":
        subtask.status = SubtaskStatus.RUNNING
    elif mutation == "wrong-task-pointer":
        task.current_run_id = uuid4()
    elif mutation == "subtask-bound":
        run.subtask_id = subtask.id
    elif mutation == "missing-attempt":
        attempt = None
    elif mutation == "wrong-local-status":
        task.status = TaskStatus.FAILED
    elif mutation == "wrong-owner":
        execution = replace(execution, current_fencing_token=attempt.fencing_token + 1)
        executions = [execution]
    elif mutation == "duplicate-unresolved":
        executions.append(replace(execution, id=uuid4()))
    elif mutation == "multiple-terminal":
        executions.append(replace(execution, id=uuid4()))
    elif mutation == "terminal-output-mismatch":
        task.output = {"answer": "different"}
    elif mutation == "terminal-error-mismatch":
        attempt.error = "runtime.different"
    else:
        run.error = None
    repo = _Repo(
        task=task,
        subtasks=(subtask,),
        runs=(run,),
        attempts={run.id: attempt} if attempt is not None else {},
        execution=execution,
        version=_version(run.runtime_version_id),
    )
    repo.executions[run.id] = executions

    with pytest.raises(RuntimeExecutionConflict):
        CoordinatedRuntimeAggregateLocker().lock(
            _Uow(repo), tenant_id=task.tenant_id, task_id=task.id
        )


def test_lock_rejects_non_coordinated_task_before_expansion() -> None:
    task = Task.create(tenant_id="tenant-a", objective="direct")
    repo = _Repo(task=task)
    with pytest.raises(RuntimeExecutionConflict):
        CoordinatedRuntimeAggregateLocker().lock(
            _Uow(repo), tenant_id=task.tenant_id, task_id=task.id
        )
    assert repo.calls == [("task.get", True)]


def test_lock_after_task_starts_with_drain_and_never_reads_task_repository() -> None:
    task = _task()
    subtask, run, attempt, execution = _managed_chain(task)
    repo = _Repo(
        task=task,
        subtasks=(subtask,),
        runs=(run,),
        attempts={run.id: attempt},
        execution=execution,
        version=_version(run.runtime_version_id),
    )

    aggregate = CoordinatedRuntimeAggregateLocker().lock_after_task(
        _Uow(repo), task, tenant_id=task.tenant_id, task_id=task.id
    )

    assert aggregate.task is task
    assert repo.calls[0] == ("drain.get_active", True)
    assert not any(name == "task.get" for name, _ in repo.calls)


def test_lock_after_task_ast_has_no_task_repository_operation() -> None:
    source = (
        Path(__file__).parents[1]
        / "src"
        / "agentmesh"
        / "application"
        / "coordinated_runtime.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    helper = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "lock_after_task"
    )
    assert not any(
        isinstance(node, ast.Attribute)
        and node.attr == "tasks"
        and isinstance(node.value, ast.Name)
        and node.value.id == "uow"
        for node in ast.walk(helper)
    )


def test_lock_is_task_lock_then_exactly_equivalent_helper_path() -> None:
    task = _task()
    subtask, run, attempt, execution = _managed_chain(task)
    version = _version(run.runtime_version_id)
    repo = _Repo(
        task=task,
        subtasks=(subtask,),
        runs=(run,),
        attempts={run.id: attempt},
        execution=execution,
        version=version,
    )
    locker = CoordinatedRuntimeAggregateLocker()
    via_lock = locker.lock(_Uow(repo), tenant_id=task.tenant_id, task_id=task.id)

    helper_repo = _Repo(
        task=task,
        subtasks=(subtask,),
        runs=(run,),
        attempts={run.id: attempt},
        execution=execution,
        version=version,
    )
    via_helper = locker.lock_after_task(
        _Uow(helper_repo), task, tenant_id=task.tenant_id, task_id=task.id
    )

    assert via_lock == via_helper
    assert repo.calls[0] == ("task.get", True)
    assert helper_repo.calls[0] == ("drain.get_active", True)


def test_lock_after_task_rejects_task_version_mutated_during_expansion() -> None:
    task = _task()
    subtask, run, attempt, execution = _managed_chain(task)
    repo = _Repo(
        task=task,
        subtasks=(subtask,),
        runs=(run,),
        attempts={run.id: attempt},
        execution=execution,
        version=_version(run.runtime_version_id),
    )
    original_drain = repo.drain

    def mutate_version(_uow, task_id, *, tenant_id, for_update=False):
        result = original_drain(task_id, tenant_id=tenant_id, for_update=for_update)
        task.version += 1
        return result

    repo.drain = mutate_version

    with pytest.raises(RuntimeExecutionConflict, match="Task version changed"):
        CoordinatedRuntimeAggregateLocker().lock_after_task(
            _Uow(repo), task, tenant_id=task.tenant_id, task_id=task.id
        )


@pytest.mark.parametrize(
    "mutate, tenant_id, task_id",
    [
        (lambda task: None, "tenant-a", uuid4()),
        (lambda task: None, "tenant-other", None),
        (lambda task: setattr(task, "execution_mode", TaskExecutionMode.DIRECT), "tenant-a", None),
        (lambda task: setattr(task, "version", 0), "tenant-a", None),
        (lambda task: setattr(task, "version", True), "tenant-a", None),
    ],
)
def test_lock_after_task_rejects_identity_cohort_and_version_mutations(
    mutate, tenant_id, task_id
) -> None:
    task = _task()
    actual_task_id = task.id
    mutate(task)
    subtask, run, attempt, execution = _managed_chain(task)
    repo = _Repo(
        task=task,
        subtasks=(subtask,),
        runs=(run,),
        attempts={run.id: attempt},
        execution=execution,
        version=_version(run.runtime_version_id),
    )

    with pytest.raises(RuntimeExecutionConflict):
        CoordinatedRuntimeAggregateLocker().lock_after_task(
            _Uow(repo),
            task,
            tenant_id=tenant_id,
            task_id=actual_task_id if task_id is None else task_id,
        )
    assert not any(name == "task.get" for name, _ in repo.calls)


def test_lock_rejects_cross_tenant_task_without_discovery() -> None:
    task = _task()
    repo = _Repo(task=task)
    with pytest.raises(RuntimeExecutionConflict):
        CoordinatedRuntimeAggregateLocker().lock(
            _Uow(repo), tenant_id="tenant-other", task_id=task.id
        )
    assert repo.calls == [("task.get", True)]


def test_lock_rejects_non_domain_runtime_version_projection() -> None:
    task = _task()
    subtask, run, attempt, execution = _managed_chain(task)
    repo = _Repo(
        task=task,
        subtasks=(subtask,),
        runs=(run,),
        attempts={run.id: attempt},
        execution=execution,
        version=object(),
    )
    with pytest.raises(RuntimeExecutionConflict):
        CoordinatedRuntimeAggregateLocker().lock(
            _Uow(repo), tenant_id=task.tenant_id, task_id=task.id
        )


@pytest.mark.parametrize("mutation", ["cohort", "role", "binding", "version", "fence"])
def test_lock_rejects_wrong_cohort_role_binding_version_or_fence(mutation) -> None:
    task = _task()
    subtask, run, attempt, execution = _managed_chain(task)
    version = _version(run.runtime_version_id)
    if mutation == "cohort":
        other = replace(
            run,
            runtime_authority="legacy",
            runtime_version_id=None,
            runtime_execution_id=None,
            runtime_execution_intent_id=None,
        )
        runs = (run, other)
        subtasks = (subtask,)
    elif mutation == "role":
        runs = (replace(run, role=RunRole.SUPERVISOR),)
        subtasks = (subtask,)
    elif mutation == "binding":
        runs = (run,)
        subtasks = (replace(subtask, current_run_id=uuid4()),)
    elif mutation == "version":
        runs = (run,)
        subtasks = (subtask,)
        execution = replace(execution, runtime_version_id=uuid4())
    else:
        runs = (run,)
        subtasks = (subtask,)
        execution = replace(execution, current_fencing_token=attempt.fencing_token + 1)
    repo = _Repo(
        task=task,
        subtasks=subtasks,
        runs=runs,
        attempts={run.id: attempt},
        execution=execution,
        version=version,
    )
    with pytest.raises(RuntimeExecutionConflict):
        CoordinatedRuntimeAggregateLocker().lock(
            _Uow(repo), tenant_id=task.tenant_id, task_id=task.id
        )


def test_lock_rejects_phantom_subtask_membership_after_locks() -> None:
    task = _task()
    subtask, run, attempt, execution = _managed_chain(task)
    phantom = Subtask.create(
        subtask_id=uuid4(),
        task_id=task.id,
        key="phantom",
        objective="phantom",
        input={},
        required_capabilities=("general.task",),
        preferred_agent_id=None,
        initially_ready=True,
    )
    repo = _Repo(
        task=task,
        subtasks=(subtask,),
        runs=(run,),
        attempts={run.id: attempt},
        execution=execution,
        version=_version(run.runtime_version_id),
        revalidation_subtasks=(subtask, phantom),
    )
    with pytest.raises(RuntimeExecutionConflict):
        CoordinatedRuntimeAggregateLocker().lock(
            _Uow(repo), tenant_id=task.tenant_id, task_id=task.id
        )


def test_aggregate_lock_has_one_production_implementation_and_no_callers() -> None:
    root = Path(__file__).parents[1] / "src" / "agentmesh" / "application"
    implementations: list[str] = []
    references: list[str] = []
    writers: list[str] = []

    def root_name(node: ast.expr) -> str | None:
        while isinstance(node, ast.Attribute):
            node = node.value
        return node.id if isinstance(node, ast.Name) else None

    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "CoordinatedRuntimeAggregateLocker":
                implementations.append(str(path))
            if path.name not in {
                "coordinated_runtime.py",
                "coordinated_runtime_dispatch.py",
                "coordinated_runtime_barrier.py",
                "coordinated_runtime_convergence.py",
                "coordinated_runtime_unknown.py",
                # c2f3 owns the task-first delivery acquisition transaction
                # and continues the same aggregate lock after the Task row.
                "coordinated_runtime_delivery_acquisition.py",
                # c2f4 owns the task-first provider-free failure transaction.
                "coordinated_runtime_predispatch_failure.py",
                # c2f6a is the caller-free pure Task cancellation planner.
                "coordinated_runtime_control.py",
                # c2e3 is the caller-free privileged transaction owner for
                # one exact parked coordinated Runtime reconciliation.
                "coordinated_runtime_reconciliation.py",
            } and isinstance(node, ast.Name):
                if node.id in {"CoordinatedRuntimeAggregateLocker", "CoordinatedRuntimeAggregate"}:
                    references.append(f"{path}:{node.lineno}:{node.id}")
            if (
                path.name == "coordinated_runtime.py"
                and isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"add", "save", "commit", "flush"}
                and root_name(node.func.value) == "uow"
            ):
                writers.append(f"{path}:{node.lineno}:{node.func.attr}")
    assert implementations == [str(root / "coordinated_runtime.py")]
    assert references == []
    assert writers == []


def test_lock_retains_historical_subtask_runs_without_requiring_current_binding() -> None:
    task = _task()
    subtask, run, attempt, execution = _managed_chain(task)
    historical = TaskRun.request(
        task.id,
        "old-agent",
        role=RunRole.EXECUTOR,
        subtask_id=subtask.id,
        runtime_version_id=run.runtime_version_id,
        runtime_authority="managed",
        at=datetime.now(UTC),
    )
    repo = _Repo(
        task=task,
        subtasks=(subtask,),
        runs=(historical, run),
        attempts={run.id: attempt},
        execution=execution,
        version=_version(run.runtime_version_id),
    )
    aggregate = CoordinatedRuntimeAggregateLocker().lock(
        _Uow(repo), tenant_id=task.tenant_id, task_id=task.id
    )
    assert {value.id for value in aggregate.runs} == {run.id, historical.id}
    assert aggregate.boundary_classifications == {
        run.id: CoordinationRuntimeBoundary.NOT_CROSSED_PREPARED
    }
