from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from agentmesh.application.coordinated_runtime_delivery import (
    CoordinatedDeliveryResultKind,
)
from agentmesh.application.coordinated_runtime_delivery_acquisition import (
    CoordinatedRuntimeDeliveryAcquisitionService,
)
from agentmesh.application.ports import WorkflowWorkItem
from agentmesh.application.quota_services import QuotaAdmissionRejected
from agentmesh.domain.coordination import (
    CoordinationRuntimeBoundary,
    CoordinationRuntimeDrain,
    CoordinationRuntimeDrainTarget,
    Subtask,
    SubtaskStatus,
)
from agentmesh.domain.errors import (
    InvalidMessage,
    InvalidTaskInput,
    RuntimeExecutionConflict,
)
from agentmesh.domain.messaging import MessageEnvelope
from agentmesh.domain.runtime_execution import RuntimeExecution, RuntimeExecutionPhase
from agentmesh.domain.tasks import (
    RunRole,
    RunStatus,
    Task,
    TaskExecutionMode,
    TaskRun,
    TaskStatus,
)
from agentmesh.runtime_sdk.assignment import RuntimeAssignment

UTC = timezone.utc


class _Repo:
    def __init__(self, task, aggregate):
        self.task = task
        self.aggregate = aggregate
        self.calls = []
        self.added_attempts = []
        self.consumed = []
        self.commits = 0

    def task_get(self, task_id, *, for_update=False):
        self.calls.append(("task", for_update))
        return self.task

    def add_attempt(self, attempt):
        self.added_attempts.append(attempt)

    def save_task(self, task):
        self.task = task

    def save_run(self, run):
        pass

    def save_subtask(self, subtask):
        pass

    def inbox_contains(self, tenant_id, consumer_name, message_id):
        return False

    def inbox_add(self, message):
        self.consumed.append(message)


class _Uow:
    def __init__(self, repo):
        self.tasks = SimpleNamespace(get=repo.task_get, save=repo.save_task)
        self.runs = SimpleNamespace(save=repo.save_run)
        self.subtasks = SimpleNamespace(save=repo.save_subtask)
        self.attempts = SimpleNamespace(add=repo.add_attempt, save=lambda value: None)
        self.inbox = SimpleNamespace(contains=repo.inbox_contains, add=repo.inbox_add)
        self.runtimes = SimpleNamespace(save_execution=lambda *args, **kwargs: None)
        self.quotas = SimpleNamespace(
            list_active_for_task=lambda *args, **kwargs: [],
            count_active_for_scope=lambda *args, **kwargs: 0,
            add_reservation=lambda value: None,
            list_reservations_for_attempt=lambda *args, **kwargs: [],
            save_reservation=lambda value: None,
        )
        self.coordination_runtime_drains = SimpleNamespace(
            add=lambda value: None,
            save=lambda value, **kwargs: None,
        )
        self._repo = repo

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def commit(self):
        self._repo.commits += 1


class _Factory:
    def __init__(self, uow):
        self.uow = uow

    def __call__(self):
        return self.uow


