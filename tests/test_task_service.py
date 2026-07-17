import time
from datetime import timedelta

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from agentmesh.application.registry_services import AgentRegistryService
from agentmesh.application.services import RunExecutionService, TaskApplicationService
from agentmesh.domain.errors import IdempotencyConflict, InvalidTaskTransition, RunLeaseUnavailable
from agentmesh.domain.tasks import AttemptStatus, RunStatus, TaskStatus
from agentmesh.orchestration.workflow import LangGraphWorkflowRunner
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
    assert queued.runs[0].agent_version_id is not None
    assert queued.runs[0].agent_version_digest is not None
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


def test_run_keeps_immutable_agent_version_when_default_changes(
    task_service: TaskApplicationService,
    registry_service: AgentRegistryService,
) -> None:
    first_task = task_service.create_task("Use the original Agent Version")
    first_run = task_service.request_run(first_task.task.id).runs[0]
    definition = next(
        item.definition
        for item in registry_service.list_definitions()
        if item.definition.name == "test-agent"
    )
    next_version = registry_service.create_version(
        definition.id,
        semantic_version="0.2.0",
        role="General task executor",
        instructions="Complete the task using the new immutable version.",
        declared_capabilities=["general.task"],
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        runtime_adapter="deterministic-local",
        execution_modes=["async"],
    )
    registry_service.submit_version(next_version.id)
    next_version = registry_service.publish_version(
        next_version.id,
        verified_capabilities=["general.task"],
        make_default=True,
    )

    persisted_first_run = task_service.get_task(first_task.task.id).runs[0]
    second_task = task_service.create_task("Use the new Agent Version")
    second_run = task_service.request_run(second_task.task.id).runs[0]

    assert persisted_first_run.agent_version_id == first_run.agent_version_id
    assert persisted_first_run.agent_version_digest == first_run.agent_version_digest
    assert second_run.agent_version_id == next_version.id
    assert second_run.agent_version_digest == next_version.content_digest
    affected = registry_service.list_affected_active_runs(first_run.agent_version_id)
    assert [run.id for run in affected] == [first_run.id]


