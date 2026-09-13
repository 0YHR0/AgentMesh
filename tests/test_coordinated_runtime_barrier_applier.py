from __future__ import annotations

import ast
from dataclasses import replace
from datetime import timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agentmesh.application.business_outcomes import KnownTerminalPhase
from agentmesh.application.coordinated_runtime_barrier import (
    CoordinatedBarrierApplicationMode,
    CoordinatedBarrierCompletion,
    CoordinatedBarrierDrainGuard,
    CoordinatedBarrierTriggerGuard,
    CoordinatedRuntimeBarrierApplier,
    plan_known_terminal,
    plan_reconciled_terminal,
    plan_unknown_outcome,
)
from agentmesh.domain.coordination import (
    CoordinationRuntimeBoundary,
    CoordinationRuntimeDrain,
    CoordinationRuntimeDrainStatus,
    CoordinationRuntimeDrainTarget,
)
from agentmesh.domain.errors import InvalidTaskInput, RuntimeExecutionConflict
from agentmesh.domain.runtime_execution import RuntimeExecutionPhase
from tests.test_coordinated_runtime_barrier import _aggregate, _aggregate_for_sibling_boundaries

UTC = timezone.utc


class _Outbox:
    def __init__(self) -> None:
        self.values = []

    def add(self, envelope) -> None:
        self.values.append(envelope)

    def add_if_absent(self, envelope) -> bool:
        if any(value.message_id == envelope.message_id for value in self.values):
            return False
        self.values.append(envelope)
        return True


class _Uow:
    def __init__(self) -> None:
        self.saves: list[tuple[str, object]] = []
        self.outbox = _Outbox()
        self.tasks = SimpleNamespace(save=lambda value: self.saves.append(("task", value)))
        self.runs = SimpleNamespace(save=lambda value: self.saves.append(("run", value)))
        self.subtasks = SimpleNamespace(save=lambda value: self.saves.append(("subtask", value)))
        self.attempts = SimpleNamespace(save=lambda value: self.saves.append(("attempt", value)))
        self.runtimes = SimpleNamespace(
            save_execution=lambda value, tenant_id: self.saves.append(("execution", value)),
            add_lifecycle_operation=lambda value: self.saves.append(("lifecycle", value)),
        )
        self.quotas = SimpleNamespace(
            list_reservations_for_attempt=lambda attempt_id, for_update=False: [],
            save_reservation=lambda value: self.saves.append(("quota", value)),
        )
        self.coordination_runtime_drains = SimpleNamespace(
            get=lambda *args, **kwargs: None,
            add=lambda value: self.saves.append(("drain.add", value)),
            save=lambda value, tenant_id: self.saves.append(("drain.save", value)),
        )


def _apply_case(boundary, *, drain_target=CoordinationRuntimeDrainTarget.RUNNING):
    task, target, siblings, aggregate = _aggregate_for_sibling_boundaries(
        (boundary,), drain_target=drain_target
    )
    phase = (
        KnownTerminalPhase.FAILED
        if drain_target is not CoordinationRuntimeDrainTarget.RUNNING
        else KnownTerminalPhase.SUCCEEDED
    )
    plan = plan_known_terminal(
        aggregate,
        triggering_run_id=target[1].id,
        phase=phase,
        cancel_intent_present=False,
        safe_error=None,
    )
    uow = _Uow()
    now = aggregate.active_drain.created_at + timedelta(seconds=1)
    result = CoordinatedRuntimeBarrierApplier().apply_in_uow(
        uow,
        aggregate=aggregate,
        plan=plan,
        now=now,
        cancel_deadline_window=timedelta(minutes=5),
    )
    return task, target, siblings[0], aggregate, plan, uow, result