class _Builder:
    def build(self, task, run, *, uow):
        return WorkflowWorkItem("do work", {"run": str(run.id)})


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
    run = TaskRun.request(
        task.id,
        "agent",
        agent_version_id=uuid4(),
        agent_version_digest="b" * 64,
        role=RunRole.EXECUTOR,
        subtask_id=subtask.id,
        runtime_version_id=uuid4(),
        runtime_authority="managed",
        at=now,
    )
    subtask.queue(run.id, at=now)
    aggregate = SimpleNamespace(
        task=task,
        active_drain=None,
        cohort=SimpleNamespace(
            runtime_authority="managed",
            runtime_version_id=run.runtime_version_id,
            comparison_mode="off",
            task_id=task.id,
            tenant_id=task.tenant_id,
        ),
        runtime_versions={run.runtime_version_id: object()},
        subtasks=(subtask,),
        runs=(run,),
        latest_attempts={run.id: None},
        executions_by_run={run.id: ()},
        assignment_snapshots_by_execution={},
        handle_snapshots_by_execution={},
        boundary_classifications={run.id: CoordinationRuntimeBoundary.NOT_CROSSED_QUEUED},
    )
    repo = _Repo(task, aggregate)
    uow = _Uow(repo)
    definition_id = uuid4()
    uow.agent_versions = SimpleNamespace(
        get=lambda value, **kwargs: SimpleNamespace(
            id=value,
            definition_id=definition_id,
            content_digest=run.agent_version_digest,
        )
    )
    uow.agent_definitions = SimpleNamespace(
        get=lambda value, **kwargs: SimpleNamespace(tenant_id=task.tenant_id)
    )
    locker = SimpleNamespace(lock_after_task=lambda *args, **kwargs: aggregate)
    service = CoordinatedRuntimeDeliveryAcquisitionService(
        uow_factory=_Factory(uow),
        worker_id="worker",
        consumer_name="consumer",
        lease_duration=timedelta(minutes=5),
        aggregate_locker=locker,
        work_item_builder=_Builder(),
    )
    return now, task, run, repo, service


def test_first_repository_operation_is_task_and_queued_delivery_acquires(monkeypatch):
    now, task, run, repo, service = _fixture()
    monkeypatch.setattr(
        "agentmesh.application.coordinated_runtime_delivery_acquisition.validate_builtin_managed_runtime_version",
        lambda value: None,
    )
    result = service.classify_and_acquire(
        MessageEnvelope.run_requested(
            tenant_id="tenant", task_id=task.id, run_id=run.id, at=now
        ),
        now=now,
    )
    assert result.kind is CoordinatedDeliveryResultKind.ACQUIRED
    assert repo.calls == [("task", True)]
    assert len(repo.added_attempts) == 1
    assert repo.consumed == []
    assert repo.commits == 1
    assert result.lease is not None
    assert result.lease.lease_deadline == now + timedelta(minutes=5)


def test_non_coordinated_task_is_not_applicable_without_aggregate_expansion():
    now, task, run, repo, service = _fixture()
    task.execution_mode = TaskExecutionMode.DIRECT
    service._aggregate_locker = SimpleNamespace(
        lock_after_task=lambda *args, **kwargs: pytest.fail("aggregate expanded")
    )

    result = service.classify_and_acquire(
        MessageEnvelope.run_requested(
            tenant_id=task.tenant_id, task_id=task.id, run_id=run.id, at=now
        ),
        now=now,
    )

    assert result.kind is CoordinatedDeliveryResultKind.NOT_APPLICABLE
    assert repo.calls == [("task", True)]
    assert repo.commits == 0


def test_legacy_cohort_is_not_applicable_after_complete_aggregate_lock():
    now, task, run, repo, service = _fixture()
    repo.aggregate.cohort.runtime_authority = "legacy"
    repo.aggregate.cohort.runtime_version_id = None
    run.runtime_authority = "legacy"
    run.runtime_version_id = None

    result = service.classify_and_acquire(
        MessageEnvelope.run_requested(
            tenant_id=task.tenant_id, task_id=task.id, run_id=run.id, at=now
        ),
        now=now,
    )

    assert result.kind is CoordinatedDeliveryResultKind.NOT_APPLICABLE
    assert repo.calls == [("task", True)]
    assert repo.commits == 0


def test_processed_inbox_delivery_is_read_only_replay(monkeypatch):
    now, task, run, repo, service = _fixture()
    monkeypatch.setattr(
        "agentmesh.application.coordinated_runtime_delivery_acquisition.validate_builtin_managed_runtime_version",
        lambda value: None,
    )
    service._uow_factory.uow.inbox.contains = lambda *args, **kwargs: True

    result = service.classify_and_acquire(
        MessageEnvelope.run_requested(
            tenant_id=task.tenant_id, task_id=task.id, run_id=run.id, at=now
        ),
        now=now,
    )

    assert result.kind is CoordinatedDeliveryResultKind.REPLAY_PROCESSED
    assert repo.added_attempts == []
    assert repo.consumed == []
    assert repo.commits == 0


