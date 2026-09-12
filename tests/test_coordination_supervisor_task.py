from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from agentmesh.domain.coordination import (
    CoordinationRuntimeDrain,
    CoordinationRuntimeDrainTarget,
)
from agentmesh.domain.errors import InvalidTaskInput, InvalidTaskTransition
from agentmesh.domain.tasks import (
    RunRole,
    Task,
    TaskExecutionMode,
    TaskRun,
    TaskStatus,
)

UTC = timezone.utc


def _supervisor_task() -> tuple[Task, TaskRun, datetime]:
    base = datetime.now(UTC) + timedelta(seconds=2)
    task = Task.create(
        tenant_id="tenant-a",
        objective="synthesize coordinated work",
        execution_mode=TaskExecutionMode.COORDINATED,
        plan_version=1,
        plan_digest="sha256:plan",
        max_concurrency=2,
    )
    task.start_coordination(at=base)
    run = TaskRun.request(
        task.id,
        "supervisor-agent",
        role=RunRole.SUPERVISOR,
        runtime_version_id=uuid4(),
        runtime_authority="managed",
        at=base + timedelta(seconds=1),
    )
    task.queue_supervisor(run.id, at=base + timedelta(seconds=2))
    return task, run, base


def _drain(
    task: Task,
    run: TaskRun,
    *,
    target: CoordinationRuntimeDrainTarget,
    at: datetime,
) -> CoordinationRuntimeDrain:
    return CoordinationRuntimeDrain.start(
        drain_id=uuid4(),
        tenant_id=task.tenant_id,
        task_id=task.id,
        triggering_run_id=run.id,
        target=target,
        reason="first cause",
        at=at,
    )


@pytest.mark.parametrize(
    ("outcome", "target", "expected_status"),
    [
        ("success", CoordinationRuntimeDrainTarget.RUNNING, TaskStatus.COMPLETED),
        (
            "budget",
            CoordinationRuntimeDrainTarget.RUNNING,
            TaskStatus.WAITING_APPROVAL,
        ),
        (
            "budget",
            CoordinationRuntimeDrainTarget.WAITING_APPROVAL,
            TaskStatus.WAITING_APPROVAL,
        ),
        ("failed", CoordinationRuntimeDrainTarget.FAILED, TaskStatus.FAILED),
        ("canceled", CoordinationRuntimeDrainTarget.CANCELED, TaskStatus.CANCELED),
    ],
)
def test_supervisor_reconciliation_state_matrix(
    outcome: str,
    target: CoordinationRuntimeDrainTarget,
    expected_status: TaskStatus,
) -> None:
    task, run, base = _supervisor_task()
    drain = _drain(task, run, target=target, at=base + timedelta(seconds=3))
    task.require_coordination_supervisor_runtime_reconciliation(
        run.id, drain, at=base + timedelta(seconds=4)
    )
    assert task.status is TaskStatus.RECONCILIATION_REQUIRED
    assert task.current_run_id == run.id
    assert task.error == "coordination.runtime_reconciliation_required"
    completed = drain.complete(at=base + timedelta(seconds=5))

    if outcome == "success":
        task.reconcile_coordination_supervisor_succeeded(
            run.id, completed, {"answer": 42}, None, at=base + timedelta(seconds=6)
        )
        assert task.output == {"answer": 42}
        assert task.candidate_output is None
        assert task.error is None
    elif outcome == "budget":
        task.reconcile_coordination_supervisor_succeeded(
            run.id,
            completed,
            {"answer": 42},
            "budget_deadline_exceeded",
            at=base + timedelta(seconds=6),
        )
        assert task.current_run_id is None
        assert task.output is None
        assert task.candidate_output == {"answer": 42}
        assert task.error == "budget_deadline_exceeded"
        assert task.budget_exhausted_reason == "budget_deadline_exceeded"
    elif outcome == "failed":
        task.reconcile_coordination_supervisor_failed(
            run.id, completed, "supervisor failed", at=base + timedelta(seconds=6)
        )
        assert task.current_run_id == run.id
        assert task.output is None
        assert task.error == "supervisor failed"
    else:
        task.reconcile_coordination_supervisor_canceled(
            run.id, completed, "operator requested cancellation", at=base + timedelta(seconds=6)
        )
        assert task.current_run_id == run.id
        assert task.output is None
        assert task.error is None
    assert task.status is expected_status


