from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agentmesh.application.coordinated_runtime_cancel_applier import (
    CoordinatedCancelApplication,
)
from agentmesh.application.coordinated_runtime_control import (
    CoordinatedCancelCompletion,
    CoordinatedCancelKind,
    plan_cancel_request,
)
from agentmesh.application.coordinated_runtime_control_service import (
    CoordinatedRuntimeControlService,
)
from agentmesh.application.coordinated_runtime_stop_primitives import (
    lifecycle_outbox_envelope,
)
from agentmesh.domain.coordination import (
    CoordinationRuntimeBoundary,
    CoordinationRuntimeDrain,
    CoordinationRuntimeDrainTarget,
)
from agentmesh.domain.errors import (
    AuthorizationDenied,
    IdempotencyConflict,
    InvalidTaskInput,
    RuntimeExecutionConflict,
)
from agentmesh.domain.identity import PrincipalContext, PrincipalType, Role
from agentmesh.domain.runtime_execution import (
    RuntimeExecutionPhase,
    RuntimeLifecycleIntent,
    RuntimeLifecycleOperation,
    RuntimeLifecycleStatus,
)
from agentmesh.domain.tasks import TaskStatus
from tests.fakes import InMemoryStore, InMemoryUnitOfWork
from tests.test_coordinated_runtime_barrier import _aggregate

UTC = timezone.utc


def _principal(*, tenant_id: str = "tenant-a", role: Role = Role.OPERATOR, authenticated=True):
    return PrincipalContext(
        principal_id="coordinated-operator",
        tenant_id=tenant_id,
        principal_type=PrincipalType.USER,
        roles=frozenset({role}),
        authenticated=authenticated,
        authentication_method="test",
    )


class _Locker:
    def __init__(self, status: TaskStatus, operations: list[str]) -> None:
        _task, _target, aggregate = _aggregate()
        self.aggregate = replace(aggregate, task=replace(aggregate.task, status=status))
        self.operations = operations

    def lock(self, uow, *, tenant_id, task_id):
        self.operations.append("aggregate.lock")
        assert tenant_id == self.aggregate.task.tenant_id
        assert task_id == self.aggregate.task.id
        return self.aggregate


class _TrackingUow(InMemoryUnitOfWork):
    def __init__(self, factory: _Factory) -> None:
        super().__init__(factory.store)
        self.factory = factory

    def __enter__(self):
        super().__enter__()
        original_lock = self.idempotency.lock
        original_get = self.idempotency.get
        original_add = self.idempotency.add
        original_outbox_add = self.outbox.add
        original_outbox_get = self.outbox.get
        self._drains = deepcopy(self.factory.drains)

        def drain_get(drain_id, *, tenant_id, for_update=False):
            self.factory.operations.append("drain.get")
            value = self._drains.get(drain_id)
            return deepcopy(value) if value is not None and value.tenant_id == tenant_id else None

        def drain_add(value):
            self.factory.operations.append("drain.add")
            self._drains[value.id] = deepcopy(value)

        def drain_save(value, *, tenant_id):
            self.factory.operations.append("drain.save")
            assert value.tenant_id == tenant_id
            self._drains[value.id] = deepcopy(value)

        self.coordination_runtime_drains = SimpleNamespace(
            get=drain_get,
            add=drain_add,
            save=drain_save,
        )

        def lock(scope, key):
            self.factory.operations.append("idempotency.lock")
            return original_lock(scope, key)

        def get(scope, key):
            self.factory.operations.append("idempotency.get")
            return original_get(scope, key)

        def add(record):
            self.factory.operations.append("idempotency.add")
            if self.factory.fail_on == "idempotency.add":
                raise RuntimeError("injected idempotency failure")
            return original_add(record)

        def outbox_add(envelope):
            self.factory.operations.append("outbox.add")
            if self.factory.fail_on == "outbox.add":
                raise RuntimeError("injected outbox failure")
            return original_outbox_add(envelope)

        def outbox_get(message_id, *, tenant_id):
            self.factory.operations.append("outbox.get")
            return original_outbox_get(message_id, tenant_id=tenant_id)

        self.idempotency.lock = lock
        self.idempotency.get = get
        self.idempotency.add = add
        self.outbox.add = outbox_add
        self.outbox.get = outbox_get
        return self

    def commit(self) -> None:
        self.factory.operations.append("commit")
        if self.factory.fail_on == "commit":
            raise RuntimeError("injected commit failure")
        self.factory.commits += 1
        super().commit()
        self.factory.drains = deepcopy(self._drains)