def test_invalid_envelope_is_rejected_before_uow():
    _, _, _, repo, service = _fixture()
    with pytest.raises(InvalidMessage):
        service.classify_and_acquire(object())
    assert repo.calls == []


def test_constructor_requires_explicit_work_item_builder():
    with pytest.raises(InvalidTaskInput):
        CoordinatedRuntimeDeliveryAcquisitionService(
            uow_factory=lambda: None,
            worker_id="worker",
            consumer_name="consumer",
            lease_duration=timedelta(minutes=1),
        )


@pytest.mark.parametrize("field", ["worker_id", "consumer_name"])
def test_constructor_rejects_whitespace_padded_identity(field):
    kwargs = {
        "uow_factory": lambda: None,
        "worker_id": "worker",
        "consumer_name": "consumer",
        "lease_duration": timedelta(minutes=1),
        "work_item_builder": _Builder(),
    }
    kwargs[field] = f" {kwargs[field]}"
    with pytest.raises(InvalidTaskInput):
        CoordinatedRuntimeDeliveryAcquisitionService(**kwargs)


def _crossed_fixture(expired: bool):
    now, task, run, repo, service = _fixture()
    run.start(at=now)
    subtask = repo.aggregate.subtasks[0]
    subtask.start(run.id, at=now)
    from agentmesh.domain.tasks import TaskAttempt

    attempt = TaskAttempt.lease(
        run_id=run.id,
        worker_id="owner",
        fencing_token=1,
        lease_expires_at=now + (timedelta(minutes=-1) if expired else timedelta(minutes=5)),
        at=now,
    )
    execution = RuntimeExecution.prepare(
        tenant_id=task.tenant_id,
        run_id=run.id,
        runtime_version_id=run.runtime_version_id,
            assignment_id=uuid4(),
        assignment_digest="c" * 64,
        dispatch_key="dispatch",
        dispatch_digest="d" * 64,
        execution_id=run.runtime_execution_intent_id,
        now=now,
    ).claim(
        attempt_id=attempt.id,
        fencing_token=attempt.fencing_token,
        expected_owner_attempt_id=None,
        expected_fencing_token=None,
        expected_version=1,
        now=now,
    ).apply_observation(
        phase=RuntimeExecutionPhase.RUNNING,
        provider_sequence=None,
        now=now,
    )
    run.bind_runtime_execution(execution.id)
    repo.aggregate.latest_attempts = {run.id: attempt}
    repo.aggregate.executions_by_run = {run.id: (execution,)}
    repo.aggregate.boundary_classifications = {
        run.id: CoordinationRuntimeBoundary.CROSSED_ACTIVE
    }
    return now, task, run, repo, service


def test_unexpired_crossed_owner_is_in_progress(monkeypatch):
    now, task, run, repo, service = _crossed_fixture(expired=False)
    monkeypatch.setattr(
        "agentmesh.application.coordinated_runtime_delivery_acquisition.validate_builtin_managed_runtime_version",
        lambda value: None,
    )
    result = service.classify_and_acquire(
        MessageEnvelope.run_requested(
            tenant_id="tenant", task_id=task.id, run_id=run.id, at=now
        ),
        now=now,
    )
    assert result.kind is CoordinatedDeliveryResultKind.IN_PROGRESS
    assert repo.added_attempts == []
    assert repo.consumed == []
    assert repo.commits == 0


def test_expired_crossed_owner_returns_proof_without_replacement(monkeypatch):
    now, task, run, repo, service = _crossed_fixture(expired=True)
    monkeypatch.setattr(
        "agentmesh.application.coordinated_runtime_delivery_acquisition.validate_builtin_managed_runtime_version",
        lambda value: None,
    )
    result = service.classify_and_acquire(
        MessageEnvelope.run_requested(
            tenant_id="tenant", task_id=task.id, run_id=run.id, at=now
        ),
        now=now,
    )
    assert result.kind is CoordinatedDeliveryResultKind.RECOVER_CROSSED
    assert result.recovery_crossed_proof is not None
    assert (
        result.recovery_crossed_proof.expired_owner_attempt_id
        == repo.aggregate.latest_attempts[run.id].id
    )
    assert repo.added_attempts == []
    assert repo.consumed == []
    assert repo.commits == 0


