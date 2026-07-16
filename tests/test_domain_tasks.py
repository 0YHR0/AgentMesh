import pytest

from agentmesh.domain.errors import InvalidTaskInput, InvalidTaskTransition
from agentmesh.domain.tasks import Task, TaskRun, TaskStatus


def test_task_rejects_empty_objective() -> None:
    with pytest.raises(InvalidTaskInput):
        Task.create("   ")


def test_task_happy_path() -> None:
    task = Task.create("Build a minimal AgentMesh task", {"priority": "low"})
    run = TaskRun.start(task.id, "demo-agent")

    task.start(run.id)
    output = {"summary": "done"}
    run.complete(output)
    task.complete(run.id, output)

    assert task.status == TaskStatus.COMPLETED
    assert task.output == output
    assert run.thread_id == str(run.id)


def test_completed_task_cannot_run_again() -> None:
    task = Task.create("Complete once")
    run = TaskRun.start(task.id, "demo-agent")
    task.start(run.id)
    task.complete(run.id, {"summary": "done"})

    with pytest.raises(InvalidTaskTransition):
        task.start(TaskRun.start(task.id, "demo-agent").id)
