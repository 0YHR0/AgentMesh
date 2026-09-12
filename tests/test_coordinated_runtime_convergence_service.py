from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest

from agentmesh.application.coordinated_runtime_barrier import (
    CoordinatedBarrierCompletion,
)
from agentmesh.application.coordinated_runtime_convergence import (
    CoordinatedKnownTerminalKind,
    CoordinatedRuntimeConvergenceService,
)
from agentmesh.domain.coordination import (
    CoordinationRuntimeBoundary,
    CoordinationRuntimeDrain,
    CoordinationRuntimeDrainTarget,
)
from agentmesh.domain.errors import RuntimeExecutionConflict
from agentmesh.domain.runtime_execution import (
    RuntimeObservationEvidence,
    RuntimeObservationOutcome,
)
from agentmesh.runtime_sdk import RuntimeObservation, RuntimePhase
from tests.test_coordinated_runtime_barrier import (
    _aggregate,
    _aggregate_for_sibling_boundaries,
    _cancel_intent,
)

UTC = timezone.utc


def _now_for(target) -> datetime:
    return max(
        datetime.now(UTC),
        target[3].updated_at.astimezone(UTC) + timedelta(seconds=2),
    )


def _observation(target, *, phase: RuntimePhase, now: datetime) -> RuntimeObservation:
    execution = target[3]
    return RuntimeObservation(
        observation_id=str(uuid4()),
        runtime_execution_id=str(execution.id),
        assignment_id=str(execution.assignment_id),
        assignment_digest=execution.assignment_digest,
        phase=phase,
        observed_at=now,
        provider_event_id="service-test-provider-event",
        provider_sequence=(execution.provider_sequence or 0) + 1,
        output={"ok": True} if phase is RuntimePhase.SUCCEEDED else None,
    )


class _State:
    def __init__(self, aggregate):
        self.aggregate = aggregate
        self.evidence: list[RuntimeObservationEvidence] = []
        self.operation_log: list[str] = []
        self.commits = 0
        self.rollbacks = 0
        self.task_saves = 0
        self.execution_saves = 0
        self.observation_adds = 0
        self.quota_release_reads = 0
        self.scheduler_calls: list[tuple[datetime, UUID]] = []
        self.drain: CoordinationRuntimeDrain | None = aggregate.active_drain
        self.outbox_count = 0
        self.lifecycle_count = 0

    def factory(self):
        return _Uow(self)


class _Uow:
    def __init__(self, state: _State):
        self.state = state
        self._snapshot = None
        self._pending_evidence: list[RuntimeObservationEvidence] = []
        self._committed = False
        self.tasks = SimpleNamespace(save=self._save_task)
        self.runs = SimpleNamespace(save=self._save_run)
        self.subtasks = SimpleNamespace(save=self._save_subtask)
        self.attempts = SimpleNamespace(save=self._save_attempt)
        self.coordination_runtime_drains = SimpleNamespace(
            get=self._get_drain,
            add=self._add_drain,
            save=self._save_drain,
        )
        self.quotas = SimpleNamespace(
            list_reservations_for_attempt=self._list_reservations,
            save_reservation=self._save_reservation,
        )
        self.runtimes = SimpleNamespace(
            prior_observations=self._prior_observations,
            accepted_terminal_observations=self._accepted_observations,
            add_observation=self._add_observation,
            save_execution=self._save_execution,
            add_lifecycle_operation=self._add_lifecycle,
        )
        self.outbox = SimpleNamespace(add=self._add_outbox)

    def __enter__(self):
        self.state.operation_log.append("uow.enter")
        # The fake's aggregate is the transaction-local object.  Restoring the
        # identity on rollback is sufficient for the pre-write conflict cases
        # covered here and avoids copying immutable mappingproxy projections.
        self._snapshot = self.state.aggregate
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is not None or not self._committed:
            self.state.rollbacks += 1
            self.state.operation_log.append("uow.rollback")
            if self._snapshot is not None:
                self.state.aggregate = self._snapshot
        return False

    def commit(self) -> None:
        self.state.commits += 1
        self.state.operation_log.append("uow.commit")
        self.state.evidence.extend(self._pending_evidence)
        self._committed = True

    def _save_task(self, _value) -> None:
        self.state.task_saves += 1
        self.state.operation_log.append("tasks.save")

    def _save_run(self, _value) -> None:
        self.state.operation_log.append("runs.save")

    def _save_subtask(self, _value) -> None:
        self.state.operation_log.append("subtasks.save")

    def _save_attempt(self, _value) -> None:
        self.state.operation_log.append("attempts.save")

    def _get_drain(self, _drain_id, *, tenant_id, for_update=False):
        self.state.operation_log.append("drains.get")
        if self.state.drain is not None and self.state.drain.tenant_id == tenant_id:
            return self.state.drain
        return None

    def _add_drain(self, drain, *, tenant_id):
        self.state.operation_log.append("drains.add")
        self.state.drain = drain

    def _save_drain(self, drain, *, tenant_id):
        self.state.operation_log.append("drains.save")
        self.state.drain = drain
        self.state.aggregate = replace(self.state.aggregate, active_drain=drain)

    def _list_reservations(self, _attempt_id, *, for_update=False):
        self.state.quota_release_reads += 1
        self.state.operation_log.append("quotas.list")
        return []

    def _save_reservation(self, _reservation):
        self.state.operation_log.append("quotas.save")

    def _prior_observations(self, execution_id, *, tenant_id, observation_id, digest):
        return [
            value
            for value in self.state.evidence
            if value.runtime_execution_id == execution_id
            and (value.observation_id == observation_id or value.observation_digest == digest)
        ]

    def _accepted_observations(self, execution_id, *, tenant_id, phase):
        return [
            value
            for value in self.state.evidence
            if value.runtime_execution_id == execution_id
            and value.phase is phase
            and value.processing_outcome is RuntimeObservationOutcome.APPLIED
        ]

    def _add_observation(self, evidence):
        self.state.observation_adds += 1
        self.state.operation_log.append("observations.add")
        self._pending_evidence.append(evidence)

    def _save_execution(self, execution, *, tenant_id):
        self.state.execution_saves += 1
        self.state.operation_log.append("executions.save")
        values = tuple(
            execution if value.id == execution.id else value
            for value in self.state.aggregate.executions
        )
        self.state.aggregate = replace(self.state.aggregate, executions=values)

    def _add_lifecycle(self, _value):
        self.state.lifecycle_count += 1
        self.state.operation_log.append("lifecycle.add")

    def _add_outbox(self, _value):
        self.state.outbox_count += 1
        self.state.operation_log.append("outbox.add")


