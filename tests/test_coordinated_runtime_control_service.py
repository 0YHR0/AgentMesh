from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agentmesh.application.coordinated_runtime_control import CoordinatedCancelKind
from agentmesh.application.coordinated_runtime_control_service import (
    CoordinatedRuntimeControlService,
)
from agentmesh.domain.errors import (
    AuthorizationDenied,
    IdempotencyConflict,
    InvalidTaskInput,
    RuntimeExecutionConflict,
)
from agentmesh.domain.identity import PrincipalContext, PrincipalType, Role
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
        self.factory.commits += 1
        super().commit()


class _Factory:
    def __init__(self, *, fail_on: str | None = None) -> None:
        self.store = InMemoryStore()
        self.operations: list[str] = []
        self.opens = 0
        self.commits = 0
        self.fail_on = fail_on

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


def test_active_task_fails_before_idempotency_or_audit_mutation() -> None:
    fixture = _fixture(TaskStatus.RUNNING)

    with pytest.raises(RuntimeExecutionConflict, match="not implemented"):
        fixture.service.request_cancel(**fixture.request)

    assert fixture.factory.operations == ["aggregate.lock"]
    assert not fixture.factory.store.outbox
    assert not fixture.factory.store.idempotency
    assert fixture.factory.commits == 0


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