def _parked_unknown_case():
    task, target, aggregate = _aggregate()
    run, attempt, execution = target[1], target[2], target[3]
    subtask = target[0]
    now = execution.updated_at + timedelta(seconds=1)
    plan = plan_unknown_outcome(
        aggregate,
        triggering_run_id=run.id,
        reason="runtime.outcome_unknown",
    )
    parked_execution = execution.apply_observation(
        phase=RuntimeExecutionPhase.OUTCOME_UNKNOWN,
        provider_sequence=2,
        now=now,
    )
    attempt.mark_outcome_unknown("runtime.outcome_unknown", at=now)
    run.require_runtime_reconciliation("runtime.outcome_unknown", at=now)
    subtask.require_runtime_reconciliation(run.id, "runtime.outcome_unknown", at=now)
    parked = replace(
        aggregate,
        runs=(run,),
        latest_attempts={run.id: attempt},
        executions=(parked_execution,),
        subtasks=(subtask,),
    )
    return task, target, parked, plan, now


def test_unknown_parking_requires_explicit_flag_and_never_completes() -> None:
    _task, target, parked, plan, now = _parked_unknown_case()
    with pytest.raises(RuntimeExecutionConflict, match="unknown parking"):
        CoordinatedRuntimeBarrierApplier().apply_in_uow(
            _Uow(),
            aggregate=parked,
            plan=plan,
            now=now,
            cancel_deadline_window=timedelta(minutes=5),
        )

    uow = _Uow()
    result = CoordinatedRuntimeBarrierApplier().apply_in_uow(
        uow,
        aggregate=parked,
        plan=plan,
        now=now,
        cancel_deadline_window=timedelta(minutes=5),
        application_mode=CoordinatedBarrierApplicationMode.UNKNOWN_PARKING,
    )
    assert result.completion is CoordinatedBarrierCompletion.WAIT_RECONCILIATION
    assert result.completion is not CoordinatedBarrierCompletion.CONTINUE_SUCCESS
    assert {kind for kind, _value in uow.saves} == {"drain.add"}
    assert not {kind for kind, _value in uow.saves} & {"run", "subtask", "task"}
    assert not uow.outbox.values


def test_reconciled_terminal_plan_is_not_accepted_by_unknown_parking_path() -> None:
    _task, target, parked, _unknown_plan, now = _parked_unknown_case()
    drain = CoordinationRuntimeDrain.start(
        drain_id=uuid4(),
        tenant_id=parked.task.tenant_id,
        task_id=parked.task.id,
        triggering_run_id=target[1].id,
        target=CoordinationRuntimeDrainTarget.RUNNING,
        reason="coordination.runtime_reconciliation_required",
        at=now,
    )
    parked.task.require_coordination_runtime_reconciliation(drain, at=now)
    parked = replace(
        parked,
        active_drain=drain,
        boundary_classifications={
            target[1].id: CoordinationRuntimeBoundary.RECONCILIATION_EVIDENCE
        },
    )
    plan = plan_reconciled_terminal(
        parked,
        triggering_run_id=target[1].id,
        phase=KnownTerminalPhase.FAILED,
        cancel_intent_present=False,
        safe_error=None,
    )
    uow = _Uow()
    with pytest.raises(RuntimeExecutionConflict, match="unknown parking"):
        CoordinatedRuntimeBarrierApplier().apply_in_uow(
            uow,
            aggregate=parked,
            plan=plan,
            now=now + timedelta(seconds=1),
            cancel_deadline_window=timedelta(minutes=5),
            application_mode=CoordinatedBarrierApplicationMode.UNKNOWN_PARKING,
        )
    assert not uow.saves
    assert not uow.outbox.values


def test_reconciled_terminal_plan_requires_its_closed_safe_mode() -> None:
    _task, target, parked, _unknown_plan, now = _parked_unknown_case()
    drain = CoordinationRuntimeDrain.start(
        drain_id=uuid4(),
        tenant_id=parked.task.tenant_id,
        task_id=parked.task.id,
        triggering_run_id=target[1].id,
        target=CoordinationRuntimeDrainTarget.RUNNING,
        reason="coordination.runtime_reconciliation_required",
        at=now,
    )
    parked.task.require_coordination_runtime_reconciliation(drain, at=now)
    parked = replace(
        parked,
        active_drain=drain,
        boundary_classifications={
            target[1].id: CoordinationRuntimeBoundary.RECONCILIATION_EVIDENCE
        },
    )
    plan = plan_reconciled_terminal(
        parked,
        triggering_run_id=target[1].id,
        phase=KnownTerminalPhase.FAILED,
        cancel_intent_present=False,
        safe_error=None,
    )
    uow = _Uow()
    result = CoordinatedRuntimeBarrierApplier().apply_in_uow(
        uow,
        aggregate=parked,
        plan=plan,
        now=now + timedelta(seconds=1),
        cancel_deadline_window=timedelta(minutes=5),
        application_mode=CoordinatedBarrierApplicationMode.RECONCILED_TERMINAL,
    )
    assert result.completion is CoordinatedBarrierCompletion.APPLY_FAILED
    assert {kind for kind, _value in uow.saves} == {"drain.save"}
    assert not uow.outbox.values


