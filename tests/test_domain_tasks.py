import pytest

from agentmesh.domain.errors import InvalidTaskInput, InvalidTaskTransition
from agentmesh.domain.tasks import RunStatus, Task, TaskRun, TaskStatus


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