def test_supervisor_hold_and_reconciliation_are_exactly_idempotent() -> None:
    task, run, base = _supervisor_task()
    drain = _drain(
        task, run, target=CoordinationRuntimeDrainTarget.RUNNING, at=base + timedelta(seconds=3)
    )
    task.require_coordination_supervisor_runtime_reconciliation(
        run.id, drain, at=base + timedelta(seconds=4)
    )
    held_version = task.version
    task.require_coordination_supervisor_runtime_reconciliation(run.id, drain, at=base)
    assert task.version == held_version
    completed = drain.complete(at=base + timedelta(seconds=5))
    task.reconcile_coordination_supervisor_succeeded(
        run.id, completed, {"answer": 42}, None, at=base + timedelta(seconds=6)
    )
    completed_version = task.version
    task.reconcile_coordination_supervisor_succeeded(
        run.id, completed, {"answer": 42}, None, at=base
    )
    assert task.version == completed_version
    with pytest.raises(InvalidTaskTransition):
        task.reconcile_coordination_supervisor_succeeded(
            run.id, completed, {"answer": 43}, None, at=base + timedelta(seconds=7)
        )


def test_supervisor_reconciliation_rejects_wrong_pointer_state_drain_and_target() -> None:
    task, run, base = _supervisor_task()
    other_run = TaskRun.request(
        task.id,
        "other-supervisor",
        role=RunRole.SUPERVISOR,
        runtime_version_id=uuid4(),
        runtime_authority="managed",
        at=base + timedelta(seconds=3),
    )
    drain = _drain(
        task, run, target=CoordinationRuntimeDrainTarget.RUNNING, at=base + timedelta(seconds=3)
    )
    with pytest.raises(InvalidTaskTransition):
        task.require_coordination_supervisor_runtime_reconciliation(
            other_run.id, drain, at=base + timedelta(seconds=4)
        )
    task.current_run_id = other_run.id
    with pytest.raises(InvalidTaskTransition):
        task.require_coordination_supervisor_runtime_reconciliation(
            run.id, drain, at=base + timedelta(seconds=4)
        )
    task.current_run_id = run.id
    with pytest.raises(InvalidTaskTransition):
        task.reconcile_coordination_supervisor_succeeded(
            run.id, drain, {}, None, at=base + timedelta(seconds=4)
        )
    completed = drain.complete(at=base + timedelta(seconds=5))
    with pytest.raises(InvalidTaskTransition):
        task.reconcile_coordination_supervisor_failed(
            run.id, completed, "failed", at=base + timedelta(seconds=6)
        )

    direct = Task.create(tenant_id="tenant-a", objective="direct")
    with pytest.raises(InvalidTaskTransition):
        direct.require_coordination_supervisor_runtime_reconciliation(run.id, drain, at=base)


def test_supervisor_reconciliation_rejects_backward_time_and_invalid_input_without_mutation(
) -> None:
    task, run, base = _supervisor_task()
    drain = _drain(
        task, run, target=CoordinationRuntimeDrainTarget.RUNNING, at=base + timedelta(seconds=3)
    )
    before = (task.status, task.current_run_id, task.version, task.updated_at)
    with pytest.raises(InvalidTaskTransition):
        task.require_coordination_supervisor_runtime_reconciliation(
            run.id, drain, at=base
        )
    assert (task.status, task.current_run_id, task.version, task.updated_at) == before
    with pytest.raises(InvalidTaskInput):
        task.reconcile_coordination_supervisor_succeeded(run.id, drain, [], None, at=base)

    task.require_coordination_supervisor_runtime_reconciliation(
        run.id, drain, at=base + timedelta(seconds=4)
    )
    completed = drain.complete(at=base + timedelta(seconds=5))
    held = (task.status, task.current_run_id, task.version, task.updated_at)
    with pytest.raises(InvalidTaskTransition):
        task.reconcile_coordination_supervisor_succeeded(
            run.id, completed, {"answer": 1}, None, at=base + timedelta(seconds=4)
        )
    assert (task.status, task.current_run_id, task.version, task.updated_at) == held


def test_supervisor_cancellation_replay_requires_exact_projection() -> None:
    task, run, base = _supervisor_task()
    drain = _drain(
        task, run, target=CoordinationRuntimeDrainTarget.CANCELED, at=base + timedelta(seconds=3)
    )
    task.require_coordination_supervisor_runtime_reconciliation(
        run.id, drain, at=base + timedelta(seconds=4)
    )
    completed = drain.complete(at=base + timedelta(seconds=5))
    task.reconcile_coordination_supervisor_canceled(
        run.id, completed, "operator canceled", at=base + timedelta(seconds=6)
    )
    version = task.version
    task.reconcile_coordination_supervisor_canceled(
        run.id, completed, "different bounded reason", at=base
    )
    assert task.version == version
    assert task.status is TaskStatus.CANCELED
    assert task.error is None