def test_barrier_rejects_invalid_application_mode_before_writes() -> None:
    _task, _target, parked, plan, now = _parked_unknown_case()
    uow = _Uow()
    with pytest.raises(RuntimeExecutionConflict, match="mode is invalid"):
        CoordinatedRuntimeBarrierApplier().apply_in_uow(
            uow,
            aggregate=parked,
            plan=plan,
            now=now,
            cancel_deadline_window=timedelta(minutes=5),
            application_mode="UNKNOWN_PARKING",
        )
    assert not uow.saves
    assert not uow.outbox.values


@pytest.mark.parametrize(
    "boundary",
    [
        CoordinationRuntimeBoundary.NOT_CROSSED_QUEUED,
        CoordinationRuntimeBoundary.NOT_CROSSED_NO_EXECUTION,
        CoordinationRuntimeBoundary.NOT_CROSSED_PREPARED,
    ],
)
def test_provider_free_release_actions_mutate_only_the_locked_sibling(boundary) -> None:
    _task, _target, sibling, _aggregate, plan, uow, result = _apply_case(boundary)
    assert result.completion is CoordinatedBarrierCompletion.APPLY_RUNNING
    saved = {kind for kind, _value in uow.saves}
    assert {"run", "subtask"}.issubset(saved)
    assert sibling[1].status.value == "CANCELED"
    assert sibling[0].current_run_id is None
    if boundary is CoordinationRuntimeBoundary.NOT_CROSSED_PREPARED:
        assert "execution" in saved
        assert len(uow.outbox.values) == 1
        assert uow.outbox.values[0].schema_name == "agentmesh.runtime.dispatch.aborted"
    else:
        assert "execution" not in saved
        assert not uow.outbox.values
    assert result.made_progress is True
    assert plan.sibling_actions[0].run_id in result.changed_ids


def test_cancel_action_uses_stable_deadline_intent_and_replays_without_duplicate_outbox() -> None:
    _task, target, sibling, aggregate, plan, uow, first = _apply_case(
        CoordinationRuntimeBoundary.CROSSED_ACTIVE,
        drain_target=CoordinationRuntimeDrainTarget.FAILED,
    )
    assert first.lifecycle_operation_ids == (
        f"runtime-cancel:{sibling[3].id}:v1",
    )
    assert len(uow.outbox.values) == 1
    assert sibling[3].phase is RuntimeExecutionPhase.RUNNING

    lifecycle = next(value for kind, value in uow.saves if kind == "lifecycle")
    updated_execution = sibling[3].apply_observation(
        phase=RuntimeExecutionPhase.CANCEL_REQUESTED,
        provider_sequence=None,
        now=aggregate.active_drain.created_at + timedelta(seconds=1),
    )
    replay_aggregate = replace(
        aggregate,
        executions=tuple(
            updated_execution if value.id == updated_execution.id else value
            for value in aggregate.executions
        ),
        lifecycle_operations=(lifecycle,),
    )
    replay = CoordinatedRuntimeBarrierApplier().apply_in_uow(
        uow,
        aggregate=replay_aggregate,
        plan=plan,
        now=aggregate.active_drain.created_at + timedelta(seconds=2),
        cancel_deadline_window=timedelta(minutes=5),
    )
    assert replay.lifecycle_operation_ids == first.lifecycle_operation_ids
    assert replay.made_progress is False
    assert len(uow.outbox.values) == 1


