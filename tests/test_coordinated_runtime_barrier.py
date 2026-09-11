from __future__ import annotations

import ast
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from uuid import uuid4

import pytest

from agentmesh.application.business_outcomes import KnownTerminalPhase
from agentmesh.application.coordinated_runtime import CoordinatedRuntimeAggregateLocker
from agentmesh.application.coordinated_runtime_barrier import (
    CoordinatedBarrierCompletion,
    CoordinatedSiblingActionKind,
    plan_known_terminal,
)
from agentmesh.domain.coordination import (
    CoordinationRuntimeBoundary,
    CoordinationRuntimeDrain,
    CoordinationRuntimeDrainTarget,
    SubtaskStatus,
)
from agentmesh.domain.errors import RuntimeExecutionConflict
from agentmesh.domain.runtime_execution import (
    RuntimeExecutionPhase,
    RuntimeTrustProfile,
    RuntimeVersion,
    RuntimeVersionStatus,
)
from agentmesh.domain.tasks import AttemptStatus, RunStatus, TaskStatus
from agentmesh.runtime_sdk.builtin import (
    LANGGRAPH_V2_DESCRIPTOR,
    builtin_langgraph_runtime_id,
    builtin_langgraph_version_id,
)
from agentmesh.runtime_sdk.canonical import canonical_digest
from tests.test_coordinated_runtime_aggregate import _managed_chain, _Repo, _task, _Uow

UTC = timezone.utc


def _builtin_version() -> RuntimeVersion:
    now = datetime.now(UTC)
    return RuntimeVersion(
        id=builtin_langgraph_version_id("v2"),
        runtime_id=builtin_langgraph_runtime_id(),
        api_version=1,
        adapter_kind="python-in-process",
        artifact_digest=canonical_digest(
            {"package": "agentmesh", "runtime": "agentmesh.langgraph", "release": "v2"}
        ),
        configuration_digest=canonical_digest(
            {
                "runtime_key": LANGGRAPH_V2_DESCRIPTOR["runtime_key"],
                "capabilities": LANGGRAPH_V2_DESCRIPTOR["capabilities"],
                "limits": LANGGRAPH_V2_DESCRIPTOR["limits"],
            }
        ),
        descriptor=MappingProxyType(LANGGRAPH_V2_DESCRIPTOR),
        trust_profile=RuntimeTrustProfile.BUILT_IN,
        compatibility=MappingProxyType({}),
        status=RuntimeVersionStatus.PUBLISHED,
        created_at=now,
        published_at=now,
    )


def _aggregate(*, siblings=(), sibling_count=0, drain_target=None):
    task = _task()
    target = _managed_chain(task)
    version = _builtin_version()
    target_run = replace(target[1], runtime_version_id=version.id)
    target_execution = replace(target[3], runtime_version_id=version.id)
    target = (
        target[0],
        target_run,
        target[2],
        target_execution.apply_observation(
            phase=RuntimeExecutionPhase.RUNNING,
            provider_sequence=1,
            now=target[3].updated_at + timedelta(seconds=1),
        ),
    )
    if sibling_count:
        siblings = []
        for _ in range(sibling_count):
            sibling = _managed_chain(task)
            sibling = (
                sibling[0],
                sibling[1],
                sibling[2],
                sibling[3].apply_observation(
                    phase=RuntimeExecutionPhase.RUNNING,
                    provider_sequence=1,
                    now=sibling[3].updated_at + timedelta(seconds=1),
                ),
            )
            siblings.append(sibling)
    runs = [target[1]]
    subtasks = [target[0]]
    attempts = {target[1].id: target[2]}
    executions = [target[3]]
    for _subtask, run, attempt, execution in siblings:
        run = replace(run, runtime_version_id=target[1].runtime_version_id)
        execution = replace(execution, runtime_version_id=target[1].runtime_version_id)
        runs.append(run)
        subtasks.append(_subtask)
        attempts[run.id] = attempt
        executions.append(execution)
    drain = None
    if drain_target is not None:
        drain = CoordinationRuntimeDrain.start(
            drain_id=uuid4(),
            tenant_id=task.tenant_id,
            task_id=task.id,
            triggering_run_id=target[1].id,
            target=drain_target,
            reason="initial.failure",
            at=datetime.now(UTC),
        )
    repo = _Repo(
        task=task,
        subtasks=tuple(subtasks),
        runs=tuple(runs),
        attempts=attempts,
        execution=executions[0],
        version=version,
    )
    repo.executions = {}
    for execution in executions:
        repo.executions.setdefault(execution.run_id, []).append(execution)

    def get_drain(_self, task_id, *, tenant_id, for_update=False):
        return drain

    repo.drain = get_drain
    aggregate = CoordinatedRuntimeAggregateLocker().lock(
        _Uow(repo), tenant_id=task.tenant_id, task_id=task.id
    )
    return task, target, aggregate


