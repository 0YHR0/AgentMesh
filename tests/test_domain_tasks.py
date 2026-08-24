from datetime import timedelta
from uuid import uuid4

import pytest

from agentmesh.domain.errors import InvalidTaskInput, InvalidTaskTransition, TaskExecutionFailed
from agentmesh.domain.tasks import (
    AcceptanceCriterion,
    AcceptanceCriterionKind,
    AttemptStatus,
    RunStatus,
    Task,
    TaskAttempt,
    TaskExecutionMode,
    TaskRun,
    TaskStatus,
    utc_now,
)


def test_task_execution_failure_preserves_task_identity() -> None:
    task_id = uuid4()

    error = TaskExecutionFailed(task_id, "executor unavailable")

    assert str(error) == "executor unavailable"
    assert error.task_id == task_id


def test_task_rejects_empty_objective() -> None:
    with pytest.raises(InvalidTaskInput):
        Task.create(tenant_id="test", objective="   ")


def test_task_happy_path() -> None:
    task = Task.create(
        tenant_id="test",
        objective="Build a minimal AgentMesh task",
        input={"priority": "low"},
    )
    run = TaskRun.request(task.id, "demo-agent")

    task.queue(run.id)
    run.start()
    task.start(run.id)
    output = {"summary": "done"}
    run.succeed(output)
    task.complete(run.id, output)

    assert task.status == TaskStatus.COMPLETED
    assert task.output == output
    assert run.status == RunStatus.SUCCEEDED
    assert run.thread_id == str(run.id)


def test_managed_direct_execution_can_be_parked_for_runtime_reconciliation() -> None:
    task = Task.create(
        tenant_id="test",
        objective="Reconcile an uncertain provider outcome",
        execution_mode=TaskExecutionMode.DIRECT,
    )
    run = TaskRun.request(
        task.id,
        "demo-agent",
        runtime_version_id=uuid4(),
        runtime_authority="managed",
    )
    task.queue(run.id)
    task.start(run.id)
    run.start()
    attempt = TaskAttempt.lease(
        run_id=run.id,
        worker_id="worker-a",
        fencing_token=1,
        lease_expires_at=utc_now() + timedelta(minutes=1),
    )

    task.require_runtime_reconciliation(run.id, "runtime.provider_outcome_unknown")
    run.require_runtime_reconciliation("runtime.provider_outcome_unknown")
    attempt.mark_outcome_unknown("runtime.provider_outcome_unknown")

    assert task.status is TaskStatus.RECONCILIATION_REQUIRED
    assert task.output is None
    assert run.status is RunStatus.RECONCILIATION_REQUIRED
    assert run.completed_at is None
    assert attempt.status is AttemptStatus.OUTCOME_UNKNOWN
    assert attempt.completed_at is not None
    with pytest.raises(InvalidTaskTransition):
        task.cancel()
    with pytest.raises(InvalidTaskTransition):
        run.fail("ordinary failure")
    with pytest.raises(InvalidTaskTransition):
        attempt.succeed()


def test_runtime_reconciliation_state_is_fail_closed() -> None:
    reviewed = Task.create(
        tenant_id="test",
        objective="Reviewed task",
        execution_mode=TaskExecutionMode.REVIEWED,
        acceptance_criteria=(
            AcceptanceCriterion.create(
                key="summary",
                description="Summary exists",
                kind=AcceptanceCriterionKind.OUTPUT_PATH_EXISTS,
                path=("summary",),
            ),
        ),
        max_revisions=1,
    )
    legacy = TaskRun.request(reviewed.id, "demo-agent")
    reviewed.queue(legacy.id)
    reviewed.start(legacy.id)
    legacy.start()

    with pytest.raises(InvalidTaskTransition):
        reviewed.require_runtime_reconciliation(legacy.id, "runtime.lost")
    with pytest.raises(InvalidTaskTransition):
        legacy.require_runtime_reconciliation("runtime.lost")
    with pytest.raises(InvalidTaskInput):
        TaskAttempt.lease(
            run_id=legacy.id,
            worker_id="worker-a",
            fencing_token=1,
            lease_expires_at=utc_now() + timedelta(minutes=1),
        ).mark_outcome_unknown("x" * 513)


