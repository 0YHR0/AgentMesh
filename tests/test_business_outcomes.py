from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest

from agentmesh.application.budget_services import BudgetController
from agentmesh.application.business_outcomes import (
    AccountingDisposition,
    BusinessOutcomeApplication,
    BusinessOutcomeApplier,
    KnownTerminalPhase,
    PreparedAccountingBatch,
    PreparedAccountingTransition,
    ProgressionContext,
)
from agentmesh.application.coordination_services import CoordinatedScheduler
from agentmesh.domain.budgets import BudgetSettlementSource, TaskBudget
from agentmesh.domain.coordination import CoordinatedPlan, SubtaskSpec, SubtaskStatus
from agentmesh.domain.errors import InvalidTaskInput, InvalidTaskTransition
from agentmesh.domain.resolutions import TaskResolutionAction
from agentmesh.domain.tasks import (
    AcceptanceCriterion,
    AcceptanceCriterionKind,
    AttemptStatus,
    RunRole,
    RunStatus,
    TaskAttempt,
    TaskExecutionMode,
    TaskRun,
    TaskStatus,
    utc_now,
)


def _manual_running_coordinated(uow_factory, task_service, *, budget=None, count=2):
    plan = CoordinatedPlan.create(
        tuple(
            SubtaskSpec.create(
                key=chr(ord("a") + index),
                objective=f"Execute {chr(ord('a') + index)}",
                input={},
            )
            for index in range(count)
        ),
        max_concurrency=count,
    )
    created = task_service.create_task(
        "Manual coordinated outcome",
        execution_mode=TaskExecutionMode.COORDINATED,
        coordinated_plan=plan,
        budget=budget,
    )
    started = task_service.request_run(created.task.id)
    target_run = started.runs[0]
    with uow_factory() as uow:
        task = uow.tasks.get(created.task.id, for_update=True)
        run = uow.runs.get(target_run.id, for_update=True)
        subtask = uow.subtasks.get(target_run.subtask_id, for_update=True)
        assert task is not None and run is not None and subtask is not None
        at = max(task.updated_at, run.queued_at, subtask.updated_at) + timedelta(seconds=1)
        subtask.start(run.id, at=at)
        run.start(at=at)
        attempt = TaskAttempt.lease(
            run_id=run.id,
            worker_id="coord-worker",
            fencing_token=1,
            lease_expires_at=at + timedelta(minutes=5),
        )
        uow.tasks.save(task)
        uow.subtasks.save(subtask)
        uow.runs.save(run)
        uow.attempts.add(attempt)
        uow.commit()
    return created.task.id, target_run.id, attempt.id


def _manual_running_supervisor(uow_factory, task_service, *, budget=None):
    plan = CoordinatedPlan.create(
        (
            SubtaskSpec.create(key="a", objective="A", input={}),
            SubtaskSpec.create(key="b", objective="B", input={}, depends_on=("a",)),
        ),
        max_concurrency=1,
    )
    created = task_service.create_task(
        "Manual supervisor outcome",
        execution_mode=TaskExecutionMode.COORDINATED,
        coordinated_plan=plan,
        budget=budget,
    )
    started = task_service.request_run(created.task.id)
    executor = started.runs[0]
    scheduler = task_service._coordinated_scheduler
    with uow_factory() as uow:
        task = uow.tasks.get(created.task.id, for_update=True)
        run = uow.runs.get(executor.id, for_update=True)
        subtask = uow.subtasks.get(executor.subtask_id, for_update=True)
        assert task is not None and run is not None and subtask is not None
        at = max(task.updated_at, run.queued_at, subtask.updated_at) + timedelta(seconds=1)
        subtask.start(run.id, at=at)
        run.start(at=at)
        attempt = TaskAttempt.lease(
            run_id=run.id,
            worker_id="coord-worker",
            fencing_token=1,
            lease_expires_at=at + timedelta(minutes=5),
        )
        if budget is not None:
            attempt.settle_budget(tokens=1, cost_micros=0, source=BudgetSettlementSource.ACTUAL)
        uow.tasks.save(task)
        uow.subtasks.save(subtask)
        uow.runs.save(run)
        uow.attempts.add(attempt)
        uow.commit()
    with uow_factory() as uow:
        task = uow.tasks.get(created.task.id, for_update=True)
        run = uow.runs.get(executor.id, for_update=True)
        subtask = uow.subtasks.get(executor.subtask_id, for_update=True)
        attempt = uow.attempts.get(attempt.id, for_update=True)
        assert task is not None and run is not None and subtask is not None and attempt is not None
        at = max(task.updated_at, run.started_at, subtask.updated_at, attempt.heartbeat_at)
        at += timedelta(seconds=1)
        planned = scheduler.plan(
            uow, task, completing_subtask_id=subtask.id, completion_output={"executor": True}, at=at
        )
        assert len(planned.planned_runs) == 1
        subtask.complete(run.id, {"executor": True}, at=at)
        run.succeed({"executor": True}, at=at)
        attempt.succeed(at=at)
        uow.subtasks.save(subtask)
        uow.runs.save(run)
        uow.attempts.save(attempt)
        scheduler.apply(uow, task, planned)
        next_executor = planned.planned_runs[0]
        uow.commit()
    with uow_factory() as uow:
        task = uow.tasks.get(created.task.id, for_update=True)
        run = uow.runs.get(next_executor.id, for_update=True)
        subtask = uow.subtasks.get(next_executor.subtask_id, for_update=True)
        assert task is not None and run is not None and subtask is not None
        at = max(task.updated_at, run.queued_at, subtask.updated_at) + timedelta(seconds=1)
        subtask.start(run.id, at=at)
        run.start(at=at)
        attempt = TaskAttempt.lease(
            run_id=run.id,
            worker_id="coord-worker",
            fencing_token=1,
            lease_expires_at=at + timedelta(minutes=5),
        )
        if budget is not None:
            attempt.settle_budget(tokens=1, cost_micros=0, source=BudgetSettlementSource.ACTUAL)
        uow.subtasks.save(subtask)
        uow.runs.save(run)
        uow.attempts.add(attempt)
        uow.commit()
    with uow_factory() as uow:
        task = uow.tasks.get(created.task.id, for_update=True)
        run = uow.runs.get(next_executor.id, for_update=True)
        subtask = uow.subtasks.get(next_executor.subtask_id, for_update=True)
        attempt = uow.attempts.get(attempt.id, for_update=True)
        assert task is not None and run is not None and subtask is not None and attempt is not None
        at = max(task.updated_at, run.started_at, subtask.updated_at, attempt.heartbeat_at)
        at += timedelta(seconds=1)
        planned = scheduler.plan(
            uow, task, completing_subtask_id=subtask.id, completion_output={"executor": True}, at=at
        )
        assert planned.planned_runs[0].role is RunRole.SUPERVISOR
        subtask.complete(run.id, {"executor": True}, at=at)
        run.succeed({"executor": True}, at=at)
        attempt.succeed(at=at)
        uow.subtasks.save(subtask)
        uow.runs.save(run)
        uow.attempts.save(attempt)
        scheduler.apply(uow, task, planned)
        supervisor = planned.planned_runs[0]
        uow.commit()
    with uow_factory() as uow:
        task = uow.tasks.get(created.task.id, for_update=True)
        run = uow.runs.get(supervisor.id, for_update=True)
        assert task is not None and run is not None
        at = max(task.updated_at, run.queued_at) + timedelta(seconds=1)
        run.start(at=at)
        supervisor_attempt = TaskAttempt.lease(
            run_id=run.id,
            worker_id="supervisor-worker",
            fencing_token=1,
            lease_expires_at=at + timedelta(minutes=5),
        )
        if budget is not None:
            supervisor_attempt.settle_budget(
                tokens=1, cost_micros=0, source=BudgetSettlementSource.ACTUAL
            )
        uow.runs.save(run)
        uow.attempts.add(supervisor_attempt)
        uow.commit()
    return created.task.id, supervisor.id, supervisor_attempt.id


def _running_direct(uow_factory, task_service):
    aggregate = task_service.create_task("Apply one result")
    started = task_service.request_run(aggregate.task.id)
    run = started.runs[0]
    with uow_factory() as uow:
        task = uow.tasks.get(aggregate.task.id, for_update=True)
        persisted_run = uow.runs.get(run.id, for_update=True)
        assert task is not None and persisted_run is not None
        at = max(task.updated_at, persisted_run.queued_at) + timedelta(seconds=1)
        task.start(persisted_run.id, at=at)
        persisted_run.start(at=at)
        attempt = TaskAttempt.lease(
            run_id=persisted_run.id,
            worker_id="worker",
            fencing_token=1,
            lease_expires_at=at + timedelta(minutes=5),
        )
        uow.tasks.save(task)
        uow.runs.save(persisted_run)
        uow.attempts.add(attempt)
        uow.commit()
    return aggregate.task.id, run.id, attempt.id


def _manual_running_direct(uow_factory, task_service, *, managed=False, budget=None):
    task = task_service.create_task("Manual outcome", budget=budget).task
    with uow_factory() as uow:
        persisted = uow.tasks.get(task.id, for_update=True)
        assert persisted is not None
        at = utc_now() + timedelta(seconds=2)
        run = TaskRun.request(
            persisted.id,
            "test-agent",
            runtime_authority="managed" if managed else "legacy",
            runtime_version_id=uuid4() if managed else None,
            at=at,
        )
        uow.runs.add(run)
        persisted.queue(run.id, at=at)
        persisted.start(run.id, at=at)
        run.start(at=at)
        attempt = TaskAttempt.lease(
            run_id=run.id,
            worker_id="worker",
            fencing_token=1,
            lease_expires_at=at + timedelta(minutes=5),
        )
        uow.tasks.save(persisted)
        uow.runs.save(run)
        uow.attempts.add(attempt)
        uow.commit()
    return task.id, run.id, attempt.id


def _manual_running_reviewed(
    uow_factory, task_service, *, role=RunRole.EXECUTOR, budget=None, max_revisions=1
):
    criterion = AcceptanceCriterion.create(
        key="quality",
        description="quality",
        kind=AcceptanceCriterionKind.OUTPUT_PATH_EXISTS,
        path=("quality",),
    )
    task = task_service.create_task(
        "Manual reviewed outcome",
        execution_mode=TaskExecutionMode.REVIEWED,
        acceptance_criteria=(criterion,),
        max_revisions=max_revisions,
        budget=budget,
    ).task
    runtime_version_id = uuid4()
    with uow_factory() as uow:
        persisted = uow.tasks.get(task.id, for_update=True)
        assert persisted is not None
        at = utc_now() + timedelta(seconds=2)
        executor = TaskRun.request(
            persisted.id,
            "test-agent",
            runtime_authority="managed",
            runtime_version_id=runtime_version_id,
            at=at,
        )
        uow.runs.add(executor)
        persisted.queue(executor.id, at=at)
        persisted.start(executor.id, at=at)
        executor.start(at=at)
        if role is RunRole.EXECUTOR:
            run = executor
        else:
            executor.succeed({"quality": True}, at=at)
            reviewer = TaskRun.request(
                persisted.id,
                "test-reviewer",
                role=RunRole.REVIEWER,
                runtime_authority="managed",
                runtime_version_id=runtime_version_id,
                at=at,
            )
            persisted.queue_review(executor.id, {"quality": True}, reviewer.id, at=at)
            reviewer.start(at=at)
            uow.runs.add(reviewer)
            run = reviewer
        attempt = TaskAttempt.lease(
            run_id=run.id,
            worker_id="worker",
            fencing_token=1,
            lease_expires_at=at + timedelta(minutes=5),
            reserved_tokens=(budget.token_reservation_per_attempt if budget else 0),
        )
        if budget is not None:
            BudgetController.reserve_attempt(persisted, attempt, at=at)
        uow.tasks.save(persisted)
        uow.runs.save(executor)
        uow.runs.save(run)
        uow.attempts.add(attempt)
        uow.commit()
    return task.id, run.id, attempt.id


class _ReviewedContinuationResolver:
    def create_continuation_in_uow(
        self,
        uow,
        task,
        *,
        agent_id,
        agent_version_id,
        agent_version_digest,
        role,
        revision_number=0,
        parent_run=None,
        **kwargs,
    ):
        assert parent_run is not None
        return TaskRun.request(
            task.id,
            agent_id,
            agent_version_id=agent_version_id,
            agent_version_digest=agent_version_digest,
            role=role,
            revision_number=revision_number,
            runtime_version_id=parent_run.runtime_version_id,
            runtime_authority=parent_run.runtime_authority,
            at=kwargs.get("at"),
        )


def _outcome_entities(uow, ids):
    task_id, run_id, attempt_id = ids
    task = uow.tasks.get(task_id, for_update=True)
    run = uow.runs.get(run_id, for_update=True)
    attempt = uow.attempts.get(attempt_id, for_update=True)
    assert task is not None and run is not None and attempt is not None
    at = max(task.updated_at, run.started_at, attempt.heartbeat_at) + timedelta(seconds=1)
    return task, run, attempt, at


def _settle_for_disposition(task, attempt, disposition):
    if disposition is AccountingDisposition.NOT_APPLICABLE:
        return
    source = (
        BudgetSettlementSource.ACTUAL
        if disposition is AccountingDisposition.SETTLED
        else BudgetSettlementSource.RELEASED
    )
    attempt.settle_budget(tokens=0, cost_micros=0, source=source)