def test_known_failure_creates_failed_drain_plan_without_mutation() -> None:
    task, target, aggregate = _aggregate()
    before = (target[0].status, target[1].status, target[2].status, target[3].phase)
    plan = plan_known_terminal(
        aggregate,
        triggering_run_id=target[1].id,
        phase=KnownTerminalPhase.FAILED,
        cancel_intent_present=False,
        safe_error=None,
    )
    assert plan.create_drain is True
    assert plan.effective_target.value == "FAILED"
    assert plan.effective_reason == "runtime.failed"
    assert plan.completion is CoordinatedBarrierCompletion.APPLY_FAILED
    assert plan.sibling_actions == ()
    assert before == (target[0].status, target[1].status, target[2].status, target[3].phase)
    assert task.execution_mode.value == "COORDINATED"


def test_success_without_drain_is_ordinary_continuation() -> None:
    _task_value, target, aggregate = _aggregate()
    plan = plan_known_terminal(
        aggregate,
        triggering_run_id=target[1].id,
        phase=KnownTerminalPhase.SUCCEEDED,
        cancel_intent_present=False,
        safe_error=None,
    )
    assert plan.completion is CoordinatedBarrierCompletion.CONTINUE_SUCCESS
    assert plan.create_drain is False
    assert plan.requested_target is None
    assert plan.sibling_actions == ()


@pytest.mark.parametrize(
    ("phase", "error", "reason"),
    [
        (KnownTerminalPhase.FAILED, "stable.failure", "stable.failure"),
        (KnownTerminalPhase.TIMED_OUT, None, "runtime.timed_out"),
        (KnownTerminalPhase.CANCELED, None, "runtime.unrequested_cancellation"),
    ],
)
def test_failure_phases_have_stable_failed_target(phase, error, reason) -> None:
    _task_value, target, aggregate = _aggregate()
    plan = plan_known_terminal(
        aggregate,
        triggering_run_id=target[1].id,
        phase=phase,
        cancel_intent_present=False,
        safe_error=error,
    )
    assert plan.effective_target.value == "FAILED"
    assert plan.effective_reason == reason


def test_invalid_success_error_fails_closed() -> None:
    _task_value, target, aggregate = _aggregate()
    with pytest.raises(RuntimeExecutionConflict):
        plan_known_terminal(
            aggregate,
            triggering_run_id=target[1].id,
            phase=KnownTerminalPhase.SUCCEEDED,
            cancel_intent_present=False,
            safe_error="unexpected",
        )


def test_invalid_target_and_cancel_flag_fail_closed() -> None:
    _task_value, target, aggregate = _aggregate()
    with pytest.raises(RuntimeExecutionConflict):
        plan_known_terminal(
            aggregate,
            triggering_run_id=target[1].id,
            phase=KnownTerminalPhase.CANCELED,
            cancel_intent_present=True,
            safe_error=None,
        )
    with pytest.raises(RuntimeExecutionConflict):
        plan_known_terminal(
            aggregate,
            triggering_run_id=target[1].id,
            phase=KnownTerminalPhase.FAILED,
            cancel_intent_present=False,
            safe_error=" bad\nreason",
        )


