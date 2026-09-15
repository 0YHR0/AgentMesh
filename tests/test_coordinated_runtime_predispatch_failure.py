from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import MappingProxyType, SimpleNamespace
from uuid import uuid4

import pytest

from agentmesh.application.authority_cohorts import AuthorityCohort
from agentmesh.application.coordinated_runtime import CoordinatedRuntimeAggregate
from agentmesh.application.coordinated_runtime_predispatch_failure import (
    CoordinatedPredispatchFailureKind,
    CoordinatedRuntimePredispatchFailureService,
)
from agentmesh.domain.coordination import CoordinationRuntimeBoundary, Subtask
from agentmesh.domain.errors import InvalidTaskInput, RuntimeExecutionConflict
from agentmesh.domain.messaging import MessageEnvelope
from agentmesh.domain.tasks import (
    RunRole,
    Task,
    TaskAttempt,
    TaskExecutionMode,
    TaskRun,
    TaskStatus,
)

UTC = timezone.utc


class _Uow:
    def __init__(self, aggregate):
        self.aggregate = aggregate
        self.calls = []
        self.inbox_rows = []
        self.outbox_rows = []
        self.drains = []
        self.commits = 0
        self.tasks = SimpleNamespace(save=lambda value: self.calls.append(("save-task", value.id)))
        self.runs = SimpleNamespace(save=lambda value: self.calls.append(("save-run", value.id)))
        self.subtasks = SimpleNamespace(
            save=lambda value: self.calls.append(("save-subtask", value.id))
        )
        self.attempts = SimpleNamespace(
            save=lambda value: self.calls.append(("save-attempt", value.id))
        )
        self.runtimes = SimpleNamespace(
            save_execution=lambda value, **kwargs: self.calls.append(
                ("save-execution", value)
            )
        )
        self.quotas = SimpleNamespace(
            list_reservations_for_attempt=lambda *args, **kwargs: [],
            save_reservation=lambda value: None,
        )
        self.coordination_runtime_drains = SimpleNamespace(
            get=lambda *args, **kwargs: None,
            add=lambda value: self.drains.append(value),
            save=lambda value, **kwargs: self.drains.append(value),
        )
        self.inbox = SimpleNamespace(
            contains=lambda tenant, consumer, message: any(
                row.tenant_id == tenant
                and row.consumer_name == consumer
                and row.message_id == message
                for row in self.inbox_rows
            ),
            add=self.inbox_rows.append,
        )
        self.outbox = SimpleNamespace(
            add_if_absent=self._add_outbox,
            add=self.outbox_rows.append,
        )

    def _add_outbox(self, value):
        if any(row.message_id == value.message_id for row in self.outbox_rows):
            return False
        self.outbox_rows.append(value)
        return True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def commit(self):
        self.commits += 1


class _Locker:
    def __init__(self, aggregate, calls):
        self.aggregate = aggregate
        self.calls = calls

    def lock(self, uow, *, tenant_id, task_id):
        self.calls.append(("lock-task-first", task_id))
        return self.aggregate


def _fixture():
    now = datetime(2030, 1, 1, tzinfo=UTC)
    task = Task.create(
        tenant_id="tenant",
        objective="coordinate",
        execution_mode=TaskExecutionMode.COORDINATED,
        plan_version=1,
        plan_digest="sha256:" + "a" * 64,
        max_concurrency=2,
    )
    task.status = TaskStatus.RUNNING
    subtask = Subtask.create(
        subtask_id=uuid4(),
        task_id=task.id,
        key="step",
        objective="step",
        input={},
        required_capabilities=("general.task",),
        preferred_agent_id="agent",
        initially_ready=True,
    )
    runtime_version_id = uuid4()
    run = TaskRun.request(
        task.id,
        "agent",
        agent_version_id=uuid4(),
        agent_version_digest="b" * 64,
        role=RunRole.EXECUTOR,
        subtask_id=subtask.id,
        runtime_version_id=runtime_version_id,
        runtime_authority="managed",
        at=now,
    )
    subtask.queue(run.id, at=now)
    subtask.start(run.id, at=now)
    run.start(at=now)
    attempt = TaskAttempt.lease(
        run_id=run.id,
        worker_id="worker",
        fencing_token=3,
        lease_expires_at=now + timedelta(minutes=5),
        at=now,
    )
    aggregate = CoordinatedRuntimeAggregate(
        task=task,
        active_drain=None,
        cohort=AuthorityCohort(
            "managed",
            runtime_version_id,
            "off",
            task_id=task.id,
            tenant_id=task.tenant_id,
        ),
        runtime_versions=MappingProxyType({runtime_version_id: object()}),
        subtasks=(subtask,),
        runs=(run,),
        latest_attempts=MappingProxyType({run.id: attempt}),
        executions=(),
        assignment_snapshots=(),
        handle_snapshots=(),
        lifecycle_operations=(),
        integrity_incidents=(),
        boundary_classifications=MappingProxyType(
            {run.id: CoordinationRuntimeBoundary.NOT_CROSSED_NO_EXECUTION}
        ),
    )
    uow = _Uow(aggregate)
    calls = []
    service = CoordinatedRuntimePredispatchFailureService(
        uow_factory=lambda: uow,
        aggregate_locker=_Locker(aggregate, calls),
    )
    envelope = MessageEnvelope.run_requested(
        tenant_id=task.tenant_id, task_id=task.id, run_id=run.id, at=now
    )
    return now, aggregate, attempt, uow, calls, service, envelope


