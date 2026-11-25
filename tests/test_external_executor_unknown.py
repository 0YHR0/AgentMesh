from datetime import timedelta

import pytest

from agentmesh.application.ports import ExternalExecutionUncertain
from agentmesh.domain.budgets import TaskBudget
from agentmesh.domain.coordination import CoordinatedPlan, SubtaskSpec, SubtaskStatus
from agentmesh.domain.messaging import MessageEnvelope
from agentmesh.domain.tasks import AttemptStatus, RunStatus, TaskExecutionMode, TaskStatus, utc_now
from tests.test_coordinated_execution import run_wakeup


@pytest.mark.parametrize("coordinated", [False, True])
def test_unknown_dispatch_is_parked_without_retry_or_freeing_budget(
    task_service, execution_service, uow_factory, monkeypatch, coordinated
):
    calls = []

    def uncertain(*args, **kwargs):
        calls.append(True)
        raise ExternalExecutionUncertain()

    monkeypatch.setattr(execution_service._workflow_runner, "run", uncertain)
    options = {}
    if coordinated:
        options = dict(
            execution_mode=TaskExecutionMode.COORDINATED,
            coordinated_plan=CoordinatedPlan.create(
                (
                    SubtaskSpec.create(
                        key="lyrics",
                        objective="Write lyrics",
                        input={},
                        required_capabilities=("general.task",),
                        depends_on=(),
                        preferred_agent_id="test-agent",
                    ),
                    SubtaskSpec.create(
                        key="arrangement", objective="Arrange", depends_on=("lyrics",)
                    ),
                ),
                max_concurrency=1,
            ),
        )
    draft = task_service.create_task(
        "External sandbox",
        budget=TaskBudget.create(
            deadline=utc_now() + timedelta(minutes=10),
            max_tokens=100,
            token_reservation_per_attempt=10,
        ),
        **options,
    )
    started = task_service.request_run(draft.task.id)
    run = started.runs[0]
    message = run_wakeup(uow_factory, run.id)
    assert execution_service.process(message)
    result = task_service.get_task(draft.task.id)
    assert result.runs[0].status is RunStatus.RECONCILIATION_REQUIRED
    with uow_factory() as uow:
        attempt = uow.attempts.latest_for_run(run.id)
    assert attempt.status is AttemptStatus.OUTCOME_UNKNOWN
    assert attempt.reserved_tokens == 10 and attempt.settled_tokens is None
    if coordinated:
        assert (
            next(s for s in result.subtasks if s.key == "lyrics").status
            is SubtaskStatus.RECONCILIATION_REQUIRED
        )
    else:
        assert result.task.status is TaskStatus.RECONCILIATION_REQUIRED
    assert execution_service.process(message) is False
    fresh = MessageEnvelope.run_requested(
        tenant_id="test-tenant", task_id=draft.task.id, run_id=run.id
    )
    assert execution_service.process(fresh) is False
    assert len(calls) == 1