class _Factory:
    def __init__(self, *, fail_on: str | None = None) -> None:
        self.store = InMemoryStore()
        self.operations: list[str] = []
        self.opens = 0
        self.commits = 0
        self.fail_on = fail_on
        self.drains = {}

    def __call__(self):
        self.opens += 1
        return _TrackingUow(self)


def _fixture(status: TaskStatus, *, fail_on: str | None = None):
    factory = _Factory(fail_on=fail_on)
    locker = _Locker(status, factory.operations)
    service = CoordinatedRuntimeControlService(
        uow_factory=factory,
        aggregate_locker=locker,
    )
    request = {
        "tenant_id": locker.aggregate.task.tenant_id,
        "task_id": locker.aggregate.task.id,
        "principal": _principal(tenant_id=locker.aggregate.task.tenant_id),
        "reason": "operator.requested",
        "idempotency_key": "cancel-1",
        "causation_id": uuid4(),
        "at": datetime(2026, 9, 15, 10, 0, tzinfo=UTC),
    }
    return SimpleNamespace(factory=factory, service=service, request=request, locker=locker)


class _ActiveApplier:
    def __init__(
        self,
        operations: list[str],
        *,
        fail: bool = False,
        no_progress: bool = False,
        lifecycle_operation_ids=(),
    ) -> None:
        self.operations = operations
        self.fail = fail
        self.no_progress = no_progress
        self.lifecycle_operation_ids = lifecycle_operation_ids
        self.calls = 0

    def apply_in_uow(self, uow, *, aggregate, plan, now, cancel_deadline_window):
        self.operations.append("applier.apply")
        self.calls += 1
        if self.fail:
            raise RuntimeError("injected applier failure")
        if self.no_progress:
            assert aggregate.active_drain is not None
            return CoordinatedCancelApplication(
                effective_drain=aggregate.active_drain,
                task_status=aggregate.task.status,
                changed_ids=(),
                lifecycle_operation_ids=self.lifecycle_operation_ids,
                completion=CoordinatedCancelCompletion.WAIT_ACTIVE,
                made_progress=False,
            )
        drain = CoordinationRuntimeDrain.start(
            drain_id=plan.drain_id,
            tenant_id=aggregate.task.tenant_id,
            task_id=aggregate.task.id,
            triggering_run_id=plan.audit_anchor_run_id,
            target=CoordinationRuntimeDrainTarget.CANCELED,
            reason=plan.effective_reason,
            at=now,
        ).complete(at=now)
        uow.coordination_runtime_drains.add(drain)
        aggregate.task.cancel_coordination_from_control(drain, at=now)
        return CoordinatedCancelApplication(
            effective_drain=drain,
            task_status=aggregate.task.status,
            changed_ids=tuple(sorted((aggregate.task.id, drain.id), key=str)),
            lifecycle_operation_ids=(),
            completion=CoordinatedCancelCompletion.APPLY_CANCELED,
            made_progress=True,
        )