def test_expired_no_execution_is_recovered_with_fenced_replacement(monkeypatch):
    now, task, run, repo, service = _fixture()
    run.start(at=now)
    subtask = repo.aggregate.subtasks[0]
    subtask.start(run.id, at=now)
    from agentmesh.domain.tasks import TaskAttempt

    old = TaskAttempt.lease(
        run_id=run.id,
        worker_id="owner",
        fencing_token=3,
        lease_expires_at=now - timedelta(minutes=1),
        at=now,
    )
    repo.aggregate.latest_attempts = {run.id: old}
    repo.aggregate.boundary_classifications = {
        run.id: CoordinationRuntimeBoundary.NOT_CROSSED_NO_EXECUTION
    }
    monkeypatch.setattr(
        "agentmesh.application.coordinated_runtime_delivery_acquisition.validate_builtin_managed_runtime_version",
        lambda value: None,
    )
    result = service.classify_and_acquire(
        MessageEnvelope.run_requested(
            tenant_id="tenant", task_id=task.id, run_id=run.id, at=now
        ),
        now=now,
    )
    assert result.kind is CoordinatedDeliveryResultKind.RECOVERED_PRE_BOUNDARY
    assert len(repo.added_attempts) == 1
    assert repo.added_attempts[0].fencing_token == 4
    assert repo.commits == 1


def test_expired_prepared_owner_replaces_only_runtime_ownership(monkeypatch):
    now, task, run, repo, service = _crossed_fixture(expired=True)
    old = repo.aggregate.latest_attempts[run.id]
    execution = repo.aggregate.executions_by_run[run.id][0]
    assignment = RuntimeAssignment(
        assignment_id=str(execution.assignment_id),
        tenant_id=task.tenant_id,
        task_id=str(task.id),
        run_id=str(run.id),
        agent_definition_id=str(uuid4()),
        agent_version_id=str(run.agent_version_id),
        agent_version_digest=run.agent_version_digest,
        runtime_version_id=str(run.runtime_version_id),
        runtime_descriptor_digest="d" * 64,
        execution_mode="managed_async",
        run_role=run.role.value,
        revision=run.revision_number,
        objective="do work",
        structured_input={"run": str(execution.id)},
        correlation_ids={"runtime_execution_id": str(execution.id)},
    )
    execution = RuntimeExecution.prepare(
        tenant_id=task.tenant_id,
        run_id=run.id,
        runtime_version_id=run.runtime_version_id,
        assignment_id=execution.assignment_id,
        assignment_digest=assignment.assignment_digest,
        dispatch_key=execution.dispatch_key,
        dispatch_digest=execution.dispatch_digest,
        execution_id=execution.id,
        now=now,
    ).claim(
        attempt_id=old.id,
        fencing_token=old.fencing_token,
        expected_owner_attempt_id=None,
        expected_fencing_token=None,
        expected_version=1,
        now=now,
    )
    repo.aggregate.executions_by_run = {run.id: (execution,)}
    repo.aggregate.assignment_snapshots_by_execution = {
        execution.id: SimpleNamespace(
            runtime_execution_id=execution.id,
            assignment_id=execution.assignment_id,
            assignment_digest=assignment.assignment_digest,
            canonical_payload=assignment.to_dict(),
        )
    }
    repo.aggregate.boundary_classifications = {
        run.id: CoordinationRuntimeBoundary.NOT_CROSSED_PREPARED
    }
    repo.aggregate.runtime_versions[run.runtime_version_id] = SimpleNamespace(
        id=run.runtime_version_id, descriptor={}
    )
    monkeypatch.setattr(
        "agentmesh.application.coordinated_runtime_delivery_acquisition.validate_builtin_managed_runtime_version",
        lambda value: None,
    )
    monkeypatch.setattr(
        "agentmesh.application.coordinated_runtime_delivery_acquisition.RuntimeDescriptor.from_dict",
        lambda value: SimpleNamespace(digest=lambda: "d" * 64),
    )
    result = service.classify_and_acquire(
        MessageEnvelope.run_requested(
            tenant_id="tenant", task_id=task.id, run_id=run.id, at=now
        ),
        now=now,
    )
    assert result.kind is CoordinatedDeliveryResultKind.RECOVERED_PRE_BOUNDARY
    assert len(repo.added_attempts) == 1
    assert repo.added_attempts[0].fencing_token == old.fencing_token + 1
    assert repo.commits == 1


