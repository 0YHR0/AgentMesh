from __future__ import annotations

import ast
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from agentmesh.domain.coordination import (
    CoordinationRuntimeBoundary,
    CoordinationRuntimeDrain,
    CoordinationRuntimeDrainTarget,
    Subtask,
    SubtaskStatus,
    classify_runtime_boundary,
)
from agentmesh.domain.errors import InvalidTaskInput, InvalidTaskTransition
from agentmesh.domain.runtime_execution import RuntimeExecution, RuntimeExecutionPhase
from agentmesh.domain.tasks import (
    AttemptStatus,
    RunRole,
    RunStatus,
    Task,
    TaskAttempt,
    TaskExecutionMode,
    TaskRun,
    TaskStatus,
)

UTC = timezone.utc
TASK_ID = uuid4()


def _drain(
    *,
    task_id=TASK_ID,
    target: CoordinationRuntimeDrainTarget = CoordinationRuntimeDrainTarget.RUNNING,
):
    at = datetime(2026, 1, 1, tzinfo=UTC)
    return CoordinationRuntimeDrain.start(
        drain_id=uuid4(),
        tenant_id="tenant-a",
        task_id=task_id,
        triggering_run_id=uuid4(),
        target=target,
        reason="first cause",
        at=at,
    )


def _chain(*, phase: RuntimeExecutionPhase | None = None):
    base = datetime.now(UTC) + timedelta(seconds=1)
    subtask = Subtask.create(
        subtask_id=uuid4(),
        task_id=TASK_ID,
        key="worker",
        objective="worker",
        input={},
        required_capabilities=("general.task",),
        preferred_agent_id=None,
        initially_ready=True,
    )
    run = TaskRun.request(
        TASK_ID,
        "agent",
        role=RunRole.EXECUTOR,
        subtask_id=subtask.id,
        runtime_version_id=uuid4(),
        runtime_authority="managed",
        at=base,
    )
    subtask.queue(run.id, at=base + timedelta(seconds=1))
    subtask.start(run.id, at=base + timedelta(seconds=2))
    run.start(at=base + timedelta(seconds=2))
    attempt = TaskAttempt.lease(
        run_id=run.id,
        worker_id="worker",
        fencing_token=1,
        lease_expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    executions = []
    if phase is not None:
        execution = RuntimeExecution.prepare(
            tenant_id="tenant-a",
            run_id=run.id,
            runtime_version_id=run.runtime_version_id,
            assignment_id=uuid4(),
            assignment_digest="a" * 64,
            dispatch_key=f"dispatch:{run.id}",
            dispatch_digest="b" * 64,
            execution_id=run.runtime_execution_intent_id,
            now=base,
            ).claim(
            attempt_id=attempt.id,
            fencing_token=attempt.fencing_token,
            expected_owner_attempt_id=None,
            expected_fencing_token=None,
            expected_version=1,
            now=base + timedelta(seconds=1),
        )
        if phase is not RuntimeExecutionPhase.PREPARED:
            paths = {
                RuntimeExecutionPhase.WAITING_INPUT: (
                    RuntimeExecutionPhase.ACCEPTED,
                    RuntimeExecutionPhase.WAITING_INPUT,
                ),
                RuntimeExecutionPhase.WAITING_APPROVAL: (
                    RuntimeExecutionPhase.ACCEPTED,
                    RuntimeExecutionPhase.WAITING_APPROVAL,
                ),
                RuntimeExecutionPhase.PAUSE_REQUESTED: (
                    RuntimeExecutionPhase.ACCEPTED,
                    RuntimeExecutionPhase.PAUSE_REQUESTED,
                ),
                RuntimeExecutionPhase.PAUSED: (
                    RuntimeExecutionPhase.ACCEPTED,
                    RuntimeExecutionPhase.PAUSE_REQUESTED,
                    RuntimeExecutionPhase.PAUSED,
                ),
            }
            for index, observed_phase in enumerate(paths.get(phase, (phase,)), 2):
                execution = execution.apply_observation(
                    phase=observed_phase,
                    provider_sequence=index,
                    now=base + timedelta(seconds=index),
                )
        run.bind_runtime_execution(execution.id)
        executions.append(execution)
        if phase is RuntimeExecutionPhase.SUCCEEDED:
            attempt.status = AttemptStatus.SUCCEEDED
            run.status = RunStatus.SUCCEEDED
            subtask.status = SubtaskStatus.COMPLETED
        elif phase in {RuntimeExecutionPhase.FAILED, RuntimeExecutionPhase.TIMED_OUT}:
            attempt.status = AttemptStatus.FAILED
            run.status = RunStatus.FAILED
            subtask.status = SubtaskStatus.FAILED
        elif phase is RuntimeExecutionPhase.CANCELED:
            attempt.status = AttemptStatus.CANCELED
            run.status = RunStatus.CANCELED
            subtask.status = SubtaskStatus.CANCELED
        elif phase in {RuntimeExecutionPhase.LOST, RuntimeExecutionPhase.OUTCOME_UNKNOWN}:
            attempt.status = AttemptStatus.OUTCOME_UNKNOWN
            run.status = RunStatus.RECONCILIATION_REQUIRED
            subtask.status = SubtaskStatus.RECONCILIATION_REQUIRED
    return subtask, run, attempt, executions


def test_drain_precedence_replay_and_completion_are_closed() -> None:
    drain = _drain()
    at = drain.updated_at + timedelta(seconds=1)
    waiting = drain.retarget(
        target=CoordinationRuntimeDrainTarget.WAITING_APPROVAL,
        reason="budget hold",
        at=at,
    )
    assert waiting.stopping is True
    assert waiting.version == 2
    assert waiting.retarget(
        target=CoordinationRuntimeDrainTarget.RUNNING, reason="ignored", at=at
    ) is waiting
    failed = waiting.retarget(
        target=CoordinationRuntimeDrainTarget.FAILED,
        reason="failure",
        at=at + timedelta(seconds=1),
    )
    assert failed.reason == "failure"
    assert failed.retarget(
        target=CoordinationRuntimeDrainTarget.CANCELED,
        reason="ignored",
        at=at + timedelta(seconds=2),
    ) is failed
    complete = failed.complete(at=at + timedelta(seconds=2))
    assert complete.status.value == "COMPLETE"
    assert complete.completed_at == complete.updated_at
    assert complete.complete(at=at - timedelta(seconds=1)) is complete


def test_drain_rejects_nonmonotonic_clock_and_unsafe_reason() -> None:
    with pytest.raises(InvalidTaskInput):
        CoordinationRuntimeDrain.start(
            drain_id=uuid4(),
            tenant_id="tenant-a",
            task_id=TASK_ID,
            triggering_run_id=uuid4(),
            target=CoordinationRuntimeDrainTarget.RUNNING,
            reason="bad\nreason",
            at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    drain = _drain()
    with pytest.raises(InvalidTaskTransition):
        drain.complete(at=drain.updated_at - timedelta(seconds=1))


@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        (RuntimeExecutionPhase.PREPARED, CoordinationRuntimeBoundary.NOT_CROSSED_PREPARED),
        (RuntimeExecutionPhase.DISPATCHING, CoordinationRuntimeBoundary.CROSSED_ACTIVE),
        (RuntimeExecutionPhase.ACCEPTED, CoordinationRuntimeBoundary.CROSSED_ACTIVE),
        (RuntimeExecutionPhase.RUNNING, CoordinationRuntimeBoundary.CROSSED_ACTIVE),
        (RuntimeExecutionPhase.WAITING_INPUT, CoordinationRuntimeBoundary.CROSSED_ACTIVE),
        (RuntimeExecutionPhase.WAITING_APPROVAL, CoordinationRuntimeBoundary.CROSSED_ACTIVE),
        (RuntimeExecutionPhase.PAUSE_REQUESTED, CoordinationRuntimeBoundary.CROSSED_ACTIVE),
        (RuntimeExecutionPhase.PAUSED, CoordinationRuntimeBoundary.CROSSED_ACTIVE),
        (RuntimeExecutionPhase.CANCEL_REQUESTED, CoordinationRuntimeBoundary.CROSSED_ACTIVE),
        (RuntimeExecutionPhase.SUCCEEDED, CoordinationRuntimeBoundary.KNOWN_TERMINAL),
        (RuntimeExecutionPhase.FAILED, CoordinationRuntimeBoundary.KNOWN_TERMINAL),
        (RuntimeExecutionPhase.CANCELED, CoordinationRuntimeBoundary.KNOWN_TERMINAL),
        (RuntimeExecutionPhase.TIMED_OUT, CoordinationRuntimeBoundary.KNOWN_TERMINAL),
        (RuntimeExecutionPhase.LOST, CoordinationRuntimeBoundary.RECONCILIATION_EVIDENCE),
        (
            RuntimeExecutionPhase.OUTCOME_UNKNOWN,
            CoordinationRuntimeBoundary.RECONCILIATION_EVIDENCE,
        ),
    ],
)
def test_classifier_exhaustively_maps_runtime_phases(phase, expected) -> None:
    subtask, run, attempt, executions = _chain(phase=phase)
    assert classify_runtime_boundary(
        subtask=subtask, run=run, latest_attempt=attempt, executions=executions
    ) is expected


