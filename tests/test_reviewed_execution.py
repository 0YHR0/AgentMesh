from __future__ import annotations

from datetime import timedelta

import pytest

from agentmesh.application.services import RunExecutionService, TaskApplicationService
from agentmesh.domain.errors import FeatureDisabled
from agentmesh.domain.messaging import RUN_REQUESTED_SCHEMA
from agentmesh.domain.tasks import (
    AcceptanceCriterion,
    AcceptanceCriterionKind,
    RunRole,
    TaskExecutionMode,
    TaskStatus,
    utc_now,
)
from agentmesh.features import FeatureGateSet
from tests.fakes import InMemoryUnitOfWorkFactory


def criterion(
    *, expected: object | None = None, equals: bool = False
) -> AcceptanceCriterion:
    return AcceptanceCriterion.create(
        key="contract",
        description="Candidate satisfies the output contract",
        kind=(
            AcceptanceCriterionKind.OUTPUT_PATH_EQUALS
            if equals
            else AcceptanceCriterionKind.OUTPUT_PATH_EXISTS
        ),
        path=["revision", "number"] if equals else ["summary"],
        expected=expected,
    )


def process_latest_run(
    execution_service: RunExecutionService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    envelope = next(
        message
        for message in reversed(uow_factory.store.outbox)
        if message.schema_name == RUN_REQUESTED_SCHEMA
        and not any(key[2] == message.message_id for key in uow_factory.store.inbox)
    )
    assert execution_service.process(envelope) is True


def test_reviewed_task_completes_only_after_independent_review(
    task_service: TaskApplicationService,
    execution_service: RunExecutionService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    created = task_service.create_task(
        "Produce a reviewed result",
        execution_mode=TaskExecutionMode.REVIEWED,
        acceptance_criteria=(criterion(),),
        max_revisions=1,
    )
    task_service.request_run(created.task.id)

    process_latest_run(execution_service, uow_factory)
    reviewing = task_service.get_task(created.task.id)
    assert reviewing.task.status == TaskStatus.REVIEWING
    assert [run.role for run in reviewing.runs] == [RunRole.EXECUTOR, RunRole.REVIEWER]
    assert reviewing.task.output is None

    process_latest_run(execution_service, uow_factory)
    completed = task_service.get_task(created.task.id)
    assert completed.task.status == TaskStatus.COMPLETED
    assert completed.task.output is not None
    assert completed.task.latest_review == {
        "accepted": True,
        "score_basis_points": 10_000,
        "criteria": [
            {"key": "contract", "passed": True, "reason": "Output path exists"}
        ],
        "feedback": [],
    }
    assert len(completed.attempts) == 2


def test_failed_review_schedules_bounded_revision_then_accepts(
    task_service: TaskApplicationService,
    execution_service: RunExecutionService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    created = task_service.create_task(
        "Revise until accepted",
        execution_mode=TaskExecutionMode.REVIEWED,
        acceptance_criteria=(criterion(expected=1, equals=True),),
        max_revisions=1,
    )
    task_service.request_run(created.task.id)

    for _ in range(4):
        process_latest_run(execution_service, uow_factory)

    completed = task_service.get_task(created.task.id)
    assert completed.task.status == TaskStatus.COMPLETED
    assert completed.task.revision_count == 1
    assert [(run.role, run.revision_number) for run in completed.runs] == [
        (RunRole.EXECUTOR, 0),
        (RunRole.REVIEWER, 0),
        (RunRole.EXECUTOR, 1),
        (RunRole.REVIEWER, 1),
    ]
    assert completed.task.output is not None
    assert completed.task.output["revision"]["number"] == 1


def test_revision_limit_escalates_to_waiting_approval(
    task_service: TaskApplicationService,
    execution_service: RunExecutionService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    created = task_service.create_task(
        "Escalate an unresolved review",
        execution_mode=TaskExecutionMode.REVIEWED,
        acceptance_criteria=(criterion(expected=2, equals=True),),
        max_revisions=1,
    )
    task_service.request_run(created.task.id)

    for _ in range(4):
        process_latest_run(execution_service, uow_factory)

    escalated = task_service.get_task(created.task.id)
    assert escalated.task.status == TaskStatus.WAITING_APPROVAL
    assert escalated.task.error == "review_revision_limit_reached"
    assert escalated.task.revision_count == 1
    assert escalated.task.output is None


def test_review_deadline_escalates_without_scheduling_revision(
    task_service: TaskApplicationService,
    execution_service: RunExecutionService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    created = task_service.create_task(
        "Escalate after the review deadline",
        execution_mode=TaskExecutionMode.REVIEWED,
        acceptance_criteria=(criterion(expected=1, equals=True),),
        max_revisions=1,
        review_deadline=utc_now() + timedelta(days=1),
    )
    task_service.request_run(created.task.id)
    process_latest_run(execution_service, uow_factory)
    uow_factory.store.tasks[created.task.id].review_deadline = utc_now() - timedelta(seconds=1)

    process_latest_run(execution_service, uow_factory)

    escalated = task_service.get_task(created.task.id)
    assert escalated.task.status == TaskStatus.WAITING_APPROVAL
    assert escalated.task.error == "review_deadline_exceeded"
    assert len(escalated.runs) == 2


def test_reviewed_execution_is_disabled_in_minimal_profile(
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    service = TaskApplicationService(
        uow_factory,
        agent_id="test-agent",
        reviewer_agent_id="test-reviewer",
        tenant_id="test-tenant",
        feature_gates=FeatureGateSet.from_config("minimal"),
    )
    with pytest.raises(FeatureDisabled):
        service.create_task(
            "Not enabled",
            execution_mode=TaskExecutionMode.REVIEWED,
            acceptance_criteria=(criterion(),),
        )