def test_active_drain_releases_queued_delivery_and_consumes_inbox(monkeypatch):
    now, task, run, repo, service = _fixture()
    repo.aggregate.active_drain = CoordinationRuntimeDrain.start(
        drain_id=uuid4(),
        tenant_id=task.tenant_id,
        task_id=task.id,
        triggering_run_id=run.id,
        target=CoordinationRuntimeDrainTarget.CANCELED,
        reason="operator stopping",
        at=now,
    )
    monkeypatch.setattr(
        "agentmesh.application.coordinated_runtime_delivery_acquisition.validate_builtin_managed_runtime_version",
        lambda value: None,
    )
    result = service.classify_and_acquire(
        MessageEnvelope.run_requested(
            tenant_id="tenant", task_id=task.id, run_id=run.id, at=now
        ),
        now=now,
    )
    assert result.kind is CoordinatedDeliveryResultKind.BLOCKED_BY_DRAIN
    assert len(repo.added_attempts) == 0
    assert len(repo.consumed) == 1
    assert run.status is RunStatus.CANCELED


def test_budget_rejection_creates_waiting_approval_drain(monkeypatch):
    now, task, run, repo, service = _fixture()
    from agentmesh.domain.budgets import TaskBudget

    task.budget = TaskBudget.create(
        max_tokens=1,
        token_reservation_per_attempt=1,
    )
    task.reserved_tokens = 1
    monkeypatch.setattr(
        "agentmesh.application.coordinated_runtime_delivery_acquisition.validate_builtin_managed_runtime_version",
        lambda value: None,
    )
    result = service.classify_and_acquire(
        MessageEnvelope.run_requested(
            tenant_id="tenant", task_id=task.id, run_id=run.id, at=now
        ),
        now=now,
    )
    assert result.kind is CoordinatedDeliveryResultKind.WAITING_APPROVAL
    assert len(repo.added_attempts) == 0
    assert len(repo.consumed) == 1
    assert task.status is TaskStatus.WAITING_APPROVAL


def _supervisor_fixture():
    now, task, _executor, repo, service = _fixture()
    subtask = repo.aggregate.subtasks[0]
    subtask.status = SubtaskStatus.COMPLETED
    subtask.current_run_id = None
    supervisor = TaskRun.request(
        task.id,
        "supervisor",
        agent_version_id=uuid4(),
        agent_version_digest="e" * 64,
        role=RunRole.SUPERVISOR,
        runtime_version_id=next(iter(repo.aggregate.runtime_versions)),
        runtime_authority="managed",
        at=now,
    )
    task.queue_supervisor(supervisor.id, at=now)
    repo.aggregate.runs = (supervisor,)
    repo.aggregate.latest_attempts = {supervisor.id: None}
    repo.aggregate.executions_by_run = {supervisor.id: ()}
    repo.aggregate.boundary_classifications = {
        supervisor.id: CoordinationRuntimeBoundary.NOT_CROSSED_QUEUED
    }
    return now, task, supervisor, repo, service