def test_classifier_maps_queued_and_no_execution_and_does_not_mutate_inputs() -> None:
    subtask, run, attempt, executions = _chain()
    subtask.status = SubtaskStatus.READY
    run.status = RunStatus.QUEUED
    queued_before = deepcopy((subtask, run, attempt, executions))
    assert classify_runtime_boundary(
        subtask=subtask, run=run, latest_attempt=None, executions=executions
    ) is CoordinationRuntimeBoundary.NOT_CROSSED_QUEUED
    assert (subtask, run, attempt, executions) == queued_before
    subtask.status = SubtaskStatus.RUNNING
    run.status = RunStatus.RUNNING
    running_before = deepcopy((subtask, run, attempt, executions))
    assert classify_runtime_boundary(
        subtask=subtask, run=run, latest_attempt=attempt, executions=executions
    ) is CoordinationRuntimeBoundary.NOT_CROSSED_NO_EXECUTION
    assert (subtask, run, attempt, executions) == running_before


def test_classifier_fails_closed_for_wrong_identity_owner_and_multiple_unresolved() -> None:
    subtask, run, attempt, executions = _chain(phase=RuntimeExecutionPhase.RUNNING)
    cases = [
        (replace(subtask, task_id=uuid4()), run, attempt, executions),
        (subtask, replace(run, role=None), attempt, executions),
        (
            subtask,
            replace(run, runtime_version_id=uuid4()),
            attempt,
            executions,
        ),
        (subtask, run, replace(attempt, fencing_token=2), executions),
        (subtask, run, attempt, executions + executions),
    ]
    for values in cases:
        with pytest.raises(InvalidTaskTransition):
            classify_runtime_boundary(
                subtask=values[0],
                run=values[1],
                latest_attempt=values[2],
                executions=values[3],
            )


