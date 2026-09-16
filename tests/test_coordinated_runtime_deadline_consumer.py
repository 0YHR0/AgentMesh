from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from agentmesh.application.coordinated_runtime_deadline_consumer import (
    CoordinatedRuntimeDeadlineConsumer,
)
from agentmesh.application.runtime_snapshots import handle_snapshot_for
from agentmesh.domain.errors import RuntimeExecutionConflict
from agentmesh.domain.runtime_execution import RuntimeLifecycleStatus
from agentmesh.domain.tasks import TaskExecutionMode
from agentmesh.features import FeatureGateSet
from agentmesh.runtime_sdk import RuntimeExecutionHandle, RuntimeObservation, RuntimePhase
from tests.test_coordinated_runtime_barrier import _aggregate, _cancel_intent

UTC = timezone.utc


class _Repo:
    def __init__(self, fixture):
        self.fixture = fixture

    def claim_deadline_lifecycle(self, **kwargs):
        value = self.fixture.lifecycle
        if (
            value.status is RuntimeLifecycleStatus.EXPIRED
            or value.deadline > kwargs["now"]
            or (
                value.claim_token is not None
                and value.claim_expires_at > kwargs["now"]
            )
            or (
                kwargs["execution_id"] is not None
                and kwargs["execution_id"] != value.runtime_execution_id
            )
            or (
                kwargs["operation_id"] is not None
                and kwargs["operation_id"] != value.operation_id
            )
        ):
            return None
        self.fixture.lifecycle = value.claim_for_deadline(
            now=kwargs["now"], lease=kwargs["lease"]
        )
        return self.fixture.lifecycle

    def get_execution(self, execution_id, *, tenant_id):
        if self.fixture.missing_identity or tenant_id != self.fixture.tenant_id:
            return None
        return (
            self.fixture.execution
            if execution_id == self.fixture.execution.id
            else None
        )

    def get_handle_snapshot(self, execution_id, *, tenant_id):
        if tenant_id != self.fixture.tenant_id or execution_id != self.fixture.execution.id:
            return None
        return self.fixture.handle_snapshot

    def find_lifecycle_operation(
        self, execution_id, *, tenant_id, operation_id, for_update=False
    ):
        self.fixture.operations.append(("lifecycle.find", for_update))
        value = self.fixture.lifecycle
        if (
            tenant_id == value.tenant_id
            and execution_id == value.runtime_execution_id
            and operation_id == value.operation_id
        ):
            return value
        return None

    def save_lifecycle_operation(self, value):
        self.fixture.lifecycle = value


class _Uow:
    def __init__(self, fixture):
        self.fixture = fixture
        self.runtimes = _Repo(fixture)
        self.runs = _Lookup(fixture.run)
        self.tasks = _Lookup(fixture.task)
        self.attempts = _Attempts(fixture)

    def __enter__(self):
        self.fixture.active_uows += 1
        return self

    def __exit__(self, *_args):
        self.fixture.active_uows -= 1
        return False

    def commit(self):
        self.fixture.commits += 1


class _Lookup:
    def __init__(self, value):
        self.value = value

    def get(self, identity, **_kwargs):
        return self.value if self.value.id == identity else None


class _Attempts:
    def __init__(self, fixture):
        self.fixture = fixture

    def latest_for_run(self, run_id):
        if self.fixture.missing_identity:
            return None
        return self.fixture.attempt if self.fixture.attempt.run_id == run_id else None


class _Adapter:
    def __init__(self, fixture, observation=None, *, raises=False):
        self.fixture = fixture
        self.observation = observation
        self.raises = raises
        self.calls = 0

    def inspect(self, _handle):
        assert self.fixture.active_uows == 0
        self.calls += 1
        if self.raises:
            raise RuntimeError("inspection transport failed")
        return self.observation


class _Recovery:
    def __init__(self, fixture, *, stale=False, enforce_lease=False):
        self.fixture = fixture
        self.stale = stale
        self.enforce_lease = enforce_lease
        self.calls = []

    def finalize(self, *args):
        assert self.fixture.active_uows == 0
        self.calls.append(args)
        if self.stale or (
            self.enforce_lease
            and self.fixture.lifecycle.claim_expires_at <= args[-1]
        ):
            raise RuntimeExecutionConflict("stale deadline claim")
        self.fixture.lifecycle = self.fixture.lifecycle.expire(
            now=args[-1], error_code="test.finalized"
        )
        inspection = args[-2]
        return "terminal" if inspection is not None else "unknown"


class _Fixture:
    pass