def test_direct_success_owns_business_rows_and_summary(uow_factory, task_service) -> None:
    task_id, run_id, attempt_id = _running_direct(uow_factory, task_service)
    with uow_factory() as uow:
        task = uow.tasks.get(task_id, for_update=True)
        run = uow.runs.get(run_id, for_update=True)
        attempt = uow.attempts.get(attempt_id, for_update=True)
        assert task is not None and run is not None and attempt is not None
        at = max(task.updated_at, run.started_at, attempt.heartbeat_at) + timedelta(seconds=1)
        summary = BusinessOutcomeApplier().apply_known_terminal_in_uow(
            uow,
            task,
            run,
            attempt,
            ProgressionContext.ORDINARY,
            KnownTerminalPhase.SUCCEEDED,
            {"answer": "ok"},
            None,
            None,
            False,
            AccountingDisposition.NOT_APPLICABLE,
            at,
            uuid4(),
        )

        assert summary.task_status is TaskStatus.COMPLETED
        assert summary.run_status is RunStatus.SUCCEEDED
        assert summary.attempt_status is AttemptStatus.SUCCEEDED
        assert summary.task_completed is True
        assert summary.may_capture_completion_memory is True


def test_invalid_accounting_disposition_does_not_mutate_or_save(uow_factory, task_service) -> None:
    task_id, run_id, attempt_id = _running_direct(uow_factory, task_service)
    with uow_factory() as uow:
        task = uow.tasks.get(task_id, for_update=True)
        run = uow.runs.get(run_id, for_update=True)
        attempt = uow.attempts.get(attempt_id, for_update=True)
        assert task is not None and run is not None and attempt is not None
        before = (task.status, run.status, attempt.status, len(uow.outbox._outbox))
        at = max(task.updated_at, run.started_at, attempt.heartbeat_at) + timedelta(seconds=1)
        with pytest.raises(InvalidTaskTransition):
            BusinessOutcomeApplier().apply_known_terminal_in_uow(
                uow,
                task,
                run,
                attempt,
                ProgressionContext.ORDINARY,
                KnownTerminalPhase.SUCCEEDED,
                {"answer": "ok"},
                None,
                None,
                False,
                AccountingDisposition.SETTLED,
                at,
                uuid4(),
            )
        assert (task.status, run.status, attempt.status, len(uow.outbox._outbox)) == before


@pytest.mark.parametrize(
    ("phase", "disposition", "expected_run", "expected_task"),
    [
        (
            KnownTerminalPhase.SUCCEEDED,
            AccountingDisposition.SETTLED,
            RunStatus.SUCCEEDED,
            TaskStatus.COMPLETED,
        ),
        (
            KnownTerminalPhase.FAILED,
            AccountingDisposition.RELEASED,
            RunStatus.FAILED,
            TaskStatus.FAILED,
        ),
        (
            KnownTerminalPhase.CANCELED,
            AccountingDisposition.RELEASED,
            RunStatus.CANCELED,
            TaskStatus.CANCELED,
        ),
        (
            KnownTerminalPhase.TIMED_OUT,
            AccountingDisposition.RELEASED,
            RunStatus.FAILED,
            TaskStatus.FAILED,
        ),
    ],
)
def test_budgeted_direct_terminal_matrix(
    uow_factory,
    task_service,
    phase,
    disposition,
    expected_run,
    expected_task,
) -> None:
    budget = TaskBudget.create(max_tokens=10, token_reservation_per_attempt=1)
    ids = _manual_running_direct(uow_factory, task_service, budget=budget)
    with uow_factory() as uow:
        task, run, attempt, at = _outcome_entities(uow, ids)
        _settle_for_disposition(task, attempt, disposition)
        uow.attempts.save(attempt)
        summary = BusinessOutcomeApplier().apply_known_terminal_in_uow(
            uow,
            task,
            run,
            attempt,
            ProgressionContext.ORDINARY,
            phase,
            {"ok": True} if phase is KnownTerminalPhase.SUCCEEDED else None,
            None if phase is KnownTerminalPhase.SUCCEEDED else "stable.failure",
            None,
            False,
            disposition,
            at,
            uuid4(),
        )
        assert summary.run_status is expected_run
        assert summary.task_status is expected_task


@pytest.mark.parametrize(
    ("phase", "disposition"),
    [
        (KnownTerminalPhase.SUCCEEDED, AccountingDisposition.RELEASED),
        (KnownTerminalPhase.FAILED, AccountingDisposition.SETTLED),
        (KnownTerminalPhase.CANCELED, AccountingDisposition.SETTLED),
        (KnownTerminalPhase.TIMED_OUT, AccountingDisposition.NOT_APPLICABLE),
    ],
)
def test_budgeted_direct_rejects_wrong_accounting_before_mutation(
    uow_factory, task_service, phase, disposition
) -> None:
    budget = TaskBudget.create(max_tokens=10, token_reservation_per_attempt=1)
    ids = _manual_running_direct(uow_factory, task_service, budget=budget)
    with uow_factory() as uow:
        task, run, attempt, at = _outcome_entities(uow, ids)
        before = (task.status, task.version, run.status, attempt.status, len(uow.outbox._outbox))
        with pytest.raises(InvalidTaskTransition):
            BusinessOutcomeApplier().apply_known_terminal_in_uow(
                uow,
                task,
                run,
                attempt,
                ProgressionContext.ORDINARY,
                phase,
                {"ok": True} if phase is KnownTerminalPhase.SUCCEEDED else None,
                None if phase is KnownTerminalPhase.SUCCEEDED else "failure",
                None,
                False,
                disposition,
                at,
                uuid4(),
            )
        assert (
            task.status,
            task.version,
            run.status,
            attempt.status,
            len(uow.outbox._outbox),
        ) == before


def test_budgeted_success_accepts_conservative_settlement_source(uow_factory, task_service) -> None:
    budget = TaskBudget.create(max_tokens=10, token_reservation_per_attempt=1)
    ids = _manual_running_direct(uow_factory, task_service, budget=budget)
    with uow_factory() as uow:
        task, run, attempt, at = _outcome_entities(uow, ids)
        attempt.settle_budget(
            tokens=1,
            cost_micros=0,
            source=BudgetSettlementSource.CONSERVATIVE_ESTIMATE,
        )
        uow.attempts.save(attempt)
        summary = BusinessOutcomeApplier().apply_known_terminal_in_uow(
            uow,
            task,
            run,
            attempt,
            ProgressionContext.ORDINARY,
            KnownTerminalPhase.SUCCEEDED,
            {"ok": True},
            None,
            None,
            False,
            AccountingDisposition.SETTLED,
            at,
            uuid4(),
        )
        assert summary.task_status is TaskStatus.COMPLETED


@pytest.mark.parametrize(
    "source, phase, disposition",
    [
        (
            BudgetSettlementSource.RELEASED,
            KnownTerminalPhase.SUCCEEDED,
            AccountingDisposition.SETTLED,
        ),
        (BudgetSettlementSource.ACTUAL, KnownTerminalPhase.FAILED, AccountingDisposition.RELEASED),
    ],
)
def test_budgeted_direct_rejects_wrong_persisted_source(
    uow_factory, task_service, source, phase, disposition
) -> None:
    budget = TaskBudget.create(max_tokens=10, token_reservation_per_attempt=1)
    ids = _manual_running_direct(uow_factory, task_service, budget=budget)
    with uow_factory() as uow:
        task, run, attempt, at = _outcome_entities(uow, ids)
        attempt.settle_budget(tokens=0, cost_micros=0, source=source)
        uow.attempts.save(attempt)
        before = (task.status, task.version, run.status, attempt.status)
        with pytest.raises(InvalidTaskTransition):
            BusinessOutcomeApplier().apply_known_terminal_in_uow(
                uow,
                task,
                run,
                attempt,
                ProgressionContext.ORDINARY,
                phase,
                {"ok": True} if phase is KnownTerminalPhase.SUCCEEDED else None,
                None if phase is KnownTerminalPhase.SUCCEEDED else "failure",
                None,
                False,
                disposition,
                at,
                uuid4(),
            )
        assert (task.status, task.version, run.status, attempt.status) == before


def test_invalid_activation_and_inputs_have_zero_saves(uow_factory, task_service) -> None:
    ids = _manual_running_direct(uow_factory, task_service)
    with uow_factory() as uow:
        task, run, attempt, at = _outcome_entities(uow, ids)
        saves: list[str] = []
        uow.tasks.save = lambda value: saves.append("task")
        uow.runs.save = lambda value: saves.append("run")
        uow.attempts.save = lambda value: saves.append("attempt")
        outbox_before = len(uow.outbox._outbox)
        before = (task.status, task.version, task.updated_at, run.status, attempt.status)
        with pytest.raises(InvalidTaskInput):
            BusinessOutcomeApplier().apply_known_terminal_in_uow(
                uow,
                task,
                run,
                attempt,
                "BAD",
                KnownTerminalPhase.SUCCEEDED,
                {"ok": True},
                None,
                None,
                False,
                AccountingDisposition.NOT_APPLICABLE,
                at,
                uuid4(),
            )
        assert saves == []
        assert len(uow.outbox._outbox) == outbox_before
        assert (task.status, task.version, task.updated_at, run.status, attempt.status) == before


def _managed_pause(uow_factory, task_service):
    ids = _manual_running_direct(uow_factory, task_service, managed=True)
    with uow_factory() as uow:
        task, run, attempt, at = _outcome_entities(uow, ids)
        task.request_pause(run.id, at=at)
        run.request_pause(at=at)
        uow.tasks.save(task)
        uow.runs.save(run)
        uow.commit()
    return ids


@pytest.mark.parametrize(
    ("phase", "expected_run", "expected_task"),
    [
        (KnownTerminalPhase.SUCCEEDED, RunStatus.SUCCEEDED, TaskStatus.COMPLETED),
        (KnownTerminalPhase.FAILED, RunStatus.FAILED, TaskStatus.FAILED),
        (KnownTerminalPhase.CANCELED, RunStatus.FAILED, TaskStatus.FAILED),
        (KnownTerminalPhase.TIMED_OUT, RunStatus.FAILED, TaskStatus.FAILED),
    ],
)
def test_managed_pause_exact_alignment_matrix(
    uow_factory, task_service, phase, expected_run, expected_task
) -> None:
    ids = _managed_pause(uow_factory, task_service)
    with uow_factory() as uow:
        task, run, attempt, at = _outcome_entities(uow, ids)
        summary = BusinessOutcomeApplier().apply_known_terminal_in_uow(
            uow,
            task,
            run,
            attempt,
            ProgressionContext.ORDINARY,
            phase,
            {"ok": True} if phase is KnownTerminalPhase.SUCCEEDED else None,
            None if phase is KnownTerminalPhase.SUCCEEDED else "pause.failure",
            None,
            False,
            AccountingDisposition.NOT_APPLICABLE,
            at,
            uuid4(),
        )
        assert summary.run_status is expected_run
        assert summary.task_status is expected_task
        persisted_run = uow.runs.get(run.id)
        assert persisted_run is not None
        assert persisted_run.pause_requested_at is None
        assert persisted_run.paused_at is None


def _managed_reconciliation(uow_factory, task_service, *, budget=False):
    policy = TaskBudget.create(max_tokens=10, token_reservation_per_attempt=1) if budget else None
    ids = _manual_running_direct(uow_factory, task_service, managed=True, budget=policy)
    with uow_factory() as uow:
        task, run, attempt, at = _outcome_entities(uow, ids)
        if budget:
            attempt.settle_budget(
                tokens=1,
                cost_micros=0,
                source=BudgetSettlementSource.CONSERVATIVE_ESTIMATE,
            )
        task.require_runtime_reconciliation(run.id, "runtime.lost", at=at)
        run.require_runtime_reconciliation("runtime.lost", at=at)
        attempt.mark_outcome_unknown("runtime.lost", at=at)
        uow.tasks.save(task)
        uow.runs.save(run)
        uow.attempts.save(attempt)
        uow.commit()
    return ids


@pytest.mark.parametrize(
    ("phase", "intent", "expected_action", "expected_reason", "expected_task"),
    [
        (
            KnownTerminalPhase.SUCCEEDED,
            False,
            TaskResolutionAction.RECONCILE_RUNTIME_SUCCEEDED,
            "runtime.confirmed_success",
            TaskStatus.COMPLETED,
        ),
        (
            KnownTerminalPhase.FAILED,
            False,
            TaskResolutionAction.RECONCILE_RUNTIME_FAILED,
            "runtime.reconciled_failed",
            TaskStatus.FAILED,
        ),
        (
            KnownTerminalPhase.TIMED_OUT,
            False,
            TaskResolutionAction.RECONCILE_RUNTIME_TIMED_OUT,
            "runtime.reconciled_timed_out",
            TaskStatus.FAILED,
        ),
        (
            KnownTerminalPhase.CANCELED,
            True,
            TaskResolutionAction.RECONCILE_RUNTIME_CANCELED,
            "runtime.reconciled_canceled",
            TaskStatus.CANCELED,
        ),
        (
            KnownTerminalPhase.CANCELED,
            False,
            TaskResolutionAction.RECONCILE_RUNTIME_CANCELED,
            "runtime.unrequested_cancellation",
            TaskStatus.FAILED,
        ),
    ],
)
def test_direct_reconciliation_action_reason_matrix(
    uow_factory, task_service, phase, intent, expected_action, expected_reason, expected_task
) -> None:
    ids = _managed_reconciliation(uow_factory, task_service)
    with uow_factory() as uow:
        task, run, attempt, at = _outcome_entities(uow, ids)
        summary = BusinessOutcomeApplier().apply_known_terminal_in_uow(
            uow,
            task,
            run,
            attempt,
            ProgressionContext.DIRECT_RECONCILIATION,
            phase,
            {"ok": True} if phase is KnownTerminalPhase.SUCCEEDED else None,
            None if phase is KnownTerminalPhase.SUCCEEDED else "provider text ignored",
            None,
            intent,
            AccountingDisposition.NOT_APPLICABLE,
            at,
            uuid4(),
        )
        assert summary.reconciliation_action is expected_action
        assert summary.reconciliation_reason == expected_reason
        assert summary.task_status is expected_task
        assert summary.task_completed is (expected_task is TaskStatus.COMPLETED)


