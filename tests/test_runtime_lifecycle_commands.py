from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from agentmesh.application.runtime_services import RuntimeRegistryService
from agentmesh.domain.errors import InvalidTaskInput, RuntimeExecutionConflict
from agentmesh.domain.runtime_execution import (
    RuntimeExecution,
    RuntimeLifecycleOperation,
    RuntimeLifecycleStatus,
)
from agentmesh.features import FeatureGateSet


class _RuntimeRepo:
    def __init__(self, execution):
        self.execution = execution
        self.lifecycle = None

    def get_execution(self, execution_id, *, tenant_id, for_update=False):
        return self.execution if execution_id == self.execution.id else None

    def find_lifecycle_operation(self, execution_id, *, tenant_id, operation_id, for_update=False):
        if (
            self.lifecycle is not None
            and self.lifecycle.runtime_execution_id == execution_id
            and self.lifecycle.operation_id == operation_id
        ):
            return self.lifecycle
        return None

    def add_lifecycle_operation(self, value):
        self.lifecycle = value

    def save_execution(self, value, *, tenant_id):
        self.execution = value

    def update_lifecycle_status(self, value, *, status, now):
        self.lifecycle = value.__class__(
            **{
                **value.__dict__,
                "status": status,
                "updated_at": now,
                "version": value.version + 1,
            }
        )


class _Outbox:
    def __init__(self):
        self.events = []

    def add(self, event):
        self.events.append(event)


class _Uow:
    def __init__(self, repo, outbox):
        self.runtimes = repo
        self.outbox = outbox
        self.commit_count = 0
        self._snapshot = None

    def __enter__(self):
        self._snapshot = (
            self.runtimes.execution,
            self.runtimes.lifecycle,
            list(self.outbox.events),
        )
        return self

    def __exit__(self, exc_type, *_):
        if exc_type is not None and self._snapshot is not None:
            self.runtimes.execution, self.runtimes.lifecycle, events = self._snapshot
            self.outbox.events[:] = events
        return None

    def commit(self):
        self.commit_count += 1


def _service_fixture():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    execution = RuntimeExecution.prepare(
        tenant_id="tenant-a",
        run_id=uuid4(),
        runtime_version_id=uuid4(),
        assignment_id=uuid4(),
        assignment_digest="a" * 64,
        dispatch_key="dispatch",
        dispatch_digest="b" * 64,
        now=now,
    )
    repo = _RuntimeRepo(execution)
    outbox = _Outbox()
    service = RuntimeRegistryService(
        uow_factory=lambda: _Uow(repo, outbox),
        tenant_id="tenant-a",
        feature_gates=FeatureGateSet.from_config("full", "managed_agent_runtime=true"),
    )
    return now, execution, repo, outbox, service


def test_lifecycle_command_exact_replay_after_deadline_has_one_row_and_outbox():
    now, execution, repo, outbox, service = _service_fixture()
    deadline = now + timedelta(seconds=1)

    first = service.request_lifecycle_operation(
        execution_id=execution.id,
        operation_id=f"runtime-cancel:{execution.id}:v1",
        operation=RuntimeLifecycleOperation.CANCEL,
        deadline=deadline,
        now=now,
    )
    replay = service.request_lifecycle_operation(
        execution_id=execution.id,
        operation_id=f"runtime-cancel:{execution.id}:v1",
        operation=RuntimeLifecycleOperation.CANCEL,
        deadline=deadline.astimezone(timezone(timedelta(hours=8))),
        now=now + timedelta(minutes=5),
    )

    assert first is RuntimeLifecycleStatus.REQUESTED
    assert replay is first
    assert repo.lifecycle is not None
    assert len(outbox.events) == 1


