import pytest

from agentmesh.application.services import RunExecutionService, TaskApplicationService
from agentmesh.domain.errors import IdempotencyConflict, InvalidTaskTransition
from agentmesh.domain.tasks import AttemptStatus, RunStatus, TaskStatus
from tests.fakes import InMemoryUnitOfWorkFactory


def test_request_and_execute_task(
    task_service: TaskApplicationService,
    execution_service: RunExecutionService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    created = task_service.create_task(
        "Explain the AgentMesh execution path",
        {"format": "short"},
    )

    queued = task_service.request_run(created.task.id)

    assert queued.task.status == TaskStatus.READY
    assert queued.runs[0].status == RunStatus.QUEUED
    assert len(uow_factory.store.outbox) == 1

    assert execution_service.process(uow_factory.store.outbox[0]) is True
    completed = task_service.get_task(created.task.id)

    assert completed.task.status == TaskStatus.COMPLETED
    assert completed.task.output is not None
    assert completed.task.output["agent"]["id"] == "test-agent"
    assert completed.task.output["input"] == {"format": "short"}
    assert completed.runs[0].status == RunStatus.SUCCEEDED
    assert completed.attempts[0].status == AttemptStatus.SUCCEEDED


def test_duplicate_delivery_is_ignored_after_inbox_commit(
    task_service: TaskApplicationService,
    execution_service: RunExecutionService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    task_id = task_service.create_task("Exactly once business effect").task.id
    task_service.request_run(task_id)
    envelope = uow_factory.store.outbox[0]

    assert execution_service.process(envelope) is True
    assert execution_service.process(envelope) is False
    assert len(uow_factory.store.attempts) == 1


def test_run_request_is_not_repeatable_after_queue(task_service: TaskApplicationService) -> None:
    task_id = task_service.create_task("Only once").task.id
    task_service.request_run(task_id)

    with pytest.raises(InvalidTaskTransition):
        task_service.request_run(task_id)


def test_idempotency_key_replays_same_run(task_service: TaskApplicationService) -> None:
    task_id = task_service.create_task("Idempotent run").task.id
    first = task_service.request_run(task_id, idempotency_key="request-1")
    replay = task_service.request_run(task_id, idempotency_key="request-1")

    assert replay.task.id == first.task.id
    assert replay.runs[0].id == first.runs[0].id


def test_idempotency_key_cannot_be_reused_for_another_task(
    task_service: TaskApplicationService,
) -> None:
    first = task_service.create_task("First").task.id
    second = task_service.create_task("Second").task.id
    task_service.request_run(first, idempotency_key="shared-key")

    with pytest.raises(IdempotencyConflict):
        task_service.request_run(second, idempotency_key="shared-key")
