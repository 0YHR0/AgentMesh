from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from agentmesh.application.ports import UnitOfWorkFactory
from agentmesh.domain.budgets import (
    BudgetSettlementSource,
    TaskBudgetStatus,
)
from agentmesh.domain.errors import InvalidTaskInput, TaskNotFound
from agentmesh.domain.observability import UsageRecord
from agentmesh.domain.tasks import RunStatus, Task, TaskAttempt, utc_now


class BudgetController:
    """Transaction-local Task admission and conservative reservation policy."""

    @staticmethod
    def run_rejection(uow: Any, task: Task, *, now: datetime | None = None) -> str | None:
        policy = task.budget
        if policy is None:
            return None
        evaluated_at = now or utc_now()
        if policy.deadline is not None and evaluated_at >= policy.deadline:
            return "budget_deadline_exceeded"
        runs = uow.runs.list_for_task(task.id)
        if (
            policy.max_runs is not None
            and len(runs) >= policy.max_runs
        ):
            return "budget_run_limit_exhausted"
        queued_runs = sum(
            1
            for run in runs
            if run.status == RunStatus.QUEUED
        )
        if policy.max_tokens is not None and (
            task.settled_tokens
            + task.reserved_tokens
            + ((queued_runs + 1) * policy.token_reservation_per_attempt)
            > policy.max_tokens
        ):
            return "budget_token_limit_exhausted"
        if policy.max_cost_micros is not None and (
            task.settled_cost_micros
            + task.reserved_cost_micros
            + ((queued_runs + 1) * policy.cost_reservation_micros_per_attempt)
            > policy.max_cost_micros
        ):
            return "budget_cost_limit_exhausted"
        return None

    @staticmethod
    def attempt_rejection(
        uow: Any, task: Task, *, now: datetime | None = None
    ) -> str | None:
        policy = task.budget
        if policy is None:
            return None
        evaluated_at = now or utc_now()
        if policy.deadline is not None and evaluated_at >= policy.deadline:
            return "budget_deadline_exceeded"
        if (
            policy.max_attempts is not None
            and len(uow.attempts.list_for_task(task.id)) >= policy.max_attempts
        ):
            return "budget_attempt_limit_exhausted"
        if policy.max_tokens is not None and (
            task.settled_tokens
            + task.reserved_tokens
            + policy.token_reservation_per_attempt
            > policy.max_tokens
        ):
            return "budget_token_limit_exhausted"
        if policy.max_cost_micros is not None and (
            task.settled_cost_micros
            + task.reserved_cost_micros
            + policy.cost_reservation_micros_per_attempt
            > policy.max_cost_micros
        ):
            return "budget_cost_limit_exhausted"
        return None

    @staticmethod
    def reserve_attempt(task: Task, attempt: TaskAttempt) -> None:
        policy = task.budget
        if policy is None:
            return
        task.reserve_budget(
            tokens=attempt.reserved_tokens,
            cost_micros=attempt.reserved_cost_micros,
        )

    @staticmethod
    def settle_attempt(
        task: Task,
        attempt: TaskAttempt,
        records: tuple[UsageRecord, ...],
    ) -> str | None:
        policy = task.budget
        if policy is None:
            return None
        actual_tokens, actual_cost, source = BudgetController._actual_usage(
            task, attempt, records
        )
        if attempt.budget_settlement_source == BudgetSettlementSource.RELEASED:
            if not records:
                return None
            attempt.restate_released_budget(
                tokens=actual_tokens,
                cost_micros=actual_cost,
            )
            task.settle_budget(
                reserved_tokens=0,
                reserved_cost_micros=0,
                actual_tokens=actual_tokens,
                actual_cost_micros=actual_cost,
            )
            return BudgetController._exhausted_reason(task)
        attempt.settle_budget(
            tokens=actual_tokens,
            cost_micros=actual_cost,
            source=source,
        )
        task.settle_budget(
            reserved_tokens=attempt.reserved_tokens,
            reserved_cost_micros=attempt.reserved_cost_micros,
            actual_tokens=actual_tokens,
            actual_cost_micros=actual_cost,
        )
        return BudgetController._exhausted_reason(task)

    @staticmethod
    def _exhausted_reason(task: Task) -> str | None:
        policy = task.budget
        assert policy is not None
        if policy.max_tokens is not None and task.settled_tokens > policy.max_tokens:
            return "budget_token_limit_exhausted"
        if policy.max_cost_micros is not None and task.settled_cost_micros > policy.max_cost_micros:
            return "budget_cost_limit_exhausted"
        if policy.deadline is not None and utc_now() >= policy.deadline:
            return "budget_deadline_exceeded"
        return None

    @staticmethod
    def release_attempt(task: Task, attempt: TaskAttempt) -> None:
        policy = task.budget
        if policy is None or attempt.budget_settlement_source is not None:
            return
        attempt.settle_budget(
            tokens=0,
            cost_micros=0,
            source=BudgetSettlementSource.RELEASED,
        )
        task.settle_budget(
            reserved_tokens=attempt.reserved_tokens,
            reserved_cost_micros=attempt.reserved_cost_micros,
            actual_tokens=0,
            actual_cost_micros=0,
        )

    @staticmethod
    def _actual_usage(
        task: Task,
        attempt: TaskAttempt,
        records: tuple[UsageRecord, ...],
    ) -> tuple[int, int, BudgetSettlementSource]:
        policy = task.budget
        assert policy is not None
        if not records:
            return (
                attempt.reserved_tokens,
                attempt.reserved_cost_micros,
                BudgetSettlementSource.CONSERVATIVE_ESTIMATE,
            )
        tokens = 0
        cost = 0
        usage_complete = True
        cost_complete = True
        for record in records:
            if policy.max_tokens is not None and "total" not in record.usage_details:
                usage_complete = False
            tokens += record.usage_details.get("total", 0)
            if policy.max_cost_micros is not None and "total" not in record.cost_details_micros:
                cost_complete = False
            record_cost = record.cost_details_micros.get("total", 0)
            if record_cost and record.currency != policy.currency:
                raise InvalidTaskInput(
                    "Budget settlement cannot mix currencies for non-zero cost"
                )
            if record.currency == policy.currency:
                cost += record_cost
        source = BudgetSettlementSource.ACTUAL
        if not usage_complete:
            tokens = max(tokens, attempt.reserved_tokens)
            source = BudgetSettlementSource.CONSERVATIVE_ESTIMATE
        if not cost_complete:
            cost = max(cost, attempt.reserved_cost_micros)
            source = BudgetSettlementSource.CONSERVATIVE_ESTIMATE
        return tokens, cost, source


class BudgetQueryService:
    def __init__(self, uow_factory: UnitOfWorkFactory, tenant_id: str) -> None:
        self._uow_factory = uow_factory
        self._tenant_id = tenant_id

    def get_status(self, task_id: UUID) -> TaskBudgetStatus:
        with self._uow_factory() as uow:
            task = uow.tasks.get(task_id)
            if task is None or task.tenant_id != self._tenant_id:
                raise TaskNotFound(task_id)
            if task.budget is None:
                raise InvalidTaskInput(f"Task {task_id} has no budget policy")
            return TaskBudgetStatus(
                task_id=task.id,
                policy=task.budget,
                run_count=len(uow.runs.list_for_task(task.id)),
                attempt_count=len(uow.attempts.list_for_task(task.id)),
                settled_tokens=task.settled_tokens,
                reserved_tokens=task.reserved_tokens,
                settled_cost_micros=task.settled_cost_micros,
                reserved_cost_micros=task.reserved_cost_micros,
                exhausted_reason=task.budget_exhausted_reason,
            )