def test_budget_deadline_reconciliation_keeps_candidate_and_reason(
    uow_factory, task_service
) -> None:
    ids = _managed_reconciliation(uow_factory, task_service, budget=True)
    with uow_factory() as uow:
        task, run, attempt, at = _outcome_entities(uow, ids)
        summary = BusinessOutcomeApplier().apply_known_terminal_in_uow(
            uow,
            task,
            run,
            attempt,
            ProgressionContext.DIRECT_RECONCILIATION,
            KnownTerminalPhase.SUCCEEDED,
            {"ok": True},
            None,
            "budget_deadline_exceeded",
            False,
            AccountingDisposition.ALREADY_CONSERVATIVE,
            at,
            uuid4(),
        )
        assert summary.reconciliation_reason == "budget_deadline_exceeded"
        persisted_task = uow.tasks.get(task.id)
        assert persisted_task is not None
        assert persisted_task.status is TaskStatus.WAITING_APPROVAL
        assert persisted_task.candidate_output == {"ok": True}


@pytest.mark.parametrize(
    "context", [ProgressionContext.ORDINARY, ProgressionContext.DIRECT_RECONCILIATION]
)
def test_invalid_reasons_and_contexts_fail_before_repository_writes(
    uow_factory, task_service, context
) -> None:
    ids = (
        _managed_reconciliation(uow_factory, task_service)
        if context is ProgressionContext.DIRECT_RECONCILIATION
        else _manual_running_direct(uow_factory, task_service)
    )
    with uow_factory() as uow:
        task, run, attempt, at = _outcome_entities(uow, ids)
        before = (task.status, task.version, task.updated_at, run.status, attempt.status)
        saves: list[str] = []
        uow.tasks.save = lambda value: saves.append("task")
        uow.runs.save = lambda value: saves.append("run")
        uow.attempts.save = lambda value: saves.append("attempt")
        with pytest.raises(InvalidTaskInput):
            BusinessOutcomeApplier().apply_known_terminal_in_uow(
                uow,
                task,
                run,
                attempt,
                context,
                KnownTerminalPhase.FAILED,
                None,
                "bad\nreason",
                None,
                False,
                AccountingDisposition.NOT_APPLICABLE,
                at,
                uuid4(),
            )
        assert saves == []
        assert (task.status, task.version, task.updated_at, run.status, attempt.status) == before


def test_coordinated_applier_does_not_commit_or_touch_external_repositories(
    uow_factory, task_service
) -> None:
    task_id, run_id, attempt_id = _manual_running_coordinated(uow_factory, task_service)
    with uow_factory() as uow:
        task = uow.tasks.get(task_id, for_update=True)
        run = uow.runs.get(run_id, for_update=True)
        attempt = uow.attempts.get(attempt_id, for_update=True)
        assert task is not None and run is not None and attempt is not None
        commits: list[bool] = []
        uow.commit = lambda: commits.append(True)

        class Bomb:
            def __getattr__(self, name):
                raise AssertionError(f"unexpected external repository access: {name}")

        for name in ("runtime", "runtimes", "usage", "quotas", "memory", "inbox", "idempotency"):
            setattr(uow, name, Bomb())
        at = max(task.updated_at, run.started_at, attempt.heartbeat_at) + timedelta(seconds=1)
        BusinessOutcomeApplier(
            coordinated_scheduler=task_service._coordinated_scheduler
        ).apply_known_terminal_in_uow(
            uow,
            task,
            run,
            attempt,
            ProgressionContext.ORDINARY,
            KnownTerminalPhase.FAILED,
            None,
            "coordinated.failure",
            None,
            False,
            AccountingDisposition.NOT_APPLICABLE,
            at,
            uuid4(),
        )
        assert commits == []


def test_reconciliation_rejects_unknown_budget_reason_before_mutation(
    uow_factory, task_service
) -> None:
    ids = _managed_reconciliation(uow_factory, task_service)
    with uow_factory() as uow:
        task, run, attempt, at = _outcome_entities(uow, ids)
        before = (task.status, task.version, run.status, attempt.status)
        with pytest.raises(InvalidTaskInput):
            BusinessOutcomeApplier().apply_known_terminal_in_uow(
                uow,
                task,
                run,
                attempt,
                ProgressionContext.DIRECT_RECONCILIATION,
                KnownTerminalPhase.SUCCEEDED,
                {"ok": True},
                None,
                "budget_token_limit_exhausted",
                False,
                AccountingDisposition.ALREADY_CONSERVATIVE,
                at,
                uuid4(),
            )
        assert (task.status, task.version, run.status, attempt.status) == before


def test_summary_requires_closed_reconciliation_details() -> None:
    with pytest.raises(InvalidTaskInput):
        BusinessOutcomeApplication(
            task_id=uuid4(),
            run_id=uuid4(),
            attempt_id=uuid4(),
            task_status=TaskStatus.FAILED,
            run_status=RunStatus.FAILED,
            attempt_status=AttemptStatus.FAILED,
            new_run_ids=(),
            task_completed=False,
            may_capture_completion_memory=False,
            accounting_disposition=AccountingDisposition.NOT_APPLICABLE,
            progression_context=ProgressionContext.DIRECT_RECONCILIATION,
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"reconciliation_action": TaskResolutionAction.RECONCILE_RUNTIME_FAILED},
        {"reconciliation_reason": "runtime.reconciled_failed"},
        {"reconciliation_action": "RECONCILE_RUNTIME_FAILED", "reconciliation_reason": "x"},
    ],
)
def test_summary_rejects_mismatched_action_or_context(kwargs) -> None:
    with pytest.raises(InvalidTaskInput):
        BusinessOutcomeApplication(
            task_id=uuid4(),
            run_id=uuid4(),
            attempt_id=uuid4(),
            task_status=TaskStatus.FAILED,
            run_status=RunStatus.FAILED,
            attempt_status=AttemptStatus.FAILED,
            new_run_ids=(),
            task_completed=False,
            may_capture_completion_memory=False,
            accounting_disposition=AccountingDisposition.NOT_APPLICABLE,
            progression_context=ProgressionContext.ORDINARY,
            **kwargs,
        )


@pytest.mark.parametrize(
    "task_completed, memory_allowed",
    [(True, True), (True, False), (False, True)],
)
def test_summary_rejects_completion_inconsistency(task_completed, memory_allowed) -> None:
    with pytest.raises(InvalidTaskInput):
        BusinessOutcomeApplication(
            task_id=uuid4(),
            run_id=uuid4(),
            attempt_id=uuid4(),
            task_status=TaskStatus.FAILED,
            run_status=RunStatus.FAILED,
            attempt_status=AttemptStatus.FAILED,
            new_run_ids=(),
            task_completed=task_completed,
            may_capture_completion_memory=memory_allowed,
            accounting_disposition=AccountingDisposition.NOT_APPLICABLE,
            progression_context=ProgressionContext.ORDINARY,
        )


@pytest.mark.parametrize(
    "field, value",
    [
        ("progression_context", "ORDINARY"),
        ("accounting_disposition", "NOT_APPLICABLE"),
        ("task_status", "FAILED"),
        ("run_status", "FAILED"),
        ("attempt_status", "FAILED"),
        ("reconciliation_reason", ""),
        ("reconciliation_reason", " padded "),
        ("reconciliation_reason", "bad\nreason"),
    ],
)
def test_summary_constructor_rejects_non_enum_or_unsafe_fields(field, value) -> None:
    base = dict(
        task_id=uuid4(),
        run_id=uuid4(),
        attempt_id=uuid4(),
        task_status=TaskStatus.FAILED,
        run_status=RunStatus.FAILED,
        attempt_status=AttemptStatus.FAILED,
        new_run_ids=(),
        task_completed=False,
        may_capture_completion_memory=False,
        accounting_disposition=AccountingDisposition.NOT_APPLICABLE,
        progression_context=ProgressionContext.ORDINARY,
    )
    base[field] = value
    with pytest.raises(InvalidTaskInput):
        BusinessOutcomeApplication(**base)


def test_applier_does_not_commit_or_touch_external_repositories(uow_factory, task_service) -> None:
    ids = _manual_running_direct(uow_factory, task_service)
    with uow_factory() as uow:
        task, run, attempt, at = _outcome_entities(uow, ids)
        commits: list[bool] = []
        uow.commit = lambda: commits.append(True)

        class Bomb:
            def __getattr__(self, name):
                raise AssertionError(f"unexpected external repository access: {name}")

        for name in ("runtime", "runtimes", "usage", "quotas", "memory", "inbox", "idempotency"):
            setattr(uow, name, Bomb())
        BusinessOutcomeApplier().apply_known_terminal_in_uow(
            uow,
            task,
            run,
            attempt,
            ProgressionContext.ORDINARY,
            KnownTerminalPhase.SUCCEEDED,
            {"ok": True},
            None,
            None,
            False,
            AccountingDisposition.NOT_APPLICABLE,
            at,
            uuid4(),
        )
        assert commits == []


def test_scheduler_continuation_message_preserves_causation_and_policy_clock(
    uow_factory, task_service
) -> None:
    ids = _manual_running_direct(uow_factory, task_service)
    with uow_factory() as uow:
        task = uow.tasks.get(ids[0], for_update=True)
        assert task is not None
        at = task.updated_at + timedelta(seconds=1)
        new_run = TaskRun.request(task.id, "test-agent", at=at)
        causation = uuid4()
        CoordinatedScheduler._persist_run_request(uow, task, new_run, at=at, causation_id=causation)
        envelope = uow.outbox._outbox[-1]
        assert envelope.causation_id == causation
        assert envelope.occurred_at == at
        assert new_run.queued_at == at


def test_reviewed_executor_creates_one_causation_bound_reviewer_run(
    uow_factory, task_service
) -> None:
    criterion = AcceptanceCriterion.create(
        key="quality",
        description="quality",
        kind=AcceptanceCriterionKind.OUTPUT_PATH_EXISTS,
        path=("quality",),
    )
    created = task_service.create_task(
        "Reviewed outcome",
        execution_mode=TaskExecutionMode.REVIEWED,
        acceptance_criteria=(criterion,),
        max_revisions=1,
    )
    started = task_service.request_run(created.task.id)
    parent_id = started.runs[0].id
    with uow_factory() as uow:
        task = uow.tasks.get(created.task.id, for_update=True)
        run = uow.runs.get(parent_id, for_update=True)
        assert task is not None and run is not None
        at = max(task.updated_at, run.queued_at) + timedelta(seconds=1)
        task.start(run.id, at=at)
        run.start(at=at)
        attempt = TaskAttempt.lease(
            run_id=run.id,
            worker_id="worker",
            fencing_token=1,
            lease_expires_at=at + timedelta(minutes=5),
        )
        uow.tasks.save(task)
        uow.runs.save(run)
        uow.attempts.add(attempt)
        uow.commit()
    causation = uuid4()
    with uow_factory() as uow:
        task, run, attempt, at = _outcome_entities(uow, (created.task.id, parent_id, attempt.id))
        summary = BusinessOutcomeApplier(
            reviewer_agent_id="test-reviewer"
        ).apply_known_terminal_in_uow(
            uow,
            task,
            run,
            attempt,
            ProgressionContext.ORDINARY,
            KnownTerminalPhase.SUCCEEDED,
            {"quality": True},
            None,
            None,
            False,
            AccountingDisposition.NOT_APPLICABLE,
            at,
            causation,
        )
        assert summary.new_run_ids and len(summary.new_run_ids) == 1
        reviewer = uow.runs.get(summary.new_run_ids[0])
        assert reviewer is not None and reviewer.queued_at == at
        messages = [
            item for item in uow.outbox._outbox if item.payload["run_id"] == str(reviewer.id)
        ]
        assert len(messages) == 1
        assert messages[0].causation_id == causation
        assert messages[0].occurred_at == at


def test_managed_reviewed_executor_success_queues_reviewer_in_same_cohort(
    uow_factory, task_service
) -> None:
    ids = _manual_running_reviewed(uow_factory, task_service, role=RunRole.EXECUTOR)
    causation = uuid4()
    with uow_factory() as uow:
        task, run, attempt, at = _outcome_entities(uow, ids)
        summary = BusinessOutcomeApplier(
            authority_cohort_resolver=_ReviewedContinuationResolver(),
            reviewer_agent_id="test-reviewer",
        ).apply_known_terminal_in_uow(
            uow,
            task,
            run,
            attempt,
            ProgressionContext.ORDINARY,
            KnownTerminalPhase.SUCCEEDED,
            {"quality": True},
            None,
            None,
            False,
            AccountingDisposition.NOT_APPLICABLE,
            at,
            causation,
        )
        assert summary.task_status is TaskStatus.REVIEWING
        assert summary.new_run_ids and len(summary.new_run_ids) == 1
        reviewer = uow.runs.get(summary.new_run_ids[0])
        assert reviewer is not None
        assert reviewer.runtime_authority == run.runtime_authority == "managed"
        assert reviewer.runtime_version_id == run.runtime_version_id
        assert reviewer.runtime_execution_intent_id != run.runtime_execution_intent_id
        persisted_task = uow.tasks.get(task.id)
        assert persisted_task is not None
        assert persisted_task.candidate_output == {"quality": True}
        messages = [
            item for item in uow.outbox._outbox if item.payload.get("run_id") == str(reviewer.id)
        ]
        assert len(messages) == 1
        assert messages[0].causation_id == causation