def _active_fixture(*, fail_on: str | None = None, no_progress: bool = False):
    fixture = _fixture(TaskStatus.RUNNING, fail_on=fail_on)
    lifecycle_ids = ()
    if no_progress:
        plan = plan_cancel_request(fixture.locker.aggregate, fixture.request["reason"])
        prior = fixture.locker.aggregate.executions[0].updated_at + timedelta(seconds=1)
        fixture.request["at"] = prior + timedelta(minutes=1)
        drain = CoordinationRuntimeDrain.start(
            drain_id=plan.drain_id,
            tenant_id=fixture.request["tenant_id"],
            task_id=fixture.request["task_id"],
            triggering_run_id=plan.audit_anchor_run_id,
            target=CoordinationRuntimeDrainTarget.CANCELED,
            reason=plan.effective_reason,
            at=prior,
        )
        execution = fixture.locker.aggregate.executions[0].apply_observation(
            phase=RuntimeExecutionPhase.CANCEL_REQUESTED,
            provider_sequence=None,
            now=prior,
        )
        lifecycle = RuntimeLifecycleIntent(
            id=uuid4(),
            tenant_id=fixture.request["tenant_id"],
            runtime_execution_id=execution.id,
            operation_id=f"runtime-cancel:{execution.id}:v1",
            operation=RuntimeLifecycleOperation.CANCEL,
            intent_digest="c" * 64,
            status=RuntimeLifecycleStatus.REQUESTED,
            deadline=fixture.request["at"] + timedelta(minutes=5),
            receipt_summary=None,
            version=1,
            created_at=prior,
            updated_at=prior,
            next_attempt_at=prior,
        )
        fixture.locker.aggregate = replace(
            fixture.locker.aggregate,
            active_drain=drain,
            executions=(execution,),
            lifecycle_operations=(lifecycle,),
        )
        fixture.factory.drains[drain.id] = deepcopy(drain)
        lifecycle_event = lifecycle_outbox_envelope(
            tenant_id=fixture.request["tenant_id"],
            execution_id=execution.id,
            operation_id=lifecycle.operation_id,
            deadline=lifecycle.deadline,
            at=prior,
        )
        fixture.factory.store.outbox.append(lifecycle_event)
        lifecycle_ids = (lifecycle.id,)
    applier = _ActiveApplier(
        fixture.factory.operations,
        fail=fail_on == "applier.apply",
        no_progress=no_progress,
        lifecycle_operation_ids=lifecycle_ids,
    )
    fixture.service = CoordinatedRuntimeControlService(
        uow_factory=fixture.factory,
        aggregate_locker=fixture.locker,
        cancel_applier=applier,
        cancel_deadline_window=timedelta(minutes=5),
    )
    fixture.applier = applier
    return fixture


@pytest.mark.parametrize(
    "status",
    (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELED),
)
def test_terminal_fresh_writes_one_audit_and_task_idempotency(status: TaskStatus) -> None:
    fixture = _fixture(status)

    result = fixture.service.request_cancel(**fixture.request)

    assert result.kind is CoordinatedCancelKind.ALREADY_TERMINAL
    assert result.task_status is status
    assert result.audit_event_id is not None
    assert result.drain_id is None
    assert fixture.factory.operations == [
        "aggregate.lock",
        "idempotency.lock",
        "idempotency.get",
        "outbox.add",
        "idempotency.add",
        "commit",
    ]
    assert len(fixture.factory.store.outbox) == 1
    assert fixture.factory.store.outbox[0].message_id == result.audit_event_id
    assert fixture.factory.store.outbox[0].payload["business_state_changed"] is False
    assert len(fixture.factory.store.idempotency) == 1
    ((scope, key), record), = fixture.factory.store.idempotency.items()
    assert scope.startswith("coordinated-runtime-cancel:")
    assert key == "cancel-1"
    assert record.result["task_status"] == status.value


