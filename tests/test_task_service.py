import pytest

from agentmesh.application.services import TaskApplicationService
from agentmesh.domain.errors import InvalidTaskTransition
from agentmesh.domain.tasks import RunStatus, TaskStatus


def test_create_and_run_task(task_service: TaskApplicationService) -> None:
    created = task_service.create_task(
        "Explain the AgentMesh execution path",
        {"format": "short"},
    )

    completed = task_service.run_task(created.task.id)

    assert completed.task.status == TaskStatus.COMPLETED
    assert completed.task.output is not None
    assert completed.task.output["agent"]["id"] == "test-agent"
    assert completed.task.output["input"] == {"format": "short"}
    assert len(completed.runs) == 1
    assert completed.runs[0].status == RunStatus.COMPLETED


def test_run_task_is_not_repeatable_in_mvp(task_service: TaskApplicationService) -> None:
    task_id = task_service.create_task("Only once").task.id
    task_service.run_task(task_id)

    with pytest.raises(InvalidTaskTransition):
        task_service.run_task(task_id)