def test_cancel_deadline_is_derived_from_drain_creation_and_expires_fail_closed() -> None:
    _task, _target, _siblings, aggregate = _aggregate_for_sibling_boundaries(
        (CoordinationRuntimeBoundary.CROSSED_ACTIVE,),
        drain_target=CoordinationRuntimeDrainTarget.FAILED,
    )
    plan = plan_known_terminal(
        aggregate,
        triggering_run_id=_target[1].id,
        phase=KnownTerminalPhase.FAILED,
        cancel_intent_present=False,
        safe_error=None,
    )
    sibling = _siblings[0]
    uow = _Uow()
    with pytest.raises(RuntimeExecutionConflict, match="deadline"):
        CoordinatedRuntimeBarrierApplier().apply_in_uow(
            uow,
            aggregate=aggregate,
            plan=plan,
            now=aggregate.active_drain.created_at + timedelta(minutes=5),
            cancel_deadline_window=timedelta(minutes=5),
        )
    assert not uow.outbox.values
    assert not [kind for kind, _value in uow.saves if kind == "lifecycle"]
    assert sibling[3].phase is RuntimeExecutionPhase.RUNNING


def test_applier_revalidates_plan_identity_and_does_not_relock() -> None:
    _task, target, _siblings, aggregate = _aggregate_for_sibling_boundaries(
        (CoordinationRuntimeBoundary.NOT_CROSSED_QUEUED,),
        drain_target=CoordinationRuntimeDrainTarget.RUNNING,
    )
    plan = plan_known_terminal(
        aggregate,
        triggering_run_id=target[1].id,
        phase=KnownTerminalPhase.SUCCEEDED,
        cancel_intent_present=False,
        safe_error=None,
    )
    stale = replace(plan, task_id=uuid4())
    uow = _Uow()
    with pytest.raises(RuntimeExecutionConflict):
        CoordinatedRuntimeBarrierApplier().apply_in_uow(
            uow,
            aggregate=aggregate,
            plan=stale,
            now=aggregate.active_drain.created_at + timedelta(seconds=1),
            cancel_deadline_window=timedelta(minutes=5),
        )
    assert not uow.saves


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_id", None),
        ("subtask_id", "not-a-uuid"),
        ("execution_id", object()),
        ("attempt_id", None),
        ("fencing_token", True),
        ("boundary", CoordinationRuntimeBoundary.NOT_CROSSED_PREPARED),
    ],
)
def test_trigger_guard_rejects_invalid_fields_and_boundary(field, value) -> None:
    _task, target, _siblings, aggregate = _aggregate_for_sibling_boundaries(
        (CoordinationRuntimeBoundary.CROSSED_ACTIVE,)
    )
    plan = plan_known_terminal(
        aggregate,
        triggering_run_id=target[1].id,
        phase=KnownTerminalPhase.SUCCEEDED,
        cancel_intent_present=False,
        safe_error=None,
    )
    with pytest.raises(RuntimeExecutionConflict, match="trigger guard"):
        CoordinatedBarrierTriggerGuard(
            **{
                **plan.trigger_guard.__dict__,
                field: value,
            }
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", None),
        ("version", True),
        ("status", CoordinationRuntimeDrainStatus.COMPLETE),
        ("target", object()),
        ("reason", " unsafe reason "),
        ("triggering_run_id", "not-a-uuid"),
        ("created_at", None),
    ],
)
def test_source_drain_guard_rejects_invalid_fields(field, value) -> None:
    _task, target, _siblings, aggregate = _aggregate_for_sibling_boundaries(
        (), drain_target=CoordinationRuntimeDrainTarget.RUNNING
    )
    plan = plan_known_terminal(
        aggregate,
        triggering_run_id=target[1].id,
        phase=KnownTerminalPhase.FAILED,
        cancel_intent_present=False,
        safe_error=None,
    )
    assert plan.source_drain_guard is not None
    with pytest.raises(RuntimeExecutionConflict, match="drain guard"):
        CoordinatedBarrierDrainGuard(
            **{
                **plan.source_drain_guard.__dict__,
                field: value,
            }
        )


