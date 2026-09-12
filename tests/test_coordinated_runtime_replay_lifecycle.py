from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest

from agentmesh.application.coordinated_runtime_convergence import (
    _validate_replay_supervisor_descendant,
)
from agentmesh.domain.errors import RuntimeExecutionConflict
from agentmesh.domain.runtime_execution import RuntimeExecution, RuntimeExecutionPhase
from agentmesh.domain.tasks import (
    AttemptStatus,
    RunRole,
    RunStatus,
    TaskAttempt,
    TaskRun,
    TaskStatus,
)
from agentmesh.runtime_sdk import RuntimePhase
from tests.test_coordinated_runtime_barrier import _aggregate
from tests.test_coordinated_runtime_convergence_service import (
    _call,
    _now_for,
    _service_state,
)


def _with_successor_executor(aggregate):
    """Add the scheduler's queued successor while leaving the Task unbound."""
    successor = TaskRun.request(
        aggregate.task.id,
        "successor",
        role=RunRole.EXECUTOR,
        subtask_id=uuid4(),
        runtime_version_id=aggregate.cohort.runtime_version_id,
        runtime_authority="managed",
    )
    return replace(
        aggregate,
        runs=(*aggregate.runs, successor),
        latest_attempts={**aggregate.latest_attempts, successor.id: None},
    )


def _supervisor_descendant(aggregate, lifecycle: str):
    """Build a persisted monotonic Supervisor descendant for replay validation."""
    task = aggregate.task
    version_id = aggregate.cohort.runtime_version_id
    run = TaskRun.request(
        task.id,
        "supervisor",
        role=RunRole.SUPERVISOR,
        runtime_version_id=version_id,
        runtime_authority="managed",
    )
    attempts = dict(aggregate.latest_attempts)
    executions = list(aggregate.executions)
    now = run.queued_at + timedelta(seconds=1)

    if lifecycle == "queued":
        supervisor_attempt = None
    else:
        run.start(at=now)
        supervisor_attempt = TaskAttempt.lease(
            run_id=run.id,
            worker_id="supervisor-worker",
            fencing_token=7,
            lease_expires_at=now + timedelta(hours=1),
        )
        attempts[run.id] = supervisor_attempt

    if lifecycle in {"prepared", "active", "success", "failure", "canceled"}:
        execution = RuntimeExecution.prepare(
            tenant_id=task.tenant_id,
            run_id=run.id,
            runtime_version_id=version_id,
            assignment_id=uuid4(),
            assignment_digest="c" * 64,
            dispatch_key=f"dispatch:{run.id}",
            dispatch_digest="d" * 64,
            execution_id=run.runtime_execution_intent_id,
            now=now,
        ).claim(
            attempt_id=supervisor_attempt.id,
            fencing_token=supervisor_attempt.fencing_token,
            expected_owner_attempt_id=None,
            expected_fencing_token=None,
            expected_version=1,
            now=now + timedelta(seconds=1),
        )
        run.bind_runtime_execution(execution.id)
        if lifecycle == "active":
            execution = execution.apply_observation(
                phase=RuntimeExecutionPhase.RUNNING,
                provider_sequence=1,
                now=now + timedelta(seconds=2),
            )
        elif lifecycle == "success":
            output = {"answer": "ok"}
            execution = execution.apply_observation(
                phase=RuntimeExecutionPhase.SUCCEEDED,
                provider_sequence=1,
                now=now + timedelta(seconds=2),
            )
            supervisor_attempt.succeed(at=now + timedelta(seconds=3))
            run = replace(
                run,
                status=RunStatus.SUCCEEDED,
                output=output,
                completed_at=now + timedelta(seconds=3),
            )
        elif lifecycle == "failure":
            execution = execution.apply_observation(
                phase=RuntimeExecutionPhase.FAILED,
                provider_sequence=1,
                now=now + timedelta(seconds=2),
            )
            supervisor_attempt = replace(
                supervisor_attempt,
                status=AttemptStatus.FAILED,
                completed_at=now + timedelta(seconds=3),
                error="supervisor.failed",
            )
            run = replace(
                run,
                status=RunStatus.FAILED,
                error="supervisor.failed",
                completed_at=now + timedelta(seconds=3),
            )
        elif lifecycle == "canceled":
            execution = execution.apply_observation(
                phase=RuntimeExecutionPhase.CANCELED,
                provider_sequence=1,
                now=now + timedelta(seconds=2),
            )
            supervisor_attempt = replace(
                supervisor_attempt,
                status=AttemptStatus.CANCELED,
                completed_at=now + timedelta(seconds=3),
            )
            run = replace(
                run,
                status=RunStatus.CANCELED,
                completed_at=now + timedelta(seconds=3),
            )
        executions.append(execution)

    if lifecycle != "queued":
        attempts[run.id] = supervisor_attempt
    task = replace(task, current_run_id=run.id, status=TaskStatus.RUNNING)
    if lifecycle == "success":
        task = replace(
            task,
            status=TaskStatus.COMPLETED,
            output=run.output,
            error=None,
        )
    elif lifecycle == "failure":
        task = replace(
            task,
            status=TaskStatus.FAILED,
            output=None,
            error=run.error,
        )
    elif lifecycle == "canceled":
        task = replace(task, status=TaskStatus.CANCELED, output=None, error=None)
    return replace(
        aggregate,
        task=task,
        runs=(*aggregate.runs, run),
        latest_attempts=attempts,
        executions=tuple(executions),
    )