def _fixture(
    *,
    terminal=True,
    inspect_raises=False,
    missing_handle=False,
    damaged_handle=False,
):
    task, target, _aggregate_value = _aggregate()
    _subtask, run, attempt, execution = target
    now = max(datetime.now(UTC), execution.updated_at + timedelta(seconds=2))
    lifecycle = _cancel_intent(tenant_id=task.tenant_id, execution_id=execution.id)
    lifecycle = replace(
        lifecycle,
        deadline=now - timedelta(seconds=1),
        updated_at=now - timedelta(seconds=1),
    )
    handle = RuntimeExecutionHandle(
        runtime_execution_id=str(execution.id),
        runtime_version_id=str(execution.runtime_version_id),
        provider_execution_ref="deadline-provider",
        assignment_id=str(execution.assignment_id),
        assignment_digest=execution.assignment_digest,
        created_at=now - timedelta(seconds=2),
    )
    observation = None
    if terminal:
        observation = RuntimeObservation(
            observation_id=str(uuid4()),
            runtime_execution_id=str(execution.id),
            assignment_id=str(execution.assignment_id),
            assignment_digest=execution.assignment_digest,
            phase=RuntimePhase.SUCCEEDED,
            observed_at=now,
            provider_event_id="deadline-provider-terminal",
            output={"ok": True},
        )
    fixture = _Fixture()
    fixture.tenant_id = task.tenant_id
    fixture.task = task
    fixture.run = run
    fixture.attempt = attempt
    fixture.execution = execution
    fixture.lifecycle = lifecycle
    fixture.handle_snapshot = (
        None if missing_handle else handle_snapshot_for(handle, tenant_id=task.tenant_id)
    )
    if damaged_handle:
        fixture.handle_snapshot = object()
    fixture.missing_identity = False
    fixture.active_uows = 0
    fixture.commits = 0
    fixture.operations = []
    fixture.adapter = _Adapter(
        fixture,
        observation,
        raises=inspect_raises,
    )
    fixture.recovery = _Recovery(fixture)
    fixture.service = CoordinatedRuntimeDeadlineConsumer(
        uow_factory=lambda: _Uow(fixture),
        tenant_id=fixture.tenant_id,
        feature_gates=FeatureGateSet.from_config(
            "full", "managed_agent_runtime=true"
        ),
        adapter=fixture.adapter,
        recovery_service=fixture.recovery,
        clock=lambda: now,
    )
    fixture.now = now
    return fixture


def test_deadline_consumer_inspects_outside_uow_and_routes_terminal():
    fixture = _fixture()

    result = fixture.service.process_next_deadline(now=fixture.now)

    assert result.finalized is True
    assert result.convergence_result == "terminal"
    assert fixture.adapter.calls == len(fixture.recovery.calls) == 1
    assert fixture.commits == 1
    assert fixture.recovery.calls[0][1:6] == (
        fixture.task.id,
        fixture.run.id,
        fixture.attempt.id,
        fixture.attempt.fencing_token,
        fixture.execution.id,
    )


@pytest.mark.parametrize("handle_state", ["available", "missing", "damaged"])
def test_deadline_consumer_routes_failed_or_missing_inspection_to_unknown(
    handle_state,
):
    fixture = _fixture(
        terminal=False,
        inspect_raises=handle_state == "available",
        missing_handle=handle_state == "missing",
        damaged_handle=handle_state == "damaged",
    )

    result = fixture.service.process_deadline(
        fixture.execution.id,
        operation_id=fixture.lifecycle.operation_id,
        now=fixture.now,
    )

    assert result.convergence_result == "unknown"
    assert fixture.recovery.calls[0][-2] is None
    assert fixture.adapter.calls == (1 if handle_state == "available" else 0)


def test_deadline_consumer_repeated_claim_does_not_inspect_twice():
    fixture = _fixture()

    first = fixture.service.process_next_deadline(now=fixture.now)
    second = fixture.service.process_next_deadline(now=fixture.now)

    assert first.finalized is True
    assert second.operation is None and second.finalized is False
    assert fixture.adapter.calls == len(fixture.recovery.calls) == 1


def test_deadline_consumer_missing_identity_expires_claim_without_finalize():
    fixture = _fixture()
    fixture.missing_identity = True

    result = fixture.service.process_next_deadline(now=fixture.now)

    assert result.finalized is result.inspection_attempted is False
    assert fixture.adapter.calls == 0
    assert fixture.recovery.calls == []
    assert fixture.lifecycle.status is RuntimeLifecycleStatus.EXPIRED
    assert fixture.lifecycle.last_error_code == "runtime.deadline_target_invalid"
    assert fixture.commits == 2


@pytest.mark.parametrize("invalid_target", ["legacy", "direct"])
def test_deadline_consumer_expires_out_of_scope_target_without_inspection(
    invalid_target,
):
    fixture = _fixture()
    if invalid_target == "legacy":
        fixture.run = replace(fixture.run, runtime_authority="legacy")
    else:
        fixture.task = replace(
            fixture.task,
            execution_mode=TaskExecutionMode.DIRECT,
        )

    result = fixture.service.process_next_deadline(now=fixture.now)

    assert result.finalized is result.inspection_attempted is False
    assert fixture.adapter.calls == 0
    assert fixture.recovery.calls == []
    assert fixture.lifecycle.status is RuntimeLifecycleStatus.EXPIRED
    assert fixture.lifecycle.last_error_code == "runtime.deadline_target_invalid"


def test_deadline_consumer_stale_finalize_race_fails_closed():
    fixture = _fixture()
    fixture.recovery = _Recovery(fixture, stale=True)
    fixture.service._recovery = fixture.recovery

    with pytest.raises(RuntimeExecutionConflict, match="stale"):
        fixture.service.process_next_deadline(now=fixture.now)

    assert fixture.adapter.calls == 1
    assert len(fixture.recovery.calls) == 1
    assert fixture.lifecycle.claim_token is not None


def test_deadline_consumer_refreshes_clock_and_rejects_expired_inspection_claim():
    fixture = _fixture()
    clock = [fixture.now]
    fixture.service._clock = lambda: clock[0]
    original_inspect = fixture.adapter.inspect

    def inspect_then_expire(handle):
        observation = original_inspect(handle)
        clock[0] += timedelta(seconds=31)
        return observation

    fixture.adapter.inspect = inspect_then_expire
    fixture.recovery = _Recovery(fixture, enforce_lease=True)
    fixture.service._recovery = fixture.recovery

    with pytest.raises(RuntimeExecutionConflict, match="stale"):
        fixture.service.process_next_deadline()

    assert fixture.adapter.calls == 1
    assert len(fixture.recovery.calls) == 1
    assert fixture.recovery.calls[0][-1] == clock[0]