def test_terminal_exact_replay_validates_full_projection_and_does_not_commit() -> None:
    fixture = _fixture(TaskStatus.COMPLETED)
    first = fixture.service.request_cancel(**fixture.request)
    fixture.factory.operations.clear()

    replay = fixture.service.request_cancel(
        **{**fixture.request, "at": fixture.request["at"] + timedelta(hours=1)}
    )

    assert replay.kind is CoordinatedCancelKind.REPLAY
    assert replay.task_status is TaskStatus.COMPLETED
    assert replay.audit_event_id == first.audit_event_id
    assert fixture.factory.operations == [
        "aggregate.lock",
        "idempotency.lock",
        "idempotency.get",
        "outbox.get",
    ]
    assert fixture.factory.commits == 1
    assert len(fixture.factory.store.outbox) == 1
    assert len(fixture.factory.store.idempotency) == 1


def test_aware_non_utc_timestamp_is_normalized_in_audit() -> None:
    fixture = _fixture(TaskStatus.COMPLETED)
    local_time = datetime(2026, 9, 15, 18, 0, tzinfo=timezone(timedelta(hours=8)))

    result = fixture.service.request_cancel(**{**fixture.request, "at": local_time})

    audit = fixture.factory.store.outbox[0]
    assert result.audit_event_id == audit.message_id
    assert audit.occurred_at == datetime(2026, 9, 15, 10, 0, tzinfo=UTC)
    assert audit.payload["tenant_id"] == fixture.request["tenant_id"]


def test_same_key_different_request_conflicts_without_writes() -> None:
    fixture = _fixture(TaskStatus.FAILED)
    fixture.service.request_cancel(**fixture.request)
    fixture.factory.operations.clear()

    with pytest.raises(IdempotencyConflict):
        fixture.service.request_cancel(**{**fixture.request, "reason": "operator.changed"})

    assert fixture.factory.operations == [
        "aggregate.lock",
        "idempotency.lock",
        "idempotency.get",
    ]
    assert fixture.factory.commits == 1
    assert len(fixture.factory.store.outbox) == 1


@pytest.mark.parametrize("corruption", ("idempotency", "outbox"))
def test_partial_or_corrupt_replay_fails_closed(corruption: str) -> None:
    fixture = _fixture(TaskStatus.CANCELED)
    result = fixture.service.request_cancel(**fixture.request)
    if corruption == "idempotency":
        identity = next(iter(fixture.factory.store.idempotency))
        record = fixture.factory.store.idempotency[identity]
        fixture.factory.store.idempotency[identity] = replace(
            record,
            result={key: value for key, value in record.result.items() if key != "task_status"},
        )
    else:
        fixture.factory.store.outbox.clear()
    fixture.factory.operations.clear()

    with pytest.raises(RuntimeExecutionConflict):
        fixture.service.request_cancel(**fixture.request)

    assert "commit" not in fixture.factory.operations
    assert fixture.factory.commits == 1
    assert result.audit_event_id is not None


def test_active_fresh_applies_after_idempotency_read_and_commits_once() -> None:
    fixture = _active_fixture()

    result = fixture.service.request_cancel(**fixture.request)

    assert result.kind is CoordinatedCancelKind.APPLIED
    assert result.task_status is TaskStatus.CANCELED
    assert result.reason == "operator.requested"
    assert result.effective_target is CoordinationRuntimeDrainTarget.CANCELED
    assert fixture.factory.operations == [
        "aggregate.lock",
        "idempotency.lock",
        "idempotency.get",
        "applier.apply",
        "drain.add",
        "outbox.add",
        "idempotency.add",
        "commit",
    ]
    assert fixture.applier.calls == 1
    assert fixture.factory.commits == 1
    assert len(fixture.factory.store.outbox) == 1
    assert len(fixture.factory.store.idempotency) == 1
    assert len(fixture.factory.drains) == 1