def test_supervisor_queued_delivery_acquires_and_closed_binding_is_rejected(monkeypatch):
    now, task, supervisor, repo, service = _supervisor_fixture()
    monkeypatch.setattr(
        "agentmesh.application.coordinated_runtime_delivery_acquisition.validate_builtin_managed_runtime_version",
        lambda value: None,
    )
    result = service.classify_and_acquire(
        MessageEnvelope.run_requested(
            tenant_id=task.tenant_id, task_id=task.id, run_id=supervisor.id, at=now
        ),
        now=now,
    )
    assert result.kind is CoordinatedDeliveryResultKind.ACQUIRED
    assert result.lease is not None and result.lease.role is RunRole.SUPERVISOR

    now, task, supervisor, repo, service = _supervisor_fixture()
    supervisor.subtask_id = repo.aggregate.subtasks[0].id
    monkeypatch.setattr(
        "agentmesh.application.coordinated_runtime_delivery_acquisition.validate_builtin_managed_runtime_version",
        lambda value: None,
    )
    with pytest.raises(RuntimeExecutionConflict):
        service.classify_and_acquire(
            MessageEnvelope.run_requested(
                tenant_id=task.tenant_id, task_id=task.id, run_id=supervisor.id, at=now
            ),
            now=now,
        )


@pytest.mark.parametrize(
    "boundary", [
        CoordinationRuntimeBoundary.KNOWN_TERMINAL,
        CoordinationRuntimeBoundary.RECONCILIATION_EVIDENCE,
    ],
)
def test_terminal_and_reconciliation_replay_consumes_inbox_at_supplied_time(boundary, monkeypatch):
    now, task, run, repo, service = _fixture()
    repo.aggregate.boundary_classifications = {run.id: boundary}
    monkeypatch.setattr(
        "agentmesh.application.coordinated_runtime_delivery_acquisition.validate_builtin_managed_runtime_version",
        lambda value: None,
    )
    result = service.classify_and_acquire(
        MessageEnvelope.run_requested(
            tenant_id=task.tenant_id, task_id=task.id, run_id=run.id, at=now
        ),
        now=now,
    )
    assert result.kind is CoordinatedDeliveryResultKind.REPLAY_PROCESSED
    assert len(repo.consumed) == 1
    assert repo.consumed[0].processed_at == now
    assert repo.commits == 1


def test_quota_admission_rejection_has_no_attempt_or_inbox_write(monkeypatch):
    now, task, run, repo, service = _fixture()
    service._feature_gates = SimpleNamespace(is_enabled=lambda feature: True)
    policy = SimpleNamespace(
        scope=SimpleNamespace(value="TASK"), scope_key="task", max_concurrent_attempts=1
    )
    monkeypatch.setattr(
        "agentmesh.application.coordinated_runtime_delivery_acquisition.QuotaController.reserve_attempt",
        lambda *args, **kwargs: (_ for _ in ()).throw(QuotaAdmissionRejected(policy)),
    )
    monkeypatch.setattr(
        "agentmesh.application.coordinated_runtime_delivery_acquisition.validate_builtin_managed_runtime_version",
        lambda value: None,
    )
    with pytest.raises(QuotaAdmissionRejected):
        service.classify_and_acquire(
            MessageEnvelope.run_requested(
                tenant_id=task.tenant_id, task_id=task.id, run_id=run.id, at=now
            ),
            now=now,
        )
    assert repo.added_attempts == []
    assert repo.consumed == []
    assert repo.commits == 0


