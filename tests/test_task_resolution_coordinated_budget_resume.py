from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from agentmesh.application.coordination_services import CoordinatedScheduleReceipt
from agentmesh.domain.budgets import TaskBudget
from agentmesh.domain.coordination import (
    COORDINATION_BUDGET_DRAIN_CANCEL_REQUESTED,
    CoordinationRuntimeBoundary,
    Subtask,
    SubtaskCancellationSource,
)
from agentmesh.domain.errors import InvalidTaskTransition
from agentmesh.domain.messaging import RUN_REQUESTED_SCHEMA, MessageEnvelope
from agentmesh.domain.runtime_execution import RuntimeExecutionPhase, RuntimeLifecycleStatus
from agentmesh.domain.tasks import AttemptStatus, RunRole, RunStatus, TaskRun, TaskStatus
from tests.test_task_resolution_coordinated_candidate import _fixture as candidate_fixture


class _Scheduler:
    def __init__(self, factory, *, fail=False) -> None:
        self.factory = factory
        self.fail = fail
        self.calls = 0

    def schedule_with_receipt(self, uow, task, *, at=None, causation_id=None):
        self.calls += 1
        self.factory.operations.append(("scheduler.schedule", None))
        if self.fail:
            raise RuntimeError("injected scheduler failure")
        subtask = uow.aggregate.subtasks[0]
        subtask.mark_ready(at=at)
        previous = uow.aggregate.runs[0]
        run = TaskRun.request(
            task.id,
            previous.agent_id,
            agent_version_id=previous.agent_version_id,
            agent_version_digest=previous.agent_version_digest,
            role=RunRole.EXECUTOR,
            subtask_id=subtask.id,
            runtime_version_id=previous.runtime_version_id,
            runtime_authority="managed",
            at=at,
        )
        subtask.queue(run.id, at=at)
        uow.subtasks.save(subtask)
        event = MessageEnvelope.run_requested(
            tenant_id=task.tenant_id,
            task_id=task.id,
            run_id=run.id,
            at=at,
        )
        uow.outbox.add(event)
        uow.aggregate = replace(
            uow.aggregate,
            subtasks=tuple(uow.aggregate.subtasks),
            runs=tuple((*uow.aggregate.runs, run)),
            latest_attempts={**uow.aggregate.latest_attempts, run.id: None},
            boundary_classifications={
                **uow.aggregate.boundary_classifications,
                run.id: CoordinationRuntimeBoundary.NOT_CROSSED_QUEUED,
            },
        )
        return CoordinatedScheduleReceipt((run,), (event,))