def test_managed_reviewed_reviewer_accept_completes_with_candidate(uow_factory, task_service):
    ids = _manual_running_reviewed(uow_factory, task_service, role=RunRole.REVIEWER)
    with uow_factory() as uow:
        task, run, attempt, at = _outcome_entities(uow, ids)
        summary = BusinessOutcomeApplier().apply_known_terminal_in_uow(
            uow,
            task,
            run,
            attempt,
            ProgressionContext.ORDINARY,
            KnownTerminalPhase.SUCCEEDED,
            {"criteria": [{"key": "quality", "passed": True}], "feedback": []},
            None,
            None,
            False,
            AccountingDisposition.NOT_APPLICABLE,
            at,
            uuid4(),
        )
        assert summary.task_status is TaskStatus.COMPLETED
        assert summary.task_completed is True
        assert summary.new_run_ids == ()
        persisted_task = uow.tasks.get(task.id)
        assert persisted_task is not None
        assert persisted_task.output == {"quality": True}
        assert persisted_task.latest_review is not None
        assert persisted_task.latest_review["accepted"] is True


def test_managed_reviewed_reviewer_reject_queues_revision_in_same_cohort(
    uow_factory, task_service
) -> None:
    ids = _manual_running_reviewed(uow_factory, task_service, role=RunRole.REVIEWER)
    with uow_factory() as uow:
        task, run, attempt, at = _outcome_entities(uow, ids)
        summary = BusinessOutcomeApplier(
            authority_cohort_resolver=_ReviewedContinuationResolver(),
            executor_agent_id="test-agent",
        ).apply_known_terminal_in_uow(
            uow,
            task,
            run,
            attempt,
            ProgressionContext.ORDINARY,
            KnownTerminalPhase.SUCCEEDED,
            {"criteria": [{"key": "quality", "passed": False}], "feedback": []},
            None,
            None,
            False,
            AccountingDisposition.NOT_APPLICABLE,
            at,
            uuid4(),
        )
        assert summary.task_status is TaskStatus.READY
        assert summary.new_run_ids and len(summary.new_run_ids) == 1
        revision = uow.runs.get(summary.new_run_ids[0])
        assert revision is not None
        assert revision.role is RunRole.EXECUTOR
        assert revision.revision_number == 1
        assert revision.runtime_authority == run.runtime_authority == "managed"
        assert revision.runtime_version_id == run.runtime_version_id
        assert len(
            [item for item in uow.outbox._outbox if item.payload.get("run_id") == str(revision.id)]
        ) == 1


def test_managed_reviewed_invalid_decision_is_business_failure_with_success_accounting(
    uow_factory, task_service
) -> None:
    budget = TaskBudget.create(
        deadline=utc_now() + timedelta(minutes=5),
        max_tokens=100,
        token_reservation_per_attempt=10,
    )
    ids = _manual_running_reviewed(
        uow_factory, task_service, role=RunRole.REVIEWER, budget=budget
    )
    with uow_factory() as uow:
        task, run, attempt, at = _outcome_entities(uow, ids)
        before_task = deepcopy(task)
        before_attempt = deepcopy(attempt)
        attempt.settle_budget(tokens=0, cost_micros=0, source=BudgetSettlementSource.ACTUAL)
        task.settle_budget(
            reserved_tokens=before_attempt.reserved_tokens,
            reserved_cost_micros=before_attempt.reserved_cost_micros,
            actual_tokens=0,
            actual_cost_micros=0,
            at=at,
        )
        batch = PreparedAccountingBatch.single(
            PreparedAccountingTransition.from_entities(
                before_task,
                before_attempt,
                task,
                attempt,
                run_id=run.id,
                finalized_at=at,
            )
        )
        summary = BusinessOutcomeApplier().apply_known_terminal_in_uow(
            uow,
            task,
            run,
            attempt,
            ProgressionContext.ORDINARY,
            KnownTerminalPhase.SUCCEEDED,
            {},
            None,
            None,
            False,
            AccountingDisposition.SETTLED,
            at,
            uuid4(),
            accounting_batch=batch,
        )
        assert summary.task_status is TaskStatus.FAILED
        assert summary.run_status is RunStatus.FAILED
        assert summary.attempt_status is AttemptStatus.FAILED
        persisted_task = uow.tasks.get(task.id)
        persisted_run = uow.runs.get(run.id)
        persisted_attempt = uow.attempts.get(attempt.id)
        assert persisted_task is not None and persisted_run is not None
        assert persisted_attempt is not None
        assert persisted_task.error == persisted_run.error == "review.invalid_decision"
        assert persisted_attempt.budget_settlement_source is BudgetSettlementSource.ACTUAL
        assert summary.new_run_ids == ()
        assert not uow.outbox._outbox


def test_managed_coordinated_outcome_rejects_before_mutation(uow_factory, task_service) -> None:
    # The activation gate is exercised using a direct managed Run retargeted to
    # the unsupported coordinated mode; no business repository save is permitted.
    mode = TaskExecutionMode.COORDINATED
    task = task_service.create_task("Unsupported managed mode").task
    with uow_factory() as uow:
        persisted = uow.tasks.get(task.id, for_update=True)
        assert persisted is not None
        persisted.execution_mode = mode
        at = utc_now() + timedelta(seconds=2)
        run = TaskRun.request(
            persisted.id,
            "test-agent",
            runtime_authority="managed",
            runtime_version_id=uuid4(),
            at=at,
        )
        uow.runs.add(run)
        persisted.queue(run.id, at=at)
        persisted.start(run.id, at=at)
        run.start(at=at)
        attempt = TaskAttempt.lease(
            run_id=run.id,
            worker_id="worker",
            fencing_token=1,
            lease_expires_at=at + timedelta(minutes=5),
        )
        uow.tasks.save(persisted)
        uow.runs.save(run)
        uow.attempts.add(attempt)
        uow.commit()
    with uow_factory() as uow:
        task = uow.tasks.get(task.id, for_update=True)
        run = uow.runs.get(run.id, for_update=True)
        attempt = uow.attempts.get(attempt.id, for_update=True)
        assert task is not None and run is not None and attempt is not None
        at = max(task.updated_at, run.started_at, attempt.heartbeat_at) + timedelta(seconds=1)
        with pytest.raises(InvalidTaskTransition):
            BusinessOutcomeApplier().apply_known_terminal_in_uow(
                uow,
                task,
                run,
                attempt,
                ProgressionContext.ORDINARY,
                KnownTerminalPhase.SUCCEEDED,
                {"ok": True},
                None,
                None,
                False,
                AccountingDisposition.NOT_APPLICABLE,
                at,
                uuid4(),
            )


@pytest.mark.parametrize(
    "mutation", ["current_run", "role", "subtask", "attempt", "run_terminal", "terminal"]
)
def test_activation_prestate_rejections_are_zero_write(uow_factory, task_service, mutation) -> None:
    ids = _manual_running_direct(uow_factory, task_service)
    with uow_factory() as uow:
        task, run, attempt, at = _outcome_entities(uow, ids)
        if mutation == "current_run":
            task.current_run_id = None
        elif mutation == "role":
            from agentmesh.domain.tasks import RunRole

            run.role = RunRole.REVIEWER
        elif mutation == "subtask":
            run.subtask_id = uuid4()
        elif mutation == "attempt":
            attempt.status = AttemptStatus.SUCCEEDED
        elif mutation == "run_terminal":
            run.status = RunStatus.SUCCEEDED
        else:
            task.status = TaskStatus.COMPLETED
        uow.tasks.save(task)
        uow.runs.save(run)
        uow.attempts.save(attempt)
        uow.commit()
    with uow_factory() as uow:
        task, run, attempt, at = _outcome_entities(uow, ids)
        before = (task.status, task.version, task.updated_at, run.status, attempt.status)
        saves: list[str] = []
        uow.tasks.save = lambda value: saves.append("task")
        uow.runs.save = lambda value: saves.append("run")
        uow.attempts.save = lambda value: saves.append("attempt")
        outbox_before = len(uow.outbox._outbox)
        with pytest.raises(InvalidTaskTransition):
            BusinessOutcomeApplier().apply_known_terminal_in_uow(
                uow,
                task,
                run,
                attempt,
                ProgressionContext.ORDINARY,
                KnownTerminalPhase.SUCCEEDED,
                {"ok": True},
                None,
                None,
                False,
                AccountingDisposition.NOT_APPLICABLE,
                at,
                uuid4(),
            )
        assert saves == []
        assert len(uow.outbox._outbox) == outbox_before
        assert (task.status, task.version, task.updated_at, run.status, attempt.status) == before


def test_latest_attempt_and_owner_fence_mismatch_reject_without_writes(
    uow_factory, task_service
) -> None:
    ids = _manual_running_direct(uow_factory, task_service)
    with uow_factory() as uow:
        task, run, attempt, at = _outcome_entities(uow, ids)
        newer = TaskAttempt.lease(
            run_id=run.id,
            worker_id="other",
            fencing_token=2,
            lease_expires_at=at + timedelta(minutes=5),
        )
        uow.attempts.add(newer)
        before = (task.status, task.version, task.updated_at, run.status, attempt.status)
        saves: list[str] = []
        uow.tasks.save = lambda value: saves.append("task")
        uow.runs.save = lambda value: saves.append("run")
        uow.attempts.save = lambda value: saves.append("attempt")
        outbox_before = len(uow.outbox._outbox)
        with pytest.raises(InvalidTaskTransition):
            BusinessOutcomeApplier().apply_known_terminal_in_uow(
                uow,
                task,
                run,
                attempt,
                ProgressionContext.ORDINARY,
                KnownTerminalPhase.SUCCEEDED,
                {"ok": True},
                None,
                None,
                False,
                AccountingDisposition.NOT_APPLICABLE,
                at,
                uuid4(),
            )
        assert saves == []
        assert len(uow.outbox._outbox) == outbox_before
        assert (task.status, task.version, task.updated_at, run.status, attempt.status) == before


def test_coordinated_executor_success_applies_hypothetical_successor(
    uow_factory, task_service
) -> None:
    plan = CoordinatedPlan.create(
        (
            SubtaskSpec.create(key="a", objective="A", input={}),
            SubtaskSpec.create(key="b", objective="B", input={}, depends_on=("a",)),
        ),
        max_concurrency=1,
    )
    created = task_service.create_task(
        "Coordinated successor", execution_mode=TaskExecutionMode.COORDINATED, coordinated_plan=plan
    )
    started = task_service.request_run(created.task.id)
    target_run = started.runs[0]
    with uow_factory() as uow:
        task = uow.tasks.get(created.task.id, for_update=True)
        run = uow.runs.get(target_run.id, for_update=True)
        subtask = uow.subtasks.get(target_run.subtask_id, for_update=True)
        assert task is not None and run is not None and subtask is not None
        at = max(task.updated_at, run.queued_at, subtask.updated_at) + timedelta(seconds=1)
        subtask.start(run.id, at=at)
        run.start(at=at)
        attempt = TaskAttempt.lease(
            run_id=run.id,
            worker_id="coord-worker",
            fencing_token=1,
            lease_expires_at=at + timedelta(minutes=5),
        )
        uow.tasks.save(task)
        uow.subtasks.save(subtask)
        uow.runs.save(run)
        uow.attempts.add(attempt)
        uow.commit()
    causation_id = uuid4()
    with uow_factory() as uow:
        task = uow.tasks.get(created.task.id, for_update=True)
        run = uow.runs.get(target_run.id, for_update=True)
        attempt = uow.attempts.get(attempt.id, for_update=True)
        assert task is not None and run is not None and attempt is not None
        at = max(task.updated_at, run.started_at, attempt.heartbeat_at) + timedelta(seconds=1)
        summary = BusinessOutcomeApplier(
            coordinated_scheduler=task_service._coordinated_scheduler
        ).apply_known_terminal_in_uow(
            uow,
            task,
            run,
            attempt,
            ProgressionContext.ORDINARY,
            KnownTerminalPhase.SUCCEEDED,
            {"result": "ok"},
            None,
            None,
            False,
            AccountingDisposition.NOT_APPLICABLE,
            at,
            causation_id,
        )
        assert summary.task_status is TaskStatus.RUNNING
        assert summary.run_status is RunStatus.SUCCEEDED
        assert len(summary.new_run_ids) == 1
        successor = uow.runs.get(summary.new_run_ids[0])
        assert successor is not None and successor.queued_at == at
        messages = [
            item for item in uow.outbox._outbox if item.payload["run_id"] == str(successor.id)
        ]
        assert len(messages) == 1
        assert messages[0].causation_id == causation_id
        assert messages[0].occurred_at == at


