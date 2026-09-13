from datetime import datetime, timedelta

import pytest

from agentmesh.application.budget_services import BudgetController
from agentmesh.domain.budgets import TaskBudget
from agentmesh.domain.errors import InvalidTaskInput
from agentmesh.domain.tasks import Task, utc_now


def test_public_budget_exhaustion_reader_is_pure_and_bounded() -> None:
    now = utc_now()
    task = Task.create(
        tenant_id="tenant-a",
        objective="budget read",
        budget=TaskBudget.create(deadline=now + timedelta(seconds=1)),
    )
    before = dict(task.__dict__)
    assert BudgetController.exhausted_reason(task, now=now) is None
    assert BudgetController.exhausted_reason(
        task, now=now + timedelta(seconds=1)
    ) == "budget_deadline_exceeded"
    assert task.__dict__ == before


def test_public_budget_exhaustion_reader_accepts_no_budget() -> None:
    task = Task.create(tenant_id="tenant-a", objective="no budget")
    assert BudgetController.exhausted_reason(task, now=utc_now()) is None


def test_public_budget_exhaustion_reader_rejects_invalid_inputs() -> None:
    task = Task.create(tenant_id="tenant-a", objective="budget validation")
    with pytest.raises(InvalidTaskInput):
        BudgetController.exhausted_reason(object(), now=utc_now())
    with pytest.raises(InvalidTaskInput):
        BudgetController.exhausted_reason(task, now=datetime(2026, 1, 1))
