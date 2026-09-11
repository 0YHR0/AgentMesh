from __future__ import annotations

import ast
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
)
from agentmesh.domain.errors import RuntimeExecutionConflict
from agentmesh.domain.runtime_execution import RuntimeExecutionPhase
from tests.test_coordinated_runtime_aggregate import _managed_chain, _Repo, _task, _Uow, _version

UTC = timezone.utc


def _aggregate(*, siblings=(), drain_target=None):
    task = _task()
    target = _managed_chain(task)
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
        version=_version(target[1].runtime_version_id),
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


def test_current_sibling_actions_are_uuid_ordered_and_historical_runs_are_ignored() -> None:
    task = _task()
    target = _managed_chain(task)
    sibling = _managed_chain(task)
    sibling_run = replace(sibling[1], runtime_version_id=target[1].runtime_version_id)
    sibling_execution = replace(sibling[3], runtime_version_id=target[1].runtime_version_id)
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
        version=_version(target[1].runtime_version_id),
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


def test_planner_has_no_production_callers() -> None:
    root = Path(__file__).parents[1] / "src" / "agentmesh"
    calls: list[str] = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "plan_known_terminal"
                and path.name != "coordinated_runtime_barrier.py"
            ):
                calls.append(f"{path}:{node.lineno}")
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