def _prepared_fixture():
    now, task, run, repo, service = _fixture()
    run.start(at=now)
    subtask = repo.aggregate.subtasks[0]
    subtask.start(run.id, at=now)
    from agentmesh.domain.tasks import TaskAttempt

    attempt = TaskAttempt.lease(
        run_id=run.id,
        worker_id="owner",
        fencing_token=1,
        lease_expires_at=now + timedelta(minutes=5),
        at=now,
    )
    assignment = RuntimeAssignment(
        assignment_id=str(uuid4()),
        tenant_id=task.tenant_id,
        task_id=str(task.id),
        run_id=str(run.id),
        agent_definition_id=str(uuid4()),
        agent_version_id=str(run.agent_version_id),
        agent_version_digest=run.agent_version_digest,
        runtime_version_id=str(run.runtime_version_id),
        runtime_descriptor_digest="d" * 64,
        execution_mode="managed_async",
        run_role=run.role.value,
        revision=run.revision_number,
        objective="do work",
        structured_input={"run": str(run.runtime_execution_intent_id)},
        correlation_ids={"runtime_execution_id": str(run.runtime_execution_intent_id)},
    )
    execution = RuntimeExecution.prepare(
        tenant_id=task.tenant_id,
        run_id=run.id,
        runtime_version_id=run.runtime_version_id,
            assignment_id=UUID(assignment.assignment_id),
        assignment_digest=assignment.assignment_digest,
        dispatch_key="dispatch",
        dispatch_digest="d" * 64,
        execution_id=run.runtime_execution_intent_id,
        now=now,
    ).claim(
        attempt_id=attempt.id,
        fencing_token=attempt.fencing_token,
        expected_owner_attempt_id=None,
        expected_fencing_token=None,
        expected_version=1,
        now=now,
    )
    run.bind_runtime_execution(execution.id)
    repo.aggregate.latest_attempts = {run.id: attempt}
    repo.aggregate.executions_by_run = {run.id: (execution,)}
    repo.aggregate.assignment_snapshots_by_execution = {
        execution.id: SimpleNamespace(
            runtime_execution_id=execution.id,
            assignment_id=execution.assignment_id,
            assignment_digest=assignment.assignment_digest,
            canonical_payload=assignment.to_dict(),
        )
    }
    repo.aggregate.boundary_classifications = {
        run.id: CoordinationRuntimeBoundary.NOT_CROSSED_PREPARED
    }
    repo.aggregate.runtime_versions[run.runtime_version_id] = SimpleNamespace(
        id=run.runtime_version_id, descriptor={}
    )
    return now, task, run, repo, service


def test_prepared_assignment_payload_mutation_rejects_without_writes(monkeypatch):
    now, task, run, repo, service = _prepared_fixture()
    snapshot = repo.aggregate.assignment_snapshots_by_execution[run.runtime_execution_id]
    mutated = dict(snapshot.canonical_payload)
    mutated["objective"] = "tampered"
    snapshot.canonical_payload = mutated
    monkeypatch.setattr(
        "agentmesh.application.coordinated_runtime_delivery_acquisition.validate_builtin_managed_runtime_version",
        lambda value: None,
    )
    monkeypatch.setattr(
        "agentmesh.application.coordinated_runtime_delivery_acquisition.RuntimeDescriptor.from_dict",
        lambda value: SimpleNamespace(digest=lambda: "d" * 64),
    )
    with pytest.raises(RuntimeExecutionConflict):
        service.classify_and_acquire(
            MessageEnvelope.run_requested(
                tenant_id=task.tenant_id, task_id=task.id, run_id=run.id, at=now
            ),
            now=now,
        )
    assert repo.added_attempts == []
    assert repo.consumed == []
    assert repo.commits == 0


def test_acquisition_source_has_task_first_single_uow_and_no_outward_runtime_callers():
    path = (
        Path(__file__).parents[1]
        / "src/agentmesh/application/coordinated_runtime_delivery_acquisition.py"
    )
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    assert not any(
        isinstance(node, ast.Name)
        and node.id in {"RunExecutionService", "ManagedAgentRuntime"}
        for node in ast.walk(tree)
    )
    assert "_cancel_coordinated_siblings" not in source
    with_nodes = [node for node in ast.walk(tree) if isinstance(node, ast.With)]
    assert len(with_nodes) == 1
    with_body = with_nodes[0].body
    first = with_body[0]
    assert isinstance(first, ast.Assign)
    call = first.value
    assert isinstance(call, ast.Call)
    assert isinstance(call.func, ast.Attribute) and call.func.attr == "get"
    assert isinstance(call.func.value, ast.Attribute) and call.func.value.attr == "tasks"
    assert any(
        keyword.arg == "for_update"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is True
        for keyword in call.keywords
    )
