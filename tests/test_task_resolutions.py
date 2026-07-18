from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from agentmesh.api.app import create_app
from agentmesh.application.resolution_services import TaskResolutionService
from agentmesh.application.services import RunExecutionService, TaskApplicationService
from agentmesh.domain.budgets import TaskBudget
from agentmesh.domain.coordination import CoordinatedPlan, SubtaskSpec
from agentmesh.domain.errors import IdempotencyConflict, InvalidTaskInput
from agentmesh.domain.messaging import RUN_REQUESTED_SCHEMA
from agentmesh.domain.resolutions import TaskResolutionAction
from agentmesh.domain.tasks import (
    AcceptanceCriterion,
    AcceptanceCriterionKind,
    TaskExecutionMode,
    TaskStatus,
    utc_now,
)
from agentmesh.features import FeatureGateSet
from tests.fakes import InMemoryUnitOfWorkFactory


def _criterion(expected: int) -> AcceptanceCriterion:
    return AcceptanceCriterion.create(
        key="revision",
        description="Expected revision",
        kind=AcceptanceCriterionKind.OUTPUT_PATH_EQUALS,
        path=["revision", "number"],
        expected=expected,
    )


def _process_latest(
    execution_service: RunExecutionService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    envelope = next(
        value
        for value in reversed(uow_factory.store.outbox)
        if value.schema_name == RUN_REQUESTED_SCHEMA
        and not any(key[2] == value.message_id for key in uow_factory.store.inbox)
    )
    assert execution_service.process(envelope) is True


def test_operator_accepts_review_candidate_with_idempotent_audit(
    task_service: TaskApplicationService,
    resolution_service: TaskResolutionService,
    execution_service: RunExecutionService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    created = task_service.create_task(
        "Accept the best bounded candidate",
        execution_mode=TaskExecutionMode.REVIEWED,
        acceptance_criteria=(_criterion(1),),
        max_revisions=0,
    )
    task_service.request_run(created.task.id)
    _process_latest(execution_service, uow_factory)
    _process_latest(execution_service, uow_factory)
    assert task_service.get_task(created.task.id).task.status is TaskStatus.WAITING_APPROVAL

    first = resolution_service.accept_candidate(
        created.task.id,
        actor="operator-1",
        reason="Candidate is sufficient for this incident",
        idempotency_key="accept-1",
    )
    replay = resolution_service.accept_candidate(
        created.task.id,
        actor="operator-1",
        reason="Candidate is sufficient for this incident",
        idempotency_key="accept-1",
    )

    assert first.aggregate.task.status is TaskStatus.COMPLETED
    assert first.aggregate.task.output == first.aggregate.task.candidate_output
    assert replay.resolution.id == first.resolution.id
    assert first.resolution.action is TaskResolutionAction.ACCEPT_CANDIDATE
    assert resolution_service.list_resolutions(created.task.id) == [first.resolution]
    with pytest.raises(IdempotencyConflict):
        resolution_service.accept_candidate(
            created.task.id,
            actor="operator-1",
            reason="Changed reason",
            idempotency_key="accept-1",
        )


def test_operator_rejects_waiting_task(
    task_service: TaskApplicationService,
    resolution_service: TaskResolutionService,
    execution_service: RunExecutionService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    created = task_service.create_task(
        "Reject unresolved candidate",
        execution_mode=TaskExecutionMode.REVIEWED,
        acceptance_criteria=(_criterion(2),),
        max_revisions=0,
    )
    task_service.request_run(created.task.id)
    _process_latest(execution_service, uow_factory)
    _process_latest(execution_service, uow_factory)

    result = resolution_service.reject_task(
        created.task.id,
        actor="operator-2",
        reason="Required criterion was not met",
    )

    assert result.aggregate.task.status is TaskStatus.FAILED
    assert result.aggregate.task.error == "operator_rejected"
    assert result.resolution.previous_error == "review_revision_limit_reached"


def test_budget_increase_accepts_already_produced_direct_candidate(
    task_service: TaskApplicationService,
    resolution_service: TaskResolutionService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    from tests.test_budget_admission import _reporting_service

    original = TaskBudget.create(max_tokens=100, token_reservation_per_attempt=50)
    task_id = task_service.create_task("Overrun then approve", budget=original).task.id
    task_service.request_run(task_id)
    _process_latest(_reporting_service(uow_factory), uow_factory)

    result = resolution_service.increase_budget_and_resume(
        task_id,
        replacement=TaskBudget.create(
            max_tokens=200,
            token_reservation_per_attempt=50,
        ),
        actor="finance-operator",
        reason="Approve the measured overrun",
        idempotency_key="budget-1",
    )

    assert result.aggregate.task.status is TaskStatus.COMPLETED
    assert result.aggregate.task.budget_revision == 2
    assert result.aggregate.task.settled_tokens == 150
    assert result.resolution.details["previous_budget_revision"] == 1
    assert result.resolution.details["budget_revision"] == 2


def test_deadline_increase_queues_new_run_without_reviving_history(
    task_service: TaskApplicationService,
    resolution_service: TaskResolutionService,
    execution_service: RunExecutionService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    original = TaskBudget.create(
        max_runs=2,
        deadline=utc_now() + timedelta(hours=1),
    )
    task_id = task_service.create_task("Resume expired admission", budget=original).task.id
    stored = uow_factory.store.tasks[task_id]
    stored.budget = replace(stored.budget, deadline=utc_now() - timedelta(seconds=1))
    waiting = task_service.request_run(task_id)
    assert waiting.task.status is TaskStatus.WAITING_APPROVAL

    resumed = resolution_service.increase_budget_and_resume(
        task_id,
        replacement=TaskBudget.create(
            max_runs=2,
            deadline=utc_now() + timedelta(hours=2),
        ),
        actor="operator-3",
        reason="Extend the business deadline",
    )

    assert resumed.aggregate.task.status is TaskStatus.READY
    assert len(resumed.aggregate.runs) == 1
    _process_latest(execution_service, uow_factory)
    assert task_service.get_task(task_id).task.status is TaskStatus.COMPLETED


def test_coordinated_budget_increase_reopens_only_unfinished_subtasks(
    task_service: TaskApplicationService,
    resolution_service: TaskResolutionService,
    execution_service: RunExecutionService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    plan = CoordinatedPlan.create(
        (
            SubtaskSpec.create(key="left", objective="Left"),
            SubtaskSpec.create(key="right", objective="Right"),
        ),
        max_concurrency=1,
    )
    task_id = task_service.create_task(
        "Resume coordinated budget",
        execution_mode=TaskExecutionMode.COORDINATED,
        coordinated_plan=plan,
        budget=TaskBudget.create(max_runs=1),
    ).task.id
    task_service.request_run(task_id)
    _process_latest(execution_service, uow_factory)
    waiting = task_service.get_task(task_id)
    assert waiting.task.status is TaskStatus.WAITING_APPROVAL

    resumed = resolution_service.increase_budget_and_resume(
        task_id,
        replacement=TaskBudget.create(max_runs=3),
        actor="operator-4",
        reason="Fund the remaining branch and Supervisor",
    )
    assert resumed.aggregate.task.status is TaskStatus.RUNNING
    assert len(resumed.aggregate.runs) == 2

    _process_latest(execution_service, uow_factory)
    _process_latest(execution_service, uow_factory)
    completed = task_service.get_task(task_id)
    assert completed.task.status is TaskStatus.COMPLETED
    assert len(completed.runs) == 3


def test_resolution_api_and_feature_gate(
    task_service: TaskApplicationService,
    execution_service: RunExecutionService,
    uow_factory: InMemoryUnitOfWorkFactory,
    application_container,
) -> None:
    created = task_service.create_task(
        "Resolve over API",
        execution_mode=TaskExecutionMode.REVIEWED,
        acceptance_criteria=(_criterion(1),),
        max_revisions=0,
    )
    task_service.request_run(created.task.id)
    _process_latest(execution_service, uow_factory)
    _process_latest(execution_service, uow_factory)

    with TestClient(create_app(application_container)) as client:
        accepted = client.post(
            f"/api/v1/tasks/{created.task.id}/resolutions/accept-candidate",
            headers={"Idempotency-Key": "api-resolution"},
            json={"actor": "api-operator", "reason": "Accept via API"},
        )
        assert accepted.status_code == 200
        assert accepted.json()["task"]["status"] == "COMPLETED"
        audit = client.get(f"/api/v1/tasks/{created.task.id}/resolutions")
        assert audit.json()["items"][0]["action"] == "ACCEPT_CANDIDATE"

    minimal = replace(
        application_container,
        feature_gates=FeatureGateSet.from_config("minimal"),
    )
    with TestClient(create_app(minimal)) as client:
        disabled = client.get(f"/api/v1/tasks/{created.task.id}/resolutions")
    assert disabled.status_code == 403
    assert disabled.json()["code"] == "feature_disabled"


def test_budget_replacement_must_be_monotonic() -> None:
    original = TaskBudget.create(max_tokens=100, token_reservation_per_attempt=20)
    with pytest.raises(InvalidTaskInput, match="reduce"):
        original.require_monotonic_increase(
            TaskBudget.create(max_tokens=90, token_reservation_per_attempt=20)
        )
    with pytest.raises(InvalidTaskInput, match="reservation"):
        original.require_monotonic_increase(
            TaskBudget.create(max_tokens=110, token_reservation_per_attempt=10)
        )