def test_source_drain_guard_rejects_non_utc_creation_time() -> None:
    _task, target, _siblings, aggregate = _aggregate_for_sibling_boundaries(
        (), drain_target=CoordinationRuntimeDrainTarget.RUNNING
    )
    plan = plan_known_terminal(
        aggregate,
        triggering_run_id=target[1].id,
        phase=KnownTerminalPhase.FAILED,
        cancel_intent_present=False,
        safe_error=None,
    )
    assert plan.source_drain_guard is not None
    with pytest.raises(RuntimeExecutionConflict, match="creation time"):
        replace(
            plan.source_drain_guard,
            created_at=plan.source_drain_guard.created_at.astimezone(timezone(timedelta(hours=8))),
        )


def test_plan_guard_must_match_triggering_run_and_drain_modes_are_disjoint() -> None:
    _task, target, _siblings, aggregate = _aggregate_for_sibling_boundaries(())
    created = plan_known_terminal(
        aggregate,
        triggering_run_id=target[1].id,
        phase=KnownTerminalPhase.FAILED,
        cancel_intent_present=False,
        safe_error=None,
    )
    with pytest.raises(RuntimeExecutionConflict, match="trigger guard"):
        replace(created, triggering_run_id=uuid4())

    _task, target, _siblings, aggregate = _aggregate_for_sibling_boundaries(
        (), drain_target=CoordinationRuntimeDrainTarget.RUNNING
    )
    retargeted = plan_known_terminal(
        aggregate,
        triggering_run_id=target[1].id,
        phase=KnownTerminalPhase.FAILED,
        cancel_intent_present=False,
        safe_error=None,
    )
    assert created.source_drain_guard is None
    assert retargeted.source_drain_guard is not None
    with pytest.raises(RuntimeExecutionConflict, match="source drain guard"):
        replace(created, source_drain_guard=retargeted.source_drain_guard)
    with pytest.raises(RuntimeExecutionConflict, match="retargeted"):
        replace(retargeted, create_drain=True)
    with pytest.raises(RuntimeExecutionConflict, match="source drain guard"):
        replace(retargeted, source_drain_guard=None)


@pytest.mark.parametrize("stale", ["attempt", "execution", "fence", "boundary", "binding"])
def test_apply_rejects_each_stale_trigger_guard_before_any_write(stale) -> None:
    _task, target, _siblings, aggregate = _aggregate_for_sibling_boundaries(
        (CoordinationRuntimeBoundary.CROSSED_ACTIVE,)
    )
    plan = plan_known_terminal(
        aggregate,
        triggering_run_id=target[1].id,
        phase=KnownTerminalPhase.SUCCEEDED,
        cancel_intent_present=False,
        safe_error=None,
    )
    if stale == "attempt":
        attempts = dict(aggregate.latest_attempts)
        attempts[target[1].id] = replace(attempts[target[1].id], id=uuid4())
        aggregate = replace(aggregate, latest_attempts=attempts)
    elif stale == "execution":
        execution = replace(target[3], id=uuid4())
        aggregate = replace(aggregate, executions=(execution,))
        aggregate = replace(
            aggregate,
            runs=(replace(target[1], runtime_execution_id=execution.id),),
        )
    elif stale == "fence":
        attempts = dict(aggregate.latest_attempts)
        attempts[target[1].id] = replace(
            attempts[target[1].id], fencing_token=attempts[target[1].id].fencing_token + 1
        )
        aggregate = replace(aggregate, latest_attempts=attempts)
    elif stale == "boundary":
        boundaries = dict(aggregate.boundary_classifications)
        boundaries[target[1].id] = CoordinationRuntimeBoundary.NOT_CROSSED_PREPARED
        aggregate = replace(aggregate, boundary_classifications=boundaries)
    else:
        aggregate = replace(
            aggregate,
            runs=(replace(target[1], subtask_id=uuid4()),),
        )
    uow = _Uow()
    with pytest.raises(RuntimeExecutionConflict, match="trigger guard"):
        CoordinatedRuntimeBarrierApplier().apply_in_uow(
            uow,
            aggregate=aggregate,
            plan=plan,
            now=target[3].updated_at + timedelta(seconds=1),
            cancel_deadline_window=timedelta(minutes=5),
        )
    assert not uow.saves
    assert not uow.outbox.values