@pytest.mark.parametrize(
    "phase",
    [
        RuntimeExecutionPhase.SUCCEEDED,
        RuntimeExecutionPhase.FAILED,
        RuntimeExecutionPhase.CANCELED,
        RuntimeExecutionPhase.TIMED_OUT,
        RuntimeExecutionPhase.LOST,
        RuntimeExecutionPhase.OUTCOME_UNKNOWN,
    ],
)
def test_classifier_rejects_running_statuses_for_persisted_terminal_chain(phase) -> None:
    subtask, run, attempt, executions = _chain(phase=phase)
    # A terminal Runtime execution must reload with its matching business
    # statuses; a stale all-RUNNING chain is contradictory and must fail closed.
    subtask.status = SubtaskStatus.RUNNING
    run.status = RunStatus.RUNNING
    attempt.status = AttemptStatus.RUNNING
    with pytest.raises(InvalidTaskTransition, match="status is inconsistent"):
        classify_runtime_boundary(
            subtask=subtask, run=run, latest_attempt=attempt, executions=executions
        )


def test_subtask_reconciliation_and_release_transitions_are_closed() -> None:
    subtask, run, _attempt, _ = _chain()
    at = subtask.updated_at + timedelta(seconds=1)
    subtask.require_runtime_reconciliation(run.id, "  provider lost  ", at=at)
    version = subtask.version
    assert subtask.status is SubtaskStatus.RECONCILIATION_REQUIRED
    subtask.require_runtime_reconciliation(run.id, "provider lost", at=at - timedelta(seconds=1))
    assert subtask.version == version
    with pytest.raises(InvalidTaskTransition):
        subtask.cancel(at=at + timedelta(seconds=1))
    subtask.reconcile_runtime_succeeded(run.id, {"answer": 42}, at=at + timedelta(seconds=1))
    assert subtask.status is SubtaskStatus.COMPLETED
    assert subtask.output == {"answer": 42}

    released, release_run, _attempt, _ = _chain()
    release_at = released.updated_at + timedelta(seconds=1)
    released.release_never_dispatched_run(release_run.id, at=release_at)
    assert released.status is SubtaskStatus.READY
    assert released.current_run_id is None
    with pytest.raises(InvalidTaskTransition):
        released.release_never_dispatched_run(release_run.id, at=release_at)


