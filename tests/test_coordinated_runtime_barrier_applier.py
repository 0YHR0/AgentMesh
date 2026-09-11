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
    CoordinatedBarrierCompletion,
    CoordinatedRuntimeBarrierApplier,
    plan_known_terminal,
)
from agentmesh.domain.coordination import (
    CoordinationRuntimeBoundary,
    CoordinationRuntimeDrainTarget,
)
from agentmesh.domain.errors import InvalidTaskInput, RuntimeExecutionConflict
from agentmesh.domain.runtime_execution import RuntimeExecutionPhase
from tests.test_coordinated_runtime_barrier import _aggregate_for_sibling_boundaries

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