def test_list_tasks_batch_loads_child_collections(
    task_service: TaskApplicationService,
    execution_service: RunExecutionService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    assert task_service.list_tasks(limit=10, offset=100) == []
    assert uow_factory.store.run_list_for_tasks_calls == 0
    assert uow_factory.store.attempt_list_for_tasks_calls == 0

    created_only = task_service.create_task("Created only")
    queued_task = task_service.create_task("Queued only")
    queued = task_service.request_run(queued_task.task.id)
    completed_task = task_service.create_task("Completed")
    completed_run = task_service.request_run(completed_task.task.id)
    wakeup = next(
        envelope
        for envelope in reversed(uow_factory.store.outbox)
        if envelope.payload["run_id"] == str(completed_run.runs[0].id)
    )
    assert execution_service.process(wakeup) is True

    values = task_service.list_tasks(limit=10, offset=0)
    by_id = {value.task.id: value for value in values}

    assert by_id[created_only.task.id].runs == []
    assert by_id[created_only.task.id].attempts == []
    assert [run.id for run in by_id[queued_task.task.id].runs] == [queued.runs[0].id]
    assert by_id[queued_task.task.id].attempts == []
    assert [run.id for run in by_id[completed_task.task.id].runs] == [completed_run.runs[0].id]
    assert len(by_id[completed_task.task.id].attempts) == 1
    assert uow_factory.store.run_list_for_task_calls == 0
    assert uow_factory.store.attempt_list_for_task_calls == 0
    assert uow_factory.store.run_list_for_tasks_calls == 1
    assert uow_factory.store.attempt_list_for_tasks_calls == 1


def test_queued_task_pause_consumes_old_wakeup_then_resume_completes(
    task_service: TaskApplicationService,
    execution_service: RunExecutionService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    task_id = task_service.create_task("Pause queued work").task.id
    queued = task_service.request_run(task_id)
    original_wakeup = uow_factory.store.outbox[0]

    paused = task_service.pause_task(task_id)
    outbox_size = len(uow_factory.store.outbox)
    paused_again = task_service.pause_task(task_id)

    assert paused.task.status == TaskStatus.PAUSED
    assert paused.runs[0].status == RunStatus.PAUSED
    assert paused.runs[0].paused_at is not None
    assert paused_again.task.status == TaskStatus.PAUSED
    assert len(uow_factory.store.outbox) == outbox_size
    assert execution_service.process(original_wakeup) is False
    assert not uow_factory.store.attempts

    resumed = task_service.resume_task(task_id)
    resume_wakeup = next(
        item
        for item in reversed(uow_factory.store.outbox)
        if item.schema_name == original_wakeup.schema_name
        and item.message_id != original_wakeup.message_id
    )
    outbox_size = len(uow_factory.store.outbox)
    resumed_again = task_service.resume_task(task_id)

    assert resumed.task.status == TaskStatus.READY
    assert resumed.runs[0].status == RunStatus.QUEUED
    assert resumed.runs[0].resumed_at is not None
    assert resumed_again.task.status == TaskStatus.READY
    assert len(uow_factory.store.outbox) == outbox_size
    assert execution_service.process(resume_wakeup) is True
    assert task_service.get_task(task_id).task.status == TaskStatus.COMPLETED
    assert queued.runs[0].id == resumed.runs[0].id


class _PauseOnceExecutor:
    def __init__(self, task_service: TaskApplicationService) -> None:
        self._task_service = task_service
        self.calls = 0

    def execute(self, *, objective, input, context):
        self.calls += 1
        if self.calls == 1:
            self._task_service.pause_task(context.task_id)
        return {"objective": objective, "input": dict(input), "checkpointed": True}


def test_running_task_resumes_from_checkpoint_without_reexecuting_agent(
    task_service: TaskApplicationService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    executor = _PauseOnceExecutor(task_service)
    workflow = LangGraphWorkflowRunner(
        agent_executor=executor,
        checkpointer=InMemorySaver(),
    )
    execution_service = RunExecutionService(
        uow_factory=uow_factory,
        workflow_runner=workflow,
        worker_id="pause-worker",
        consumer_name="pause-worker-v1",
        lease_duration=timedelta(minutes=5),
    )
    task_id = task_service.create_task("Pause after checkpoint").task.id
    task_service.request_run(task_id)
    original_wakeup = uow_factory.store.outbox[0]

    assert execution_service.process(original_wakeup) is True
    paused = task_service.get_task(task_id)
    assert paused.task.status == TaskStatus.PAUSED
    assert paused.task.output is None
    assert paused.runs[0].status == RunStatus.PAUSED
    assert paused.attempts[0].status == AttemptStatus.PAUSED

    task_service.resume_task(task_id)
    resume_wakeup = next(
        item
        for item in reversed(uow_factory.store.outbox)
        if item.schema_name == original_wakeup.schema_name
        and item.message_id != original_wakeup.message_id
    )
    assert execution_service.process(resume_wakeup) is True

    completed = task_service.get_task(task_id)
    assert completed.task.status == TaskStatus.COMPLETED
    assert completed.task.output == {
        "objective": "Pause after checkpoint",
        "input": {},
        "checkpointed": True,
    }
    assert [attempt.status for attempt in completed.attempts] == [
        AttemptStatus.PAUSED,
        AttemptStatus.SUCCEEDED,
    ]
    assert [attempt.fencing_token for attempt in completed.attempts] == [1, 2]
    assert executor.calls == 1


def test_expired_attempt_converges_pause_request_without_reexecution(
    task_service: TaskApplicationService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    executor = _PauseOnceExecutor(task_service)
    execution_service = RunExecutionService(
        uow_factory=uow_factory,
        workflow_runner=LangGraphWorkflowRunner(
            agent_executor=executor,
            checkpointer=InMemorySaver(),
        ),
        worker_id="expired-worker",
        consumer_name="expired-worker-v1",
        lease_duration=timedelta(seconds=-1),
    )
    task_id = task_service.create_task("Recover a paused crashed worker").task.id
    queued = task_service.request_run(task_id)
    wakeup = uow_factory.store.outbox[0]
    assert (
        execution_service._acquire(
            wakeup,
            task_id=task_id,
            run_id=queued.runs[0].id,
        )
        is not None
    )

    requested = task_service.pause_task(task_id)
    assert requested.task.status == TaskStatus.PAUSE_REQUESTED
    assert execution_service.process(wakeup) is False

    paused = task_service.get_task(task_id)
    assert paused.task.status == TaskStatus.PAUSED
    assert paused.runs[0].status == RunStatus.PAUSED
    assert paused.attempts[0].status == AttemptStatus.LEASE_EXPIRED
    assert executor.calls == 0

    with pytest.raises(RunLeaseUnavailable):
        execution_service._finalize_success(
            wakeup,
            task_id,
            queued.runs[0].id,
            paused.attempts[0].id,
            {"late": True},
        )
    assert task_service.get_task(task_id).task.status == TaskStatus.PAUSED


class _SlowWorkflowRunner:
    def __init__(self, sleep_seconds: float) -> None:
        self._sleep_seconds = sleep_seconds

    def run(self, task, run, attempt):
        from agentmesh.application.ports import WorkflowExecutionResult

        time.sleep(self._sleep_seconds)
        return WorkflowExecutionResult(output={"renewed": True})


def test_running_attempt_lease_is_renewed_while_workflow_runs(
    task_service: TaskApplicationService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    execution_service = RunExecutionService(
        uow_factory=uow_factory,
        workflow_runner=_SlowWorkflowRunner(sleep_seconds=0.12),
        worker_id="renew-worker",
        consumer_name="renew-worker-v1",
        lease_duration=timedelta(seconds=1),
        lease_renewal_interval=timedelta(seconds=0.02),
    )
    task_id = task_service.create_task("Renew a running lease").task.id
    task_service.request_run(task_id)
    wakeup = uow_factory.store.outbox[0]

    assert execution_service.process(wakeup) is True

    completed = task_service.get_task(task_id)
    attempt = completed.attempts[0]
    assert completed.task.status == TaskStatus.COMPLETED
    assert attempt.status == AttemptStatus.SUCCEEDED
    assert attempt.heartbeat_at > attempt.started_at


def test_default_lease_renewal_interval_is_before_expiry() -> None:
    assert RunExecutionService._default_renewal_interval(
        timedelta(seconds=1)
    ) == timedelta(seconds=1 / 3)


def test_attempt_lease_renewal_requires_current_live_owner(
    task_service: TaskApplicationService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    execution_service = RunExecutionService(
        uow_factory=uow_factory,
        workflow_runner=_SlowWorkflowRunner(sleep_seconds=0),
        worker_id="renew-worker",
        consumer_name="renew-worker-v1",
        lease_duration=timedelta(seconds=1),
    )
    task_id = task_service.create_task("Reject stale renewal").task.id
    queued = task_service.request_run(task_id)
    wakeup = uow_factory.store.outbox[0]
    task, run, attempt = execution_service._acquire(
        wakeup,
        task_id=task_id,
        run_id=queued.runs[0].id,
    )
    del task, run
    original_expires_at = attempt.lease_expires_at

    assert (
        execution_service._renew_attempt_lease(
            run_id=queued.runs[0].id,
            attempt_id=attempt.id,
            lease_token=attempt.lease_token,
        )
        is True
    )
    renewed = task_service.get_task(task_id).attempts[0]
    assert renewed.lease_expires_at >= original_expires_at

    with uow_factory() as uow:
        renewed.lease_expires_at = renewed.started_at - timedelta(seconds=1)
        uow.attempts.save(renewed)
        uow.commit()

    assert (
        execution_service._renew_attempt_lease(
            run_id=queued.runs[0].id,
            attempt_id=attempt.id,
            lease_token=attempt.lease_token,
        )
        is False
    )