def _fixture(
    *,
    corrupt_drain=False,
    unfinished=False,
    open_lifecycle=False,
    reopened_count=1,
    fail_on=None,
):
    fixture = candidate_fixture(fail_on=fail_on)
    aggregate = fixture.factory.aggregate
    drain = fixture.factory.drain
    previous = aggregate.runs[0]
    attempt = aggregate.latest_attempts[previous.id]
    execution = aggregate.executions[0]
    subtask = Subtask.create(
        subtask_id=uuid4(),
        task_id=aggregate.task.id,
        key="retry-budget-worker",
        objective="Retry after budget approval",
        input={},
        required_capabilities=("general.task",),
        preferred_agent_id=None,
        initially_ready=True,
    )
    at = max(subtask.updated_at, aggregate.task.updated_at, drain.updated_at) + timedelta(seconds=1)
    run = replace(
        previous,
        role=RunRole.EXECUTOR,
        subtask_id=subtask.id,
        status=RunStatus.CANCELED,
        output=None,
        error="runtime.canceled",
    )
    attempt = replace(attempt, status=AttemptStatus.CANCELED, error="runtime.canceled")
    execution = replace(execution, phase=RuntimeExecutionPhase.CANCELED)
    subtask.queue(run.id, at=at)
    subtask.cancel_by_drain(
        run.id,
        uuid4() if corrupt_drain else drain.id,
        source=SubtaskCancellationSource.BUDGET_DRAIN,
        at=at,
    )
    assert subtask.error == COORDINATION_BUDGET_DRAIN_CANCEL_REQUESTED
    task = replace(
        aggregate.task,
        status=TaskStatus.WAITING_APPROVAL,
        current_run_id=None,
        output=None,
        candidate_output=None,
        error=drain.reason,
        budget_exhausted_reason=drain.reason,
        budget=TaskBudget.create(max_runs=1),
        budget_revision=1,
        updated_at=at,
    )
    boundary = (
        CoordinationRuntimeBoundary.CROSSED_ACTIVE
        if unfinished
        else CoordinationRuntimeBoundary.KNOWN_TERMINAL
    )
    lifecycle = aggregate.lifecycle_operations
    if open_lifecycle:
        lifecycle = (SimpleNamespace(status=RuntimeLifecycleStatus.REQUESTED),)
    subtasks = [subtask]
    runs = [run]
    attempts = {run.id: attempt}
    executions = [execution]
    boundaries = {run.id: boundary}
    if reopened_count == 2:
        second_subtask = Subtask.create(
            subtask_id=uuid4(),
            task_id=aggregate.task.id,
            key="retry-budget-worker-2",
            objective="Retry second worker after budget approval",
            input={},
            required_capabilities=("general.task",),
            preferred_agent_id=None,
            initially_ready=True,
        )
        second_run = replace(run, id=uuid4(), subtask_id=second_subtask.id)
        second_attempt = replace(attempt, id=uuid4(), run_id=second_run.id)
        second_execution = replace(
            execution,
            id=uuid4(),
            run_id=second_run.id,
            current_owner_attempt_id=second_attempt.id,
        )
        second_subtask.queue(second_run.id, at=at)
        second_subtask.cancel_by_drain(
            second_run.id,
            drain.id,
            source=SubtaskCancellationSource.BUDGET_DRAIN,
            at=at,
        )
        subtasks.append(second_subtask)
        runs.append(second_run)
        attempts[second_run.id] = second_attempt
        executions.append(second_execution)
        boundaries[second_run.id] = boundary
    fixture.factory.aggregate = replace(
        aggregate,
        task=task,
        subtasks=tuple(subtasks),
        runs=tuple(runs),
        latest_attempts=attempts,
        executions=tuple(executions),
        boundary_classifications=boundaries,
        lifecycle_operations=lifecycle,
    )
    scheduler = _Scheduler(fixture.factory, fail=fail_on == "scheduler")
    fixture.service._scheduler = scheduler
    fixture.service._require_future_admission = (
        lambda _uow, _task: fixture.factory.operations.append(("future.admission", None))
    )
    fixture.scheduler = scheduler
    fixture.replacement = TaskBudget.create(max_runs=3)
    return fixture


def _request(fixture):
    return {
        "task_id": fixture.task.id,
        "replacement": fixture.replacement,
        "actor": "finance-operator",
        "reason": "Increase the bounded execution budget",
        "idempotency_key": "budget-resume-1",
    }


def test_fresh_managed_coordinated_budget_resume_is_one_atomic_schedule():
    fixture = _fixture()

    result = fixture.service.increase_budget_and_resume(**_request(fixture))

    assert result.aggregate.task.status is TaskStatus.RUNNING
    assert result.aggregate.task.budget_revision == 2
    assert fixture.factory.drain.status.value == "COMPLETE"
    assert fixture.scheduler.calls == 1
    assert fixture.factory.commits == 1
    details = result.resolution.details
    assert details["drain_version_after"] == details["drain_version_before"] + 1
    assert len(details["reopened_subtask_ids"]) == len(details["scheduled_run_ids"]) == 1
    assert any(
        value.schema_name == RUN_REQUESTED_SCHEMA
        for value in fixture.factory.events.values()
    )
    names = [value[0] for value in fixture.factory.operations]
    assert names.index("task.get") < names.index("aggregate.lock_after_task")
    assert names.index("aggregate.lock_after_task") < names.index("idem.lock")
    assert names.count("scheduler.schedule") == names.count("commit") == 1


def test_two_budget_canceled_subtasks_resume_but_single_concurrency_schedules_one():
    fixture = _fixture(reopened_count=2)

    first = fixture.service.increase_budget_and_resume(**_request(fixture))
    replay = fixture.service.increase_budget_and_resume(**_request(fixture))

    assert replay.resolution == first.resolution
    details = first.resolution.details
    assert len(details["reopened_subtask_ids"]) == 2
    assert len(details["scheduled_run_ids"]) == 1
    current = fixture.factory.aggregate.subtasks
    assert sum(value.current_run_id is not None for value in current) == 1
    assert sum(value.current_run_id is None for value in current) == 1
    assert fixture.scheduler.calls == fixture.factory.commits == 1