def test_lifecycle_command_changed_identity_and_naive_deadline_fail_closed():
    now, execution, _, _, service = _service_fixture()
    operation_id = f"runtime-cancel:{execution.id}:v1"
    deadline = now + timedelta(minutes=1)
    service.request_lifecycle_operation(
        execution_id=execution.id,
        operation_id=operation_id,
        operation=RuntimeLifecycleOperation.CANCEL,
        deadline=deadline,
        now=now,
    )

    with pytest.raises(RuntimeExecutionConflict):
        service.request_lifecycle_operation(
            execution_id=execution.id,
            operation_id=operation_id,
            operation=RuntimeLifecycleOperation.PAUSE,
            deadline=deadline,
            now=now,
        )
    with pytest.raises(RuntimeExecutionConflict):
        service.request_lifecycle_operation(
            execution_id=execution.id,
            operation_id=operation_id,
            operation=RuntimeLifecycleOperation.CANCEL,
            deadline=deadline + timedelta(seconds=1),
            now=now,
        )
    _, fresh_execution, _, _, fresh_service = _service_fixture()
    with pytest.raises(InvalidTaskInput, match="operation identity"):
        fresh_service.request_lifecycle_operation(
            execution_id=fresh_execution.id,
            operation_id=f"runtime-cancel:{fresh_execution.id}:v1",
            operation=RuntimeLifecycleOperation.CANCEL,
            deadline=datetime(2026, 1, 1, 0, 1),
            now=now,
        )
    with pytest.raises(InvalidTaskInput, match="timestamp"):
        service.request_lifecycle_operation(
            execution_id=execution.id,
            operation_id=operation_id,
            operation=RuntimeLifecycleOperation.CANCEL,
            deadline=deadline,
            now=datetime(2026, 1, 1, 0, 0),
        )


def test_lifecycle_command_in_uow_replay_is_one_row_and_one_outbox_without_helper_commit():
    now, execution, repo, outbox, service = _service_fixture()
    deadline = now + timedelta(seconds=1)
    uow = _Uow(repo, outbox)
    with uow:
        first = service.request_lifecycle_operation_in_uow(
            uow,
            execution_id=execution.id,
            operation_id=f"runtime-cancel:{execution.id}:v1",
            operation=RuntimeLifecycleOperation.CANCEL,
            deadline=deadline,
            now=now,
        )
        replay = service.request_lifecycle_operation_in_uow(
            uow,
            execution_id=execution.id,
            operation_id=f"runtime-cancel:{execution.id}:v1",
            operation=RuntimeLifecycleOperation.CANCEL,
            deadline=deadline,
            now=now + timedelta(minutes=5),
        )
        assert uow.commit_count == 0
        uow.commit()

    assert first is RuntimeLifecycleStatus.REQUESTED
    assert replay is first
    assert repo.lifecycle is not None
    assert len(outbox.events) == 1
    assert uow.commit_count == 1


def test_lifecycle_command_in_uow_changed_deadline_conflicts_without_mutation():
    now, execution, repo, outbox, service = _service_fixture()
    deadline = now + timedelta(minutes=1)
    with _Uow(repo, outbox) as uow:
        service.request_lifecycle_operation_in_uow(
            uow,
            execution_id=execution.id,
            operation_id=f"runtime-cancel:{execution.id}:v1",
            operation=RuntimeLifecycleOperation.CANCEL,
            deadline=deadline,
            now=now,
        )
        before = (repo.execution, repo.lifecycle, list(outbox.events))
        with pytest.raises(RuntimeExecutionConflict):
            service.request_lifecycle_operation_in_uow(
                uow,
                execution_id=execution.id,
                operation_id=f"runtime-cancel:{execution.id}:v1",
                operation=RuntimeLifecycleOperation.CANCEL,
                deadline=deadline + timedelta(seconds=1),
                now=now,
            )
        assert (repo.execution, repo.lifecycle, outbox.events) == before


def test_lifecycle_command_in_uow_rolls_back_without_persistence_on_caller_failure():
    now, execution, repo, outbox, service = _service_fixture()
    with pytest.raises(RuntimeError):
        with _Uow(repo, outbox) as uow:
            service.request_lifecycle_operation_in_uow(
                uow,
                execution_id=execution.id,
                operation_id=f"runtime-cancel:{execution.id}:v1",
                operation=RuntimeLifecycleOperation.CANCEL,
                deadline=now + timedelta(minutes=1),
                now=now,
            )
            raise RuntimeError("caller rollback")
    assert repo.execution == execution
    assert repo.lifecycle is None
    assert outbox.events == []