def test_coordinated_task_drain_hold_resume_and_failure() -> None:
    at = datetime.now(UTC) + timedelta(seconds=1)
    task = Task.create(
        tenant_id="tenant-a",
        objective="coordinated",
        execution_mode=TaskExecutionMode.COORDINATED,
        plan_version=1,
        plan_digest="sha256:plan",
        max_concurrency=2,
    )
    task.start_coordination(at=at)
    drain = _drain(task_id=task.id)
    task.require_coordination_runtime_reconciliation(drain, at=at + timedelta(seconds=1))
    assert task.status is TaskStatus.RECONCILIATION_REQUIRED
    version = task.version
    task.require_coordination_runtime_reconciliation(drain, at=at)
    assert task.version == version
    resumed = drain.complete(at=at + timedelta(seconds=2))
    task.resume_coordination_after_runtime_reconciliation(resumed, at=at + timedelta(seconds=2))
    assert task.status is TaskStatus.RUNNING

    failing = Task.create(
        tenant_id="tenant-a",
        objective="coordinated",
        execution_mode=TaskExecutionMode.COORDINATED,
        plan_version=1,
        plan_digest="sha256:plan",
        max_concurrency=2,
    )
    failing.start_coordination(at=at)
    failed_drain = _drain(
        task_id=failing.id, target=CoordinationRuntimeDrainTarget.FAILED
    )
    active_failed_drain = _drain(
        task_id=failing.id, target=CoordinationRuntimeDrainTarget.FAILED
    )
    failed_drain = failed_drain.complete(
        at=at + timedelta(seconds=1)
    )
    failing.require_coordination_runtime_reconciliation(
        active_failed_drain, at=at + timedelta(seconds=1)
    )
    failing.fail_coordination_after_runtime_reconciliation(
        failed_drain, at=at + timedelta(seconds=2)
    )
    assert failing.status is TaskStatus.FAILED
    assert failing.error == "first cause"


@pytest.mark.parametrize(
    ("target", "expected_status"),
    [
        (CoordinationRuntimeDrainTarget.CANCELED, TaskStatus.CANCELED),
        (
            CoordinationRuntimeDrainTarget.WAITING_APPROVAL,
            TaskStatus.WAITING_APPROVAL,
        ),
    ],
)
def test_coordinated_executor_reconciliation_cancel_and_wait_are_exact(
    target, expected_status
) -> None:
    at = datetime.now(UTC) + timedelta(seconds=1)
    task = Task.create(
        tenant_id="tenant-a",
        objective="coordinated",
        execution_mode=TaskExecutionMode.COORDINATED,
        plan_version=1,
        plan_digest="sha256:plan",
        max_concurrency=2,
    )
    task.start_coordination(at=at)
    active = _drain(task_id=task.id, target=target)
    task.require_coordination_runtime_reconciliation(active, at=at + timedelta(seconds=1))
    completed = active.complete(at=at + timedelta(seconds=2))
    method = (
        task.cancel_coordination_after_runtime_reconciliation
        if target is CoordinationRuntimeDrainTarget.CANCELED
        else task.wait_coordination_after_runtime_reconciliation
    )
    method(completed, at=at + timedelta(seconds=2))
    assert task.status is expected_status
    assert task.current_run_id is None
    assert task.output is None and task.candidate_output is None
    if target is CoordinationRuntimeDrainTarget.CANCELED:
        assert task.error is None and task.budget_exhausted_reason is None
    else:
        assert task.error == "first cause"
        assert task.budget_exhausted_reason == "first cause"

    version = task.version
    method(completed, at=at)
    assert task.version == version

    if target is CoordinationRuntimeDrainTarget.CANCELED:
        task.candidate_output = {"unexpected": True}
    else:
        task.error = "different projection"
    with pytest.raises(InvalidTaskTransition):
        method(completed, at=at + timedelta(seconds=3))


def test_new_domain_mutators_have_no_non_domain_production_call_sites() -> None:
    root = Path(__file__).parents[1] / "src" / "agentmesh"
    names = {
        "release_never_dispatched_run",
        "require_coordination_runtime_reconciliation",
        "resume_coordination_after_runtime_reconciliation",
        "fail_coordination_after_runtime_reconciliation",
        "cancel_coordination_after_runtime_reconciliation",
        "wait_coordination_after_runtime_reconciliation",
        "classify_runtime_boundary",
    }
    violations: list[str] = []
    for path in root.rglob("*.py"):
        if path.parent.name == "domain" or path.name in {
            "coordinated_runtime_barrier.py",
            "coordinated_runtime_convergence.py",
            "coordinated_runtime_unknown.py",
            "coordinated_runtime_reconciliation.py",
        }:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in names:
                    violations.append(f"{path}:{node.lineno}:{node.func.attr}")
    assert violations == []