@pytest.mark.parametrize(
    "stale",
    ["created", "deleted", "version", "status", "target", "reason", "trigger", "created_at"],
)
def test_apply_rejects_each_stale_active_drain_guard_before_any_write(stale) -> None:
    _task, target, _siblings, aggregate = _aggregate_for_sibling_boundaries(
        (), drain_target=CoordinationRuntimeDrainTarget.RUNNING
    )
    plan = plan_known_terminal(
        aggregate,
        triggering_run_id=target[1].id,
        phase=KnownTerminalPhase.FAILED,
        cancel_intent_present=False,
        safe_error=None,
    )
    active = aggregate.active_drain
    assert active is not None
    if stale == "created":
        changed = replace(active, id=uuid4())
    elif stale == "deleted":
        changed = None
    elif stale == "version":
        changed = replace(active, version=active.version + 1)
    elif stale == "status":
        changed = active.complete(at=active.updated_at + timedelta(seconds=1))
    elif stale == "target":
        changed = replace(active, target=CoordinationRuntimeDrainTarget.WAITING_APPROVAL)
    elif stale == "reason":
        changed = replace(active, reason="different.failure")
    elif stale == "trigger":
        changed = replace(active, triggering_run_id=uuid4())
    else:
        timestamp = active.created_at + timedelta(seconds=1)
        changed = replace(active, created_at=timestamp, updated_at=timestamp)
    aggregate = replace(aggregate, active_drain=changed)
    uow = _Uow()
    with pytest.raises(RuntimeExecutionConflict, match="drain guard"):
        CoordinatedRuntimeBarrierApplier().apply_in_uow(
            uow,
            aggregate=aggregate,
            plan=plan,
            now=target[3].updated_at + timedelta(seconds=1),
            cancel_deadline_window=timedelta(minutes=5),
        )
    assert not uow.saves
    assert not uow.outbox.values


def test_first_failure_creates_one_stable_drain_without_reopening_a_uow() -> None:
    _task, target, _siblings, aggregate = _aggregate_for_sibling_boundaries(())
    plan = plan_known_terminal(
        aggregate,
        triggering_run_id=target[1].id,
        phase=KnownTerminalPhase.FAILED,
        cancel_intent_present=False,
        safe_error=None,
    )
    uow = _Uow()
    now = target[3].updated_at + timedelta(seconds=1)
    applier = CoordinatedRuntimeBarrierApplier()
    result = applier.apply_in_uow(
        uow,
        aggregate=aggregate,
        plan=plan,
        now=now,
        cancel_deadline_window=timedelta(minutes=5),
    )
    assert result.effective_drain is not None
    assert result.effective_drain.id == result.drain.id
    assert result.effective_drain.target is CoordinationRuntimeDrainTarget.FAILED
    assert [kind for kind, _value in uow.saves] == ["drain.add"]
    assert result.made_progress is True


@pytest.mark.parametrize(
    "window",
    [timedelta(0), timedelta(seconds=-1), timedelta(days=8)],
)
def test_cancel_deadline_window_must_be_positive_and_bounded(window) -> None:
    _task, target, _siblings, aggregate = _aggregate_for_sibling_boundaries(())
    plan = plan_known_terminal(
        aggregate,
        triggering_run_id=target[1].id,
        phase=KnownTerminalPhase.FAILED,
        cancel_intent_present=False,
        safe_error=None,
    )
    with pytest.raises(InvalidTaskInput, match="window"):
        CoordinatedRuntimeBarrierApplier().apply_in_uow(
            _Uow(),
            aggregate=aggregate,
            plan=plan,
            now=target[3].updated_at + timedelta(seconds=1),
            cancel_deadline_window=window,
        )


def test_applier_has_no_runtime_registry_or_adapter_calls() -> None:
    path = (
        Path(__file__).parents[1]
        / "src"
        / "agentmesh"
        / "application"
        / "coordinated_runtime_barrier.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"))
    forbidden = {"RuntimeRegistryService", "ManagedAgentRuntime", "RuntimeAdapter"}
    assert not any(
        isinstance(node, ast.Name) and node.id in forbidden for node in ast.walk(tree)
    )
