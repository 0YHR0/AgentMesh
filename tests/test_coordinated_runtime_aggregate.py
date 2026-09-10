from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agentmesh.application.coordinated_runtime import CoordinatedRuntimeAggregateLocker
from agentmesh.domain.coordination import CoordinationRuntimeBoundary, Subtask
from agentmesh.domain.errors import RuntimeExecutionConflict
from agentmesh.domain.runtime_execution import RuntimeExecution
from agentmesh.domain.tasks import (
    RunRole,
    Task,
    TaskAttempt,
    TaskExecutionMode,
    TaskRun,
)

UTC = timezone.utc


class _Repo:
    def __init__(self, *, task, subtasks=(), runs=(), attempts=None, execution=None, version=None):
        self.task = task
        self.subtasks = tuple(subtasks)
        self.runs = tuple(runs)
        self.attempts = attempts or {}
        self.execution = execution
        self.executions = {execution.run_id: [execution]} if execution is not None else {}
        self.version = version
        self.calls: list[tuple[str, object]] = []

    def task_get(self, task_id, *, for_update=False):
        self.calls.append(("task.get", for_update))
        return self.task

    def drain(self, task_id, *, tenant_id, for_update=False):
        self.calls.append(("drain.get_active", for_update))
        return None

    def subtask_list(self, task_id, *, for_update=False):
        self.calls.append(("subtasks.list", for_update))
        return list(self.subtasks)

    def run_list(self, task_id, *, for_update=False):
        self.calls.append(("runs.list", for_update))
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


def _managed_chain(task: Task):
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
    return subtask, run, attempt, execution


def test_lock_expands_in_fixed_order_and_reaches_classifier() -> None:
    task = _task()
    subtask, run, attempt, execution = _managed_chain(task)
    repo = _Repo(
        task=task,
        subtasks=(subtask,),
        runs=(run,),
        attempts={run.id: attempt},
        execution=execution,
        version=SimpleNamespace(id=run.runtime_version_id),
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


def test_lock_rejects_non_coordinated_task_before_expansion() -> None:
    task = Task.create(tenant_id="tenant-a", objective="direct")
    repo = _Repo(task=task)
    with pytest.raises(RuntimeExecutionConflict):
        CoordinatedRuntimeAggregateLocker().lock(
            _Uow(repo), tenant_id=task.tenant_id, task_id=task.id
        )
    assert repo.calls == [("task.get", True)]


def test_lock_rejects_cross_tenant_task_without_discovery() -> None:
    task = _task()
    repo = _Repo(task=task)
    with pytest.raises(RuntimeExecutionConflict):
        CoordinatedRuntimeAggregateLocker().lock(
            _Uow(repo), tenant_id="tenant-other", task_id=task.id
        )
    assert repo.calls == [("task.get", True)]


def test_aggregate_lock_has_one_production_implementation_and_no_callers() -> None:
    root = Path(__file__).parents[1] / "src" / "agentmesh" / "application"
    implementations: list[str] = []
    callers: list[str] = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "CoordinatedRuntimeAggregateLocker":
                implementations.append(str(path))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "lock" and isinstance(node.func.value, ast.Name):
                    if node.func.value.id == "CoordinatedRuntimeAggregateLocker":
                        callers.append(f"{path}:{node.lineno}")
    assert implementations == [str(root / "coordinated_runtime.py")]
    assert callers == []


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
        version=SimpleNamespace(id=run.runtime_version_id),
    )
    aggregate = CoordinatedRuntimeAggregateLocker().lock(
        _Uow(repo), tenant_id=task.tenant_id, task_id=task.id
    )
    assert {value.id for value in aggregate.runs} == {run.id, historical.id}
    assert aggregate.boundary_classifications == {
        run.id: CoordinationRuntimeBoundary.NOT_CROSSED_PREPARED
    }