def test_service_replay_accepts_queued_successor_executor_without_scheduling_again(monkeypatch):
    _task, target, aggregate = _aggregate()
    state, service = _service_state(monkeypatch, aggregate, target)
    now = _now_for(target)
    first, observation = _call(service, target, phase=RuntimePhase.SUCCEEDED, now=now)
    assert first.scheduled_run_ids

    state.aggregate = _with_successor_executor(state.aggregate)
    before = (state.commits, state.observation_adds, len(state.scheduler_calls))
    replay, _ = _call(
        service,
        target,
        phase=RuntimePhase.SUCCEEDED,
        now=now + timedelta(seconds=2),
        observation=observation,
    )
    assert replay.kind.value == "REPLAY"
    assert replay.task_status is TaskStatus.RUNNING
    assert replay.scheduled_run_ids == ()
    assert (state.commits, state.observation_adds, len(state.scheduler_calls)) == before


@pytest.mark.parametrize("lifecycle", ["queued", "claimed", "prepared", "active"])
def test_replay_accepts_each_pre_provider_or_active_supervisor_lifecycle(lifecycle):
    _task, _target, aggregate = _aggregate()
    if lifecycle == "claimed":
        descendant = _supervisor_descendant(aggregate, "active")
        run = descendant.runs[-1]
        descendant = replace(
            descendant,
            runs=(*descendant.runs[:-1], replace(run, runtime_execution_id=None)),
            executions=descendant.executions[:-1],
        )
    else:
        descendant = _supervisor_descendant(aggregate, lifecycle)
    _validate_replay_supervisor_descendant(descendant)


def test_replay_accepts_scheduler_budget_wait_without_supervisor_only_with_empty_candidate():
    _task, _target, aggregate = _aggregate()
    held = replace(
        aggregate,
        task=replace(
            aggregate.task,
            status=TaskStatus.WAITING_APPROVAL,
            current_run_id=None,
            output=None,
            error="budget_deadline_exceeded",
            budget_exhausted_reason="budget_deadline_exceeded",
            candidate_output=None,
        ),
    )
    _validate_replay_supervisor_descendant(held)
    with pytest.raises(RuntimeExecutionConflict, match="candidate"):
        _validate_replay_supervisor_descendant(
            replace(held, task=replace(held.task, candidate_output={"forged": True}))
        )