def test_coordinated_accounting_cas_rejects_forged_final_plan_version(
    uow_factory, task_service
) -> None:
    task_id, run_id, attempt_id = _manual_running_coordinated(
        uow_factory,
        task_service,
        budget=TaskBudget.create(max_tokens=100, token_reservation_per_attempt=1),
        count=2,
    )
    with uow_factory() as uow:
        task = uow.tasks.get(task_id, for_update=True)
        run = uow.runs.get(run_id, for_update=True)
        attempt = uow.attempts.get(attempt_id, for_update=True)
        assert task is not None and run is not None and attempt is not None
        assert run.subtask_id is not None
        subtask = uow.subtasks.get(run.subtask_id, for_update=True)
        assert subtask is not None
        at = max(
            task.updated_at, run.started_at, attempt.heartbeat_at, subtask.updated_at
        ) + timedelta(seconds=1)
        before_task = deepcopy(task)
        before_attempt = deepcopy(attempt)
        BudgetController.settle_attempt(task, attempt, (), at=at)
        batch = PreparedAccountingBatch.single(
            PreparedAccountingTransition.from_entities(
                before_task,
                before_attempt,
                task,
                attempt,
                run_id=run.id,
                finalized_at=at,
            )
        )
        scheduler = task_service._coordinated_scheduler
        before_task = deepcopy(task)
        before_run = deepcopy(run)
        before_attempt = deepcopy(attempt)
        before_subtasks = deepcopy(uow.subtasks.list_for_task(task_id, for_update=True))
        before_runs = deepcopy(uow.runs.list_for_task(task_id))
        before_attempts = deepcopy(uow.attempts.list_for_task(task_id))
        writes: list[str] = []
        uow.tasks.save = lambda value: writes.append("task")
        uow.runs.save = lambda value: writes.append("run")
        uow.subtasks.save = lambda value: writes.append("subtask")
        uow.attempts.save = lambda value: writes.append("attempt")
        uow.outbox.add = lambda value: writes.append("outbox")

        class ForgedPlanScheduler:
            def plan(self, uow, task, **kwargs):
                planned = scheduler.plan(uow, task, **kwargs)
                return replace(planned, task_version=task.version + 1)

            def apply(self, uow, plan):
                return scheduler.apply(uow, plan)

        applier = BusinessOutcomeApplier(coordinated_scheduler=ForgedPlanScheduler())
        with pytest.raises(InvalidTaskTransition, match="schedule plan is stale"):
            applier.apply_known_terminal_in_uow(
                uow,
                task,
                run,
                attempt,
                ProgressionContext.ORDINARY,
                KnownTerminalPhase.SUCCEEDED,
                {"result": "ok"},
                None,
                None,
                False,
                AccountingDisposition.SETTLED,
                at,
                uuid4(),
                accounting_batch=batch,
            )
        assert writes == []
        assert task == before_task
        assert run == before_run
        assert attempt == before_attempt
        assert uow.subtasks.list_for_task(task_id, for_update=True) == before_subtasks
        assert uow.runs.list_for_task(task_id) == before_runs
        assert uow.attempts.list_for_task(task_id) == before_attempts


@pytest.mark.parametrize(
    ("phase", "expected_task", "expected_run"),
    [
        (KnownTerminalPhase.FAILED, TaskStatus.FAILED, RunStatus.FAILED),
        (KnownTerminalPhase.TIMED_OUT, TaskStatus.FAILED, RunStatus.FAILED),
        (KnownTerminalPhase.CANCELED, TaskStatus.CANCELED, RunStatus.CANCELED),
    ],
)
def test_coordinated_executor_stop_converges_siblings(
    uow_factory, task_service, phase, expected_task, expected_run
) -> None:
    task_id, run_id, attempt_id = _manual_running_coordinated(uow_factory, task_service)
    with uow_factory() as uow:
        task = uow.tasks.get(task_id, for_update=True)
        run = uow.runs.get(run_id, for_update=True)
        attempt = uow.attempts.get(attempt_id, for_update=True)
        assert task is not None and run is not None and attempt is not None
        at = max(task.updated_at, run.started_at, attempt.heartbeat_at) + timedelta(seconds=1)
        summary = BusinessOutcomeApplier().apply_known_terminal_in_uow(
            uow,
            task,
            run,
            attempt,
            ProgressionContext.ORDINARY,
            phase,
            None,
            None,
            None,
            False,
            AccountingDisposition.NOT_APPLICABLE,
            at,
            uuid4(),
        )
        assert summary.task_status is expected_task
        assert summary.run_status is expected_run
        assert summary.new_run_ids == ()
        all_subtasks = uow.subtasks.list_for_task(task_id)
        all_runs = uow.runs.list_for_task(task_id)
        assert all(
            subtask.status
            in {SubtaskStatus.COMPLETED, SubtaskStatus.FAILED, SubtaskStatus.CANCELED}
            for subtask in all_subtasks
        )
        assert all(
            run.status in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELED}
            for run in all_runs
        )


def test_coordinated_executor_budget_rejection_holds_without_candidate(
    uow_factory, task_service
) -> None:
    budget = TaskBudget.create(max_tokens=10, token_reservation_per_attempt=1)
    task_id, run_id, attempt_id = _manual_running_coordinated(
        uow_factory, task_service, budget=budget
    )
    with uow_factory() as uow:
        task = uow.tasks.get(task_id, for_update=True)
        run = uow.runs.get(run_id, for_update=True)
        attempt = uow.attempts.get(attempt_id, for_update=True)
        assert task is not None and run is not None and attempt is not None
        attempt.settle_budget(tokens=1, cost_micros=0, source=BudgetSettlementSource.ACTUAL)
        uow.attempts.save(attempt)
        at = max(task.updated_at, run.started_at, attempt.heartbeat_at) + timedelta(seconds=1)
        summary = BusinessOutcomeApplier().apply_known_terminal_in_uow(
            uow,
            task,
            run,
            attempt,
            ProgressionContext.ORDINARY,
            KnownTerminalPhase.SUCCEEDED,
            {"result": "ok"},
            None,
            "budget_deadline_exceeded",
            False,
            AccountingDisposition.SETTLED,
            at,
            uuid4(),
        )
        assert summary.task_status is TaskStatus.WAITING_APPROVAL
        assert task.candidate_output is None
        assert summary.new_run_ids == ()


@pytest.mark.parametrize(
    "reason",
    [
        "budget_deadline_exceeded",
        "budget_token_limit_exhausted",
        "budget_cost_limit_exhausted",
        "budget_run_limit_exhausted",
    ],
)
def test_ordinary_budget_rejection_reason_set_is_accepted(
    uow_factory, task_service, reason
) -> None:
    """The ordinary path accepts every stable admission rejection reason."""
    budget = TaskBudget.create(max_tokens=10, token_reservation_per_attempt=1)
    ids = _manual_running_direct(uow_factory, task_service, budget=budget)
    with uow_factory() as uow:
        task, run, attempt, at = _outcome_entities(uow, ids)
        attempt.settle_budget(tokens=1, cost_micros=0, source=BudgetSettlementSource.ACTUAL)
        uow.attempts.save(attempt)
        summary = BusinessOutcomeApplier().apply_known_terminal_in_uow(
            uow,
            task,
            run,
            attempt,
            ProgressionContext.ORDINARY,
            KnownTerminalPhase.SUCCEEDED,
            {"ok": True},
            None,
            reason,
            False,
            AccountingDisposition.SETTLED,
            at,
            uuid4(),
        )
        assert summary.task_status is TaskStatus.WAITING_APPROVAL


def test_coordinated_budget_run_limit_rejection_is_accepted(uow_factory, task_service) -> None:
    budget = TaskBudget.create(max_tokens=10, token_reservation_per_attempt=1)
    task_id, run_id, attempt_id = _manual_running_coordinated(
        uow_factory, task_service, budget=budget
    )
    with uow_factory() as uow:
        task = uow.tasks.get(task_id, for_update=True)
        run = uow.runs.get(run_id, for_update=True)
        attempt = uow.attempts.get(attempt_id, for_update=True)
        assert task is not None and run is not None and attempt is not None
        attempt.settle_budget(tokens=1, cost_micros=0, source=BudgetSettlementSource.ACTUAL)
        uow.attempts.save(attempt)
        at = max(task.updated_at, run.started_at, attempt.heartbeat_at) + timedelta(seconds=1)
        summary = BusinessOutcomeApplier().apply_known_terminal_in_uow(
            uow,
            task,
            run,
            attempt,
            ProgressionContext.ORDINARY,
            KnownTerminalPhase.SUCCEEDED,
            {"result": "ok"},
            None,
            "budget_run_limit_exhausted",
            False,
            AccountingDisposition.SETTLED,
            at,
            uuid4(),
        )
        assert summary.task_status is TaskStatus.WAITING_APPROVAL


def test_reviewed_budget_cost_rejection_is_accepted(uow_factory, task_service) -> None:
    budget = TaskBudget.create(max_tokens=10, token_reservation_per_attempt=1)
    criterion = AcceptanceCriterion.create(
        key="quality",
        description="quality",
        kind=AcceptanceCriterionKind.OUTPUT_PATH_EXISTS,
        path=("quality",),
    )
    created = task_service.create_task(
        "Reviewed budget outcome",
        execution_mode=TaskExecutionMode.REVIEWED,
        acceptance_criteria=(criterion,),
        budget=budget,
    )
    started = task_service.request_run(created.task.id)
    run_id = started.runs[0].id
    with uow_factory() as uow:
        task = uow.tasks.get(created.task.id, for_update=True)
        run = uow.runs.get(run_id, for_update=True)
        assert task is not None and run is not None
        at = max(task.updated_at, run.queued_at) + timedelta(seconds=1)
        task.start(run.id, at=at)
        run.start(at=at)
        attempt = TaskAttempt.lease(
            run_id=run.id,
            worker_id="review-worker",
            fencing_token=1,
            lease_expires_at=at + timedelta(minutes=5),
        )
        attempt.settle_budget(tokens=1, cost_micros=2, source=BudgetSettlementSource.ACTUAL)
        uow.tasks.save(task)
        uow.runs.save(run)
        uow.attempts.add(attempt)
        uow.commit()
    with uow_factory() as uow:
        task, run, attempt, at = _outcome_entities(uow, (created.task.id, run_id, attempt.id))
        summary = BusinessOutcomeApplier().apply_known_terminal_in_uow(
            uow,
            task,
            run,
            attempt,
            ProgressionContext.ORDINARY,
            KnownTerminalPhase.SUCCEEDED,
            {"quality": True},
            None,
            "budget_cost_limit_exhausted",
            False,
            AccountingDisposition.SETTLED,
            at,
            uuid4(),
        )
        assert summary.task_status is TaskStatus.WAITING_APPROVAL
        persisted = uow.tasks.get(task.id)
        assert persisted is not None and persisted.candidate_output == {"quality": True}


def test_prepared_accounting_transition_is_applied_without_caller_business_saves(
    uow_factory, task_service
) -> None:
    budget = TaskBudget.create(max_tokens=10, token_reservation_per_attempt=1)
    ids = _manual_running_direct(uow_factory, task_service, budget=budget)
    with uow_factory() as uow:
        task, run, attempt, at = _outcome_entities(uow, ids)
        before_task = deepcopy(task)
        before_attempt = deepcopy(attempt)
        attempt.settle_budget(tokens=1, cost_micros=0, source=BudgetSettlementSource.ACTUAL)
        task.settle_budget(
            reserved_tokens=0,
            reserved_cost_micros=0,
            actual_tokens=1,
            actual_cost_micros=0,
            at=at,
        )
        transition = PreparedAccountingTransition.from_entities(
            before_task,
            before_attempt,
            task,
            attempt,
            run_id=run.id,
            finalized_at=at,
        )
        summary = BusinessOutcomeApplier().apply_known_terminal_in_uow(
            uow,
            task,
            run,
            attempt,
            ProgressionContext.ORDINARY,
            KnownTerminalPhase.SUCCEEDED,
            {"ok": True},
            None,
            None,
            False,
            AccountingDisposition.SETTLED,
            at,
            uuid4(),
            accounting_transition=transition,
        )
        assert summary.task_status is TaskStatus.COMPLETED


@pytest.mark.parametrize(
    ("disposition", "phase", "expected_task"),
    [
        (AccountingDisposition.SETTLED, KnownTerminalPhase.SUCCEEDED, TaskStatus.COMPLETED),
        (AccountingDisposition.RELEASED, KnownTerminalPhase.FAILED, TaskStatus.FAILED),
    ],
)
def test_prepared_accounting_transition_supports_nonzero_reservation(
    uow_factory, task_service, disposition, phase, expected_task
) -> None:
    budget = TaskBudget.create(max_tokens=10, token_reservation_per_attempt=2)
    ids = _manual_running_direct(uow_factory, task_service, budget=budget)
    with uow_factory() as uow:
        task, run, attempt, at = _outcome_entities(uow, ids)
        attempt.reserved_tokens = 2
        task.reserve_budget(tokens=2, cost_micros=0, at=at)
        uow.tasks.save(task)
        uow.attempts.save(attempt)
        uow.commit()
    with uow_factory() as uow:
        task, run, attempt, at = _outcome_entities(uow, ids)
        before_task = deepcopy(task)
        before_attempt = deepcopy(attempt)
        if disposition is AccountingDisposition.SETTLED:
            attempt.settle_budget(tokens=2, cost_micros=0, source=BudgetSettlementSource.ACTUAL)
            task.settle_budget(
                reserved_tokens=2,
                reserved_cost_micros=0,
                actual_tokens=2,
                actual_cost_micros=0,
                at=at,
            )
        else:
            attempt.settle_budget(tokens=0, cost_micros=0, source=BudgetSettlementSource.RELEASED)
            task.settle_budget(
                reserved_tokens=2,
                reserved_cost_micros=0,
                actual_tokens=0,
                actual_cost_micros=0,
                at=at,
            )
        transition = PreparedAccountingTransition.from_entities(
            before_task,
            before_attempt,
            task,
            attempt,
            run_id=run.id,
            finalized_at=at,
        )
        summary = BusinessOutcomeApplier().apply_known_terminal_in_uow(
            uow,
            task,
            run,
            attempt,
            ProgressionContext.ORDINARY,
            phase,
            {"ok": True} if phase is KnownTerminalPhase.SUCCEEDED else None,
            None if phase is KnownTerminalPhase.SUCCEEDED else "failure",
            None,
            False,
            disposition,
            at,
            uuid4(),
            accounting_transition=transition,
        )
        assert summary.task_status is expected_task