class _Locker:
    def __init__(self, state: _State):
        self.state = state

    def lock(self, uow, *, tenant_id, task_id):
        assert not [value for value in self.state.operation_log if value.endswith(".get")]
        self.state.operation_log.append("locker.lock")
        return self.state.aggregate


class _Scheduler:
    def __init__(self, state: _State):
        self.state = state

    def schedule(self, uow, task, *, at, causation_id):
        assert uow is not None
        self.state.scheduler_calls.append((at, causation_id))
        self.state.operation_log.append("scheduler.schedule")
        return (SimpleNamespace(id=uuid4()), SimpleNamespace(id=uuid4()))


class _Barrier:
    def __init__(self, state: _State):
        self.state = state

    def apply_in_uow(
        self,
        uow,
        *,
        aggregate,
        plan,
        now,
        cancel_deadline_window,
        defer_task_save=False,
    ):
        self.state.operation_log.append("barrier.apply")
        drain = aggregate.active_drain
        if drain is None and plan.effective_target is not None:
            drain = CoordinationRuntimeDrain.start(
                drain_id=uuid5(
                    NAMESPACE_URL,
                    f"coordination-runtime-drain:{aggregate.task.tenant_id}:{aggregate.task.id}",
                ),
                tenant_id=aggregate.task.tenant_id,
                task_id=aggregate.task.id,
                triggering_run_id=plan.triggering_run_id,
                target=plan.effective_target,
                reason=plan.effective_reason or "runtime.failed",
                at=now,
            )
            self.state.drain = drain
        if plan.completion is CoordinatedBarrierCompletion.WAIT_ACTIVE:
            if any(action.kind.value == "REQUEST_CANCEL" for action in plan.sibling_actions):
                uow.runtimes.add_lifecycle_operation(object())
                uow.outbox.add(object())
        return SimpleNamespace(
            completion=plan.completion,
            effective_drain=drain,
            changed_ids=frozenset(),
        )


def _service_state(monkeypatch, aggregate, target):
    state = _State(aggregate)
    locker = _Locker(state)
    scheduler = _Scheduler(state)
    barrier = _Barrier(state)

    def select(_aggregate, **_kwargs):
        current_run = next(value for value in state.aggregate.runs if value.id == target[1].id)
        current_subtask = next(
            value for value in state.aggregate.subtasks if value.id == current_run.subtask_id
        )
        current_attempt = state.aggregate.latest_attempts[current_run.id]
        current_execution = next(
            value
            for value in state.aggregate.executions
            if value.id == current_run.runtime_execution_id
        )
        version = state.aggregate.runtime_versions[current_run.runtime_version_id]
        return current_subtask, current_run, current_attempt, current_execution, None, version

    monkeypatch.setattr(
        "agentmesh.application.coordinated_runtime_convergence._select_target", select
    )
    service = CoordinatedRuntimeConvergenceService(
        uow_factory=state.factory,
        coordinated_scheduler=scheduler,
        cancel_deadline_window=timedelta(minutes=5),
        aggregate_locker=locker,
        barrier_applier=barrier,
    )
    return state, service