def _parked_managed_direct():
    task = Task.create(
        tenant_id="test",
        objective="Converge evidence",
        execution_mode=TaskExecutionMode.DIRECT,
    )
    run = TaskRun.request(
        task.id,
        "demo-agent",
        runtime_version_id=uuid4(),
        runtime_authority="managed",
    )
    task.queue(run.id)
    task.start(run.id)
    run.start()
    attempt = TaskAttempt.lease(
        run_id=run.id,
        worker_id="worker-a",
        fencing_token=1,
        lease_expires_at=utc_now() + timedelta(minutes=1),
    )
    task.require_runtime_reconciliation(run.id, "runtime.unknown")
    run.require_runtime_reconciliation("runtime.unknown")
    attempt.mark_outcome_unknown("runtime.unknown")
    return task, run, attempt


def test_parked_managed_direct_has_dedicated_success_exit() -> None:
    task, run, attempt = _parked_managed_direct()
    task.candidate_output = {"stale": True}
    task.budget_exhausted_reason = "stale"

    run.reconcile_runtime_succeeded({"answer": 42})
    attempt.reconcile_runtime_succeeded()
    task.reconcile_runtime_succeeded(run.id, {"answer": 42})

    assert task.status is TaskStatus.COMPLETED
    assert task.output == {"answer": 42}
    assert task.candidate_output is None
    assert task.budget_exhausted_reason is None
    assert run.status is RunStatus.SUCCEEDED
    assert attempt.status is AttemptStatus.SUCCEEDED
    assert attempt.completed_at is not None


def test_parked_success_at_budget_deadline_waits_for_approval() -> None:
    task, run, attempt = _parked_managed_direct()

    run.reconcile_runtime_succeeded({"answer": 42})
    attempt.reconcile_runtime_succeeded()
    task.reconcile_runtime_succeeded(
        run.id, {"answer": 42}, budget_deadline_exceeded=True
    )

    assert task.status is TaskStatus.WAITING_APPROVAL
    assert task.current_run_id is None
    assert task.output is None
    assert task.candidate_output == {"answer": 42}
    assert task.error == task.budget_exhausted_reason == "budget_deadline_exceeded"


def test_dedicated_runtime_reconciliation_exits_reject_ordinary_states() -> None:
    task, run, attempt = _parked_managed_direct()
    task.reconcile_runtime_failed(run.id, "runtime.failed")
    run.reconcile_runtime_failed("runtime.failed")
    attempt.reconcile_runtime_failed("runtime.failed")
    assert task.status is TaskStatus.FAILED
    assert run.status is RunStatus.FAILED
    assert attempt.status is AttemptStatus.FAILED
    assert attempt.completed_at is not None
    with pytest.raises(InvalidTaskTransition):
        attempt.reconcile_runtime_succeeded()


def test_completed_task_cannot_run_again() -> None:
    task = Task.create(tenant_id="test", objective="Complete once")
    run = TaskRun.request(task.id, "demo-agent")
    task.queue(run.id)
    task.start(run.id)
    task.complete(run.id, {"summary": "done"})

    with pytest.raises(InvalidTaskTransition):
        task.queue(TaskRun.request(task.id, "demo-agent").id)


def test_queued_task_can_pause_and_resume() -> None:
    task = Task.create(tenant_id="test", objective="Pause before execution")
    run = TaskRun.request(task.id, "demo-agent")
    task.queue(run.id)

    task.request_pause(run.id)
    run.request_pause()
    assert task.status == TaskStatus.PAUSED
    assert run.status == RunStatus.PAUSED
    assert run.pause_requested_at is not None
    assert run.paused_at is not None
    assert run.paused_from_status == RunStatus.QUEUED

    task.resume(run.id)
    run.resume()
    assert task.status == TaskStatus.READY
    assert run.status == RunStatus.QUEUED
    assert run.resumed_at is not None


def test_running_task_pauses_only_at_safe_boundary() -> None:
    task = Task.create(tenant_id="test", objective="Pause at checkpoint")
    run = TaskRun.request(task.id, "demo-agent")
    task.queue(run.id)
    task.start(run.id)
    run.start()

    task.request_pause(run.id)
    run.request_pause()
    assert task.status == TaskStatus.PAUSE_REQUESTED
    assert run.status == RunStatus.PAUSE_REQUESTED
    assert run.paused_from_status == RunStatus.RUNNING

    with pytest.raises(InvalidTaskTransition):
        task.resume(run.id)

    task.mark_paused(run.id)
    run.mark_paused()
    assert task.status == TaskStatus.PAUSED
    assert run.status == RunStatus.PAUSED