@pytest.mark.parametrize(
    "field",
    [
        "after_task_settled_tokens",
        "after_task_reserved_tokens",
        "after_attempt_source",
        "after_attempt_settled_tokens",
        "attempt_reserved_tokens",
        "after_task_version",
        "finalized_at",
        "task_id",
    ],
)
def test_forged_prepared_accounting_transition_rejects_before_business_writes(
    uow_factory, task_service, field
) -> None:
    budget = TaskBudget.create(max_tokens=10, token_reservation_per_attempt=1)
    ids = _manual_running_direct(uow_factory, task_service, budget=budget)
    with uow_factory() as uow:
        task, run, attempt, at = _outcome_entities(uow, ids)
        before_task = deepcopy(task)
        before_attempt = deepcopy(attempt)
        attempt.settle_budget(tokens=1, cost_micros=0, source=BudgetSettlementSource.ACTUAL)
        task.settle_budget(
            reserved_tokens=0,
            reserved_cost_micros=0,
            actual_tokens=1,
            actual_cost_micros=0,
            at=at,
        )
        transition = PreparedAccountingTransition.from_entities(
            before_task,
            before_attempt,
            task,
            attempt,
            run_id=run.id,
            finalized_at=at,
        )
        if field == "after_task_settled_tokens":
            transition = replace(transition, after_task_settled_tokens=2)
        elif field == "after_task_reserved_tokens":
            transition = replace(transition, after_task_reserved_tokens=1)
        elif field == "after_attempt_source":
            transition = replace(transition, after_attempt_source=BudgetSettlementSource.RELEASED)
        elif field == "after_attempt_settled_tokens":
            transition = replace(transition, after_attempt_settled_tokens=None)
        elif field == "attempt_reserved_tokens":
            transition = replace(transition, attempt_reserved_tokens=1)
        elif field == "after_task_version":
            transition = replace(transition, after_task_version=transition.after_task_version + 1)
        elif field == "finalized_at":
            transition = replace(
                transition,
                finalized_at=transition.before_task_updated_at - timedelta(seconds=1),
            )
        else:
            transition = replace(transition, task_id=uuid4())
        before = (task.status, task.version, task.updated_at, run.status, attempt.status)
        saves: list[str] = []
        uow.tasks.save = lambda value: saves.append("task")
        uow.runs.save = lambda value: saves.append("run")
        uow.attempts.save = lambda value: saves.append("attempt")
        with pytest.raises(InvalidTaskTransition):
            BusinessOutcomeApplier().apply_known_terminal_in_uow(
                uow,
                task,
                run,
                attempt,
                ProgressionContext.ORDINARY,
                KnownTerminalPhase.SUCCEEDED,
                {"ok": True},
                None,
                None,
                False,
                AccountingDisposition.SETTLED,
                at,
                uuid4(),
                accounting_transition=transition,
            )
        assert saves == []
        assert (task.status, task.version, task.updated_at, run.status, attempt.status) == before


def test_prepared_accounting_transition_rejects_after_entity_identity_mismatch(
    uow_factory, task_service
) -> None:
    budget = TaskBudget.create(max_tokens=10, token_reservation_per_attempt=1)
    ids = _manual_running_direct(uow_factory, task_service, budget=budget)
    with uow_factory() as uow:
        task, _run, attempt, at = _outcome_entities(uow, ids)
        with pytest.raises(InvalidTaskInput):
            PreparedAccountingTransition.from_entities(
                deepcopy(task),
                deepcopy(attempt),
                replace(deepcopy(task), id=uuid4()),
                deepcopy(attempt),
                run_id=attempt.run_id,
                finalized_at=at,
            )


@pytest.mark.parametrize("terminalizer", ["fail", "expire", "unknown"])
def test_coordinated_active_sibling_with_nonactive_attempt_rejects_zero_write(
    uow_factory, task_service, terminalizer
) -> None:
    """A non-queued sibling cannot be canceled when its latest attempt is terminal."""
    task_id, target_run_id, target_attempt_id = _manual_running_coordinated(
        uow_factory, task_service
    )
    with uow_factory() as uow:
        task = uow.tasks.get(task_id, for_update=True)
        target_run = uow.runs.get(target_run_id, for_update=True)
        target_attempt = uow.attempts.get(target_attempt_id, for_update=True)
        assert task is not None and target_run is not None and target_attempt is not None
        sibling = next(
            candidate
            for candidate in uow.runs.list_for_task(task_id)
            if candidate.id != target_run_id
        )
        sibling_subtask = uow.subtasks.get(sibling.subtask_id, for_update=True)
        assert sibling_subtask is not None
        base = max(task.updated_at, sibling.queued_at, sibling_subtask.updated_at)
        sibling.start(at=base + timedelta(seconds=1))
        sibling_attempt = TaskAttempt.lease(
            run_id=sibling.id,
            worker_id="sibling-worker",
            fencing_token=1,
            lease_expires_at=base + timedelta(minutes=5),
        )
        terminal_at = base + timedelta(seconds=2)
        if terminalizer == "fail":
            sibling_attempt.fail("sibling.failed", at=terminal_at)
        elif terminalizer == "expire":
            sibling_attempt.expire(at=terminal_at)
        else:
            sibling_attempt.mark_outcome_unknown("sibling.lost", at=terminal_at)
        uow.runs.save(sibling)
        uow.attempts.add(sibling_attempt)
        uow.commit()

    with uow_factory() as uow:
        task = uow.tasks.get(task_id, for_update=True)
        target_run = uow.runs.get(target_run_id, for_update=True)
        target_attempt = uow.attempts.get(target_attempt_id, for_update=True)
        assert task is not None and target_run is not None and target_attempt is not None
        all_runs = uow.runs.list_for_task(task_id)
        all_subtasks = uow.subtasks.list_for_task(task_id)
        all_attempts = [
            uow.attempts.latest_for_run(candidate.id, for_update=True) for candidate in all_runs
        ]
        before = (
            (task.status, task.version, task.updated_at),
            tuple(
                (item.id, item.status, item.queued_at, item.started_at, item.completed_at)
                for item in all_runs
            ),
            tuple((item.id, item.status, item.version, item.updated_at) for item in all_subtasks),
            tuple(
                (item.id, item.status, item.completed_at)
                for item in all_attempts
                if item is not None
            ),
        )
        saves: list[str] = []
        uow.tasks.save = lambda value: saves.append("task")
        uow.runs.save = lambda value: saves.append("run")
        uow.attempts.save = lambda value: saves.append("attempt")
        uow.subtasks.save = lambda value: saves.append("subtask")
        at = max(task.updated_at, target_run.started_at, target_attempt.heartbeat_at) + timedelta(
            seconds=1
        )
        with pytest.raises(InvalidTaskTransition, match="Attempt is not active"):
            BusinessOutcomeApplier().apply_known_terminal_in_uow(
                uow,
                task,
                target_run,
                target_attempt,
                ProgressionContext.ORDINARY,
                KnownTerminalPhase.FAILED,
                None,
                "target.failed",
                None,
                False,
                AccountingDisposition.NOT_APPLICABLE,
                at,
                uuid4(),
            )
        after = (
            (task.status, task.version, task.updated_at),
            tuple(
                (item.id, item.status, item.queued_at, item.started_at, item.completed_at)
                for item in all_runs
            ),
            tuple((item.id, item.status, item.version, item.updated_at) for item in all_subtasks),
            tuple(
                (item.id, item.status, item.completed_at)
                for item in all_attempts
                if item is not None
            ),
        )
        assert saves == []
        assert after == before


@pytest.mark.parametrize(
    ("phase", "budget_rejection", "expected_task"),
    [
        (KnownTerminalPhase.SUCCEEDED, None, TaskStatus.COMPLETED),
        (KnownTerminalPhase.SUCCEEDED, "budget_deadline_exceeded", TaskStatus.WAITING_APPROVAL),
        (KnownTerminalPhase.FAILED, None, TaskStatus.FAILED),
        (KnownTerminalPhase.TIMED_OUT, None, TaskStatus.FAILED),
        (KnownTerminalPhase.CANCELED, None, TaskStatus.CANCELED),
    ],
)
def test_coordinated_supervisor_terminal_matrix(
    uow_factory, task_service, phase, budget_rejection, expected_task
) -> None:
    budget = (
        TaskBudget.create(max_tokens=10, token_reservation_per_attempt=1)
        if budget_rejection is not None
        else None
    )
    task_id, run_id, attempt_id = _manual_running_supervisor(
        uow_factory, task_service, budget=budget
    )
    with uow_factory() as uow:
        task = uow.tasks.get(task_id, for_update=True)
        run = uow.runs.get(run_id, for_update=True)
        attempt = uow.attempts.get(attempt_id, for_update=True)
        assert task is not None and run is not None and attempt is not None
        at = max(task.updated_at, run.started_at, attempt.heartbeat_at) + timedelta(seconds=1)
        summary = BusinessOutcomeApplier().apply_known_terminal_in_uow(
            uow,
            task,
            run,
            attempt,
            ProgressionContext.ORDINARY,
            phase,
            {"final": True} if phase is KnownTerminalPhase.SUCCEEDED else None,
            None if phase is KnownTerminalPhase.SUCCEEDED else "supervisor.failure",
            budget_rejection,
            False,
            AccountingDisposition.SETTLED
            if budget is not None
            else AccountingDisposition.NOT_APPLICABLE,
            at,
            uuid4(),
        )
        assert summary.task_status is expected_task
        persisted = uow.tasks.get(task_id)
        assert persisted is not None
        if budget_rejection is not None:
            assert persisted.candidate_output == {"final": True}
        else:
            assert persisted.candidate_output is None


def test_coordinated_sibling_settlement_mismatch_is_zero_write(uow_factory, task_service) -> None:
    budget = TaskBudget.create(max_tokens=10, token_reservation_per_attempt=1)
    task_id, run_id, attempt_id = _manual_running_coordinated(
        uow_factory, task_service, budget=budget
    )
    with uow_factory() as uow:
        sibling = next(
            candidate for candidate in uow.runs.list_for_task(task_id) if candidate.id != run_id
        )
        sibling_subtask = uow.subtasks.get(sibling.subtask_id, for_update=True)
        task = uow.tasks.get(task_id, for_update=True)
        assert sibling_subtask is not None and task is not None
        at = max(task.updated_at, sibling.queued_at, sibling_subtask.updated_at) + timedelta(
            seconds=1
        )
        sibling_subtask.start(sibling.id, at=at)
        sibling.start(at=at)
        sibling_attempt = TaskAttempt.lease(
            run_id=sibling.id,
            worker_id="sibling-worker",
            fencing_token=1,
            lease_expires_at=at + timedelta(minutes=5),
        )
        target_attempt = uow.attempts.get(attempt_id, for_update=True)
        assert target_attempt is not None
        target_attempt.settle_budget(
            tokens=0, cost_micros=0, source=BudgetSettlementSource.RELEASED
        )
        uow.subtasks.save(sibling_subtask)
        uow.runs.save(sibling)
        uow.attempts.save(target_attempt)
        uow.attempts.add(sibling_attempt)
        uow.commit()
    with uow_factory() as uow:
        task = uow.tasks.get(task_id, for_update=True)
        run = uow.runs.get(run_id, for_update=True)
        attempt = uow.attempts.get(attempt_id, for_update=True)
        assert task is not None and run is not None and attempt is not None
        before = (task.status, task.version, run.status, attempt.status, len(uow.outbox._outbox))
        at = max(task.updated_at, run.started_at, attempt.heartbeat_at) + timedelta(seconds=1)
        with pytest.raises(InvalidTaskTransition, match="accounting"):
            BusinessOutcomeApplier().apply_known_terminal_in_uow(
                uow,
                task,
                run,
                attempt,
                ProgressionContext.ORDINARY,
                KnownTerminalPhase.FAILED,
                None,
                "failure",
                None,
                False,
                AccountingDisposition.RELEASED,
                at,
                uuid4(),
            )
        assert (
            task.status,
            task.version,
            run.status,
            attempt.status,
            len(uow.outbox._outbox),
        ) == before


def test_coordinated_success_without_scheduler_fails_before_mutation(
    uow_factory, task_service
) -> None:
    task_id, run_id, attempt_id = _manual_running_coordinated(uow_factory, task_service)
    with uow_factory() as uow:
        task = uow.tasks.get(task_id, for_update=True)
        run = uow.runs.get(run_id, for_update=True)
        attempt = uow.attempts.get(attempt_id, for_update=True)
        assert task is not None and run is not None and attempt is not None
        before = (task.status, task.version, run.status, attempt.status, len(uow.outbox._outbox))
        at = max(task.updated_at, run.started_at, attempt.heartbeat_at) + timedelta(seconds=1)
        with pytest.raises(InvalidTaskTransition, match="scheduler"):
            BusinessOutcomeApplier().apply_known_terminal_in_uow(
                uow,
                task,
                run,
                attempt,
                ProgressionContext.ORDINARY,
                KnownTerminalPhase.SUCCEEDED,
                {"result": "ok"},
                None,
                None,
                False,
                AccountingDisposition.NOT_APPLICABLE,
                at,
                uuid4(),
            )
        assert (
            task.status,
            task.version,
            run.status,
            attempt.status,
            len(uow.outbox._outbox),
        ) == before


