from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from agentmesh.api.app import create_app
from agentmesh.application.services import RunExecutionService, TaskApplicationService
from agentmesh.domain.budgets import BudgetSettlementSource, TaskBudget
from agentmesh.domain.errors import InvalidTaskInput
from agentmesh.domain.messaging import MessageEnvelope
from agentmesh.domain.observability import UsageSource
from agentmesh.domain.tasks import RunRole, TaskExecutionMode, TaskRun, TaskStatus, utc_now
from agentmesh.features import FeatureGateSet
from agentmesh.orchestration.workflow import LangGraphWorkflowRunner
from tests.fakes import InMemoryUnitOfWorkFactory


class _BudgetedExecutor:
    def execute(self, *, objective, input, context):
        context.report_usage(
            provider="openai",
            model="gpt-budget-test",
            usage_details={"input": 120, "output": 30, "total": 150},
            cost_details_micros={"total": 420},
            currency="USD",
            source=UsageSource.PROVIDER,
        )
        return {"objective": objective, "budgeted": True}


def _reporting_service(uow_factory: InMemoryUnitOfWorkFactory) -> RunExecutionService:
    return RunExecutionService(
        uow_factory=uow_factory,
        workflow_runner=LangGraphWorkflowRunner(
            agent_executor=_BudgetedExecutor(),
            checkpointer=InMemorySaver(),
        ),
        worker_id="budget-worker",
        consumer_name="budget-worker-v1",
        lease_duration=timedelta(minutes=5),
    )


@pytest.mark.parametrize(
    "values",
    [
        {"max_runs": True},
        {"max_attempts": 0},
        {"max_tokens": 10, "token_reservation_per_attempt": 11},
        {"max_cost_micros": 10, "cost_reservation_micros_per_attempt": 0},
        {"max_runs": 9_223_372_036_854_775_808},
    ],
)
def test_budget_contract_rejects_ambiguous_or_unbounded_values(values) -> None:
    with pytest.raises(InvalidTaskInput):
        TaskBudget.create(**values)


