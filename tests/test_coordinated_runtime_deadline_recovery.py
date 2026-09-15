from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agentmesh.application.coordinated_runtime_convergence import (
    CoordinatedKnownTerminalKind,
    CoordinatedKnownTerminalResult,
)
from agentmesh.application.coordinated_runtime_deadline_recovery import (
    CoordinatedRuntimeDeadlineRecoveryService,
)
from agentmesh.application.coordinated_runtime_unknown import (
    CoordinatedUnknownOutcomeKind,
    CoordinatedUnknownOutcomeResult,
)
from agentmesh.domain.coordination import CoordinationRuntimeDrainTarget, SubtaskStatus
from agentmesh.domain.errors import InvalidTaskTransition, RuntimeExecutionConflict
from agentmesh.domain.runtime_execution import RuntimeLifecycleStatus
from agentmesh.domain.tasks import RunStatus, TaskStatus
from agentmesh.runtime_sdk import RuntimeObservation, RuntimePhase
from agentmesh.runtime_sdk.canonical import canonical_digest
from tests.test_coordinated_runtime_barrier import _aggregate, _cancel_intent

UTC = timezone.utc


class _Locker:
    def __init__(self, aggregate):
        self.aggregate = aggregate
        self.calls = 0

    def lock(self, uow, *, tenant_id, task_id):
        self.calls += 1
        return self.aggregate


class _Uow:
    def __init__(self, aggregate):
        self.aggregate = aggregate
        self.commits = 0
        self.rollbacks = 0
        self.saved = []
        self.runtimes = SimpleNamespace(save_lifecycle_operation=self.saved.append)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, _value, _traceback):
        if exc_type is not None:
            self.rollbacks += 1
        return False

    def commit(self):
        self.commits += 1


