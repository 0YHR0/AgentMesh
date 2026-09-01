from datetime import timedelta
from uuid import uuid4

import pytest

from agentmesh.application.business_outcomes import (
    AccountingDisposition,
    BusinessOutcomeApplication,
    BusinessOutcomeApplier,
    KnownTerminalPhase,
    ProgressionContext,
)
from agentmesh.application.coordination_services import CoordinatedScheduler
from agentmesh.domain.budgets import BudgetSettlementSource, TaskBudget
from agentmesh.domain.errors import InvalidTaskInput, InvalidTaskTransition
from agentmesh.domain.resolutions import TaskResolutionAction
from agentmesh.domain.tasks import (
    AcceptanceCriterion,
    AcceptanceCriterionKind,
    AttemptStatus,
    RunStatus,
    TaskAttempt,
    TaskExecutionMode,
    TaskRun,
    TaskStatus,
    utc_now,
)


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


@pytest.mark.parametrize("mode", [TaskExecutionMode.REVIEWED, TaskExecutionMode.COORDINATED])
def test_managed_unsupported_modes_reject_before_mutation(uow_factory, task_service, mode) -> None:
    # The activation gate is exercised using a direct managed Run retargeted to
    # the unsupported mode; no business repository save is permitted.
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
    with uow_factory() as uow:
        task, run, attempt, at = _outcome_entities(uow, ids)
        supplied = TaskAttempt(**attempt.__dict__)
        supplied.worker_id = "forged-worker"
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