def _call(service, target, *, phase, now, causation_id=None, observation=None):
    if observation is None:
        observation = _observation(target, phase=phase, now=now)
    return service.apply_known_terminal(
        tenant_id="tenant-a",
        task_id=target[1].task_id,
        run_id=target[1].id,
        attempt_id=target[2].id,
        fencing_token=target[2].fencing_token,
        runtime_execution_id=target[3].id,
        observation=observation,
        received_at=now + timedelta(seconds=1),
        causation_id=causation_id or uuid4(),
    ), observation


def test_service_success_applies_once_and_schedules_in_same_uow(monkeypatch) -> None:
    _task, target, aggregate = _aggregate()
    state, service = _service_state(monkeypatch, aggregate, target)
    causation_id = uuid4()
    now = _now_for(target)
    result, observation = _call(
        service,
        target,
        phase=RuntimePhase.SUCCEEDED,
        now=now,
        causation_id=causation_id,
    )
    assert result.kind is CoordinatedKnownTerminalKind.APPLIED
    assert result.run_status.value == "SUCCEEDED"
    assert result.subtask_status.value == "COMPLETED"
    assert result.drain_id is None
    assert len(state.evidence) == 1
    assert state.observation_adds == 1
    assert state.execution_saves == 1
    assert state.quota_release_reads == 1
    assert state.commits == 1
    assert state.rollbacks == 0
    assert state.scheduler_calls == [(now + timedelta(seconds=1), causation_id)]
    assert result.scheduled_run_ids == tuple(sorted(result.scheduled_run_ids, key=str))
    assert state.operation_log.index("locker.lock") < state.operation_log.index("executions.save")
    assert observation.observation_id == result.observation_id


@pytest.mark.parametrize(
    ("phase", "safe_code"),
    [
        (RuntimePhase.FAILED, "runtime.failed"),
        (RuntimePhase.TIMED_OUT, "runtime.timed_out"),
        (RuntimePhase.CANCELED, "runtime.unrequested_cancellation"),
    ],
)
def test_service_failure_phases_fail_target_and_complete_failed_drain(
    monkeypatch, phase, safe_code
) -> None:
    _task, target, aggregate = _aggregate()
    state, service = _service_state(monkeypatch, aggregate, target)
    now = _now_for(target)
    result, _observation_value = _call(service, target, phase=phase, now=now)
    assert result.kind is CoordinatedKnownTerminalKind.APPLIED
    assert result.drain_target is CoordinationRuntimeDrainTarget.FAILED
    assert result.run_status.value == "FAILED"
    assert result.subtask_status.value == "FAILED"
    assert state.aggregate.task.status.value == "FAILED"
    assert state.aggregate.task.error == safe_code
    assert state.scheduler_calls == []
    assert state.task_saves == 1
    assert state.commits == 1


def test_service_cancel_intent_without_sibling_is_prewrite_rejected(monkeypatch) -> None:
    _task, target, aggregate = _aggregate(drain_target=CoordinationRuntimeDrainTarget.CANCELED)
    intent = _cancel_intent(tenant_id="tenant-a", execution_id=target[3].id)
    aggregate = replace(aggregate, lifecycle_operations=(intent,))
    state, service = _service_state(monkeypatch, aggregate, target)
    now = _now_for(target)
    with pytest.raises(RuntimeExecutionConflict, match="unsupported barrier"):
        _call(service, target, phase=RuntimePhase.CANCELED, now=now)
    assert state.commits == 0
    assert state.observation_adds == 0
    assert state.execution_saves == 0
    assert state.task_saves == 0


def test_service_cancel_intent_with_crossed_sibling_cancels_target_and_waits(
    monkeypatch,
) -> None:
    _task, target, siblings, aggregate = _aggregate_for_sibling_boundaries(
        (CoordinationRuntimeBoundary.CROSSED_ACTIVE,),
        drain_target=CoordinationRuntimeDrainTarget.CANCELED,
    )
    intent = _cancel_intent(tenant_id="tenant-a", execution_id=target[3].id)
    aggregate = replace(aggregate, lifecycle_operations=(intent,))
    state, service = _service_state(monkeypatch, aggregate, target)
    result, _observation_value = _call(
        service, target, phase=RuntimePhase.CANCELED, now=_now_for(target)
    )
    assert result.kind is CoordinatedKnownTerminalKind.DRAINING_ACTIVE
    assert result.run_status.value == "CANCELED"
    assert result.subtask_status.value == "CANCELED"
    assert state.aggregate.task.status.value == "RUNNING"
    assert state.lifecycle_count == 1
    assert state.outbox_count == 1
    assert state.commits == 1
    assert siblings[0][1].id != target[1].id