@pytest.mark.parametrize(
    "boundary",
    [
        CoordinationRuntimeBoundary.NOT_CROSSED_QUEUED,
        CoordinationRuntimeBoundary.NOT_CROSSED_NO_EXECUTION,
        CoordinationRuntimeBoundary.NOT_CROSSED_PREPARED,
        CoordinationRuntimeBoundary.KNOWN_TERMINAL,
        CoordinationRuntimeBoundary.RECONCILIATION_EVIDENCE,
    ],
)
def test_target_must_be_crossed_active_before_observation(boundary) -> None:
    _task_value, target, aggregate = _aggregate()
    classifications = dict(aggregate.boundary_classifications)
    classifications[target[1].id] = boundary
    aggregate = replace(aggregate, boundary_classifications=classifications)
    with pytest.raises(RuntimeExecutionConflict):
        plan_known_terminal(
            aggregate,
            triggering_run_id=target[1].id,
            phase=KnownTerminalPhase.FAILED,
            cancel_intent_present=False,
            safe_error=None,
        )


@pytest.mark.parametrize("mutation", ["task", "subtask", "run", "attempt"])
def test_target_pre_observation_statuses_are_fail_closed(mutation) -> None:
    task, target, aggregate = _aggregate()
    if mutation == "task":
        task.status = TaskStatus.CREATED
    elif mutation == "subtask":
        target[0].status = SubtaskStatus.READY
    elif mutation == "run":
        target[1].status = RunStatus.QUEUED
    else:
        target[2].status = AttemptStatus.PAUSED
    with pytest.raises(RuntimeExecutionConflict):
        plan_known_terminal(
            aggregate,
            triggering_run_id=target[1].id,
            phase=KnownTerminalPhase.FAILED,
            cancel_intent_present=False,
            safe_error=None,
        )


def test_reconciliation_held_task_requires_the_same_active_drain() -> None:
    task, target, aggregate = _aggregate()
    task.status = TaskStatus.RECONCILIATION_REQUIRED
    with pytest.raises(RuntimeExecutionConflict):
        plan_known_terminal(
            aggregate,
            triggering_run_id=target[1].id,
            phase=KnownTerminalPhase.FAILED,
            cancel_intent_present=False,
            safe_error=None,
        )

    task, target, aggregate = _aggregate(drain_target=CoordinationRuntimeDrainTarget.RUNNING)
    task.status = TaskStatus.RECONCILIATION_REQUIRED
    plan = plan_known_terminal(
        aggregate,
        triggering_run_id=target[1].id,
        phase=KnownTerminalPhase.FAILED,
        cancel_intent_present=False,
        safe_error=None,
    )
    assert plan.effective_target is CoordinationRuntimeDrainTarget.FAILED

    task, target, aggregate = _aggregate(drain_target=CoordinationRuntimeDrainTarget.RUNNING)
    task.status = TaskStatus.RECONCILIATION_REQUIRED
    aggregate = replace(
        aggregate,
        active_drain=replace(aggregate.active_drain, task_id=uuid4()),
    )
    with pytest.raises(RuntimeExecutionConflict):
        plan_known_terminal(
            aggregate,
            triggering_run_id=target[1].id,
            phase=KnownTerminalPhase.FAILED,
            cancel_intent_present=False,
            safe_error=None,
        )


def test_active_drain_retargets_and_preserves_first_cause() -> None:
    _task_value, target, aggregate = _aggregate(drain_target=CoordinationRuntimeDrainTarget.RUNNING)
    plan = plan_known_terminal(
        aggregate,
        triggering_run_id=target[1].id,
        phase=KnownTerminalPhase.FAILED,
        cancel_intent_present=False,
        safe_error="first.failure",
    )
    assert plan.retarget_drain is True
    assert plan.effective_target is CoordinationRuntimeDrainTarget.FAILED
    assert plan.effective_reason == "first.failure"

    _task_value, target, aggregate = _aggregate(drain_target=CoordinationRuntimeDrainTarget.FAILED)
    plan = plan_known_terminal(
        aggregate,
        triggering_run_id=target[1].id,
        phase=KnownTerminalPhase.SUCCEEDED,
        cancel_intent_present=False,
        safe_error=None,
    )
    assert plan.retarget_drain is False
    assert plan.effective_target is CoordinationRuntimeDrainTarget.FAILED
    assert plan.effective_reason == "initial.failure"