def test_active_exact_replay_is_not_misclassified_after_task_becomes_terminal() -> None:
    fixture = _active_fixture()
    first = fixture.service.request_cancel(**fixture.request)
    fixture.factory.operations.clear()

    replay = fixture.service.request_cancel(
        **{**fixture.request, "at": fixture.request["at"] + timedelta(minutes=1)}
    )

    assert replay.kind is CoordinatedCancelKind.REPLAY
    assert replay.task_status is TaskStatus.CANCELED
    assert replay.drain_id == first.drain_id
    assert replay.audit_event_id == first.audit_event_id
    assert replay.reason == first.reason
    assert fixture.applier.calls == 1
    assert fixture.factory.commits == 1
    assert fixture.factory.operations == [
        "aggregate.lock",
        "idempotency.lock",
        "idempotency.get",
        "drain.get",
        "outbox.get",
    ]


def test_active_no_progress_audit_and_replay_use_persisted_lifecycle_epoch() -> None:
    fixture = _active_fixture(no_progress=True)

    first = fixture.service.request_cancel(**fixture.request)
    audit = next(
        value
        for value in fixture.factory.store.outbox
        if value.schema_name == "agentmesh.runtime.coordinated-cancel-requested"
    )

    assert first.kind is CoordinatedCancelKind.DRAINING_ACTIVE
    assert audit.payload["business_state_changed"] is False
    assert len(first.lifecycle_operation_ids) == 1
    fixture.factory.operations.clear()

    replay = fixture.service.request_cancel(
        **{**fixture.request, "at": fixture.request["at"] + timedelta(minutes=1)}
    )

    assert replay.kind is CoordinatedCancelKind.REPLAY
    assert replay.task_status is TaskStatus.RUNNING
    assert replay.lifecycle_operation_ids == first.lifecycle_operation_ids
    assert fixture.applier.calls == 1
    assert fixture.factory.commits == 1
    assert "commit" not in fixture.factory.operations


def test_active_replay_accepts_verified_monotonic_terminal_convergence() -> None:
    fixture = _active_fixture(no_progress=True)
    first = fixture.service.request_cancel(**fixture.request)
    completed = fixture.locker.aggregate.active_drain.complete(
        at=fixture.request["at"] + timedelta(seconds=1)
    )
    fixture.factory.drains[completed.id] = deepcopy(completed)
    fixture.locker.aggregate.task.cancel_coordination_from_control(
        completed,
        at=fixture.request["at"] + timedelta(seconds=1),
    )
    fixture.locker.aggregate = replace(
        fixture.locker.aggregate,
        boundary_classifications={
            run.id: CoordinationRuntimeBoundary.KNOWN_TERMINAL
            for run in fixture.locker.aggregate.runs
        },
    )
    fixture.factory.operations.clear()

    replay = fixture.service.request_cancel(**fixture.request)

    assert replay.kind is CoordinatedCancelKind.REPLAY
    assert replay.task_status is TaskStatus.CANCELED
    assert replay.drain_id == first.drain_id
    assert fixture.applier.calls == 1
    assert fixture.factory.commits == 1
    assert "commit" not in fixture.factory.operations


def test_active_replay_rejects_terminal_task_with_unfinished_sibling() -> None:
    fixture = _active_fixture(no_progress=True)
    fixture.service.request_cancel(**fixture.request)
    completed = fixture.locker.aggregate.active_drain.complete(
        at=fixture.request["at"] + timedelta(seconds=1)
    )
    fixture.factory.drains[completed.id] = deepcopy(completed)
    fixture.locker.aggregate.task.cancel_coordination_from_control(
        completed,
        at=fixture.request["at"] + timedelta(seconds=1),
    )

    with pytest.raises(RuntimeExecutionConflict, match="unfinished members"):
        fixture.service.request_cancel(**fixture.request)

    assert fixture.applier.calls == 1
    assert fixture.factory.commits == 1


def test_active_same_key_different_hash_conflicts_before_applier() -> None:
    fixture = _active_fixture()
    fixture.service.request_cancel(**fixture.request)
    fixture.factory.operations.clear()

    with pytest.raises(IdempotencyConflict):
        fixture.service.request_cancel(**{**fixture.request, "reason": "operator.changed"})

    assert fixture.applier.calls == 1
    assert fixture.factory.operations == [
        "aggregate.lock",
        "idempotency.lock",
        "idempotency.get",
    ]