def test_forged_supplied_run_authority_cannot_authorize_cancel_intent(
    uow_factory, task_service
) -> None:
    ids = _manual_running_direct(uow_factory, task_service)
    with uow_factory() as uow:
        task, persisted_run, attempt, at = _outcome_entities(uow, ids)
        supplied_run = TaskRun(**persisted_run.__dict__)
        supplied_run.runtime_authority = "managed"
        supplied_run.runtime_version_id = uuid4()
        before = (task.status, task.version, task.updated_at, persisted_run.status, attempt.status)
        outbox_before = len(uow.outbox._outbox)
        saves: list[str] = []
        uow.tasks.save = lambda value: saves.append("task")
        uow.runs.save = lambda value: saves.append("run")
        uow.attempts.save = lambda value: saves.append("attempt")
        with pytest.raises(InvalidTaskTransition):
            BusinessOutcomeApplier().apply_known_terminal_in_uow(
                uow,
                task,
                supplied_run,
                attempt,
                ProgressionContext.ORDINARY,
                KnownTerminalPhase.CANCELED,
                None,
                None,
                None,
                True,
                AccountingDisposition.NOT_APPLICABLE,
                at,
                uuid4(),
            )
        assert saves == []
        assert len(uow.outbox._outbox) == outbox_before
        assert (
            task.status,
            task.version,
            task.updated_at,
            persisted_run.status,
            attempt.status,
        ) == before

    with uow_factory() as uow:
        task, run, attempt, at = _outcome_entities(uow, ids)
        supplied = TaskAttempt(**attempt.__dict__)
        supplied.worker_id = "forged-worker"
        before = (task.status, task.version, task.updated_at, run.status, attempt.status)
        saves: list[str] = []
        uow.tasks.save = lambda value: saves.append("task")
        uow.runs.save = lambda value: saves.append("run")
        uow.attempts.save = lambda value: saves.append("attempt")
        outbox_before = len(uow.outbox._outbox)
        with pytest.raises(InvalidTaskTransition):
            BusinessOutcomeApplier().apply_known_terminal_in_uow(
                uow,
                task,
                run,
                supplied,
                ProgressionContext.ORDINARY,
                KnownTerminalPhase.SUCCEEDED,
                {"ok": True},
                None,
                None,
                False,
                AccountingDisposition.NOT_APPLICABLE,
                at,
                uuid4(),
            )
        assert saves == []
        assert len(uow.outbox._outbox) == outbox_before
        assert (task.status, task.version, task.updated_at, run.status, attempt.status) == before


def test_managed_pause_requested_cancel_with_intent_is_cancelled(uow_factory, task_service) -> None:
    ids = _managed_pause(uow_factory, task_service)
    with uow_factory() as uow:
        task, run, attempt, at = _outcome_entities(uow, ids)
        summary = BusinessOutcomeApplier().apply_known_terminal_in_uow(
            uow,
            task,
            run,
            attempt,
            ProgressionContext.ORDINARY,
            KnownTerminalPhase.CANCELED,
            None,
            "provider ignored",
            None,
            True,
            AccountingDisposition.NOT_APPLICABLE,
            at,
            uuid4(),
        )
        assert summary.task_status is TaskStatus.CANCELED
        assert summary.run_status is RunStatus.CANCELED


def test_legacy_pause_request_is_rejected_before_mutation(uow_factory, task_service) -> None:
    ids = _manual_running_direct(uow_factory, task_service)
    with uow_factory() as uow:
        task, run, attempt, at = _outcome_entities(uow, ids)
        task.request_pause(run.id, at=at)
        run.request_pause(at=at)
        uow.tasks.save(task)
        uow.runs.save(run)
        uow.commit()
    with uow_factory() as uow:
        task, run, attempt, at = _outcome_entities(uow, ids)
        before = (task.status, task.version, task.updated_at, run.status, attempt.status)
        saves: list[str] = []
        uow.tasks.save = lambda value: saves.append("task")
        uow.runs.save = lambda value: saves.append("run")
        uow.attempts.save = lambda value: saves.append("attempt")
        with pytest.raises(InvalidTaskTransition):
            BusinessOutcomeApplier().apply_known_terminal_in_uow(
                uow,
                task,
                run,
                attempt,
                ProgressionContext.ORDINARY,
                KnownTerminalPhase.SUCCEEDED,
                {"ok": True},
                None,
                None,
                False,
                AccountingDisposition.NOT_APPLICABLE,
                at,
                uuid4(),
            )
        assert saves == []
        assert (task.status, task.version, task.updated_at, run.status, attempt.status) == before


def _make_two_step_accounting_batch(
    task, target_attempt, sibling_attempt, target_run_id, at, *, target_settled=True
) -> PreparedAccountingBatch:
    """Build a target + released sibling proof with a chained Task state."""
    before_task = deepcopy(task)
    before_target = deepcopy(target_attempt)
    before_sibling = deepcopy(sibling_attempt)

    after_target = deepcopy(before_target)
    target_source = (
        BudgetSettlementSource.ACTUAL if target_settled else BudgetSettlementSource.RELEASED
    )
    target_tokens = 1 if target_settled else 0
    after_target.settle_budget(tokens=target_tokens, cost_micros=0, source=target_source)
    after_task = deepcopy(before_task)
    after_task.settle_budget(
        reserved_tokens=1,
        reserved_cost_micros=0,
        actual_tokens=target_tokens,
        actual_cost_micros=0,
        at=at,
    )
    target_transition = PreparedAccountingTransition.from_entities(
        before_task,
        before_target,
        after_task,
        after_target,
        run_id=target_run_id,
        finalized_at=at,
    )

    after_sibling = deepcopy(before_sibling)
    after_sibling.settle_budget(tokens=0, cost_micros=0, source=BudgetSettlementSource.RELEASED)
    final_task = deepcopy(after_task)
    final_task.settle_budget(
        reserved_tokens=1,
        reserved_cost_micros=0,
        actual_tokens=0,
        actual_cost_micros=0,
        at=at,
    )
    sibling_transition = PreparedAccountingTransition.from_entities(
        after_task,
        before_sibling,
        final_task,
        after_sibling,
        run_id=sibling_attempt.run_id,
        finalized_at=at,
    )
    return PreparedAccountingBatch((target_transition, sibling_transition))


def _make_released_accounting_batch(task, attempts, target_run_id, at):
    """Build a chained RELEASED proof for a target and all active siblings."""
    current_task = deepcopy(task)
    transitions = []
    for attempt in attempts:
        before_task = deepcopy(current_task)
        before_attempt = deepcopy(attempt)
        after_attempt = deepcopy(before_attempt)
        after_attempt.settle_budget(tokens=0, cost_micros=0, source=BudgetSettlementSource.RELEASED)
        current_task = deepcopy(before_task)
        current_task.settle_budget(
            reserved_tokens=attempt.reserved_tokens,
            reserved_cost_micros=attempt.reserved_cost_micros,
            actual_tokens=0,
            actual_cost_micros=0,
            at=at,
        )
        transitions.append(
            PreparedAccountingTransition.from_entities(
                before_task,
                before_attempt,
                current_task,
                after_attempt,
                run_id=attempt.run_id,
                finalized_at=at,
            )
        )
    assert transitions[0].run_id == target_run_id
    return PreparedAccountingBatch(tuple(transitions))


def test_prepared_accounting_batch_applies_target_and_sibling_chain_before_business_mutation(
    uow_factory, task_service
) -> None:
    budget = TaskBudget.create(max_tokens=10, token_reservation_per_attempt=1)
    task_id, target_run_id, target_attempt_id = _manual_running_coordinated(
        uow_factory, task_service, budget=budget, count=2
    )
    with uow_factory() as uow:
        task = uow.tasks.get(task_id, for_update=True)
        target_run = uow.runs.get(target_run_id, for_update=True)
        target_attempt = uow.attempts.get(target_attempt_id, for_update=True)
        assert task is not None and target_run is not None and target_attempt is not None
        sibling_run = next(
            candidate
            for candidate in uow.runs.list_for_task(task_id)
            if candidate.id != target_run_id
        )
        sibling_subtask = uow.subtasks.get(sibling_run.subtask_id, for_update=True)
        assert sibling_subtask is not None
        start_at = max(
            task.updated_at, sibling_run.queued_at, sibling_subtask.updated_at
        ) + timedelta(seconds=1)
        sibling_subtask.start(sibling_run.id, at=start_at)
        sibling_run.start(at=start_at)
        sibling_attempt = TaskAttempt.lease(
            run_id=sibling_run.id,
            worker_id="sibling-worker",
            fencing_token=1,
            lease_expires_at=start_at + timedelta(minutes=5),
            reserved_tokens=1,
            reserved_cost_micros=0,
        )
        target_attempt.reserved_tokens = 1
        reserve_at = max(
            task.updated_at, sibling_run.started_at, sibling_subtask.updated_at
        ) + timedelta(seconds=1)
        task.reserve_budget(tokens=2, cost_micros=0, at=reserve_at)
        at = reserve_at + timedelta(seconds=1)
        batch = _make_two_step_accounting_batch(
            task, target_attempt, sibling_attempt, target_run.id, at
        )
        uow.tasks.save(task)
        uow.runs.save(sibling_run)
        uow.subtasks.save(sibling_subtask)
        uow.attempts.save(target_attempt)
        uow.attempts.add(sibling_attempt)
        uow.commit()

    with uow_factory() as uow:
        task = uow.tasks.get(task_id, for_update=True)
        target_run = uow.runs.get(target_run_id, for_update=True)
        target_attempt = uow.attempts.get(target_attempt_id, for_update=True)
        assert task is not None and target_run is not None and target_attempt is not None
        sibling_run = uow.runs.get(batch.transitions[1].run_id, for_update=True)
        sibling_attempt = uow.attempts.get(batch.transitions[1].attempt_id, for_update=True)
        assert sibling_run is not None and sibling_attempt is not None
        original_run_get = uow.runs.get
        original_get = uow.attempts.get

        def get_run(run_id, *, for_update=False):
            if run_id == sibling_run.id:
                return sibling_run
            return original_run_get(run_id, for_update=for_update)

        def get_attempt(attempt_id, *, for_update=False):
            if attempt_id == sibling_attempt.id:
                return sibling_attempt
            return original_get(attempt_id, for_update=for_update)

        before_accounting = (
            task.settled_tokens,
            task.reserved_tokens,
            task.version,
            task.updated_at,
            target_attempt.settled_tokens,
            target_attempt.budget_settlement_source,
            sibling_attempt.settled_tokens,
            sibling_attempt.budget_settlement_source,
        )
        sibling_run.status = RunStatus.SUCCEEDED
        uow.runs.get = get_run
        with pytest.raises(InvalidTaskTransition):
            BusinessOutcomeApplier._apply_prepared_accounting_batch(
                uow,
                task,
                target_run,
                target_attempt,
                disposition=AccountingDisposition.SETTLED,
                phase=KnownTerminalPhase.SUCCEEDED,
                finalized_at=at,
                batch=batch,
            )
        assert before_accounting == (
            task.settled_tokens,
            task.reserved_tokens,
            task.version,
            task.updated_at,
            target_attempt.settled_tokens,
            target_attempt.budget_settlement_source,
            sibling_attempt.settled_tokens,
            sibling_attempt.budget_settlement_source,
        )
        sibling_run.status = RunStatus.RUNNING
        sibling_attempt.status = AttemptStatus.FAILED
        uow.attempts.get = get_attempt
        with pytest.raises(InvalidTaskTransition):
            BusinessOutcomeApplier._apply_prepared_accounting_batch(
                uow,
                task,
                target_run,
                target_attempt,
                disposition=AccountingDisposition.SETTLED,
                phase=KnownTerminalPhase.SUCCEEDED,
                finalized_at=at,
                batch=batch,
            )
        assert before_accounting == (
            task.settled_tokens,
            task.reserved_tokens,
            task.version,
            task.updated_at,
            target_attempt.settled_tokens,
            target_attempt.budget_settlement_source,
            sibling_attempt.settled_tokens,
            sibling_attempt.budget_settlement_source,
        )
        uow.runs.get = original_run_get
        sibling_attempt.status = AttemptStatus.RUNNING
        uow.attempts.get = get_attempt
        BusinessOutcomeApplier._apply_prepared_accounting_batch(
            uow,
            task,
            target_run,
            target_attempt,
            disposition=AccountingDisposition.SETTLED,
            phase=KnownTerminalPhase.SUCCEEDED,
            finalized_at=at,
            batch=batch,
        )
        assert task.settled_tokens == 1
        assert task.reserved_tokens == 0
        assert target_attempt.budget_settlement_source is BudgetSettlementSource.ACTUAL
        assert sibling_attempt.budget_settlement_source is BudgetSettlementSource.RELEASED