@pytest.mark.parametrize("sibling_count", [1, 2])
def test_stopping_drain_request_cancel_waits_for_crossed_siblings(sibling_count) -> None:
    _task_value, target, aggregate = _aggregate(sibling_count=sibling_count)
    plan = plan_known_terminal(
        aggregate,
        triggering_run_id=target[1].id,
        phase=KnownTerminalPhase.FAILED,
        cancel_intent_present=False,
        safe_error=None,
    )
    assert plan.completion is CoordinatedBarrierCompletion.WAIT_ACTIVE
    assert all(
        action.kind is CoordinatedSiblingActionKind.REQUEST_CANCEL
        for action in plan.sibling_actions
    )


def test_current_sibling_actions_are_uuid_ordered_and_historical_runs_are_ignored() -> None:
    task = _task()
    version = _builtin_version()
    target = _managed_chain(task)
    target_run = replace(target[1], runtime_version_id=version.id)
    target_execution = replace(target[3], runtime_version_id=version.id)
    target = (
        target[0],
        target_run,
        target[2],
        target_execution.apply_observation(
            phase=RuntimeExecutionPhase.RUNNING,
            provider_sequence=1,
            now=target[3].updated_at + timedelta(seconds=1),
        ),
    )
    sibling = _managed_chain(task)
    sibling_run = replace(sibling[1], runtime_version_id=version.id)
    sibling_execution = replace(sibling[3], runtime_version_id=version.id)
    sibling_execution = sibling_execution.apply_observation(
        phase=RuntimeExecutionPhase.RUNNING,
        provider_sequence=1,
        now=sibling[3].updated_at + timedelta(seconds=1),
    )
    sibling = (sibling[0], sibling_run, sibling[2], sibling_execution)
    historical = replace(sibling_run, id=uuid4())
    repo = _Repo(
        task=task,
        subtasks=(target[0], sibling[0]),
        runs=(historical, target[1], sibling_run),
        attempts={target[1].id: target[2], sibling_run.id: sibling[2]},
        execution=target[3],
        version=version,
    )
    repo.executions = {target[3].run_id: [target[3]], sibling_run.id: [sibling_execution]}
    aggregate = CoordinatedRuntimeAggregateLocker().lock(
        _Uow(repo), tenant_id=task.tenant_id, task_id=task.id
    )
    plan = plan_known_terminal(
        aggregate,
        triggering_run_id=target[1].id,
        phase=KnownTerminalPhase.FAILED,
        cancel_intent_present=False,
        safe_error=None,
    )
    assert [action.run_id for action in plan.sibling_actions] == [sibling_run.id]
    assert plan.sibling_actions[0].kind is CoordinatedSiblingActionKind.REQUEST_CANCEL
    assert plan.completion is CoordinatedBarrierCompletion.WAIT_ACTIVE


def test_planner_has_no_production_callers() -> None:
    root = Path(__file__).parents[1] / "src" / "agentmesh"

    def detect(tree: ast.AST) -> list[int]:
        lines: list[int] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id == "plan_known_terminal":
                lines.append(node.lineno)
            elif isinstance(node.func, ast.Attribute) and node.func.attr == "plan_known_terminal":
                lines.append(node.lineno)
        return lines

    fixture = ast.parse("plan_known_terminal(x)\nplanner.plan_known_terminal(x)")
    assert len(detect(fixture)) == 2
    calls: list[str] = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        calls.extend(f"{path}:{line}" for line in detect(tree))
    assert calls == []


def test_action_vocabulary_is_closed_and_deterministic() -> None:
    assert {value.value for value in CoordinatedSiblingActionKind} == {
        "RETAIN_TERMINAL",
        "WAIT_CROSSED",
        "WAIT_RECONCILIATION",
        "RELEASE_QUEUED",
        "ABORT_NO_EXECUTION",
        "ABORT_PREPARED",
        "REQUEST_CANCEL",
    }
    assert CoordinationRuntimeBoundary.RECONCILIATION_EVIDENCE.value == "RECONCILIATION_EVIDENCE"