def test_replay_accepts_budget_wait_after_successful_supervisor_with_candidate():
    _task, _target, aggregate = _aggregate()
    descendant = _supervisor_descendant(aggregate, "success")
    supervisor = descendant.runs[-1]
    held = replace(
        descendant,
        task=replace(
            descendant.task,
            status=TaskStatus.WAITING_APPROVAL,
            current_run_id=None,
            output=None,
            error="budget_deadline_exceeded",
            budget_exhausted_reason="budget_deadline_exceeded",
            candidate_output=supervisor.output,
        ),
    )
    _validate_replay_supervisor_descendant(held)


@pytest.mark.parametrize(
    ("lifecycle", "task_status"),
    [
        ("success", TaskStatus.COMPLETED),
        ("failure", TaskStatus.FAILED),
        ("canceled", TaskStatus.CANCELED),
    ],
)
def test_replay_accepts_terminal_task_with_exactly_one_terminal_supervisor(
    lifecycle, task_status
):
    _task, _target, aggregate = _aggregate()
    descendant = _supervisor_descendant(aggregate, lifecycle)
    assert descendant.task.status is task_status
    _validate_replay_supervisor_descendant(descendant)


@pytest.mark.parametrize(
    "mutation",
    [
        "multiple_supervisors",
        "unknown_current_run",
        "cleared_pointer",
        "wrong_cohort",
        "wrong_version",
        "owner",
        "fence",
        "partial_chain",
    ],
)
def test_replay_rejects_ambiguous_or_partial_supervisor_descendants(mutation):
    _task, _target, aggregate = _aggregate()
    descendant = _supervisor_descendant(aggregate, "active")
    supervisor = descendant.runs[-1]
    if mutation == "multiple_supervisors":
        second = replace(supervisor, id=uuid4(), thread_id=str(uuid4()))
        descendant = replace(
            descendant,
            runs=(*descendant.runs, second),
            latest_attempts={**descendant.latest_attempts, second.id: None},
        )
    elif mutation == "unknown_current_run":
        descendant = replace(descendant, task=replace(descendant.task, current_run_id=uuid4()))
    elif mutation == "cleared_pointer":
        descendant = replace(descendant, task=replace(descendant.task, current_run_id=None))
    elif mutation == "wrong_cohort":
        descendant = replace(
            descendant,
            runs=(*descendant.runs[:-1], replace(supervisor, runtime_version_id=uuid4())),
        )
    elif mutation == "wrong_version":
        descendant = replace(
            descendant,
            cohort=replace(descendant.cohort, runtime_version_id=uuid4()),
        )
    elif mutation == "owner":
        execution = descendant.executions[-1]
        descendant = replace(
            descendant,
            executions=(
                *descendant.executions[:-1],
                replace(execution, current_owner_attempt_id=uuid4()),
            ),
        )
    elif mutation == "fence":
        execution = descendant.executions[-1]
        descendant = replace(
            descendant,
            executions=(*descendant.executions[:-1], replace(execution, current_fencing_token=99)),
        )
    elif mutation == "partial_chain":
        descendant = replace(
            descendant,
            latest_attempts={
                key: value
                for key, value in descendant.latest_attempts.items()
                if key != supervisor.id
            },
        )
    with pytest.raises(RuntimeExecutionConflict):
        _validate_replay_supervisor_descendant(descendant)


def test_replay_rejects_running_supervisor_with_incomplete_subtask_successor_projection():
    _task, _target, aggregate = _aggregate()
    descendant = _supervisor_descendant(aggregate, "active")
    supervisor = descendant.runs[-1]
    # A Supervisor must never be attached to a Subtask; this is a partial/cross-role chain.
    broken = replace(
        descendant,
        runs=(*descendant.runs[:-1], replace(supervisor, subtask_id=uuid4())),
    )
    with pytest.raises(RuntimeExecutionConflict):
        _validate_replay_supervisor_descendant(broken)
