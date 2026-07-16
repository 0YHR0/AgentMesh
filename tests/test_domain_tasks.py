from uuid import uuid4

import pytest

from agentmesh.domain.errors import InvalidTaskInput, InvalidTaskTransition, TaskExecutionFailed
from agentmesh.domain.tasks import RunStatus, Task, TaskRun, TaskStatus


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