def test_service_cancel_intent_without_drain_is_prewrite_conflict(monkeypatch) -> None:
    _task, target, aggregate = _aggregate()
    intent = _cancel_intent(tenant_id="tenant-a", execution_id=target[3].id)
    aggregate = replace(aggregate, lifecycle_operations=(intent,))
    state, service = _service_state(monkeypatch, aggregate, target)
    with pytest.raises(RuntimeExecutionConflict):
        _call(service, target, phase=RuntimePhase.CANCELED, now=_now_for(target))
    assert state.commits == 0
    assert state.observation_adds == 0
    assert state.execution_saves == 0
    assert state.task_saves == 0


@pytest.mark.parametrize(
    ("boundary", "kind", "task_status"),
    [
        (
            CoordinationRuntimeBoundary.CROSSED_ACTIVE,
            CoordinatedKnownTerminalKind.DRAINING_ACTIVE,
            "RUNNING",
        ),
        (
            CoordinationRuntimeBoundary.RECONCILIATION_EVIDENCE,
            CoordinatedKnownTerminalKind.DRAINING_RECONCILIATION,
            "RECONCILIATION_REQUIRED",
        ),
    ],
)
def test_service_failure_with_live_sibling_returns_closed_draining_result(
    monkeypatch, boundary, kind, task_status
) -> None:
    _task, target, _siblings, aggregate = _aggregate_for_sibling_boundaries((boundary,))
    state, service = _service_state(monkeypatch, aggregate, target)
    result, _observation_value = _call(
        service, target, phase=RuntimePhase.FAILED, now=_now_for(target)
    )
    assert result.kind is kind
    assert state.aggregate.task.status.value == task_status
    assert state.commits == 1
    if boundary is CoordinationRuntimeBoundary.CROSSED_ACTIVE:
        assert state.lifecycle_count == 1
        assert state.outbox_count == 1
    else:
        assert state.lifecycle_count == 0


def test_service_success_with_crossed_sibling_has_no_drain_and_schedules(monkeypatch) -> None:
    _task, target, _siblings, aggregate = _aggregate_for_sibling_boundaries(
        (CoordinationRuntimeBoundary.CROSSED_ACTIVE,)
    )
    state, service = _service_state(monkeypatch, aggregate, target)
    result, _observation_value = _call(
        service, target, phase=RuntimePhase.SUCCEEDED, now=_now_for(target)
    )
    assert result.kind is CoordinatedKnownTerminalKind.APPLIED
    assert result.drain_id is None
    assert state.scheduler_calls
    assert state.drain is None


def test_service_exact_success_replay_has_no_second_writes_or_commit(monkeypatch) -> None:
    _task, target, aggregate = _aggregate()
    state, service = _service_state(monkeypatch, aggregate, target)
    now = _now_for(target)
    first, observation = _call(service, target, phase=RuntimePhase.SUCCEEDED, now=now)
    counts = (
        state.observation_adds,
        state.execution_saves,
        state.task_saves,
        len(state.scheduler_calls),
        state.commits,
    )
    replay, _ = _call(
        service,
        target,
        phase=RuntimePhase.SUCCEEDED,
        now=now + timedelta(seconds=2),
        observation=observation,
    )
    assert first.kind is CoordinatedKnownTerminalKind.APPLIED
    assert replay.kind is CoordinatedKnownTerminalKind.REPLAY
    assert (
        state.observation_adds,
        state.execution_saves,
        state.task_saves,
        len(state.scheduler_calls),
        state.commits,
    ) == counts


def test_service_failed_replay_preserves_completed_drain_without_writes(monkeypatch) -> None:
    _task, target, aggregate = _aggregate()
    state, service = _service_state(monkeypatch, aggregate, target)
    now = _now_for(target)
    first, observation = _call(service, target, phase=RuntimePhase.FAILED, now=now)
    assert first.drain_target is CoordinationRuntimeDrainTarget.FAILED
    counts = (state.observation_adds, state.execution_saves, state.task_saves, state.commits)
    replay, _ = _call(
        service,
        target,
        phase=RuntimePhase.FAILED,
        now=now + timedelta(seconds=2),
        observation=observation,
    )
    assert replay.kind is CoordinatedKnownTerminalKind.REPLAY
    assert (
        state.observation_adds,
        state.execution_saves,
        state.task_saves,
        state.commits,
    ) == counts