@pytest.mark.parametrize("corruption", ("drain", "audit", "idempotency"))
def test_active_replay_partial_projection_fails_closed(corruption: str) -> None:
    fixture = _active_fixture()
    fixture.service.request_cancel(**fixture.request)
    if corruption == "drain":
        fixture.factory.drains.clear()
    elif corruption == "audit":
        fixture.factory.store.outbox.clear()
    else:
        identity = next(iter(fixture.factory.store.idempotency))
        record = fixture.factory.store.idempotency[identity]
        fixture.factory.store.idempotency[identity] = replace(
            record,
            result={
                key: value
                for key, value in record.result.items()
                if key != "audit_anchor_run_id"
            },
        )
    fixture.factory.operations.clear()

    with pytest.raises(RuntimeExecutionConflict):
        fixture.service.request_cancel(**fixture.request)

    assert fixture.applier.calls == 1
    assert fixture.factory.commits == 1
    assert "commit" not in fixture.factory.operations


def test_non_mapping_idempotency_projection_fails_closed() -> None:
    fixture = _active_fixture()
    fixture.service.request_cancel(**fixture.request)
    identity = next(iter(fixture.factory.store.idempotency))
    record = fixture.factory.store.idempotency[identity]
    fixture.factory.store.idempotency[identity] = replace(record, result=[])

    with pytest.raises(RuntimeExecutionConflict, match="projection is invalid"):
        fixture.service.request_cancel(**fixture.request)

    assert fixture.applier.calls == 1
    assert fixture.factory.commits == 1


@pytest.mark.parametrize(
    "fail_on",
    ("applier.apply", "outbox.add", "idempotency.add", "commit"),
)
def test_active_application_failures_leave_no_persisted_projection(fail_on: str) -> None:
    fixture = _active_fixture(fail_on=fail_on)

    with pytest.raises(RuntimeError, match="injected"):
        fixture.service.request_cancel(**fixture.request)

    assert fixture.factory.commits == 0
    assert not fixture.factory.store.outbox
    assert not fixture.factory.store.idempotency
    assert not fixture.factory.drains


@pytest.mark.parametrize(
    "override, error",
    (
        ({"tenant_id": " tenant-a"}, InvalidTaskInput),
        ({"task_id": "not-a-uuid"}, InvalidTaskInput),
        ({"idempotency_key": " "}, InvalidTaskInput),
        ({"reason": "contains secret"}, InvalidTaskInput),
        ({"at": datetime(2026, 9, 15, 10, 0)}, InvalidTaskInput),
        ({"causation_id": "not-a-uuid"}, InvalidTaskInput),
        ({"principal": _principal(authenticated=False)}, AuthorizationDenied),
        ({"principal": _principal(tenant_id="tenant-b")}, AuthorizationDenied),
        ({"principal": _principal(role=Role.AUDITOR)}, AuthorizationDenied),
    ),
)
def test_input_and_authorization_fail_before_uow(override, error) -> None:
    fixture = _fixture(TaskStatus.COMPLETED)

    with pytest.raises(error):
        fixture.service.request_cancel(**{**fixture.request, **override})

    assert fixture.factory.opens == 0
    assert fixture.factory.operations == []


@pytest.mark.parametrize("fail_on", ("outbox.add", "idempotency.add"))
def test_terminal_audit_and_idempotency_rollback_together(fail_on: str) -> None:
    fixture = _fixture(TaskStatus.COMPLETED, fail_on=fail_on)

    with pytest.raises(RuntimeError, match="injected"):
        fixture.service.request_cancel(**fixture.request)

    assert fixture.factory.operations[-1] == fail_on
    assert fixture.factory.commits == 0
    assert not fixture.factory.store.outbox
    assert not fixture.factory.store.idempotency