class _Convergence:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def apply_known_terminal_in_uow(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


class _Unknown:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def park_unknown_in_uow(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def _claimed_aggregate():
    _task, target, aggregate = _aggregate()
    execution = target[3]
    now = max(
        datetime.now(UTC), execution.updated_at.astimezone(UTC) + timedelta(seconds=2)
    )
    intent = _cancel_intent(
        tenant_id=aggregate.task.tenant_id, execution_id=execution.id
    )
    intent = replace(
        intent,
        deadline=now - timedelta(seconds=1),
        updated_at=now - timedelta(seconds=1),
    )
    claimed = intent.claim_for_deadline(now=now, lease=timedelta(seconds=30))
    return replace(aggregate, lifecycle_operations=(claimed,)), target, claimed, now


def _terminal_result(aggregate, target, now):
    execution = target[3]
    observation = RuntimeObservation(
        observation_id=str(uuid4()),
        runtime_execution_id=str(execution.id),
        assignment_id=str(execution.assignment_id),
        assignment_digest=execution.assignment_digest,
        phase=RuntimePhase.SUCCEEDED,
        observed_at=now,
        provider_event_id="deadline-recovery-test",
        provider_sequence=2,
        output={"ok": True},
    )
    return observation, CoordinatedKnownTerminalResult(
        kind=CoordinatedKnownTerminalKind.APPLIED,
        tenant_id=aggregate.task.tenant_id,
        task_id=aggregate.task.id,
        run_id=target[1].id,
        attempt_id=target[2].id,
        runtime_execution_id=execution.id,
        observation_id=observation.observation_id,
        observation_digest=canonical_digest(observation.to_dict()),
        task_status=TaskStatus.RUNNING,
        run_status=RunStatus.RUNNING,
        subtask_status=SubtaskStatus.RUNNING,
    )


def test_deadline_recovery_terminal_clears_claim_in_same_uow():
    aggregate, target, claimed, now = _claimed_aggregate()
    observation, result = _terminal_result(aggregate, target, now + timedelta(seconds=1))
    uow = _Uow(aggregate)
    convergence = _Convergence(result)
    unknown = _Unknown(None)
    service = CoordinatedRuntimeDeadlineRecoveryService(
        uow_factory=lambda: uow,
        convergence_service=convergence,
        unknown_service=unknown,
        aggregate_locker=_Locker(aggregate),
    )

    returned = service.finalize(
        aggregate.task.tenant_id,
        aggregate.task.id,
        target[1].id,
        target[2].id,
        target[2].fencing_token,
        target[3].id,
        claimed.operation_id,
        claimed.claim_token,
        observation,
        now + timedelta(seconds=1),
    )

    assert returned is result
    assert len(convergence.calls) == 1
    assert unknown.calls == []
    assert len(uow.saved) == 1
    assert uow.saved[0].status is RuntimeLifecycleStatus.REQUESTED
    assert uow.saved[0].claim_token is None
    assert uow.commits == 1


def test_deadline_recovery_uncertain_inspection_expires_then_parks_unknown():
    aggregate, target, claimed, now = _claimed_aggregate()
    execution = target[3]
    unknown_result = CoordinatedUnknownOutcomeResult(
        kind=CoordinatedUnknownOutcomeKind.PARKED,
        tenant_id=aggregate.task.tenant_id,
        task_id=aggregate.task.id,
        run_id=target[1].id,
        attempt_id=target[2].id,
        runtime_execution_id=execution.id,
        observation_id="unknown-recovery",
        observation_digest="a" * 64,
        task_status=TaskStatus.RECONCILIATION_REQUIRED,
        run_status=RunStatus.RECONCILIATION_REQUIRED,
        subtask_status=SubtaskStatus.RECONCILIATION_REQUIRED,
        drain_id=uuid4(),
        drain_target=CoordinationRuntimeDrainTarget.RUNNING,
    )
    uow = _Uow(aggregate)
    convergence = _Convergence(None)
    unknown = _Unknown(unknown_result)
    service = CoordinatedRuntimeDeadlineRecoveryService(
        uow_factory=lambda: uow,
        convergence_service=convergence,
        unknown_service=unknown,
        aggregate_locker=_Locker(aggregate),
    )

    returned = service.finalize(
        aggregate.task.tenant_id,
        aggregate.task.id,
        target[1].id,
        target[2].id,
        target[2].fencing_token,
        execution.id,
        claimed.operation_id,
        claimed.claim_token,
        None,
        now,
    )

    assert returned is unknown_result
    assert convergence.calls == []
    assert len(unknown.calls) == 1
    assert (
        unknown.calls[0]["aggregate"].lifecycle_operations[0].status
        is RuntimeLifecycleStatus.EXPIRED
    )
    assert uow.saved[0].status is RuntimeLifecycleStatus.EXPIRED
    assert uow.commits == 1


def test_deadline_recovery_stale_claim_fails_before_any_write():
    aggregate, target, claimed, now = _claimed_aggregate()
    uow = _Uow(aggregate)
    convergence = _Convergence(None)
    unknown = _Unknown(None)
    service = CoordinatedRuntimeDeadlineRecoveryService(
        uow_factory=lambda: uow,
        convergence_service=convergence,
        unknown_service=unknown,
        aggregate_locker=_Locker(aggregate),
    )

    with pytest.raises(RuntimeExecutionConflict):
        service.finalize(
            aggregate.task.tenant_id,
            aggregate.task.id,
            target[1].id,
            target[2].id,
            target[2].fencing_token,
            target[3].id,
            claimed.operation_id,
            uuid4(),
            None,
            now,
        )

    assert uow.saved == []
    assert uow.commits == 0
    assert convergence.calls == []
    assert unknown.calls == []


def test_deadline_recovery_expired_claim_fails_before_any_write():
    aggregate, target, claimed, _now = _claimed_aggregate()
    uow = _Uow(aggregate)
    convergence = _Convergence(None)
    unknown = _Unknown(None)
    service = CoordinatedRuntimeDeadlineRecoveryService(
        uow_factory=lambda: uow,
        convergence_service=convergence,
        unknown_service=unknown,
        aggregate_locker=_Locker(aggregate),
    )

    with pytest.raises(RuntimeExecutionConflict, match="stale"):
        service.finalize(
            aggregate.task.tenant_id,
            aggregate.task.id,
            target[1].id,
            target[2].id,
            target[2].fencing_token,
            target[3].id,
            claimed.operation_id,
            claimed.claim_token,
            None,
            claimed.claim_expires_at,
        )

    assert uow.saved == []
    assert uow.commits == 0
    assert convergence.calls == []
    assert unknown.calls == []


def test_deadline_recovery_unknown_failure_rolls_back_expiry():
    aggregate, target, claimed, now = _claimed_aggregate()
    uow = _Uow(aggregate)
    service = CoordinatedRuntimeDeadlineRecoveryService(
        uow_factory=lambda: uow,
        convergence_service=_Convergence(None),
        unknown_service=_Unknown(None),
        aggregate_locker=_Locker(aggregate),
    )

    with pytest.raises(InvalidTaskTransition):
        service.finalize(
            aggregate.task.tenant_id,
            aggregate.task.id,
            target[1].id,
            target[2].id,
            target[2].fencing_token,
            target[3].id,
            claimed.operation_id,
            claimed.claim_token,
            None,
            now,
        )

    assert len(uow.saved) == 1
    assert uow.saved[0].status is RuntimeLifecycleStatus.EXPIRED
    assert uow.commits == 0
    assert uow.rollbacks == 1
