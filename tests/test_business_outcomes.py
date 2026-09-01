from datetime import timedelta
from uuid import uuid4

import pytest

from agentmesh.application.business_outcomes import (
    AccountingDisposition,
    BusinessOutcomeApplier,
    KnownTerminalPhase,
    ProgressionContext,
)
from agentmesh.domain.errors import InvalidTaskTransition
from agentmesh.domain.tasks import AttemptStatus, RunStatus, TaskAttempt, TaskStatus


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
