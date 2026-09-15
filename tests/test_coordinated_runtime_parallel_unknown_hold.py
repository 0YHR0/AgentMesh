from __future__ import annotations

from dataclasses import replace
from types import MappingProxyType
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from agentmesh.application.coordinated_runtime_barrier import (
    is_exact_parallel_executor_reconciliation_hold,
)
from agentmesh.domain.coordination import (
    CoordinationRuntimeBoundary,
    CoordinationRuntimeDrain,
    CoordinationRuntimeDrainStatus,
    CoordinationRuntimeDrainTarget,
)
from agentmesh.domain.tasks import RunRole, TaskStatus
from tests.test_coordinated_runtime_barrier import _aggregate_for_sibling_boundaries


def _parallel_executor_hold():
    task, target, siblings, aggregate = _aggregate_for_sibling_boundaries(
        (CoordinationRuntimeBoundary.CROSSED_ACTIVE,)
    )
    sibling = siblings[0]
    drain = CoordinationRuntimeDrain.start(
        drain_id=uuid5(
            NAMESPACE_URL,
            f"coordination-runtime-drain:{task.tenant_id}:{task.id}",
        ),
        tenant_id=task.tenant_id,
        task_id=task.id,
        triggering_run_id=target[1].id,
        target=CoordinationRuntimeDrainTarget.RUNNING,
        reason="coordination.runtime_reconciliation_required",
        at=target[3].updated_at,
    )
    task.status = TaskStatus.RECONCILIATION_REQUIRED
    task.current_run_id = None
    task.error = "coordination.runtime_reconciliation_required"
    aggregate = replace(
        aggregate,
        active_drain=drain,
        boundary_classifications=MappingProxyType(
            {
                target[1].id: CoordinationRuntimeBoundary.RECONCILIATION_EVIDENCE,
                sibling[1].id: CoordinationRuntimeBoundary.CROSSED_ACTIVE,
            }
        ),
    )
    return target, sibling, aggregate


def test_exact_parallel_executor_hold_allows_an_active_sibling_to_become_unknown():
    _target, sibling, aggregate = _parallel_executor_hold()
    assert is_exact_parallel_executor_reconciliation_hold(aggregate, run=sibling[1])


def test_parallel_executor_hold_preserves_an_existing_running_drain_identity():
    target, sibling, aggregate = _parallel_executor_hold()
    assert aggregate.active_drain is not None
    aggregate = replace(
        aggregate,
        active_drain=replace(
            aggregate.active_drain,
            id=uuid4(),
            triggering_run_id=target[1].id,
            reason="existing successful Executor drain",
        ),
    )
    assert is_exact_parallel_executor_reconciliation_hold(aggregate, run=sibling[1])


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong_status",
        "task_pointer",
        "task_output",
        "task_candidate",
        "task_error",
        "task_budget_hold",
        "missing_drain",
        "completed_drain",
        "wrong_drain_target",
        "wrong_trigger_boundary",
        "supervisor",
    ],
)
def test_parallel_executor_hold_rejects_every_partial_or_supervisor_shape(mutation):
    _target, sibling, aggregate = _parallel_executor_hold()
    task = aggregate.task
    drain = aggregate.active_drain
    run = sibling[1]
    assert drain is not None
    if mutation == "wrong_status":
        task.status = TaskStatus.WAITING_APPROVAL
    elif mutation == "task_pointer":
        task.current_run_id = run.id
    elif mutation == "task_output":
        task.output = {"unsafe": True}
    elif mutation == "task_candidate":
        task.candidate_output = {"unsafe": True}
    elif mutation == "task_error":
        task.error = "different"
    elif mutation == "task_budget_hold":
        task.budget_exhausted_reason = "budget.exhausted"
    elif mutation == "missing_drain":
        aggregate = replace(aggregate, active_drain=None)
    elif mutation == "completed_drain":
        aggregate = replace(
            aggregate,
            active_drain=replace(
                drain,
                status=CoordinationRuntimeDrainStatus.COMPLETE,
                completed_at=drain.updated_at,
            ),
        )
    elif mutation == "wrong_drain_target":
        aggregate = replace(
            aggregate,
            active_drain=replace(drain, target=CoordinationRuntimeDrainTarget.WAITING_APPROVAL),
        )
    elif mutation == "wrong_trigger_boundary":
        aggregate = replace(
            aggregate,
            boundary_classifications=MappingProxyType(
                {run.id: CoordinationRuntimeBoundary.CROSSED_ACTIVE}
            ),
        )
    else:
        run = replace(run, role=RunRole.SUPERVISOR, subtask_id=None)
    assert not is_exact_parallel_executor_reconciliation_hold(aggregate, run=run)