def test_prepared_accounting_batch_rejects_scope_chain_and_identity_errors() -> None:
    task_id = uuid4()
    run_id = uuid4()
    attempt_id = uuid4()
    at = utc_now()
    base = PreparedAccountingTransition(
        task_id=task_id,
        run_id=run_id,
        attempt_id=attempt_id,
        attempt_reserved_tokens=1,
        attempt_reserved_cost_micros=0,
        attempt_worker_id="worker",
        attempt_fencing_token=1,
        attempt_lease_token=uuid4(),
        before_task_settled_tokens=0,
        before_task_reserved_tokens=2,
        before_task_settled_cost_micros=0,
        before_task_reserved_cost_micros=0,
        before_task_budget_revision=0,
        before_task_version=1,
        before_task_updated_at=at,
        after_task_settled_tokens=1,
        after_task_reserved_tokens=1,
        after_task_settled_cost_micros=0,
        after_task_reserved_cost_micros=0,
        after_task_budget_revision=0,
        after_task_version=2,
        after_task_updated_at=at,
        before_attempt_settled_tokens=None,
        before_attempt_settled_cost_micros=None,
        before_attempt_source=None,
        after_attempt_settled_tokens=1,
        after_attempt_settled_cost_micros=0,
        after_attempt_source=BudgetSettlementSource.ACTUAL,
        finalized_at=at,
    )
    second = replace(
        base,
        run_id=uuid4(),
        attempt_id=uuid4(),
        before_task_settled_tokens=1,
        before_task_reserved_tokens=1,
        before_task_version=2,
        after_task_settled_tokens=1,
        after_task_reserved_tokens=0,
        after_task_version=3,
        before_attempt_source=None,
        before_attempt_settled_tokens=None,
        before_attempt_settled_cost_micros=None,
        after_attempt_settled_tokens=0,
        after_attempt_settled_cost_micros=0,
        after_attempt_source=BudgetSettlementSource.RELEASED,
        attempt_reserved_tokens=1,
    )
    assert PreparedAccountingBatch((base, second)).transitions == (base, second)
    third = replace(
        second,
        run_id=uuid4(),
        attempt_id=uuid4(),
        attempt_reserved_tokens=0,
        before_task_settled_tokens=1,
        before_task_reserved_tokens=0,
        before_task_version=3,
        after_task_settled_tokens=1,
        after_task_reserved_tokens=0,
        after_task_version=4,
        before_attempt_source=None,
        before_attempt_settled_tokens=None,
        before_attempt_settled_cost_micros=None,
        after_attempt_settled_tokens=0,
        after_attempt_settled_cost_micros=0,
        after_attempt_source=BudgetSettlementSource.RELEASED,
    )
    assert len(PreparedAccountingBatch((base, second, third)).transitions) == 3
    with pytest.raises(InvalidTaskInput):
        PreparedAccountingBatch(())
    with pytest.raises(InvalidTaskInput):
        PreparedAccountingBatch((base, replace(second, attempt_id=base.attempt_id)))
    with pytest.raises(InvalidTaskInput):
        PreparedAccountingBatch((base, replace(second, before_task_version=3)))
    with pytest.raises(InvalidTaskInput):
        PreparedAccountingBatch((base, replace(second, task_id=uuid4())))
    with pytest.raises(InvalidTaskInput):
        PreparedAccountingBatch((base, replace(second, finalized_at=at + timedelta(seconds=1))))


def test_coordinated_all_before_batch_shutdown_is_atomic_and_saves_each_attempt_once(
    uow_factory, task_service
) -> None:
    budget = TaskBudget.create(max_tokens=10, token_reservation_per_attempt=1)
    task_id, target_run_id, target_attempt_id = _manual_running_coordinated(
        uow_factory, task_service, budget=budget, count=2
    )
    with uow_factory() as uow:
        task = uow.tasks.get(task_id, for_update=True)
        target_run = uow.runs.get(target_run_id, for_update=True)
        target_attempt = uow.attempts.get(target_attempt_id, for_update=True)
        assert task is not None and target_run is not None and target_attempt is not None
        sibling_run = next(
            candidate
            for candidate in uow.runs.list_for_task(task_id)
            if candidate.id != target_run_id
        )
        sibling_subtask = uow.subtasks.get(sibling_run.subtask_id, for_update=True)
        assert sibling_subtask is not None
        start_at = max(
            task.updated_at, sibling_run.queued_at, sibling_subtask.updated_at
        ) + timedelta(seconds=1)
        sibling_subtask.start(sibling_run.id, at=start_at)
        sibling_run.start(at=start_at)
        target_attempt.reserved_tokens = 1
        sibling_attempt = TaskAttempt.lease(
            run_id=sibling_run.id,
            worker_id="sibling-worker",
            fencing_token=1,
            lease_expires_at=start_at + timedelta(minutes=5),
            reserved_tokens=1,
            reserved_cost_micros=0,
        )
        reserve_at = max(
            task.updated_at, sibling_run.started_at, sibling_subtask.updated_at
        ) + timedelta(seconds=1)
        task.reserve_budget(tokens=2, cost_micros=0, at=reserve_at)
        at = reserve_at + timedelta(seconds=1)
        batch = _make_two_step_accounting_batch(
            task,
            target_attempt,
            sibling_attempt,
            target_run.id,
            at,
            target_settled=False,
        )
        uow.tasks.save(task)
        uow.runs.save(sibling_run)
        uow.subtasks.save(sibling_subtask)
        uow.attempts.save(target_attempt)
        uow.attempts.add(sibling_attempt)
        uow.commit()

    with uow_factory() as uow:
        task = uow.tasks.get(task_id, for_update=True)
        target_run = uow.runs.get(target_run_id, for_update=True)
        target_attempt = uow.attempts.get(target_attempt_id, for_update=True)
        assert task is not None and target_run is not None and target_attempt is not None
        sibling_run = uow.runs.get(batch.transitions[1].run_id, for_update=True)
        assert sibling_run is not None
        saves: list[object] = []
        original_save = uow.attempts.save

        def record_save(value):
            saves.append(value.id)
            original_save(value)

        uow.attempts.save = record_save
        before_outbox = len(uow._store.outbox)
        summary = BusinessOutcomeApplier().apply_known_terminal_in_uow(
            uow,
            task,
            target_run,
            target_attempt,
            ProgressionContext.ORDINARY,
            KnownTerminalPhase.FAILED,
            None,
            "executor.failed",
            None,
            False,
            AccountingDisposition.RELEASED,
            at,
            uuid4(),
            accounting_batch=batch,
        )
        assert summary.task_status is TaskStatus.FAILED
        persisted_task = uow.tasks.get(task_id)
        persisted_target_run = uow.runs.get(target_run_id)
        persisted_sibling_run = uow.runs.get(sibling_run.id)
        assert persisted_task is not None
        assert persisted_target_run is not None and persisted_sibling_run is not None
        persisted_target_attempt = uow.attempts.get(target_attempt_id)
        persisted_sibling_attempt = uow.attempts.get(batch.transitions[1].attempt_id)
        persisted_target_subtask = uow.subtasks.get(persisted_target_run.subtask_id)
        persisted_sibling_subtask = uow.subtasks.get(persisted_sibling_run.subtask_id)
        assert persisted_target_attempt is not None and persisted_sibling_attempt is not None
        assert persisted_target_subtask is not None and persisted_sibling_subtask is not None
        assert persisted_task.settled_tokens == 0 and persisted_task.reserved_tokens == 0
        assert persisted_target_run.status is RunStatus.FAILED
        assert persisted_sibling_run.status is RunStatus.CANCELED
        assert persisted_target_attempt.status is AttemptStatus.FAILED
        assert persisted_sibling_attempt.status is AttemptStatus.CANCELED
        for persisted_attempt in (persisted_target_attempt, persisted_sibling_attempt):
            assert persisted_attempt.budget_settlement_source is BudgetSettlementSource.RELEASED
            assert persisted_attempt.settled_tokens == 0
            assert persisted_attempt.settled_cost_micros == 0
        assert persisted_target_subtask.status is SubtaskStatus.FAILED
        assert persisted_sibling_subtask.status is SubtaskStatus.CANCELED
        assert saves.count(target_attempt.id) == 1
        assert saves.count(batch.transitions[1].attempt_id) == 1
        assert len(saves) == 2
        assert len(uow._store.outbox) == before_outbox


def test_coordinated_three_step_all_before_batch_shutdown_is_end_to_end(
    uow_factory, task_service
) -> None:
    budget = TaskBudget.create(max_tokens=20, token_reservation_per_attempt=1)
    task_id, target_run_id, target_attempt_id = _manual_running_coordinated(
        uow_factory, task_service, budget=budget, count=3
    )
    with uow_factory() as uow:
        task = uow.tasks.get(task_id, for_update=True)
        target_run = uow.runs.get(target_run_id, for_update=True)
        target_attempt = uow.attempts.get(target_attempt_id, for_update=True)
        assert task is not None and target_run is not None and target_attempt is not None
        target_attempt.reserved_tokens = 1
        sibling_runs = [
            candidate
            for candidate in uow.runs.list_for_task(task_id)
            if candidate.id != target_run_id
        ]
        sibling_attempts = []
        for sibling_run in sibling_runs:
            sibling_subtask = uow.subtasks.get(sibling_run.subtask_id, for_update=True)
            assert sibling_subtask is not None
            start_at = max(
                task.updated_at, sibling_run.queued_at, sibling_subtask.updated_at
            ) + timedelta(seconds=1)
            sibling_subtask.start(sibling_run.id, at=start_at)
            sibling_run.start(at=start_at)
            sibling_attempt = TaskAttempt.lease(
                run_id=sibling_run.id,
                worker_id="sibling-worker",
                fencing_token=1,
                lease_expires_at=start_at + timedelta(minutes=5),
                reserved_tokens=1,
                reserved_cost_micros=0,
            )
            sibling_attempts.append(sibling_attempt)
            uow.subtasks.save(sibling_subtask)
            uow.runs.save(sibling_run)
            uow.attempts.add(sibling_attempt)
        reserve_at = task.updated_at + timedelta(seconds=1)
        task.reserve_budget(tokens=3, cost_micros=0, at=reserve_at)
        at = reserve_at + timedelta(seconds=1)
        batch = _make_released_accounting_batch(
            task,
            [target_attempt, *sibling_attempts],
            target_run.id,
            at,
        )
        uow.tasks.save(task)
        uow.attempts.save(target_attempt)
        uow.commit()

    with uow_factory() as uow:
        task = uow.tasks.get(task_id, for_update=True)
        target_run = uow.runs.get(target_run_id, for_update=True)
        target_attempt = uow.attempts.get(target_attempt_id, for_update=True)
        assert task is not None and target_run is not None and target_attempt is not None
        bad_run = replace(target_run, subtask_id=uuid4())
        original_run_get = uow.runs.get

        def get_bad_run(run_id, *, for_update=False):
            if run_id == target_run_id:
                return bad_run
            return original_run_get(run_id, for_update=for_update)

        writes: list[str] = []
        original_task_save = uow.tasks.save
        original_run_save = uow.runs.save
        original_attempt_save = uow.attempts.save
        original_subtask_save = uow.subtasks.save
        original_outbox_add = uow.outbox.add
        uow.tasks.save = lambda value: (writes.append("task"), original_task_save(value))[1]
        uow.runs.save = lambda value: (writes.append("run"), original_run_save(value))[1]
        uow.attempts.save = lambda value: (writes.append("attempt"), original_attempt_save(value))[
            1
        ]
        uow.subtasks.save = lambda value: (writes.append("subtask"), original_subtask_save(value))[
            1
        ]
        uow.outbox.add = lambda value: (writes.append("outbox"), original_outbox_add(value))[1]
        before_entities = [
            (
                task.status,
                task.version,
                task.updated_at,
                task.reserved_tokens,
                task.settled_tokens,
                uow.attempts.get(item.attempt_id).status,
                uow.attempts.get(item.attempt_id).budget_settlement_source,
            )
            for item in batch.transitions
        ]
        uow.runs.get = get_bad_run
        with pytest.raises(InvalidTaskTransition):
            BusinessOutcomeApplier().apply_known_terminal_in_uow(
                uow,
                task,
                target_run,
                target_attempt,
                ProgressionContext.ORDINARY,
                KnownTerminalPhase.FAILED,
                None,
                "executor.failed",
                None,
                False,
                AccountingDisposition.RELEASED,
                at,
                uuid4(),
                accounting_batch=batch,
            )
        assert writes == []
        assert before_entities == [
            (
                task.status,
                task.version,
                task.updated_at,
                task.reserved_tokens,
                task.settled_tokens,
                uow.attempts.get(item.attempt_id).status,
                uow.attempts.get(item.attempt_id).budget_settlement_source,
            )
            for item in batch.transitions
        ]
        uow.runs.get = original_run_get
        uow.tasks.save = original_task_save
        uow.runs.save = original_run_save
        uow.attempts.save = original_attempt_save
        uow.subtasks.save = original_subtask_save
        uow.outbox.add = original_outbox_add
        saves: list[object] = []
        original_save = uow.attempts.save

        def record_save(value):
            saves.append(value.id)
            original_save(value)

        uow.attempts.save = record_save
        before_outbox = len(uow._store.outbox)
        summary = BusinessOutcomeApplier().apply_known_terminal_in_uow(
            uow,
            task,
            target_run,
            target_attempt,
            ProgressionContext.ORDINARY,
            KnownTerminalPhase.FAILED,
            None,
            "executor.failed",
            None,
            False,
            AccountingDisposition.RELEASED,
            at,
            uuid4(),
            accounting_batch=batch,
        )
        assert summary.task_status is TaskStatus.FAILED
        assert len(saves) == 3
        assert set(saves) == {item.attempt_id for item in batch.transitions}
        persisted_task = uow.tasks.get(task_id)
        assert persisted_task is not None
        assert persisted_task.reserved_tokens == 0
        assert persisted_task.settled_tokens == 0
        assert len(uow._store.outbox) == before_outbox
        persisted_runs = uow.runs.list_for_task(task_id)
        assert len(persisted_runs) == 3
        assert sum(run.status is RunStatus.FAILED for run in persisted_runs) == 1
        assert sum(run.status is RunStatus.CANCELED for run in persisted_runs) == 2
        persisted_attempts = [uow.attempts.latest_for_run(run.id) for run in persisted_runs]
        assert all(item is not None for item in persisted_attempts)
        assert all(
            item.budget_settlement_source is BudgetSettlementSource.RELEASED
            for item in persisted_attempts
        )
        assert all(
            item.settled_tokens == 0 and item.settled_cost_micros == 0
            for item in persisted_attempts
        )