def _fail(service, aggregate, attempt, envelope, now):
    return service.fail_delivery(
        aggregate.task.tenant_id,
        aggregate.task.id,
        aggregate.runs[0].id,
        attempt.id,
        attempt.fencing_token,
        "worker-consumer",
        envelope,
        "runtime.assignment_invalid",
        uuid4(),
        now,
    )


def test_provider_free_failure_is_atomic_and_replay_is_read_only(monkeypatch):
    now, aggregate, attempt, uow, calls, service, envelope = _fixture()
    monkeypatch.setattr(
        "agentmesh.application.coordinated_runtime_predispatch_failure."
        "validate_builtin_managed_runtime_version",
        lambda value: None,
    )

    result = _fail(service, aggregate, attempt, envelope, now)

    assert result.kind is CoordinatedPredispatchFailureKind.FAILED
    assert calls == [("lock-task-first", aggregate.task.id)]
    assert aggregate.task.status is TaskStatus.FAILED
    assert aggregate.runs[0].status.value == "FAILED"
    assert aggregate.subtasks[0].status.value == "FAILED"
    assert attempt.status.value == "FAILED"
    assert len(uow.drains) == 2  # creation followed by completion projection
    assert [row.schema_name for row in uow.outbox_rows] == [
        "agentmesh.runtime.predispatch-failed"
    ]
    assert len(uow.inbox_rows) == 1
    assert uow.commits == 1

    before = (len(uow.outbox_rows), len(uow.inbox_rows), uow.commits)
    replay = _fail(service, aggregate, attempt, envelope, now)
    assert replay.kind is CoordinatedPredispatchFailureKind.REPLAY
    assert (len(uow.outbox_rows), len(uow.inbox_rows), uow.commits) == before


def test_crossed_target_fails_closed_without_inbox_or_commit(monkeypatch):
    now, aggregate, attempt, uow, _, service, envelope = _fixture()
    monkeypatch.setattr(
        "agentmesh.application.coordinated_runtime_predispatch_failure."
        "validate_builtin_managed_runtime_version",
        lambda value: None,
    )
    crossed = replace(
        aggregate,
        boundary_classifications=MappingProxyType(
            {aggregate.runs[0].id: CoordinationRuntimeBoundary.CROSSED_ACTIVE}
        ),
    )
    service._aggregate_locker.aggregate = crossed

    with pytest.raises(RuntimeExecutionConflict, match="crossed"):
        _fail(service, aggregate, attempt, envelope, now)

    assert uow.inbox_rows == []
    assert uow.outbox_rows == []
    assert uow.commits == 0


def test_invalid_safe_reason_is_rejected_before_uow():
    now, aggregate, attempt, _, calls, service, envelope = _fixture()
    with pytest.raises(InvalidTaskInput, match="safe code"):
        service.fail_delivery(
            aggregate.task.tenant_id,
            aggregate.task.id,
            aggregate.runs[0].id,
            attempt.id,
            attempt.fencing_token,
            "worker-consumer",
            envelope,
            "provider said password=secret",
            uuid4(),
            now,
        )
    assert calls == []


def test_module_has_no_adapter_observation_or_nested_uow_calls():
    source = (
        __import__(
            "agentmesh.application.coordinated_runtime_predispatch_failure",
            fromlist=["x"],
        )
        .__loader__
        .get_source("agentmesh.application.coordinated_runtime_predispatch_failure")
    )
    assert "RuntimeObservationEvidence" not in source
    assert ".dispatch(" not in source
    assert ".inspect(" not in source
    assert "_cancel_coordinated_siblings" not in source