def test_actual_usage_overrun_is_settled_and_escalated(
    task_service: TaskApplicationService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    budget = TaskBudget.create(
        max_tokens=100,
        token_reservation_per_attempt=50,
        max_cost_micros=1_000,
        cost_reservation_micros_per_attempt=500,
    )
    task_id = task_service.create_task("Bound the call", budget=budget).task.id
    task_service.request_run(task_id)

    assert _reporting_service(uow_factory).process(uow_factory.store.outbox[0]) is True

    aggregate = task_service.get_task(task_id)
    assert aggregate.task.status is TaskStatus.WAITING_APPROVAL
    assert aggregate.task.error == "budget_token_limit_exhausted"
    assert aggregate.task.settled_tokens == 150
    assert aggregate.task.settled_cost_micros == 420
    assert aggregate.task.reserved_tokens == 0
    assert aggregate.task.candidate_output == {"objective": "Bound the call", "budgeted": True}
    assert aggregate.attempts[0].budget_settlement_source is BudgetSettlementSource.ACTUAL


def test_missing_usage_is_conservatively_settled_from_reservation(
    task_service: TaskApplicationService,
    execution_service: RunExecutionService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    budget = TaskBudget.create(
        max_tokens=100,
        token_reservation_per_attempt=40,
    )
    task_id = task_service.create_task("Use a conservative estimate", budget=budget).task.id
    task_service.request_run(task_id)

    assert execution_service.process(uow_factory.store.outbox[0]) is True

    aggregate = task_service.get_task(task_id)
    assert aggregate.task.status is TaskStatus.COMPLETED
    assert aggregate.task.settled_tokens == 40
    assert aggregate.task.reserved_tokens == 0
    assert (
        aggregate.attempts[0].budget_settlement_source
        is BudgetSettlementSource.CONSERVATIVE_ESTIMATE
    )


def test_attempt_reservation_is_atomic_across_parallel_coordinated_runs(
    task_service: TaskApplicationService,
    execution_service: RunExecutionService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    from agentmesh.domain.coordination import CoordinatedPlan, SubtaskSpec

    plan = CoordinatedPlan.create(
        (
            SubtaskSpec.create(key="a", objective="A"),
            SubtaskSpec.create(key="b", objective="B"),
        ),
        max_concurrency=2,
    )
    budget = TaskBudget.create(
        max_tokens=100,
        token_reservation_per_attempt=60,
    )
    task_id = task_service.create_task(
        "Parallel budget",
        execution_mode=TaskExecutionMode.COORDINATED,
        coordinated_plan=plan,
        budget=budget,
    ).task.id
    aggregate = task_service.request_run(task_id)
    assert len(aggregate.runs) == 1
    first = aggregate.runs[0]

    # Simulate a stale/competing scheduler that bypassed the pending-Run hard filter.
    with uow_factory() as uow:
        task = uow.tasks.get(task_id, for_update=True)
        second_subtask = next(
            value
            for value in uow.subtasks.list_for_task(task_id, for_update=True)
            if value.current_run_id is None
        )
        second = TaskRun.request(
            task_id,
            first.agent_id,
            agent_version_id=first.agent_version_id,
            agent_version_digest=first.agent_version_digest,
            role=RunRole.EXECUTOR,
            subtask_id=second_subtask.id,
        )
        second_subtask.queue(second.id)
        uow.subtasks.save(second_subtask)
        uow.runs.add(second)
        uow.outbox.add(
            MessageEnvelope.run_requested(
                tenant_id=task.tenant_id,
                task_id=task.id,
                run_id=second.id,
            )
        )
        uow.commit()

    first_envelope = next(
        item for item in uow_factory.store.outbox if item.payload.get("run_id") == str(first.id)
    )
    assert execution_service._acquire(  # noqa: SLF001 - verifies the lease boundary
        first_envelope,
        task_id=task_id,
        run_id=first.id,
    ) is not None
    second_envelope = next(
        item for item in uow_factory.store.outbox if item.payload.get("run_id") == str(second.id)
    )
    assert execution_service._acquire(  # noqa: SLF001 - verifies the lease boundary
        second_envelope,
        task_id=task_id,
        run_id=second.id,
    ) is None

    current = task_service.get_task(task_id)
    assert current.task.status is TaskStatus.WAITING_APPROVAL
    assert current.task.error == "budget_token_limit_exhausted"
    assert len(current.attempts) == 1
    assert current.task.reserved_tokens == 0
    assert current.attempts[0].budget_settlement_source is BudgetSettlementSource.RELEASED


def test_budget_feature_gate_and_status_api(
    application_container,
) -> None:
    payload = {
        "objective": "API budget",
        "budget": {
            "max_runs": 2,
            "max_tokens": 100,
            "token_reservation_per_attempt": 20,
        },
    }
    with TestClient(create_app(application_container)) as client:
        created = client.post("/api/v1/tasks", json=payload)
        assert created.status_code == 201
        task_id = created.json()["id"]
        status = client.get(f"/api/v1/tasks/{task_id}/budget")
        assert status.status_code == 200
        assert status.json()["policy"]["max_runs"] == 2
        assert status.json()["settled_tokens"] == 0

    minimal = replace(
        application_container,
        feature_gates=FeatureGateSet.from_config("minimal"),
    )
    with TestClient(create_app(minimal)) as client:
        disabled = client.post("/api/v1/tasks", json=payload)
    assert disabled.status_code == 403
    assert disabled.json()["code"] == "feature_disabled"


def test_expired_deadline_stops_run_creation(
    task_service: TaskApplicationService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    budget = TaskBudget.create(max_runs=1, deadline=utc_now() + timedelta(hours=1))
    task_id = task_service.create_task("Deadline", budget=budget).task.id
    stored = uow_factory.store.tasks[task_id]
    stored.budget = replace(stored.budget, deadline=utc_now() - timedelta(seconds=1))

    aggregate = task_service.request_run(task_id)

    assert aggregate.task.status is TaskStatus.WAITING_APPROVAL
    assert aggregate.task.error == "budget_deadline_exceeded"
    assert aggregate.runs == []