def test_fresh_budget_resume_rejects_mismatched_budget_drain_provenance():
    fixture = _fixture(corrupt_drain=True)

    with pytest.raises(InvalidTaskTransition, match="different drain"):
        fixture.service.increase_budget_and_resume(**_request(fixture))

    assert fixture.scheduler.calls == fixture.factory.commits == 0


def test_existing_managed_budget_resume_replays_without_writes():
    fixture = _fixture()
    first = fixture.service.increase_budget_and_resume(**_request(fixture))
    operations_at_commit = len(fixture.factory.operations)

    replay = fixture.service.increase_budget_and_resume(**_request(fixture))

    assert replay.resolution == first.resolution
    stored = next(iter(fixture.factory.idem_values.values())).result
    assert set(stored) == {
        "resolution_id",
        "drain_id",
        "drain_version_before",
        "drain_version_after",
        "previous_budget",
        "replacement_budget",
        "previous_budget_revision",
        "resulting_budget_revision",
        "reopened_subtask_ids",
        "scheduled_run_ids",
        "run_requested_event_ids",
        "outbox_event_id",
    }
    assert fixture.scheduler.calls == fixture.factory.commits == 1
    replay_operations = fixture.factory.operations[operations_at_commit:]
    assert not any(
        name in {"task.save", "subtask.save", "drain.save", "outbox.add", "commit"}
        for name, _value in replay_operations
    )


@pytest.mark.parametrize(
    "corruption",
    [
        "idempotency",
        "task",
        "drain",
        "resolution",
        "subtask",
        "run_requested",
        "resolution_outbox",
    ],
)
def test_existing_managed_budget_resume_rejects_partial_or_tampered_projection(
    corruption,
):
    fixture = _fixture()
    fixture.service.increase_budget_and_resume(**_request(fixture))
    record = next(iter(fixture.factory.idem_values.values()))
    projection = dict(record.result)
    if corruption == "idempotency":
        record.result.pop("drain_id")
    elif corruption == "task":
        fixture.factory.aggregate.task.budget_revision += 1
    elif corruption == "drain":
        fixture.factory.drain = replace(
            fixture.factory.drain,
            reason="tampered",
        )
    elif corruption == "resolution":
        resolution_id = next(iter(fixture.factory.resolutions))
        fixture.factory.resolutions[resolution_id] = replace(
            fixture.factory.resolutions[resolution_id],
            actor="tampered",
        )
    elif corruption == "subtask":
        subtask = fixture.factory.aggregate.subtasks[0]
        fixture.factory.aggregate = replace(
            fixture.factory.aggregate,
            subtasks=(replace(subtask, current_run_id=uuid4()),),
        )
    elif corruption == "run_requested":
        event_id = UUID(projection["run_requested_event_ids"][0])
        event = fixture.factory.events[event_id]
        fixture.factory.events[event_id] = replace(
            event,
            payload={**event.payload, "run_id": str(uuid4())},
        )
    else:
        event_id = UUID(projection["outbox_event_id"])
        event = fixture.factory.events[event_id]
        fixture.factory.events[event_id] = replace(event, producer="tampered")

    with pytest.raises(InvalidTaskTransition, match="projection|audit|Outbox"):
        fixture.service.increase_budget_and_resume(**_request(fixture))

    assert fixture.scheduler.calls == fixture.factory.commits == 1


@pytest.mark.parametrize("invalid", ["unfinished", "open_lifecycle"])
def test_fresh_budget_resume_rejects_unfinished_projection(invalid):
    fixture = _fixture(
        unfinished=invalid == "unfinished",
        open_lifecycle=invalid == "open_lifecycle",
    )

    with pytest.raises(InvalidTaskTransition, match="projection is invalid"):
        fixture.service.increase_budget_and_resume(**_request(fixture))

    assert fixture.scheduler.calls == fixture.factory.commits == 0


@pytest.mark.parametrize("fail_on", ["scheduler", "subtask.save", "drain.save", "commit"])
def test_fresh_budget_resume_failure_rolls_back(fail_on):
    fixture = _fixture(fail_on=fail_on)

    with pytest.raises(RuntimeError, match="injected"):
        fixture.service.increase_budget_and_resume(**_request(fixture))

    assert fixture.factory.commits == 0
    assert fixture.factory.aggregate.task.status is TaskStatus.WAITING_APPROVAL
    assert fixture.factory.drain.status.value == "DRAINING"
    assert not fixture.factory.resolutions
    assert not fixture.factory.idem_values
